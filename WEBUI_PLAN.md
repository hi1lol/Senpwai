# Web UI (Senweb) — Implementation Plan

## Context

Add a `senweb` web UI to Senpwai so users can search anime, build a download queue, and let downloads run in the background even after closing the browser tab. The web UI must reuse the same settings, scrapers, and `Download` class as `senpcli`.

---

## New Files

```
senpwai/webui/__init__.py          — empty package marker
senpwai/webui/server.py            — FastAPI app + all endpoints + main()
senpwai/webui/queue_manager.py     — background download queue (DownloadQueueManager)
senpwai/webui/static/index.html    — single-page frontend (vanilla JS, inline CSS)
```

## Modified Files

- `pyproject.toml` — add `fastapi`, `uvicorn[standard]` deps; add `senweb` script entry

---

## 1. `pyproject.toml` changes

```toml
# Under [tool.poetry.dependencies]:
fastapi = ">=0.111,<1.0"
uvicorn = {extras = ["standard"], version = ">=0.29,<1.0"}

# Under [tool.poetry.scripts]:
senweb = "senpwai.webui.server:main"
```

---

## 2. `queue_manager.py`

### State machine per job
`queued → resolving → downloading → done | failed | cancelled`

### Dataclasses

```python
@dataclass
class EpisodeProgress:
    episode_title: str
    status: str          # "pending" | "downloading" | "done" | "failed"
    downloaded_bytes: int
    total_bytes: int

@dataclass
class QueueItem:
    job_id: str
    anime_title: str
    page_link: str
    anime_id: str | None   # pahe only
    site: str
    start_episode: int
    end_episode: int
    quality: str
    sub_or_dub: str
    hls_mode: bool
    status: str            # see state machine
    resolve_step: str
    resolve_current: int
    resolve_total: int
    episodes: list[EpisodeProgress]
    error_message: str | None
    created_at: float
    finished_at: float | None
    # internal (excluded from serialisation)
    _cancel_event: threading.Event
    _lock: threading.Lock
```

### `DownloadQueueManager` (singleton)

Key methods:
- `enqueue(item: QueueItem) -> str` — adds job, spawns `Thread(target=_run_job)`, returns `job_id`
- `cancel(job_id) -> bool` — sets `_cancel_event`
- `get_all() -> list[dict]` — serialises all jobs (skips `_`-prefixed fields)
- `subscribe(job_id, q: asyncio.Queue)` / `unsubscribe(job_id, q)` — SSE pub/sub
- `set_event_loop(loop)` — called at FastAPI startup

### `_run_job(item)` — runs in background Thread

```
1. Construct AnimeDetails(Anime(title, page_link, anime_id), site)  [blocking network]
2. item.status = "resolving"
3. Call _resolve_pahe or _resolve_gogo → ddls / segments + sizes + is_hls flag
4. Check cancel_event
5. item.status = "downloading", populate item.episodes
6. Call _do_downloads(item, anime_details, ddls, sizes, is_hls)
7. item.status = "done"
On exception: item.status = "failed", item.error_message = str(e)
Notify subscribers after each state change.
```

### `_resolve_pahe(item, anime_details) -> list[str]`

Calls scraper classes directly (no senpcli import):

```python
# Step 1 — episode page links
ep_pages_info = pahe.get_episode_pages_info(page_link, start_ep, end_ep)
ep_page_links = pahe.GetEpisodePageLinks().get_episode_page_links(
    start_ep, end_ep, ep_pages_info, page_link, anime_id,
    lambda n: _resolve_progress(item, n)
)
# Filter episodes already on disk
anime_details.set_lacked_episodes(start_ep, end_ep)
ep_page_links = anime_details.get_lacked_links(ep_page_links)

# Step 2 — pahewin download page links
pahewin_links, info = pahe.GetPahewinPageLinks().get_pahewin_page_links_and_info(
    ep_page_links, lambda n: _resolve_progress(item, n)
)
links, info = pahe.bind_sub_or_dub_to_link_info(sub_or_dub, pahewin_links, info)
links, info = pahe.bind_quality_to_link_info(quality, links, info)

# Step 3 — direct download links (resolves kwik.cx via CF bypass)
ddls = pahe.GetDirectDownloadLinks().get_direct_download_links(
    links, lambda n: _resolve_progress(item, n)
)
return ddls
```

### `_resolve_gogo(item, anime_details) -> tuple[list, list[int]|None, bool]`

Normal mode:
```python
anime_id = gogo.extract_anime_id(anime_details.anime_page_content)
dl_page_links = gogo.get_download_page_links(start_ep, end_ep, anime_id)
anime_details.set_lacked_episodes(start_ep, end_ep)
dl_page_links = anime_details.get_lacked_links(dl_page_links)
ddls, sizes = gogo.GetDirectDownloadLinks().get_direct_download_links(
    dl_page_links, quality, lambda n: _resolve_progress(item, n)
)
return ddls, sizes, False
```

HLS mode (when `item.hls_mode`):
```python
hls_links = gogo.GetHlsLinks().get_hls_links(dl_page_links, cb)
matched = gogo.GetHlsMatchedQualityLinks().get_hls_matched_quality_links(hls_links, quality, cb)
segs = gogo.GetHlsSegmentsUrls().get_hls_segments_urls(matched, cb)
return segs, None, True
```

### `_do_downloads` — mirrors `download_manager` from `senpcli/main.py`

- Uses `threading.Semaphore(SETTINGS.max_simultaneous_downloads)` to cap concurrency
- For each episode: resolve size if unknown, create `EpisodeProgress`, construct `Download`, call `start_download()` (blocking), mark episode done
- Uses `get_max_part_size` imported from `senpwai.common.classes`
- Passes `referer=pahe.KWIK_REFERER if site == PAHE else None`

### Progress callback + SSE throttle

```python
def _make_progress_callback(item, ep_idx):
    last_notify = [0.0]
    def cb(n: int):
        with item._lock:
            item.episodes[ep_idx].downloaded_bytes += n
        if time.monotonic() - last_notify[0] > 0.5:
            last_notify[0] = time.monotonic()
            _notify_subscribers(item.job_id)
    return cb
```

`_notify_subscribers` calls `loop.call_soon_threadsafe(q.put_nowait, serialize(item))` for each subscriber asyncio.Queue.

---

## 3. `server.py`

### Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Serve `static/index.html` |
| GET | `/api/settings` | Return serialised `SETTINGS` subset |
| PATCH | `/api/settings` | Update one or more settings fields |
| GET | `/api/search?q=&site=` | Search anime (runs in executor) |
| POST | `/api/anime/details` | Fetch `AnimeDetails` metadata (runs in executor) |
| POST | `/api/queue` | Enqueue a download job |
| GET | `/api/queue` | List all jobs |
| DELETE | `/api/queue/{job_id}` | Cancel/remove a job |
| GET | `/api/queue/{job_id}/events` | SSE progress stream |

### Key endpoint details

**`GET /api/search`** — calls `pahe.search` or `gogo.search` via `run_in_executor`, returns:
```json
{"results": [{"title": "...", "page_link": "...", "anime_id": "..."}]}
```

**`POST /api/anime/details`** — constructs `AnimeDetails` in executor, returns:
```json
{
  "episode_count": 75, "dub_available": true,
  "haved_start": 1, "haved_end": 25,
  "poster_url": "...", "summary": "...",
  "airing_status": "Finished", "genres": ["Action"]
}
```

**`POST /api/queue`** request body:
```json
{
  "title": "...", "page_link": "...", "anime_id": "...", "site": "pahe",
  "start_episode": 1, "end_episode": 25,
  "quality": "720p", "sub_or_dub": "sub", "hls_mode": false
}
```
Returns `{"job_id": "..."}` immediately; background thread starts.

**`GET /api/queue/{job_id}/events`** — SSE via `StreamingResponse`:
```python
async def generator():
    q = asyncio.Queue()
    QUEUE_MANAGER.subscribe(job_id, q)
    try:
        while True:
            if await request.is_disconnected(): break
            try:
                snapshot = await asyncio.wait_for(q.get(), timeout=15.0)
                yield f"data: {json.dumps(snapshot)}\n\n"
                if snapshot["status"] in ("done", "failed", "cancelled"): break
            except asyncio.TimeoutError:
                yield ": heartbeat\n\n"   # SSE keepalive comment
    finally:
        QUEUE_MANAGER.unsubscribe(job_id, q)
return StreamingResponse(generator(), media_type="text/event-stream")
```

Downloads continue running after the browser disconnects because they live in daemon threads, not HTTP handler coroutines.

### `main()` entry point

```python
def main():
    import argparse, webbrowser
    p = argparse.ArgumentParser(prog="senweb")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--open", action="store_true")
    args = p.parse_args()
    if args.open:
        webbrowser.open(f"http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port)
```

---

## 4. `static/index.html`

Single file, vanilla JS, inline `<style>`, no build step.

### Layout sections

1. **Header** — "Senweb" title + active downloads count badge (updates via SSE)

2. **Search card** (collapsible):
   - Site toggle buttons `[pahe]` `[gogo]` (default: `SETTINGS.tracking_site`)
   - Search input + Search button → results list below
   - Clicking a result fetches `/api/anime/details` and reveals the episode range UI

3. **Episode range UI** (appears after selecting a result):
   - Poster thumbnail, title, episode count, dub badge
   - Start episode / End episode number inputs (pre-filled from `haved_end+1` / `episode_count`)
   - Quality selector (`1080p`, `720p`, `480p`, `360p`) — default from settings
   - Sub/Dub toggle — default from settings
   - HLS mode checkbox (visible only when site = gogo)
   - **"Add to Queue"** button → `POST /api/queue` → open `EventSource` for returned `job_id`

4. **Queue section** (always visible):
   - Each job rendered as a card:
     - Title, site badge, episode range, status badge
     - `resolving`: spinner + step label + `<progress value="current" max="total">`
     - `downloading`: per-episode rows with `<progress>` + "X MB / Y MB" text
     - `done` / `failed`: final badge, error block if applicable
     - Cancel button → `DELETE /api/queue/{job_id}`

5. **Settings card** (collapsible, bottom):
   - Quality, sub_or_dub, max_simultaneous_downloads, gogo_mode inputs
   - Save button → `PATCH /api/settings`

### JS architecture

```js
const state = { jobs: {}, settings: {} };

// On page load
async function init() {
  state.settings = await fetch('/api/settings').then(r => r.json());
  const { jobs } = await fetch('/api/queue').then(r => r.json());
  jobs.forEach(j => { state.jobs[j.job_id] = j; subscribeSSE(j.job_id); });
  renderQueue();
}

function subscribeSSE(job_id) {
  const es = new EventSource(`/api/queue/${job_id}/events`);
  es.onmessage = e => {
    const snap = JSON.parse(e.data);
    state.jobs[snap.job_id] = snap;
    renderJobCard(snap.job_id);
    if (['done','failed','cancelled'].includes(snap.status)) es.close();
  };
}
```

Each SSE message is a full job snapshot — re-render that card's `innerHTML` directly. No virtual DOM needed.

---

## 5. Verification

```bash
cd Senpwai
poetry install                  # installs fastapi + uvicorn
poetry run senweb --open        # opens http://127.0.0.1:8080

# Manual test:
# 1. Search "Re:ZERO" on pahe → select → ep range 1-2 → Add to Queue
# 2. Watch: queued → resolving (progress bar) → downloading (per-episode bars)
# 3. Close browser tab
# 4. Reopen http://127.0.0.1:8080 — queue persists, download continued
# 5. Confirm .mp4 files in download folder
```

Run existing tests to confirm nothing broke:
```bash
poetry run poe test_pahe
```

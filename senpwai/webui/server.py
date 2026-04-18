"""FastAPI web server for Senpweb."""
from __future__ import annotations

import asyncio
import json
import os
import uuid
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from senpwai.common.classes import SETTINGS, Anime, AnimeDetails
from senpwai.common.scraper import CLIENT
from senpwai.common.static import DUB, GOGO, PAHE
from senpwai.scrapers import gogo, pahe
from senpwai.webui.queue_manager import QUEUE_MANAGER, QueueItem

# Set SENPWEB_BROWSER_ONLY=1 to disable server-side queue downloads
BROWSER_ONLY: bool = os.environ.get("SENPWEB_BROWSER_ONLY", "").strip() in ("1", "true", "yes")

# token -> {"url": str, "title": str}
_browser_dl_store: dict[str, dict] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    QUEUE_MANAGER.set_event_loop(asyncio.get_running_loop())
    yield


app = FastAPI(title="Senpweb", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
async def root() -> HTMLResponse:
    html_path = Path(__file__).parent / "static" / "index.html"
    return HTMLResponse(html_path.read_text())


# --- Settings ---

@app.get("/api/settings")
async def get_settings() -> dict:
    return {
        "quality": SETTINGS.quality,
        "sub_or_dub": SETTINGS.sub_or_dub,
        "max_simultaneous_downloads": SETTINGS.max_simultaneous_downloads,
        "gogo_mode": SETTINGS.gogo_mode,
        "tracking_site": SETTINGS.tracking_site,
        "download_folder_paths": SETTINGS.download_folder_paths,
        "browser_only": BROWSER_ONLY,
    }


class SettingsPatch(BaseModel):
    quality: str | None = None
    sub_or_dub: str | None = None
    max_simultaneous_downloads: int | None = None
    gogo_mode: str | None = None


@app.patch("/api/settings")
async def patch_settings(body: SettingsPatch) -> dict:
    if body.quality is not None:
        SETTINGS.update_quality(body.quality)
    if body.sub_or_dub is not None:
        SETTINGS.update_sub_or_dub(body.sub_or_dub)
    if body.max_simultaneous_downloads is not None:
        SETTINGS.update_max_simultaneous_downloads(body.max_simultaneous_downloads)
    if body.gogo_mode is not None:
        SETTINGS.update_gogo_mode(body.gogo_mode)
    return {"ok": True}


# --- Search ---

def _do_search(q: str, site: str) -> list[dict]:
    if site == PAHE:
        return [
            {"title": title, "page_link": page_link, "anime_id": anime_id}
            for title, page_link, anime_id in (
                pahe.extract_anime_title_page_link_and_id(r) for r in pahe.search(q)
            )
        ]
    results = gogo.search(q)
    return [{"title": title, "page_link": link, "anime_id": None} for title, link in results]


@app.get("/api/search")
async def search(q: str, site: str = "") -> dict:
    site = site or SETTINGS.tracking_site
    loop = asyncio.get_running_loop()
    results = await loop.run_in_executor(None, _do_search, q, site)
    return {"results": results}


# --- Anime details ---

class AnimeDetailsRequest(BaseModel):
    title: str
    page_link: str
    anime_id: str | None
    site: str


def _fetch_anime_details(req: AnimeDetailsRequest) -> dict:
    anime = Anime(req.title, req.page_link, req.anime_id)
    details = AnimeDetails(anime, req.site)
    return {
        "episode_count": details.metadata.episode_count,
        "dub_available": details.dub_available,
        "haved_start": details.haved_start,
        "haved_end": details.haved_end,
        "poster_url": details.metadata.poster_url,
        "summary": details.metadata.summary,
        "airing_status": details.metadata.airing_status.value,
        "genres": details.metadata.genres,
        "release_year": details.metadata.release_year,
    }


@app.post("/api/anime/details")
async def anime_details(req: AnimeDetailsRequest) -> dict:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _fetch_anime_details, req)


# --- Queue ---

class QueueRequest(BaseModel):
    title: str
    page_link: str
    anime_id: str | None
    site: str
    start_episode: int
    end_episode: int
    quality: str = ""
    sub_or_dub: str = ""
    hls_mode: bool = False


def _require_queue(label: str = "Queue") -> None:
    if BROWSER_ONLY:
        raise HTTPException(status_code=403, detail=f"{label} disabled (SENPWEB_BROWSER_ONLY=1)")


@app.post("/api/queue")
async def add_to_queue(req: QueueRequest) -> dict:
    _require_queue()
    item = QueueItem(
        job_id=uuid.uuid4().hex,
        anime_title=req.title,
        page_link=req.page_link,
        anime_id=req.anime_id,
        site=req.site,
        start_episode=req.start_episode,
        end_episode=req.end_episode,
        quality=req.quality or SETTINGS.quality,
        sub_or_dub=req.sub_or_dub or SETTINGS.sub_or_dub,
        hls_mode=req.hls_mode,
    )
    QUEUE_MANAGER.enqueue(item)
    return {"job_id": item.job_id}


@app.get("/api/queue")
async def get_queue() -> dict:
    return {"jobs": QUEUE_MANAGER.get_all()}


@app.post("/api/queue/{job_id}/start")
async def start_queue_item(job_id: str) -> dict:
    _require_queue()
    if not QUEUE_MANAGER.start(job_id):
        raise HTTPException(status_code=400, detail="Job not found or not in queued state")
    return {"ok": True}


@app.post("/api/queue/start-all")
async def start_all_queue_items() -> dict:
    _require_queue()
    jobs = QUEUE_MANAGER.get_all()
    started = 0
    for j in jobs:
        if j["status"] == "queued" and QUEUE_MANAGER.start(j["job_id"]):
            started += 1
    return {"started": started}


@app.delete("/api/queue/{job_id}")
async def delete_queue_item(job_id: str) -> dict:
    item = QUEUE_MANAGER.get(job_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if item.status in ("done", "failed", "cancelled"):
        QUEUE_MANAGER.remove(job_id)
    else:
        QUEUE_MANAGER.cancel(job_id)
    return {"ok": True}


@app.get("/api/queue/{job_id}/events")
async def queue_events(job_id: str, request: Request) -> StreamingResponse:
    async def generator():
        q: asyncio.Queue = asyncio.Queue()
        QUEUE_MANAGER.subscribe(job_id, q)
        try:
            # Send current snapshot immediately so reconnects get fresh state
            item = QUEUE_MANAGER.get(job_id)
            if item:
                yield f"data: {json.dumps(item.to_dict())}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    snapshot = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield f"data: {json.dumps(snapshot)}\n\n"
                    if snapshot["status"] in ("done", "failed", "cancelled"):
                        break
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        finally:
            QUEUE_MANAGER.unsubscribe(job_id, q)

    return StreamingResponse(generator(), media_type="text/event-stream")


# --- Browser download ---

class BrowserDownloadRequest(BaseModel):
    title: str
    page_link: str
    anime_id: str | None
    start_episode: int
    end_episode: int
    quality: str = ""
    sub_or_dub: str = ""


def _resolve_browser_download(req: BrowserDownloadRequest) -> list[dict]:
    item = QueueItem(
        job_id="browser-" + uuid.uuid4().hex,
        anime_title=req.title,
        page_link=req.page_link,
        anime_id=req.anime_id,
        site=PAHE,
        start_episode=req.start_episode,
        end_episode=req.end_episode,
        quality=req.quality or SETTINGS.quality,
        sub_or_dub=req.sub_or_dub or SETTINGS.sub_or_dub,
        hls_mode=False,
    )
    anime = Anime(req.title, req.page_link, req.anime_id)
    anime_details = AnimeDetails(anime, PAHE)
    ddls = QUEUE_MANAGER._resolve_pahe(item, anime_details)

    episodes = []
    for i, url in enumerate(ddls):
        token = uuid.uuid4().hex
        title = anime_details.episode_title(i, False)
        _browser_dl_store[token] = {"url": url, "title": title}
        episodes.append({"token": token, "title": title})
    return episodes


@app.post("/api/browser-download/resolve")
async def resolve_browser_download(req: BrowserDownloadRequest) -> dict:
    loop = asyncio.get_running_loop()
    episodes = await loop.run_in_executor(None, _resolve_browser_download, req)
    return {"episodes": episodes}


@app.get("/api/browser-download/{token}")
async def browser_download_file(token: str) -> StreamingResponse:
    info = _browser_dl_store.get(token)
    if info is None:
        raise HTTPException(status_code=404, detail="Token not found or expired")

    def stream():
        headers = CLIENT.make_headers({"Referer": pahe.KWIK_REFERER})
        resp = CLIENT.get(info["url"], stream=True, headers=headers, allow_redirects=True)
        for chunk in resp.iter_content(chunk_size=1024 * 1024):
            yield chunk

    filename = f"{info['title']}.mp4"
    return StreamingResponse(
        stream(),
        media_type="video/mp4",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --- Entry point ---

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(prog="senpweb")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--open", action="store_true", help="Open browser on start")
    args = parser.parse_args()
    if args.open:
        webbrowser.open(f"http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()

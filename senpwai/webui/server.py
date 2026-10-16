"""FastAPI web server for Senpweb."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sys
import threading
import time
import uuid
import webbrowser
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from senpwai.common.classes import SETTINGS, Anime, AnimeDetails
from senpwai.common.errors import (
    ErrorInfo,
    SenpError,
    check_response,
    describe_exception,
    log_error,
)
from senpwai.common.scraper import CLIENT
from senpwai.common.static import DUB, GOGO, PAHE
from senpwai.common.tracker import check_tracked_anime
from senpwai.scrapers import gogo, pahe
from senpwai.webui.queue_manager import QUEUE_MANAGER, QueueItem

def _setup_logging() -> None:
    """Log to errors.log and stderr, once.

    Creating SETTINGS already attached an ERROR-level file handler to the root logger
    (Settings.setup_logger), so drop it first or every ERROR line is written twice. This
    used to be pre-empted by a module-level basicConfig(DEBUG) that ran before SETTINGS
    existed, which made setup_logger a no-op and left errors.log empty for the web UI.
    Set SENPWEB_LOG_LEVEL=DEBUG to also log every request's response body.
    """
    root = logging.getLogger()
    for handler in root.handlers[:]:
        root.removeHandler(handler)
        handler.close()
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(name)s - %(message)s")
    file_handler = RotatingFileHandler(
        os.path.join(SETTINGS.config_dir, "errors.log"),
        maxBytes=2 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    for handler in (file_handler, logging.StreamHandler(sys.stderr)):
        handler.setFormatter(formatter)
        root.addHandler(handler)
    level_name = os.environ.get("SENPWEB_LOG_LEVEL", "INFO").upper()
    root.setLevel(getattr(logging, level_name, logging.INFO))


_setup_logging()

# Set SENPWEB_BROWSER_ONLY=1 to disable server-side queue downloads
BROWSER_ONLY: bool = os.environ.get("SENPWEB_BROWSER_ONLY", "").strip() in ("1", "true", "yes")

# token -> {"url": str, "title": str}
_browser_dl_store: dict[str, dict] = {}

# Thumbnails in the search results mean a burst of image fetches per search. Give them
# their own bounded pool so they can't starve searches, detail lookups and downloads,
# which all share the default executor.
_image_executor = ThreadPoolExecutor(max_workers=6, thread_name_prefix="img")

# Recent tracking-check outcomes, newest last, for the webui's tracking panel.
_tracking_events: deque[dict] = deque(maxlen=30)
_tracking_events_lock = threading.Lock()


def _add_tracking_event(kind: str, message: str, detail: dict | None = None) -> None:
    with _tracking_events_lock:
        _tracking_events.append(
            {"time": time.time(), "kind": kind, "message": message, "detail": detail}
        )


def _run_tracking_check() -> None:
    def on_error(title: str, e: Exception) -> None:
        log_error("Tracking check failed for %s", e, title)
        _add_tracking_event(
            "error",
            f"{title}: {describe_exception(e)}",
            ErrorInfo.from_exception(e, stage="tracking check").to_dict(),
        )

    def start_download(anime_details: AnimeDetails) -> None:
        item = QueueItem(
            job_id=uuid.uuid4().hex,
            anime_title=anime_details.anime.title,
            page_link=anime_details.anime.page_link,
            anime_id=anime_details.anime.id,
            site=anime_details.site,
            start_episode=anime_details.lacked_episodes[0],
            end_episode=anime_details.lacked_episodes[-1],
            quality=SETTINGS.quality,
            sub_or_dub=SETTINGS.sub_or_dub,
            hls_mode=anime_details.is_hls_download,
        )
        QUEUE_MANAGER.enqueue(item)
        QUEUE_MANAGER.start(item.job_id)

    check_tracked_anime(
        removed_tracked_callback=SETTINGS.remove_tracked_anime,
        finished_tracking_callback=lambda title: _add_tracking_event(
            "finished", f"{title} has finished airing and was removed from tracking"
        ),
        no_dub_callback=lambda title: _add_tracking_event(
            "no_dub", f"Failed to find dub for {title}"
        ),
        start_download_callback=start_download,
        queued_new_episodes_callback=lambda titles: _add_tracking_event(
            "queued", f"Queued new episodes for {titles}"
        ),
        start_downloading_immediately=True,
        error_callback=on_error,
    )


async def _tracking_scheduler_loop() -> None:
    loop = asyncio.get_running_loop()
    while True:
        await asyncio.sleep(SETTINGS.tracking_interval_hrs * 3600)
        if SETTINGS.tracked_anime:
            try:
                await loop.run_in_executor(None, _run_tracking_check)
            except Exception as e:
                # An uncaught error here would end this task, silently stopping
                # tracking until the server restarts.
                log_error("Scheduled tracking check failed", e)
                _add_tracking_event(
                    "error",
                    f"Scheduled check failed: {describe_exception(e)}",
                    ErrorInfo.from_exception(e, stage="tracking check").to_dict(),
                )


@asynccontextmanager
async def lifespan(app: FastAPI):
    QUEUE_MANAGER.set_event_loop(asyncio.get_running_loop())
    scheduler_task = asyncio.create_task(_tracking_scheduler_loop())
    yield
    scheduler_task.cancel()


app = FastAPI(title="Senpweb", lifespan=lifespan)


# Every error response is {"detail": <one readable line>, "error": <ErrorInfo dict>|null},
# so the page can show the summary and offer the rest behind a Details toggle.


@app.exception_handler(RequestValidationError)
async def validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    lines = []
    for err in exc.errors():
        field = ".".join(str(part) for part in err.get("loc", ()) if part != "body")
        msg = err.get("msg", "invalid value")
        lines.append(f"{field}: {msg}" if field else msg)
    return JSONResponse(
        status_code=422,
        content={"detail": "\n".join(lines) or "Invalid request", "error": None},
    )


def _error_response(request: Request, exc: Exception, status_code: int) -> JSONResponse:
    where = f"{request.method} {request.url.path}"
    log_error("%s failed", exc, where)
    info = ErrorInfo.from_exception(exc, stage=where)
    return JSONResponse(
        status_code=status_code,
        content={"detail": info.summary, "error": info.to_dict()},
    )


# A SenpError is a failure talking to something upstream (the site, FlareSolverr, a
# CDN), not a bug in this server, hence 502. It gets its own handler because Starlette
# re-raises whatever the catch-all Exception handler dealt with, so uvicorn would
# print a second "Exception in ASGI application" traceback for every one of them.
@app.exception_handler(SenpError)
async def upstream_error_handler(request: Request, exc: SenpError) -> JSONResponse:
    return _error_response(request, exc, 502)


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    return _error_response(request, exc, 500)


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
        "tracking_interval_hrs": SETTINGS.tracking_interval_hrs,
        "download_folder_paths": SETTINGS.download_folder_paths,
        "browser_only": BROWSER_ONLY,
    }


class SettingsPatch(BaseModel):
    quality: str | None = None
    sub_or_dub: str | None = None
    max_simultaneous_downloads: int | None = None
    gogo_mode: str | None = None
    tracking_interval_hrs: int | None = None


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
    if body.tracking_interval_hrs is not None:
        SETTINGS.update_tracking_interval_hrs(body.tracking_interval_hrs)
    return {"ok": True}


# --- Tracking ---

class TrackingRequest(BaseModel):
    title: str


@app.get("/api/tracking")
async def get_tracking() -> dict:
    with _tracking_events_lock:
        events = list(_tracking_events)
    return {
        "tracked_anime": SETTINGS.tracked_anime,
        "tracking_site": SETTINGS.tracking_site,
        "tracking_interval_hrs": SETTINGS.tracking_interval_hrs,
        "events": events,
    }


@app.post("/api/tracking")
async def add_tracking(req: TrackingRequest) -> dict:
    if req.title in SETTINGS.tracked_anime:
        raise HTTPException(status_code=400, detail="Already tracking this anime")
    SETTINGS.add_tracked_anime(req.title)
    return {"ok": True}


@app.delete("/api/tracking/{title}")
async def remove_tracking(title: str) -> dict:
    if title not in SETTINGS.tracked_anime:
        raise HTTPException(status_code=404, detail="Not tracking this anime")
    SETTINGS.remove_tracked_anime(title)
    return {"ok": True}


@app.post("/api/tracking/check")
async def check_tracking_now() -> dict:
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _run_tracking_check)
    with _tracking_events_lock:
        events = list(_tracking_events)
    return {"ok": True, "events": events}


# --- Search ---

def _do_search(q: str, site: str) -> list[dict]:
    if site == PAHE:
        results = []
        for r in pahe.search(q):
            title, page_link, anime_id = pahe.extract_anime_title_page_link_and_id(r)
            results.append(
                {
                    "title": title,
                    "page_link": page_link,
                    "anime_id": anime_id,
                    "poster": pahe.extract_poster_url(r),
                }
            )
        return results
    results = gogo.search(q)
    return [
        {"title": title, "page_link": link, "anime_id": None, "poster": ""}
        for title, link in results
    ]


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


# Building AnimeDetails costs two animepahe round-trips, so clicking back and forth
# between results re-scrapes the same anime. Cache the built object; everything it
# exposes here is either immutable metadata or re-derivable from disk without network.
_DETAILS_TTL_SECONDS = 15 * 60
_DETAILS_CACHE_MAX = 64
# key -> (cached_at, AnimeDetails). Written from executor threads, so guard it.
_details_cache: dict[tuple[str, str], tuple[float, AnimeDetails]] = {}
_details_cache_lock = threading.Lock()


def _get_anime_details(req: AnimeDetailsRequest) -> AnimeDetails:
    key = (req.site, req.anime_id or req.page_link)
    now = time.time()
    with _details_cache_lock:
        entry = _details_cache.get(key)
        if entry is not None:
            cached_at, details = entry
            if now - cached_at < _DETAILS_TTL_SECONDS:
                # Which episodes are already on disk can have changed since we cached
                # (a download finished), so re-derive it. This re-globs the folder
                # and touches no network.
                details.set_anime_folder_path(details.get_anime_folder_path())
                return details
            del _details_cache[key]

    details = AnimeDetails(Anime(req.title, req.page_link, req.anime_id), req.site)

    with _details_cache_lock:
        if len(_details_cache) >= _DETAILS_CACHE_MAX:
            oldest = min(_details_cache, key=lambda k: _details_cache[k][0])
            del _details_cache[oldest]
        _details_cache[key] = (time.time(), details)
    return details


def _fetch_anime_details(req: AnimeDetailsRequest) -> dict:
    details = _get_anime_details(req)
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
    job_ids = [j["job_id"] for j in jobs if j["status"] == "queued"]
    started = QUEUE_MANAGER.start_all_sequential(job_ids)
    return {"started": started}


@app.post("/api/queue/{job_id}/retry-episodes")
async def retry_failed_episodes(job_id: str) -> dict:
    _require_queue()
    new_job_id = QUEUE_MANAGER.retry_episodes(job_id)
    if new_job_id is None:
        raise HTTPException(
            status_code=400,
            detail="Job not found, still running, or has no failed/skipped episodes",
        )
    return {"ok": True, "job_id": new_job_id}


@app.post("/api/queue/{job_id}/retry")
async def retry_queue_item(job_id: str) -> dict:
    _require_queue()
    if not QUEUE_MANAGER.retry(job_id):
        raise HTTPException(status_code=400, detail="Job not found or not in failed state")
    return {"ok": True}


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


def _resolve_browser_download(req: BrowserDownloadRequest) -> dict:
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

    # Episodes whose link couldn't be resolved are recorded on the throwaway item;
    # dropping them silently used to hand back a shorter list, or an empty zip.
    skipped = [
        {
            "title": ep.episode_title,
            "episode_number": ep.episode_number,
            "reason": ep.error,
        }
        for ep in item.episodes
        if ep.status == "skipped"
    ]
    if not ddls:
        if skipped:
            raise SenpError(
                f"Could not resolve a download link for any of the {len(skipped)} "
                "requested episode(s)",
                stage="browser download",
                hint="\n".join(f"{s['title']}: {s['reason']}" for s in skipped),
            )
        raise SenpError(
            "No episodes to download — the server already has them all, "
            "or the range was empty",
            stage="browser download",
        )

    episodes = []
    for i, url in enumerate(ddls):
        token = uuid.uuid4().hex
        title = anime_details.episode_title(i, False)
        _browser_dl_store[token] = {"url": url, "title": title}
        episodes.append({"token": token, "title": title})
    return {"episodes": episodes, "skipped": skipped}


@app.get("/api/proxy-image")
async def proxy_image(url: str, request: Request) -> Response:
    from urllib.parse import urlparse
    from curl_cffi import requests as curl_requests
    from senpwai.scrapers.pahe.cf_bypass import get_pahe_session
    parsed = urlparse(url)
    if not parsed.hostname or not (
        parsed.hostname.endswith("animepahe.pw") or parsed.hostname.endswith("animepahe.ru")
    ):
        raise HTTPException(status_code=400, detail="URL not allowed")

    # Posters are content addressed upstream, so the url alone identifies the bytes.
    etag = f'"{hashlib.sha1(url.encode()).hexdigest()}"'
    cache_headers = {"Cache-Control": "public, max-age=86400", "ETag": etag}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=cache_headers)

    def fetch() -> tuple[bytes, str]:
        # cf_clearance has domain .animepahe.pw, so the main session cookies
        # also cover the i.animepahe.pw image CDN.
        # Use a fresh session (not the shared PAHE_SESSION) so its accumulated
        # cookie jar doesn't override the fresh cf_clearance we pass in.
        cf = get_pahe_session()
        with curl_requests.Session(impersonate="chrome") as s:
            resp = s.get(
                url,
                cookies=cf["cookies"],
                headers={"User-Agent": cf["user_agent"], "Referer": "https://animepahe.pw/"},
                allow_redirects=True,
                timeout=(10, 30),
            )
            resp.raise_for_status()
            return resp.content, resp.headers.get("content-type", "image/jpeg")

    loop = asyncio.get_running_loop()
    try:
        content, content_type = await loop.run_in_executor(_image_executor, fetch)
    except Exception:
        # A thumbnail that won't load is not a server failure; let the page render
        # without it instead of surfacing a 500 per missing image.
        logging.warning("proxy-image failed for %s", url, exc_info=True)
        return Response(status_code=404, headers={"Cache-Control": "no-store"})
    return Response(content=content, media_type=content_type, headers=cache_headers)


@app.post("/api/browser-download/resolve")
async def resolve_browser_download(req: BrowserDownloadRequest) -> dict:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _resolve_browser_download, req)


@app.get("/api/browser-download/{token}")
async def browser_download_file(token: str) -> StreamingResponse:
    info = _browser_dl_store.get(token)
    if info is None:
        raise HTTPException(status_code=404, detail="Token not found or expired")

    def open_cdn_stream():
        # info["url"] is still a pahe.win page link (resolve was deferred just-in-time,
        # see the comment in queue_manager._resolve_pahe) — resolve it to the actual
        # owocdn/kwik direct link now, right before streaming, the same way
        # queue_manager.ep_thread does for server-side queue downloads.
        direct_url = pahe.GetDirectDownloadLinks().resolve_one(info["url"])
        # owocdn/kwik sit behind Cloudflare, which 403s plain requests — send the
        # referer verbatim and impersonate a real browser's TLS fingerprint via
        # curl_cffi, same as Download.get_total_download_size does.
        resp = CLIENT.get(
            direct_url,
            stream=True,
            allow_redirects=True,
            headers={"Referer": pahe.KWIK_REFERER},
            impersonate="chrome",
        )
        try:
            check_response(resp, "Download from CDN")
        except Exception:
            resp.close()
            raise
        return resp

    # Resolve and connect *before* building the response. Once StreamingResponse has
    # sent its 200 headers a failure can only reset the connection, so the page would
    # see a network error with no reason (and a 403 block page streamed as .mp4).
    loop = asyncio.get_running_loop()
    resp = await loop.run_in_executor(None, open_cdn_stream)

    def stream():
        try:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                yield chunk
        finally:
            resp.close()

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

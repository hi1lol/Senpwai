"""Background download queue for Senpweb."""
from __future__ import annotations

import asyncio
import threading
import time
from typing import Callable

from senpwai.common.classes import SETTINGS, Anime, AnimeDetails, get_max_part_size
from senpwai.common.scraper import Download
from senpwai.common.static import DUB, GOGO, PAHE
from senpwai.scrapers import gogo, pahe


class EpisodeProgress:
    def __init__(self, episode_title: str, total_bytes: int = 0) -> None:
        self.episode_title = episode_title
        self.status = "pending"  # pending | downloading | done | failed
        self.downloaded_bytes = 0
        self.total_bytes = total_bytes

    def to_dict(self) -> dict:
        return {
            "episode_title": self.episode_title,
            "status": self.status,
            "downloaded_bytes": self.downloaded_bytes,
            "total_bytes": self.total_bytes,
        }


class QueueItem:
    def __init__(
        self,
        job_id: str,
        anime_title: str,
        page_link: str,
        anime_id: str | None,
        site: str,
        start_episode: int,
        end_episode: int,
        quality: str,
        sub_or_dub: str,
        hls_mode: bool,
    ) -> None:
        self.job_id = job_id
        self.anime_title = anime_title
        self.page_link = page_link
        self.anime_id = anime_id
        self.site = site
        self.start_episode = start_episode
        self.end_episode = end_episode
        self.quality = quality
        self.sub_or_dub = sub_or_dub
        self.hls_mode = hls_mode
        self.status = "queued"
        self.resolve_step = ""
        self.resolve_current = 0
        self.resolve_total = 0
        self.episodes: list[EpisodeProgress] = []
        self.error_message: str | None = None
        self.created_at = time.time()
        self.finished_at: float | None = None
        # Internal — excluded from serialisation
        self._cancel_event = threading.Event()
        self._lock = threading.Lock()

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "anime_title": self.anime_title,
            "site": self.site,
            "start_episode": self.start_episode,
            "end_episode": self.end_episode,
            "quality": self.quality,
            "sub_or_dub": self.sub_or_dub,
            "hls_mode": self.hls_mode,
            "status": self.status,
            "resolve_step": self.resolve_step,
            "resolve_current": self.resolve_current,
            "resolve_total": self.resolve_total,
            "episodes": [ep.to_dict() for ep in self.episodes],
            "error_message": self.error_message,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
        }


class DownloadQueueManager:
    _instance: DownloadQueueManager | None = None
    _init_lock = threading.Lock()

    def __new__(cls) -> DownloadQueueManager:
        with cls._init_lock:
            if cls._instance is None:
                inst = super().__new__(cls)
                inst._jobs: dict[str, QueueItem] = {}
                inst._jobs_lock = threading.Lock()
                inst._sse_subscribers: dict[str, list[asyncio.Queue]] = {}
                inst._sse_lock = threading.Lock()
                inst._loop: asyncio.AbstractEventLoop | None = None
                cls._instance = inst
        return cls._instance

    def set_event_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def enqueue(self, item: QueueItem) -> str:
        with self._jobs_lock:
            self._jobs[item.job_id] = item
        return item.job_id

    def start(self, job_id: str) -> bool:
        item = self.get(job_id)
        if item is None or item.status != "queued":
            return False
        threading.Thread(target=self._run_job, args=(item,), daemon=True).start()
        return True

    def cancel(self, job_id: str) -> bool:
        item = self.get(job_id)
        if item is None:
            return False
        item._cancel_event.set()
        with item._lock:
            if item.status not in ("done", "failed", "cancelled"):
                item.status = "cancelled"
                item.finished_at = time.time()
        self._notify_subscribers(job_id)
        return True

    def remove(self, job_id: str) -> bool:
        with self._jobs_lock:
            if job_id not in self._jobs:
                return False
            del self._jobs[job_id]
        return True

    def get_all(self) -> list[dict]:
        with self._jobs_lock:
            items = list(self._jobs.values())
        return [item.to_dict() for item in items]

    def get(self, job_id: str) -> QueueItem | None:
        with self._jobs_lock:
            return self._jobs.get(job_id)

    def subscribe(self, job_id: str, q: asyncio.Queue) -> None:
        with self._sse_lock:
            self._sse_subscribers.setdefault(job_id, []).append(q)

    def unsubscribe(self, job_id: str, q: asyncio.Queue) -> None:
        with self._sse_lock:
            subs = self._sse_subscribers.get(job_id, [])
            if q in subs:
                subs.remove(q)

    def _notify_subscribers(self, job_id: str) -> None:
        if self._loop is None:
            return
        item = self.get(job_id)
        if item is None:
            return
        snapshot = item.to_dict()
        with self._sse_lock:
            subs = list(self._sse_subscribers.get(job_id, []))
        for q in subs:
            self._loop.call_soon_threadsafe(q.put_nowait, snapshot)

    def _update_status(self, item: QueueItem, status: str) -> None:
        with item._lock:
            item.status = status
        self._notify_subscribers(item.job_id)

    def _resolve_progress(self, item: QueueItem, n: int) -> None:
        with item._lock:
            item.resolve_current += n
        self._notify_subscribers(item.job_id)

    def _run_job(self, item: QueueItem) -> None:
        try:
            # For gogo dub, update page_link to dub URL before constructing AnimeDetails
            if item.sub_or_dub == DUB and item.site == GOGO:
                dub_available, page_link = gogo.dub_availability_and_link(item.anime_title)
                if not dub_available:
                    raise RuntimeError(f"Dub not available for {item.anime_title!r}")
                anime = Anime(item.anime_title, page_link, item.anime_id)
            else:
                anime = Anime(item.anime_title, item.page_link, item.anime_id)

            anime_details = AnimeDetails(anime, item.site)

            if item._cancel_event.is_set():
                return

            self._update_status(item, "resolving")

            if item.site == PAHE:
                ddls = self._resolve_pahe(item, anime_details)
                download_sizes = None
                is_hls = False
            else:
                ddls, download_sizes, is_hls = self._resolve_gogo(item, anime_details)

            if item._cancel_event.is_set():
                return

            if not ddls:
                raise RuntimeError("No episodes to download — you may already have them all")

            self._update_status(item, "downloading")
            self._do_downloads(item, anime_details, ddls, download_sizes, is_hls)

            if not item._cancel_event.is_set():
                with item._lock:
                    item.status = "done"
                    item.finished_at = time.time()
                self._notify_subscribers(item.job_id)

        except Exception as e:
            with item._lock:
                if item.status != "cancelled":
                    item.status = "failed"
                    item.error_message = str(e)
                    item.finished_at = time.time()
            self._notify_subscribers(item.job_id)

    def _resolve_pahe(self, item: QueueItem, anime_details: AnimeDetails) -> list[str]:
        from typing import cast

        anime_id = cast(str, item.anime_id)

        # Step 1 — episode page links
        ep_pages_info = pahe.get_episode_pages_info(
            item.page_link, item.start_episode, item.end_episode
        )
        with item._lock:
            item.resolve_step = "Getting episode page links"
            item.resolve_current = 0
            item.resolve_total = ep_pages_info.total
        self._notify_subscribers(item.job_id)

        ep_page_links = pahe.GetEpisodePageLinks().get_episode_page_links(
            item.start_episode,
            item.end_episode,
            ep_pages_info,
            item.page_link,
            anime_id,
            lambda n: self._resolve_progress(item, n),
        )

        # Filter episodes already on disk
        anime_details.set_lacked_episodes(item.start_episode, item.end_episode)
        if not anime_details.lacked_episodes:
            return []
        ep_page_links = anime_details.get_lacked_links(ep_page_links)

        if item._cancel_event.is_set():
            return []

        # Step 2 — pahewin download page links
        with item._lock:
            item.resolve_step = "Fetching download page links"
            item.resolve_current = 0
            item.resolve_total = len(ep_page_links)
        self._notify_subscribers(item.job_id)

        pahewin_links, info = pahe.GetPahewinPageLinks().get_pahewin_page_links_and_info(
            ep_page_links, lambda n: self._resolve_progress(item, n)
        )
        pahewin_links, info = pahe.bind_sub_or_dub_to_link_info(
            item.sub_or_dub, pahewin_links, info
        )
        pahewin_links, info = pahe.bind_quality_to_link_info(
            item.quality, pahewin_links, info
        )

        if item._cancel_event.is_set():
            return []

        # Step 3 — direct download links (resolves kwik.cx via FlareSolverr)
        with item._lock:
            item.resolve_step = "Getting direct download links"
            item.resolve_current = 0
            item.resolve_total = len(pahewin_links)
        self._notify_subscribers(item.job_id)

        ddls = pahe.GetDirectDownloadLinks().get_direct_download_links(
            pahewin_links, lambda n: self._resolve_progress(item, n)
        )
        return ddls

    def _resolve_gogo(
        self, item: QueueItem, anime_details: AnimeDetails
    ) -> tuple[list, list[int] | None, bool]:
        anime_id = gogo.extract_anime_id(anime_details.anime_page_content)
        dl_page_links = gogo.get_download_page_links(
            item.start_episode, item.end_episode, anime_id
        )

        # Filter episodes already on disk
        anime_details.set_lacked_episodes(item.start_episode, item.end_episode)
        if not anime_details.lacked_episodes:
            return [], None, False
        dl_page_links = anime_details.get_lacked_links(dl_page_links)

        if item._cancel_event.is_set():
            return [], None, False

        if item.hls_mode:
            with item._lock:
                item.resolve_step = "Getting HLS links"
                item.resolve_current = 0
                item.resolve_total = len(dl_page_links)
            self._notify_subscribers(item.job_id)

            hls_links = gogo.GetHlsLinks().get_hls_links(
                dl_page_links, lambda n: self._resolve_progress(item, n)
            )

            with item._lock:
                item.resolve_step = "Matching quality"
                item.resolve_current = 0
                item.resolve_total = len(hls_links)
            self._notify_subscribers(item.job_id)

            matched = gogo.GetHlsMatchedQualityLinks().get_hls_matched_quality_links(
                hls_links, item.quality, lambda n: self._resolve_progress(item, n)
            )

            with item._lock:
                item.resolve_step = "Getting segment URLs"
                item.resolve_current = 0
                item.resolve_total = len(matched)
            self._notify_subscribers(item.job_id)

            segs = gogo.GetHlsSegmentsUrls().get_hls_segments_urls(
                matched, lambda n: self._resolve_progress(item, n)
            )
            return segs, None, True

        else:
            with item._lock:
                item.resolve_step = "Getting direct download links"
                item.resolve_current = 0
                item.resolve_total = len(dl_page_links)
            self._notify_subscribers(item.job_id)

            ddls, sizes = gogo.GetDirectDownloadLinks().get_direct_download_links(
                dl_page_links, item.quality, lambda n: self._resolve_progress(item, n)
            )
            return ddls, sizes, False

    def _do_downloads(
        self,
        item: QueueItem,
        anime_details: AnimeDetails,
        ddls_or_segs: list,
        download_sizes: list[int] | None,
        is_hls: bool,
    ) -> None:
        anime_details.validate_anime_folder_path()
        referer = pahe.KWIK_REFERER if item.site == PAHE else None
        sem = threading.Semaphore(SETTINGS.max_simultaneous_downloads)

        with item._lock:
            item.episodes = [
                EpisodeProgress(
                    episode_title=anime_details.episode_title(i, False),
                    total_bytes=download_sizes[i] if download_sizes else 0,
                )
                for i in range(len(ddls_or_segs))
            ]
        self._notify_subscribers(item.job_id)

        def ep_thread(idx: int, link: str | list[str]) -> None:
            with sem:
                if item._cancel_event.is_set():
                    return

                if download_sizes:
                    size = download_sizes[idx]
                elif is_hls:
                    size = len(link)  # type: ignore[arg-type]
                else:
                    size, link = Download.get_total_download_size(  # type: ignore[assignment]
                        link, referer=referer  # type: ignore[arg-type]
                    )

                with item._lock:
                    item.episodes[idx].total_bytes = size
                    item.episodes[idx].status = "downloading"
                self._notify_subscribers(item.job_id)

                max_part_size = get_max_part_size(size, item.site, is_hls)
                dl = Download(
                    link,
                    anime_details.episode_title(idx, False),
                    anime_details.anime_folder_path,
                    size,
                    self._make_progress_callback(item, idx),
                    is_hls_download=is_hls,
                    max_part_size=max_part_size,
                    referer=referer,
                )
                dl.start_download()

                with item._lock:
                    item.episodes[idx].status = "done"
                self._notify_subscribers(item.job_id)

        threads = [
            threading.Thread(target=ep_thread, args=(i, link), daemon=True)
            for i, link in enumerate(ddls_or_segs)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    def _make_progress_callback(self, item: QueueItem, ep_idx: int) -> Callable[[int], None]:
        last_notify: list[float] = [0.0]

        def callback(n: int) -> None:
            with item._lock:
                item.episodes[ep_idx].downloaded_bytes += n
            now = time.monotonic()
            if now - last_notify[0] > 0.5:
                last_notify[0] = now
                self._notify_subscribers(item.job_id)

        return callback


QUEUE_MANAGER = DownloadQueueManager()

"""Background download queue for Senpweb."""
from __future__ import annotations

import asyncio
import threading
import time
import uuid
from typing import Callable

from senpwai.common.classes import SETTINGS, Anime, AnimeDetails, get_max_part_size
from senpwai.common.errors import ErrorInfo, SenpError, describe_exception, log_error
from senpwai.common.scraper import Download
from senpwai.common.static import DUB, GOGO, PAHE
from senpwai.scrapers import gogo, pahe


def _reason_lines(entries: list[tuple[str, str | None]], limit: int | None = 5) -> str:
    """One ``label: reason`` line per entry, capped at ``limit`` with a "…and N more"."""
    shown = entries if limit is None else entries[:limit]
    lines = [f"{label}: {reason or 'no reason recorded'}" for label, reason in shown]
    if limit is not None and len(entries) > limit:
        lines.append(f"…and {len(entries) - limit} more")
    return "\n".join(lines)


class EpisodeProgress:
    def __init__(
        self,
        episode_title: str,
        total_bytes: int = 0,
        episode_number: int | None = None,
        status: str = "pending",
        error: str | None = None,
        error_detail: dict | None = None,
    ) -> None:
        self.episode_title = episode_title
        # pending | downloading | done | failed | skipped
        # "skipped" means it never got as far as downloading: resolving its link
        # failed, so there was nothing to download.
        self.status = status
        self.episode_number = episode_number
        self.downloaded_bytes = 0
        self.total_bytes = total_bytes
        # `error` is the one-line summary; `error_detail` is an ErrorInfo.to_dict()
        # with the stage, URL, HTTP status and traceback behind it.
        self.error = error
        self.error_detail = error_detail

    def to_dict(self) -> dict:
        return {
            "episode_title": self.episode_title,
            "episode_number": self.episode_number,
            "status": self.status,
            "downloaded_bytes": self.downloaded_bytes,
            "total_bytes": self.total_bytes,
            "error": self.error,
            "error_detail": self.error_detail,
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
        only_episodes: list[int] | None = None,
    ) -> None:
        self.job_id = job_id
        # When set, restrict the job to exactly these episode numbers. Used to retry
        # the failed/skipped episodes of an earlier job, which need not be contiguous.
        self.only_episodes = only_episodes
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
        self.error_detail: dict | None = None
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
            "only_episodes": self.only_episodes,
            "status": self.status,
            "resolve_step": self.resolve_step,
            "resolve_current": self.resolve_current,
            "resolve_total": self.resolve_total,
            "episodes": [ep.to_dict() for ep in self.episodes],
            "error_message": self.error_message,
            "error_detail": self.error_detail,
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

    def start_all_sequential(self, job_ids: list[str]) -> int:
        queued = [jid for jid in job_ids if (item := self.get(jid)) and item.status == "queued"]
        if not queued:
            return 0
        threading.Thread(target=self._run_sequential, args=(queued,), daemon=True).start()
        return len(queued)

    def _run_sequential(self, job_ids: list[str]) -> None:
        for job_id in job_ids:
            item = self.get(job_id)
            if item is None or item.status != "queued":
                continue
            self._run_job(item)

    def retry(self, job_id: str) -> bool:
        item = self.get(job_id)
        if item is None or item.status != "failed":
            return False
        with item._lock:
            item.status = "queued"
            item.error_message = None
            item.error_detail = None
            item.finished_at = None
            item.episodes = []
            item.resolve_step = ""
            item.resolve_current = 0
            item.resolve_total = 0
            item._cancel_event.clear()
        self._notify_subscribers(item.job_id)
        threading.Thread(target=self._run_job, args=(item,), daemon=True).start()
        return True

    def retry_episodes(self, job_id: str) -> str | None:
        """Queue a new job for just the failed/skipped episodes of a finished one.

        A separate job rather than a reset of the original, so the episodes that did
        download stay visible instead of being wiped by a full retry.
        """
        item = self.get(job_id)
        if item is None or item.status not in ("done", "failed", "cancelled"):
            return None
        episode_numbers = sorted(
            {
                ep.episode_number
                for ep in item.episodes
                if ep.status in ("failed", "skipped") and ep.episode_number is not None
            }
        )
        if not episode_numbers:
            return None
        retry_item = QueueItem(
            job_id=str(uuid.uuid4()),
            anime_title=item.anime_title,
            page_link=item.page_link,
            anime_id=item.anime_id,
            site=item.site,
            start_episode=episode_numbers[0],
            end_episode=episode_numbers[-1],
            quality=item.quality,
            sub_or_dub=item.sub_or_dub,
            hls_mode=item.hls_mode,
            only_episodes=episode_numbers,
        )
        self.enqueue(retry_item)
        self.start(retry_item.job_id)
        return retry_item.job_id

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
            # Building AnimeDetails below scrapes the anime page, which can take
            # seconds (a FlareSolverr solve) or fail. Mark the job as working before it
            # starts, so the card doesn't sit on "queued" and a failure there is
            # reported as happening while resolving.
            with item._lock:
                item.resolve_step = "Loading anime details"
            self._update_status(item, "resolving")

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
                skipped = [ep for ep in item.episodes if ep.status == "skipped"]
                if skipped:
                    raise SenpError(
                        f"Could not resolve a download link for any of the "
                        f"{len(skipped)} requested episode(s)",
                        stage="resolving",
                        hint=_reason_lines(
                            [
                                (
                                    f"Ep {ep.episode_number}"
                                    if ep.episode_number is not None
                                    else ep.episode_title,
                                    ep.error,
                                )
                                for ep in skipped
                            ]
                        ),
                    )
                raise SenpError(
                    "No episodes to download — you may already have them all",
                    stage="resolving",
                )

            self._update_status(item, "downloading")
            self._do_downloads(item, anime_details, ddls, download_sizes, is_hls)

            if not item._cancel_event.is_set():
                with item._lock:
                    item.status = "done"
                    item.finished_at = time.time()
                self._notify_subscribers(item.job_id)

        except Exception as e:
            # `status` still names the phase the job was in (queued / resolving /
            # downloading) until it is overwritten just below.
            phase = item.status
            log_error(
                "Job %s (%s, eps %d-%d) failed while %s",
                e,
                item.job_id,
                item.anime_title,
                item.start_episode,
                item.end_episode,
                phase,
            )
            with item._lock:
                if item.status != "cancelled":
                    item.status = "failed"
                    item.error_message = describe_exception(e)
                    item.error_detail = ErrorInfo.from_exception(e, stage=phase).to_dict()
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
        ep_page_links = self._apply_only_episodes(item, anime_details, ep_page_links)
        if not ep_page_links:
            return []

        if item._cancel_event.is_set():
            return []

        # Step 2 — pahewin download page links
        with item._lock:
            item.resolve_step = "Fetching download page links"
            item.resolve_current = 0
            item.resolve_total = len(ep_page_links)
        self._notify_subscribers(item.job_id)

        pahewin_getter = pahe.GetPahewinPageLinks()
        pahewin_links, info = pahewin_getter.get_pahewin_page_links_and_info(
            ep_page_links, lambda n: self._resolve_progress(item, n)
        )
        # Each filtering stage below shortens the list. episode_title(idx) indexes
        # lacked_episodes positionally, so lacked_episodes has to shrink in lockstep
        # or every episode after a gap gets saved under the wrong number.
        self._drop_episodes(item, anime_details, pahewin_getter.skipped)

        dropped: list[tuple[int, str]] = []
        pahewin_links, info = pahe.bind_sub_or_dub_to_link_info(
            item.sub_or_dub, pahewin_links, info, dropped
        )
        self._drop_episodes(item, anime_details, dropped)

        pahewin_links, info = pahe.bind_quality_to_link_info(
            item.quality, pahewin_links, info
        )

        if item._cancel_event.is_set():
            return []

        # Direct download links (owocdn/kwik) are short-lived, so they're resolved
        # just before each episode's own download starts (see _do_downloads /
        # ep_thread) instead of as a batch here — a long queue would otherwise let
        # early-resolved links go stale before their turn to download comes up.
        return pahewin_links

    def _apply_only_episodes(
        self, item: QueueItem, anime_details: AnimeDetails, links: list
    ) -> list:
        """Narrow a resolve to item.only_episodes, keeping lacked_episodes aligned."""
        if item.only_episodes is None:
            return links
        wanted = set(item.only_episodes)
        keep = [
            i
            for i, ep in enumerate(anime_details.lacked_episodes)
            if ep in wanted and i < len(links)
        ]
        anime_details.lacked_episodes = [anime_details.lacked_episodes[i] for i in keep]
        return [links[i] for i in keep]

    def _drop_episodes(
        self,
        item: QueueItem,
        anime_details: AnimeDetails,
        dropped: list[tuple[int, str]],
    ) -> None:
        """Remove episodes that could not be resolved, and record them as skipped.

        ``dropped`` holds indices into the *current* lacked_episodes list, so this
        must be called immediately after the stage that produced them.
        """
        if not dropped:
            return
        by_index = dict(dropped)
        skipped = [
            EpisodeProgress(
                episode_title=anime_details.episode_title(idx, False),
                episode_number=anime_details.lacked_episodes[idx],
                status="skipped",
                error=reason,
                error_detail=ErrorInfo.from_message(reason, stage="resolving").to_dict(),
            )
            for idx, reason in sorted(dropped)
            if idx < len(anime_details.lacked_episodes)
        ]
        anime_details.lacked_episodes = [
            ep
            for idx, ep in enumerate(anime_details.lacked_episodes)
            if idx not in by_index
        ]
        with item._lock:
            item.episodes.extend(skipped)
        self._notify_subscribers(item.job_id)

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
        dl_page_links = self._apply_only_episodes(item, anime_details, dl_page_links)
        if not dl_page_links:
            return [], None, False

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
            # Episodes skipped during resolve are already recorded; keep them, but
            # after the downloadable ones so ep_thread's index still lines up.
            already_skipped = [ep for ep in item.episodes if ep.status == "skipped"]
            item.episodes = [
                EpisodeProgress(
                    episode_title=anime_details.episode_title(i, False),
                    episode_number=anime_details.lacked_episodes[i]
                    if i < len(anime_details.lacked_episodes)
                    else None,
                    total_bytes=download_sizes[i] if download_sizes else 0,
                )
                for i in range(len(ddls_or_segs))
            ] + already_skipped
        self._notify_subscribers(item.job_id)

        failures: list[tuple[int, str]] = []
        failures_lock = threading.Lock()

        def ep_thread(idx: int, link: str | list[str]) -> None:
            with sem:
                if item._cancel_event.is_set():
                    return

                # Which step was running, so a failure says where it happened.
                phase = "resolving link"
                try:
                    if download_sizes:
                        size = download_sizes[idx]
                    elif is_hls:
                        size = len(link)  # type: ignore[arg-type]
                    else:
                        # PAHE: `link` here is still the pahe.win page link, not yet
                        # resolved. Resolve it to the actual owocdn/kwik URL right
                        # before downloading — those links are short-lived, so
                        # resolving a whole batch upfront risked expiry for episodes
                        # near the end of a long queue.
                        link = pahe.GetDirectDownloadLinks().resolve_one(link)  # type: ignore[arg-type]
                        phase = "checking file size"
                        size, link = Download.get_total_download_size(  # type: ignore[assignment]
                            link, referer=referer  # type: ignore[arg-type]
                        )

                    phase = "downloading"
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
                except Exception as e:
                    # Don't let a single episode's failure crash its (daemon) thread
                    # silently — record it so the job can report the failure instead
                    # of falsely completing.
                    summary = describe_exception(e)
                    log_error(
                        "Job %s: %s failed while %s",
                        e,
                        item.job_id,
                        anime_details.episode_title(idx, True),
                        phase,
                    )
                    with item._lock:
                        item.episodes[idx].status = "failed"
                        item.episodes[idx].error = summary
                        item.episodes[idx].error_detail = ErrorInfo.from_exception(
                            e, stage=phase
                        ).to_dict()
                    with failures_lock:
                        failures.append((idx, summary))
                    self._notify_subscribers(item.job_id)

        threads = [
            threading.Thread(target=ep_thread, args=(i, link), daemon=True)
            for i, link in enumerate(ddls_or_segs)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        if failures and not item._cancel_event.is_set():
            ordered = sorted(failures)
            titles = ", ".join(
                anime_details.episode_title(idx, True) for idx, _ in ordered
            )
            raise SenpError(
                f"Failed to download {len(failures)} of {len(ddls_or_segs)} "
                f"episode(s): {titles}",
                stage="downloading",
                hint=_reason_lines(
                    [(anime_details.episode_title(idx, True), why) for idx, why in ordered],
                    limit=None,
                ),
            )

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

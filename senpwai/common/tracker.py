from senpwai.common.scraper import AiringStatus, strip_title
from senpwai.common.static import DUB, GOGO, PAHE
from senpwai.common.classes import SETTINGS, Anime, AnimeDetails
from senpwai.scrapers import pahe, gogo
from typing import Callable


def check_tracked_anime(
    removed_tracked_callback: Callable[[str], None],
    finished_tracking_callback: Callable[[str], None],
    no_dub_callback: Callable[[str], None],
    start_download_callback: Callable[[AnimeDetails], None],
    queued_new_episodes_callback: Callable[[str], None],
    start_downloading_immediately: bool,
    error_callback: Callable[[str, Exception], None] | None = None,
) -> None:
    """``error_callback`` makes one failing title non-fatal: it is reported and the
    remaining titles are still checked. Without it the exception propagates as before."""
    queued: list[str] = []
    all_anime_details: list[AnimeDetails] = []
    for title in SETTINGS.tracked_anime:
        try:
            anime_details = _check_one(
                title,
                removed_tracked_callback,
                finished_tracking_callback,
                no_dub_callback,
                start_download_callback,
                start_downloading_immediately,
            )
        except Exception as e:
            if error_callback is None:
                raise
            error_callback(title, e)
            continue
        if anime_details is None:
            continue
        queued.append(anime_details.sanitised_title)
        all_anime_details.append(anime_details)
    if queued:
        all_str = ", ".join(queued)
        queued_new_episodes_callback(all_str)
    if not start_downloading_immediately:
        for anime_details in all_anime_details:
            start_download_callback(anime_details)


def _check_one(
    title: str,
    removed_tracked_callback: Callable[[str], None],
    finished_tracking_callback: Callable[[str], None],
    no_dub_callback: Callable[[str], None],
    start_download_callback: Callable[[AnimeDetails], None],
    start_downloading_immediately: bool,
) -> AnimeDetails | None:
    """Check one tracked title. Returns its details if new episodes are queued."""
    anime: Anime
    site = SETTINGS.tracking_site
    if site == PAHE:
        result = pahe_fetch_anime_obj(title)
        if not result:
            result = gogo_fetch_anime_obj(title)
            if not result:
                return None
            site = GOGO
    else:
        result = gogo_fetch_anime_obj(title)
        if not result:
            result = pahe_fetch_anime_obj(title)
            if not result:
                return None
            site = PAHE
    anime = result
    anime_details = AnimeDetails(anime, site)
    start_eps = anime_details.haved_end if anime_details.haved_end else 1
    anime_details.set_lacked_episodes(start_eps, anime_details.episode_count)
    if (
        not anime_details.lacked_episodes
        and anime_details.metadata.airing_status == AiringStatus.FINISHED
    ):
        removed_tracked_callback(anime_details.anime.title)
        finished_tracking_callback(anime_details.sanitised_title)
        return None
    if anime_details.sub_or_dub == DUB and not anime_details.dub_available:
        no_dub_callback(anime_details.sanitised_title)
        return None
    if not anime_details.lacked_episodes:
        # Still airing and nothing new yet (every released episode is already on disk).
        # Without this the callback was handed an empty list, which the web UI's
        # `lacked_episodes[0]` crashed on, and the title was falsely reported as queued.
        return None
    if start_downloading_immediately:
        start_download_callback(anime_details)
    return anime_details


def pahe_fetch_anime_obj(title: str) -> Anime | None:
    results = pahe.search(title)
    title_fuzzy = strip_title(title, True).lower()
    for result in results:
        res_title, page_link, anime_id = pahe.extract_anime_title_page_link_and_id(
            result
        )
        if strip_title(res_title, True).lower() == title_fuzzy:
            return Anime(title, page_link, anime_id)
    return None


def gogo_fetch_anime_obj(title: str) -> Anime | None:
    results = gogo.search(title)
    title_fuzzy = strip_title(title, True).lower()
    for res_title, page_link in results:
        if strip_title(res_title, True).lower() == title_fuzzy:
            return Anime(title, page_link, None)
    return None

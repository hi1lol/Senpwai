"""
Cloudflare bypass for animepahe.pw and kwik.cx via FlareSolverr.

FlareSolverr must be running before starting a pahe search or download.
Default URL is http://localhost:8191 — override with the FLARESOLVERR_URL env var.

Quick start:
    docker run -d --name=flaresolverr -p 8191:8191 ghcr.io/flaresolverr/flaresolverr:latest
"""

import json
import os
import threading
import time
from typing import TypedDict

import requests
from appdirs import user_config_dir

CACHE_TTL_SECONDS = 20 * 60
# Ceiling on how long a solved session is trusted, even if the cookie claims longer.
MAX_CACHE_TTL_SECONDS = 12 * 60 * 60
# Treat a session as expired this long before it actually is, so a request that is
# already in flight can't have the cookie die underneath it.
EXPIRY_SKEW_SECONDS = 60
# Floor on a freshly solved session's lifetime, so a near-expiry cookie can't make
# every request trigger a new solve.
MIN_CACHE_TTL_SECONDS = 60
FLARESOLVERR_DEFAULT_URL = "http://localhost:8191"

KWIK_PROBE_URL = "https://kwik.cx/f/probe"
PAHE_PROBE_URL = "https://animepahe.pw/"


class KwikSession(TypedDict):
    cookies: dict[str, str]
    user_agent: str
    expires_at: float


# In-process cache in front of the disk cache. site_request() asks for a session on
# every single request, and hitting the filesystem + json.load each time is pure waste.
_MEMO: dict[str, KwikSession] = {}
# One lock per cache key so that concurrent misses produce a single FlareSolverr solve
# (each one spins up a browser and takes tens of seconds) instead of one per thread.
_LOCKS: dict[str, threading.Lock] = {"kwik": threading.Lock(), "pahe": threading.Lock()}
# Bumped on every successful solve. A force_refresh caller records it before taking the
# lock; if it changed by the time the lock is acquired, some other thread already
# replaced the session this caller was rejecting, so there is nothing left to do.
_GENERATION: dict[str, int] = {}


def _cache_path(key: str) -> str:
    config_dir = os.path.join(user_config_dir(), "Senpwai")
    os.makedirs(config_dir, exist_ok=True)
    return os.path.join(config_dir, f"{key}_session.json")


def _is_fresh(session: KwikSession | None) -> bool:
    if session is None:
        return False
    try:
        return float(session.get("expires_at", 0)) > time.time()
    except (TypeError, ValueError):
        return False


def _load_cached(key: str) -> KwikSession | None:
    memoized = _MEMO.get(key)
    if _is_fresh(memoized):
        return memoized
    path = _cache_path(key)
    if not os.path.isfile(path):
        return None
    try:
        with open(path) as f:
            data = json.load(f)
        if _is_fresh(data):
            _MEMO[key] = data
            return data
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return None


def _save_cache(session: KwikSession, key: str) -> None:
    _MEMO[key] = session
    # Write + rename so a concurrent reader never sees a half-written file.
    path = _cache_path(key)
    tmp_path = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp_path, "w") as f:
            json.dump(session, f)
        os.replace(tmp_path, path)
    except OSError:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def _expiry_from_cookies(solution_cookies: list[dict], names: tuple[str, ...]) -> float:
    """Earliest real expiry among the cookies that actually gate access.

    FlareSolverr reports each cookie's own `expires` epoch. cf_clearance usually lives
    far longer than CACHE_TTL_SECONDS, so honouring it avoids needless re-solves.
    """
    now = time.time()
    expiries = [
        float(c["expires"])
        for c in solution_cookies
        if c.get("name") in names and isinstance(c.get("expires"), (int, float)) and float(c["expires"]) > now
    ]
    if not expiries:
        return now + CACHE_TTL_SECONDS
    expires_at = min(min(expiries) - EXPIRY_SKEW_SECONDS, now + MAX_CACHE_TTL_SECONDS)
    # A cookie expiring within the skew window would otherwise yield an already-past
    # expiry, and every call would solve again. Keep the freshly solved session usable
    # for at least a short while; a stale one still self-heals via force_refresh.
    return max(expires_at, now + MIN_CACHE_TTL_SECONDS)


def _solve_via_flaresolverr(flaresolverr_url: str, probe_url: str, domain: str) -> KwikSession:
    endpoint = flaresolverr_url.rstrip("/") + "/v1"
    try:
        resp = requests.post(
            endpoint,
            json={"cmd": "request.get", "url": probe_url, "maxTimeout": 60000},
            timeout=90,
        )
    except requests.exceptions.ConnectionError:
        raise RuntimeError(
            f"Could not connect to FlareSolverr at {endpoint}.\n"
            "Make sure it is running, e.g.:\n"
            "  docker run -d --name=flaresolverr -p 8191:8191 "
            "ghcr.io/flaresolverr/flaresolverr:latest"
        )

    if resp.status_code != 200:
        raise RuntimeError(
            f"FlareSolverr returned HTTP {resp.status_code}: {resp.text[:300]}"
        )

    data = resp.json()
    if data.get("status") != "ok":
        raise RuntimeError(
            f"FlareSolverr failed to solve the challenge: {data.get('message', data)}"
        )

    solution = data["solution"]
    user_agent: str = solution["userAgent"]
    solution_cookies = solution.get("cookies", [])
    cookies = {
        c["name"]: c["value"]
        for c in solution_cookies
        if domain in c.get("domain", "")
    }

    return {
        "cookies": cookies,
        "user_agent": user_agent,
        "expires_at": _expiry_from_cookies(solution_cookies, ("cf_clearance",)),
    }


def _get_session(
    key: str,
    probe_url: str,
    domain: str,
    force_refresh: bool,
    flaresolverr_url: str | None,
) -> KwikSession:
    if not force_refresh:
        cached = _load_cached(key)
        if cached is not None:
            return cached
    generation_before = _GENERATION.get(key, 0)
    with _LOCKS[key]:
        # Another thread may have solved while we waited for the lock.
        if force_refresh:
            # Only reuse a session that was solved *after* we decided ours was bad.
            # The cached one is what we are refusing, expiry notwithstanding.
            if _GENERATION.get(key, 0) != generation_before:
                cached = _load_cached(key)
                if cached is not None:
                    return cached
        else:
            cached = _load_cached(key)
            if cached is not None:
                return cached
        url = flaresolverr_url or os.environ.get("FLARESOLVERR_URL", FLARESOLVERR_DEFAULT_URL)
        session = _solve_via_flaresolverr(url, probe_url, domain)
        _save_cache(session, key)
        _GENERATION[key] = _GENERATION.get(key, 0) + 1
        return session


def get_kwik_session(
    force_refresh: bool = False,
    flaresolverr_url: str | None = None,
) -> KwikSession:
    return _get_session("kwik", KWIK_PROBE_URL, "kwik.cx", force_refresh, flaresolverr_url)


def get_pahe_session(
    force_refresh: bool = False,
    flaresolverr_url: str | None = None,
) -> KwikSession:
    return _get_session("pahe", PAHE_PROBE_URL, "animepahe.pw", force_refresh, flaresolverr_url)

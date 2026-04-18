"""
Cloudflare bypass for kwik.cx via FlareSolverr.

FlareSolverr must be running before starting a pahe download. Default URL is
http://localhost:8191 — override with the FLARESOLVERR_URL environment variable.

Quick start:
    docker run -d --name=flaresolverr -p 8191:8191 ghcr.io/flaresolverr/flaresolverr:latest
"""

import json
import os
import time
from typing import TypedDict

import requests
from appdirs import user_config_dir

CACHE_TTL_SECONDS = 20 * 60
FLARESOLVERR_DEFAULT_URL = "http://localhost:8191"
PROBE_URL = "https://kwik.cx/f/probe"


class KwikSession(TypedDict):
    cookies: dict[str, str]
    user_agent: str
    expires_at: float


def _cache_path() -> str:
    config_dir = os.path.join(user_config_dir(), "Senpwai")
    os.makedirs(config_dir, exist_ok=True)
    return os.path.join(config_dir, "kwik_session.json")


def _load_cached() -> KwikSession | None:
    path = _cache_path()
    if not os.path.isfile(path):
        return None
    try:
        with open(path) as f:
            data = json.load(f)
        if float(data.get("expires_at", 0)) > time.time():
            return data
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return None


def _save_cache(session: KwikSession) -> None:
    with open(_cache_path(), "w") as f:
        json.dump(session, f)


def _solve_via_flaresolverr(flaresolverr_url: str) -> KwikSession:
    endpoint = flaresolverr_url.rstrip("/") + "/v1"
    try:
        resp = requests.post(
            endpoint,
            json={"cmd": "request.get", "url": PROBE_URL, "maxTimeout": 60000},
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
    cookies = {
        c["name"]: c["value"]
        for c in solution.get("cookies", [])
        if "kwik.cx" in c.get("domain", "")
    }

    return {
        "cookies": cookies,
        "user_agent": user_agent,
        "expires_at": time.time() + CACHE_TTL_SECONDS,
    }


def get_kwik_session(
    force_refresh: bool = False,
    flaresolverr_url: str | None = None,
) -> KwikSession:
    if not force_refresh:
        cached = _load_cached()
        if cached is not None:
            return cached
    url = flaresolverr_url or os.environ.get("FLARESOLVERR_URL", FLARESOLVERR_DEFAULT_URL)
    session = _solve_via_flaresolverr(url)
    _save_cache(session)
    return session

"""
Cloudflare bypass for kwik.cx.

kwik.cx's /f/* player paths sit behind a Cloudflare managed challenge that
requires real browser JS execution. We solve the challenge once with nodriver
(Chromium), cache the resulting cookies + User-Agent to disk, and reuse them
for subsequent requests until they expire.

Chromium must be installed system-wide. On headless/TTY systems, xvfb-run must
wrap the process (CF detects and blocks real headless mode).
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from typing import TypedDict

from appdirs import user_config_dir

CACHE_TTL_SECONDS = 20 * 60
CF_CHALLENGE_TIMEOUT_SECONDS = 30
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


def _patch_nodriver_cookie_parser() -> None:
    # nodriver 0.48.1's Cookie.from_json assumes the CDP response contains
    # `sameParty`, but current Chromium builds no longer emit that field. The
    # parser throws KeyError and the connection listener dies before resolving
    # the await, leaving cookie calls hanging indefinitely. Patch once to
    # default the missing key.
    import nodriver.cdp.network as _ndn

    if getattr(_ndn.Cookie.from_json, "_senpwai_patched", False):
        return
    _orig = _ndn.Cookie.from_json.__func__

    @classmethod
    def _patched(cls, json_dict):
        json_dict.setdefault("sameParty", False)
        return _orig(cls, json_dict)

    _patched._senpwai_patched = True
    _ndn.Cookie.from_json = _patched


async def _solve_cf_async() -> KwikSession:
    import nodriver
    from nodriver import cdp

    _patch_nodriver_cookie_parser()
    browser = await nodriver.start(headless=False, sandbox=False)
    try:
        page = await browser.get(PROBE_URL)
        deadline = time.time() + CF_CHALLENGE_TIMEOUT_SECONDS
        while time.time() < deadline:
            title = (await page.evaluate("document.title")) or ""
            if "moment" not in title.lower():
                break
            await asyncio.sleep(0.5)
        else:
            raise RuntimeError(
                f"Cloudflare challenge did not clear within {CF_CHALLENGE_TIMEOUT_SECONDS}s"
            )
        user_agent = await page.evaluate("navigator.userAgent")
        raw_cookies = await page.send(cdp.storage.get_cookies())
        cookies = {
            c.name: c.value
            for c in raw_cookies
            if c.domain and "kwik.cx" in c.domain
        }
        return {
            "cookies": cookies,
            "user_agent": user_agent,
            "expires_at": time.time() + CACHE_TTL_SECONDS,
        }
    finally:
        try:
            browser.stop()
        except Exception:
            pass


def _run_async_solve() -> KwikSession:
    # If stdin/out is a TTY without DISPLAY, nodriver's headed chromium will
    # not connect. Detect and re-exec under xvfb-run so the caller doesn't
    # need to know about display state.
    if sys.platform == "linux" and not os.environ.get("DISPLAY"):
        xvfb = shutil.which("xvfb-run")
        if xvfb is None:
            raise RuntimeError(
                "No X display available and xvfb-run is not installed. "
                "Install it (e.g. `sudo pacman -S xorg-server-xvfb` or "
                "`sudo apt install xvfb`) or run Senpwai in a graphical session."
            )
        # Re-exec this module as a subprocess under xvfb-run and read the
        # cached result back. Avoids nested asyncio loops and display setup.
        env = os.environ.copy()
        result = subprocess.run(
            [xvfb, "-a", sys.executable, "-m", "senpwai.scrapers.pahe.cf_bypass", "--solve"],
            env=env,
            capture_output=True,
            text=True,
            timeout=CF_CHALLENGE_TIMEOUT_SECONDS + 30,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"CF bypass subprocess failed (exit {result.returncode}): "
                f"{result.stderr.strip()[-500:]}"
            )
        cached = _load_cached()
        if cached is None:
            raise RuntimeError(
                "CF bypass subprocess completed but produced no cached session"
            )
        return cached
    return asyncio.run(_solve_cf_async())


def get_kwik_session(force_refresh: bool = False) -> KwikSession:
    if not force_refresh:
        cached = _load_cached()
        if cached is not None:
            return cached
    session = _run_async_solve()
    _save_cache(session)
    return session


if __name__ == "__main__":
    # Entry point for the xvfb-run subprocess path.
    if "--solve" in sys.argv:
        session = asyncio.run(_solve_cf_async())
        _save_cache(session)
        print(f"Saved kwik session with {len(session['cookies'])} cookies", flush=True)

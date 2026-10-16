"""Structured, human-readable errors shared by the scrapers and the web UI.

A bare ``str(e)`` loses the exception type, where it happened, the URL, the HTTP
status and the cause chain. ``SenpError`` carries those, and ``ErrorInfo`` is the
JSON-friendly snapshot of any exception that the web UI renders (short summary on top,
everything else behind a "Details" toggle).
"""
from __future__ import annotations

import json
import logging
import traceback
from dataclasses import dataclass, field
from typing import Any

# How much of an HTTP error page to keep for diagnosis.
BODY_SNIPPET_CHARS = 500
# How much of a body that failed to parse as JSON to keep. The UI shows it in a
# scrollable block, so this only bounds memory, not what the user can read.
JSON_BODY_CHARS = 4000
# Bodies are capped lower in the log, where they are not scrollable.
LOG_BODY_CHARS = 2000
_MAX_CAUSE_DEPTH = 10

_STATUS_HINTS = {
    403: "Blocked, most likely by a Cloudflare challenge or hotlink protection.",
    404: "Not found. The page or API endpoint may have moved.",
    429: "Rate limited by the site. Wait a few minutes before retrying.",
}

logger = logging.getLogger("senpwai")


def status_hint(status: int | None) -> str | None:
    """A short likely-cause note for common HTTP error statuses."""
    return _STATUS_HINTS.get(status) if status is not None else None


class SenpError(Exception):
    """An error with enough context to be debugged from the message alone."""

    def __init__(
        self,
        message: str,
        *,
        stage: str | None = None,
        url: str | None = None,
        http_status: int | None = None,
        attempts: int | None = None,
        hint: str | None = None,
        body_snippet: str | None = None,
        details: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.url = url
        self.http_status = http_status
        self.attempts = attempts
        self.hint = hint
        self.body_snippet = body_snippet
        # Extra labelled facts (content type, parser error, ...), shown in order.
        self.details = details or {}


def _origin(e: BaseException) -> str:
    """``dir/file.py:line`` of the innermost frame the exception passed through."""
    frames = traceback.extract_tb(e.__traceback__)
    if not frames:
        return ""
    last = frames[-1]
    parts = last.filename.replace("\\", "/").split("/")
    return f"{'/'.join(parts[-2:])}:{last.lineno}"


# Exceptions where the message alone says nothing (``KeyError: 'Location'``) and the
# place it was raised is the useful part.
_LOCATION_WORTHY = (KeyError, IndexError, AttributeError, TypeError, ValueError, AssertionError)


def describe_exception(e: BaseException) -> str:
    """One readable line for any exception."""
    if isinstance(e, SenpError):
        return str(e)
    name = type(e).__name__
    msg = str(e).strip()
    text = f"{name}: {msg}" if msg else name
    if isinstance(e, _LOCATION_WORTHY):
        where = _origin(e)
        if where:
            text += f" (at {where})"
    return text


def _cause_chain(e: BaseException) -> list[str]:
    causes: list[BaseException] = []
    seen = {id(e)}
    cur = e.__cause__ or (None if e.__suppress_context__ else e.__context__)
    while cur is not None and id(cur) not in seen and len(causes) < _MAX_CAUSE_DEPTH:
        seen.add(id(cur))
        causes.append(cur)
        cur = cur.__cause__ or (None if cur.__suppress_context__ else cur.__context__)

    # Wrapper libraries (requests -> urllib3 -> socket) re-embed the inner message in
    # each layer, so the same sentence would be listed three times. Drop a layer whose
    # message just repeats the one above it, but always keep the root cause.
    chain: list[str] = []
    for i, cause in enumerate(causes):
        above = str(e) if i == 0 else str(causes[i - 1])
        msg = str(cause).strip()
        is_root = i == len(causes) - 1
        if not is_root and msg and msg in above:
            continue
        chain.append(describe_exception(cause))
    return chain


@dataclass
class ErrorInfo:
    summary: str
    stage: str | None = None
    exc_type: str | None = None
    url: str | None = None
    http_status: int | None = None
    attempts: int | None = None
    hint: str | None = None
    body_snippet: str | None = None
    traceback: str | None = None
    cause_chain: list[str] = field(default_factory=list)
    details: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_exception(
        cls,
        e: BaseException,
        stage: str | None = None,
        url: str | None = None,
        with_traceback: bool = True,
    ) -> ErrorInfo:
        senp = e if isinstance(e, SenpError) else None
        return cls(
            summary=describe_exception(e),
            stage=(senp.stage if senp and senp.stage else stage),
            exc_type=type(e).__name__,
            url=(senp.url if senp and senp.url else url),
            http_status=senp.http_status if senp else None,
            attempts=senp.attempts if senp else None,
            hint=senp.hint if senp else None,
            body_snippet=senp.body_snippet if senp else None,
            traceback=(
                "".join(traceback.format_exception(type(e), e, e.__traceback__))
                if with_traceback
                else None
            ),
            cause_chain=_cause_chain(e),
            details=dict(senp.details) if senp else {},
        )

    @classmethod
    def from_message(
        cls, summary: str, stage: str | None = None, url: str | None = None
    ) -> ErrorInfo:
        """For failures that were never an exception (e.g. an episode with no links)."""
        return cls(summary=summary, stage=stage, url=url)

    def _fact_lines(self, body_limit: int | None = None) -> list[str]:
        lines = []
        for label, value in (
            ("Stage", self.stage),
            ("Type", self.exc_type),
            ("URL", self.url),
            ("HTTP status", self.http_status),
            ("Attempts", self.attempts),
        ):
            if value is not None:
                lines.append(f"{label}: {value}")
        for label, value in self.details.items():
            lines.append(f"{label}: {value}")
        if self.hint:
            lines.append(f"\nHint:\n{self.hint}")
        if self.body_snippet:
            body = self.body_snippet
            if body_limit is not None and len(body) > body_limit:
                body = body[:body_limit] + f"\n…[{len(body) - body_limit:,} more chars not shown]"
            lines.append(f"\nResponse body:\n{body}")
        return lines

    def facts_text(self, body_limit: int | None = None) -> str:
        """Everything except the traceback and cause chain, for the log."""
        return "\n".join(self._fact_lines(body_limit))

    def to_text(self) -> str:
        """Everything, as plain text. This is what the UI's Copy button copies."""
        lines = [self.summary, *self._fact_lines()]
        if self.cause_chain:
            lines.append("\nCaused by:\n" + "\n".join(f"  {c}" for c in self.cause_chain))
        if self.traceback:
            lines.append(f"\nTraceback:\n{self.traceback.rstrip()}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "stage": self.stage,
            "exc_type": self.exc_type,
            "url": self.url,
            "http_status": self.http_status,
            "attempts": self.attempts,
            "hint": self.hint,
            "body_snippet": self.body_snippet,
            "traceback": self.traceback,
            "cause_chain": self.cause_chain,
            "details": self.details,
            "text": self.to_text(),
        }


def body_snippet(resp: Any, limit: int = BODY_SNIPPET_CHARS) -> str:
    """First ``limit`` characters of a response body, safe against binary content."""
    try:
        return bytes(resp.content[:limit]).decode("utf-8", "replace")
    except Exception:
        return ""


def _response_text(resp: Any) -> str:
    """The whole body as text, safe against binary content."""
    try:
        return bytes(resp.content).decode("utf-8", "replace")
    except Exception:
        return ""


def _header(headers: Any, name: str) -> str | None:
    try:
        value = headers.get(name)
    except Exception:
        return None
    return str(value) if value else None


def _response_facts(resp: Any, requested_url: str | None) -> dict[str, str]:
    """Where a response came from: redirect, content type, size, and who served it."""
    facts: dict[str, str] = {}
    final = str(getattr(resp, "url", "") or "")
    if requested_url and final and final != requested_url:
        facts["Redirected to"] = final
    headers = getattr(resp, "headers", None)
    for label, name in (
        ("Content-Type", "content-type"),
        ("Server", "server"),
        ("CF-Mitigated", "cf-mitigated"),
        ("CF-Ray", "cf-ray"),
    ):
        value = _header(headers, name)
        if value:
            facts[label] = value
    try:
        facts["Response size"] = f"{len(resp.content):,} bytes"
    except Exception:
        pass
    return facts


def _classify_body(text: str, headers: Any) -> str | None:
    """Say what a body that isn't the expected JSON most likely is."""
    low = text[:4000].lower()
    if (
        (_header(headers, "cf-mitigated") or "").lower() == "challenge"
        or "just a moment" in low
        or "challenge-platform" in low
    ):
        return (
            "This looks like a Cloudflare challenge page: the FlareSolverr cookies "
            "were rejected or have expired."
        )
    if "ddos-guard" in low or "ddos guard" in low:
        return "This looks like a DDoS-Guard challenge page, not the site's API."
    stripped = text.strip()
    if stripped[:1] == "<":
        return (
            "The site sent an HTML page instead of JSON (an error or block page, "
            "or the domain changed)."
        )
    if stripped[:1] in ("{", "[") and stripped[-1:] not in ("}", "]"):
        return "The JSON is cut off: the response ended early (dropped connection?)."
    return None


def _excerpt(text: str, pos: int | None, limit: int = JSON_BODY_CHARS) -> str:
    """Enough of an unparseable body to see what it is and where parsing broke."""
    if len(text) <= limit:
        return text
    head, half = 1000, 750
    if pos is None or pos <= head + half:
        return text[:limit] + f"\n…[{len(text) - limit:,} more chars not shown]"
    # The break is deep in the body: keep the start (what is it?) and the area around
    # the break (what went wrong?).
    start, end = pos - half, min(len(text), pos + half)
    out = text[:head] + f"\n…[{start - head:,} chars omitted]…\n" + text[start:end]
    if end < len(text):
        out += f"\n…[{len(text) - end:,} more chars not shown]"
    return out


def _where_it_broke(text: str, pos: int) -> str:
    """One line of context with ⟦here⟧ at the character the parser choked on."""
    pos = min(pos, len(text))
    before = text[max(0, pos - 60):pos].replace("\n", "↵")
    after = text[pos:pos + 60].replace("\n", "↵")
    return f"{'…' if pos > 60 else ''}{before}⟦here⟧{after}{'…' if pos + 60 < len(text) else ''}"


def _join_hints(*hints: str | None) -> str | None:
    joined = "\n".join(h for h in hints if h)
    return joined or None


def check_response(
    resp: Any, stage: str, *, expect_json: bool = False, url: str | None = None
) -> Any:
    """Raise a ``SenpError`` naming the stage, URL and status unless ``resp`` is usable.

    Replaces bare ``resp.json()`` / ``resp.text`` parsing, which on a 403 or an HTML
    error page fails with an opaque ``JSONDecodeError`` or ``KeyError`` that names
    neither the URL nor the status. Returns the parsed JSON when ``expect_json``.

    ``url`` is the URL the caller asked for; pass it so a redirect is visible. When the
    body is not valid JSON the error carries the unparseable text, the parser's message
    and position, a ``⟦here⟧`` marker at the break, and a guess at what the page is.
    """
    status = resp.status_code
    url = url or str(getattr(resp, "url", "") or "") or None
    facts = _response_facts(resp, url)
    headers = getattr(resp, "headers", None)
    if status >= 400:
        snippet = body_snippet(resp)
        raise SenpError(
            f"{stage} failed: HTTP {status}",
            stage=stage,
            url=url,
            http_status=status,
            hint=_join_hints(status_hint(status), _classify_body(snippet, headers)),
            body_snippet=snippet,
            details=facts,
        )
    if not expect_json:
        return None
    if not resp.content:
        raise SenpError(
            f"{stage} failed: empty response (HTTP {status})",
            stage=stage,
            url=url,
            http_status=status,
            hint="The site may be down, or the domain may have changed.",
            details=facts,
        )
    try:
        return resp.json()
    except ValueError as first_error:
        parse_error = first_error

    # Everything below runs outside the handler above on purpose: raising from inside it
    # would chain the re-parse error to the first one, listing the same error twice.
    text = _response_text(resp)
    # Re-parse the text we show, so the reported position matches it exactly.
    try:
        json.loads(text)
    except ValueError as reparsed:
        parse_error = reparsed
    pos = getattr(parse_error, "pos", None)
    details = dict(facts)
    if pos is not None:
        pos = min(pos, len(text))
        details["Parser error"] = (
            f"{getattr(parse_error, 'msg', parse_error)} "
            f"(line {parse_error.lineno}, column {parse_error.colno}, "  # type: ignore[attr-defined]
            f"char {pos:,} of {len(text):,} chars)"
        )
        details["Error context"] = _where_it_broke(text, pos)
    else:
        details["Parser error"] = str(parse_error)
    what = ", ".join(
        [f"HTTP {status}"]
        + ([facts["Content-Type"]] if "Content-Type" in facts else [])
        + ([facts["Response size"]] if "Response size" in facts else [])
    )
    raise SenpError(
        f"{stage} failed: response was not JSON ({what})",
        stage=stage,
        url=url,
        http_status=status,
        hint=_classify_body(text, headers),
        body_snippet=_excerpt(text, pos),
        details=details,
    ) from parse_error


def log_error(message: str, exc: BaseException, *args: object) -> None:
    """Log ``message`` with ``exc``'s traceback, once, through the logging config.

    For a ``SenpError`` the stage, URL, status, details and response body are logged
    too: they otherwise only reach the browser, and the log is where you look after.
    """
    extra = ""
    if isinstance(exc, SenpError):
        extra = ErrorInfo.from_exception(exc, with_traceback=False).facts_text(LOG_BODY_CHARS)
    if extra:
        logger.error(message + "\n%s", *args, extra, exc_info=exc)
    else:
        logger.error(message, *args, exc_info=exc)

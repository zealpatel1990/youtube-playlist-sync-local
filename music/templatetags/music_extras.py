"""
Presentation-only filters.

Everything here is a pure function of one value with no database access, so a
filter can be used inside a row loop without turning a page render into N
queries. State colours live here rather than in the views because they are a
property of the dashboard, not of the pipeline.
"""

from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath

from django import template

from music.models import TrackState

register = template.Library()

#: (Bootstrap contextual colour, Bootstrap icon) per pipeline state. Colours are
#: chosen from the set that stays legible under `data-bs-theme="dark"`.
STATE_STYLE: dict[str, tuple[str, str]] = {
    TrackState.DISCOVERED: ("secondary", "bi-record-circle"),
    TrackState.IDENTIFYING: ("info", "bi-arrow-repeat"),
    TrackState.IDENTIFIED: ("primary", "bi-tag"),
    TrackState.ORGANIZED: ("success", "bi-check-circle"),
    TrackState.FAILED: ("danger", "bi-exclamation-triangle"),
    TrackState.SKIPPED: ("secondary", "bi-slash-circle"),
    TrackState.MISSING: ("warning", "bi-question-circle"),
}


@register.filter
def state_colour(state: str) -> str:
    return STATE_STYLE.get(state, ("secondary", ""))[0]


@register.filter
def state_icon(state: str) -> str:
    return STATE_STYLE.get(state, ("", "bi-dot"))[1]


@register.filter
def basename(path: str) -> str:
    """Last component of a path, whichever separator the host uses.

    The library lives on the Pi (POSIX) but development happens on Windows, and
    a stored path may use either separator, so both are treated as separators.
    """
    text = str(path or "")
    if not text:
        return ""
    if "\\" in text:
        return PureWindowsPath(text).name or text
    return PurePosixPath(text).name or text


@register.filter
def hms(seconds) -> str:
    """`3:45`, or `1:02:03` past an hour. 0 (the "unknown" sentinel) renders empty."""
    try:
        total = int(seconds or 0)
    except (TypeError, ValueError):
        return ""
    if total <= 0:
        return ""
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


@register.filter
def percent(value) -> str:
    """A 0–1 confidence as a whole percentage."""
    try:
        return f"{float(value or 0) * 100:.0f}%"
    except (TypeError, ValueError):
        return ""

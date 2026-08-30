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

from music.models import JobState, TrackState

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


#: How far a candidate's running time may sit from the file's and still read as
#: the same recording. Matches the tightest band in
#: `music.identify.textsearch.DURATION_BANDS`, which is what actually scores
#: them — the panel must not call a gap "exact" that the scorer penalised.
DURATION_EXACT_SECONDS = 2


@register.filter
def duration_delta(candidate_seconds, file_seconds) -> str:
    """How a candidate's length compares to the file's: `exact`, `+27s`, `-1:04`.

    Empty when either side is unknown, so a provider that reports no duration
    simply shows nothing rather than claiming a match with 0.
    """
    try:
        candidate = int(candidate_seconds or 0)
        actual = int(file_seconds or 0)
    except (TypeError, ValueError):
        return ""
    if candidate <= 0 or actual <= 0:
        return ""

    delta = candidate - actual
    if abs(delta) <= DURATION_EXACT_SECONDS:
        return "exact"
    sign = "+" if delta > 0 else "-"
    size = abs(delta)
    if size < 60:
        return f"{sign}{size}s"
    minutes, seconds = divmod(size, 60)
    return f"{sign}{minutes}:{seconds:02d}"


@register.filter
def duration_delta_class(candidate_seconds, file_seconds) -> str:
    """A Bootstrap text class matching how far off `duration_delta` is.

    Three bands rather than a gradient: agreeing, plausibly the same recording
    with different padding, and long enough that it is probably another cut.
    """
    try:
        candidate = int(candidate_seconds or 0)
        actual = int(file_seconds or 0)
    except (TypeError, ValueError):
        return "text-body-tertiary"
    if candidate <= 0 or actual <= 0:
        return "text-body-tertiary"

    delta = abs(candidate - actual)
    if delta <= DURATION_EXACT_SECONDS:
        return "text-success-emphasis"
    if delta <= 15:
        return "text-body-secondary"
    return "text-warning-emphasis"


#: Bootstrap badge class per job state. Separate from STATE_STYLE because a job
#: and a track have different lifecycles that happen to share some words.
JOB_STATE_CLASS: dict[str, str] = {
    JobState.QUEUED: "text-bg-secondary",
    JobState.RUNNING: "text-bg-info",
    JobState.SUCCEEDED: "text-bg-success",
    JobState.FAILED: "text-bg-danger",
    JobState.CANCELLED: "text-bg-secondary",
}


@register.filter
def job_state_class(state: str) -> str:
    return JOB_STATE_CLASS.get(state, "text-bg-secondary")


@register.filter
def duration_short(seconds) -> str:
    """A job's runtime at a glance: `0.4s`, `12s`, `3m 05s`, `1h 02m`."""
    try:
        total = float(seconds or 0)
    except (TypeError, ValueError):
        return ""
    if total <= 0:
        return "—"
    if total < 1:
        return f"{total:.1f}s"
    if total < 60:
        return f"{total:.0f}s"
    if total < 3600:
        return f"{int(total // 60)}m {int(total % 60):02d}s"
    return f"{int(total // 3600)}h {int((total % 3600) // 60):02d}m"


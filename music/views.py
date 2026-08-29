"""
The web layer: one dashboard, three htmx fragments, one SSE stream, and a set of
POST actions that do nothing but enqueue a job.

Three rules run through everything here, each one an audit finding:

**Views never do slow work** (A2, A6). Every action enqueues a `Job` and returns
204 with an `HX-Trigger` toast. A view that shells out, downloads or hashes is a
view that pins a gunicorn thread; the request pool on the Pi is four threads.

**The SSE stream costs nothing while idle** (A2). It waits on the in-process
condition variable in `core.events` — no query per tick, no cost per connected
tab — and closes itself after `SSE_MAX_STREAM_SECONDS` so threads always
recycle. The browser reconnects on its own via the `retry:` directive.

**Every query names its columns.** `.only()` / `.values()` everywhere, because a
`SELECT *` over a 20k-row library on a Pi 2's SD card is a visible pause. The
`.only()` lists below must stay in step with what the templates render — a
deferred field touched in a template costs one extra query *per row*.

Toast text reaches the browser through `HX-Trigger`; see `static/music/app.js`
for the `textContent` build that closes the stored-XSS hole (A11).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.db import connections
from django.db.models import Count, F, Q
from django.http import Http404, HttpResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from music import identify as identify_module
from music.core import envfile, events
from music.jobs import engine
from music.models import Job, JobState, Track, TrackState, YoutubeVideo

log = logging.getLogger("music.web")


# --------------------------------------------------------------------------
# Query shapes
# --------------------------------------------------------------------------

#: Exactly the columns `_track_row.html` renders. Adding a field to that
#: template without adding it here turns the list into N+1 queries.
TRACK_LIST_FIELDS = (
    "id",
    "path",
    "planned_path",
    "plan_note",
    "title",
    "artist",
    "album",
    "album_artist",
    "duration",
    "bitrate",
    "state",
    "source",
    "confidence",
    "cover_url",
    "cover_embedded",
    "track_no",
    "disc_no",
    "year",
    "is_compilation",
    "identified_by",
    "fail_count",
    "last_error",
    "previous_path",
    "updated_at",
)

#: Sort is a **whitelist**, never a user string handed to `order_by()`. The
#: values are ORM ordering tuples; the keys are what appears in the URL.
SORT_CHOICES: dict[str, tuple[str, ...]] = {
    "added": ("-created_at", "-id"),
    "-added": ("created_at", "id"),
    "title": ("title", "id"),
    "-title": ("-title", "-id"),
    "artist": ("artist", "album", "disc_no", "track_no", "id"),
    "-artist": ("-artist", "-id"),
    "album": ("album", "disc_no", "track_no", "id"),
    "-album": ("-album", "-id"),
    "state": ("state", "-updated_at"),
    "-state": ("-state", "-updated_at"),
    "duration": ("duration", "id"),
    "-duration": ("-duration", "-id"),
    "updated": ("-updated_at", "-id"),
    "-updated": ("updated_at", "id"),
}
DEFAULT_SORT = "added"

#: Clickable column headers, in table order — a subset of SORT_CHOICES, because
#: a phone-width table cannot carry six of them. `artist` and `album` stay in
#: the whitelist and remain reachable as `?sort=artist`.
SORT_COLUMNS = (
    ("title", "Track"),
    ("state", "State"),
    ("duration", "Length"),
    ("added", "Added"),
)

#: The sort dropdown. Every key is a `SORT_CHOICES` key, so the whitelist still
#: decides what reaches `order_by()`; this only decides what is offered.
SORT_OPTIONS = (
    ("added", "Newest first"),
    ("-added", "Oldest first"),
    ("updated", "Recently changed"),
    ("title", "Title A–Z"),
    ("artist", "Artist A–Z"),
    ("album", "Album A–Z"),
    ("state", "State"),
)

#: Filter pills. Like sort, a **whitelist**: the key appears in the URL and
#: `_filter_state` maps it to a predicate, so a user string never reaches
#: `filter()`. Two entries are synthetic rather than a bare state match —
#: "unidentified" spans the two pre-identification states, and "planned" is a
#: column comparison — because those are the questions actually asked of this
#: table ("what still needs work", "what is waiting for me to apply it").
TRACK_STATE_FILTERS: tuple[tuple[str, str], ...] = (
    ("", "All"),
    ("unidentified", "Needs identifying"),
    (TrackState.IDENTIFIED, "Identified"),
    ("planned", "Planned"),
    (TrackState.ORGANIZED, "Organized"),
    (TrackState.SKIPPED, "Skipped"),
    (TrackState.FAILED, "Failed"),
    (TrackState.MISSING, "Missing"),
)

#: Everything `?state=` accepts — the pill values **plus every real state**.
#:
#: The pills are a curated subset, but `_stats.html` renders a badge linking to
#: `?state=<value>` for *every* `TrackState`, so a validator limited to the pills
#: made those links silently fall back to "All": clicking "Skipped: 6" showed all
#: 559 rows. A filter that ignores itself is worse than no link, so the accepted
#: set is derived from the model rather than from the pill list.
VALID_STATE_FILTERS = frozenset(
    {value for value, _ in TRACK_STATE_FILTERS} | set(TrackState.values)
)

#: Which timestamp `?since=` applies to. "Updated" is the default because the
#: question after a long run is "what changed", not "what was imported".
TRACK_DATE_FIELDS: dict[str, tuple[str, str]] = {
    "updated": ("Updated", "updated_at"),
    "added": ("Added", "created_at"),
}
DEFAULT_DATE_FIELD = "updated"

#: Relative windows, so a bookmarked URL keeps meaning something tomorrow.
TRACK_SINCE_CHOICES: dict[str, tuple[str, timedelta]] = {
    "1h": ("Last hour", timedelta(hours=1)),
    "24h": ("Last 24 hours", timedelta(days=1)),
    "7d": ("Last 7 days", timedelta(days=7)),
    "30d": ("Last 30 days", timedelta(days=30)),
}

#: Not `settings.IDENTIFY_CHAIN`: a track keeps the provider that named it even
#: after that provider leaves the chain, and filtering it out would hide rows.
IDENTIFY_PROVIDERS = ("acoustid", "shazam", "gemini", "tags")


def _filter_state(queryset, state: str):
    """Apply one whitelisted state filter. Unknown values filter nothing."""
    if state == "unidentified":
        return queryset.filter(
            state__in=(TrackState.DISCOVERED, TrackState.IDENTIFYING)
        )
    if state == "planned":
        # A plan worth applying: set, and actually different from where the file
        # already is — `plan_track` stores `path` itself for "already in place".
        return queryset.exclude(planned_path="").exclude(planned_path=F("path"))
    if state in TrackState.values:
        return queryset.filter(state=state)
    return queryset


def _track_state_filters(current: str) -> list[dict]:
    """The filter pills. Deliberately **countless, and query-free**.

    An earlier version carried a count per pill from one conditional aggregate.
    It was still a third query on a fragment refetched on every SSE update, and
    `test_track_fragment_does_not_query_per_row` rightly caught it. The counts
    already exist one panel up: `_compute_stats()` computes them, memoizes them
    against the revision counter, and `_stats.html` now links each badge into
    the matching filter. Numbers in one place, filters in the other.
    """
    return [
        {"value": value, "label": label, "active": current == value}
        for value, label in TRACK_STATE_FILTERS
    ]


def _paginate(queryset, request):
    """One page of `queryset`, treating any nonsense `?page=` as page 1 or last.

    A 404 here would be a worse answer than a page of results: the parameter is
    as likely to come from a stale htmx fragment URL as from a person.
    """
    paginator = Paginator(queryset, settings.PAGE_SIZE)
    try:
        return paginator.page(request.GET.get("page"))
    except PageNotAnInteger:
        return paginator.page(1)
    except EmptyPage:
        return paginator.page(paginator.num_pages)


def _sort_columns(sort: str) -> list[dict]:
    """Header descriptors: where each column links to, and which way it points.

    Built here rather than with a chain of `{% if %}` in the template — the
    toggle rule belongs next to the whitelist it toggles between.
    """
    columns = []
    for key, label in SORT_COLUMNS:
        if sort == key:
            columns.append({"key": key, "label": label, "next": f"-{key}",
                            "arrow": "bi-sort-down-alt"})
        elif sort == f"-{key}":
            columns.append({"key": key, "label": label, "next": key,
                            "arrow": "bi-sort-up-alt"})
        else:
            columns.append({"key": key, "label": label, "next": key, "arrow": ""})
    return columns


def _tracks_page(request) -> dict:
    """Search / filter / sort / paginate. Shared by the full page and the fragment.

    Every parameter is validated against a whitelist and silently falls back to
    its default: these URLs arrive from bookmarks and stale htmx fragments as
    often as from a person, and a 400 would be a worse answer than page one.
    """
    query = (request.GET.get("q") or "").strip()[:200]
    sort = request.GET.get("sort") or DEFAULT_SORT
    if sort not in SORT_CHOICES:
        sort = DEFAULT_SORT

    state = request.GET.get("state") or ""
    if state not in VALID_STATE_FILTERS:
        state = ""

    on = request.GET.get("on") or DEFAULT_DATE_FIELD
    if on not in TRACK_DATE_FIELDS:
        on = DEFAULT_DATE_FIELD

    since = request.GET.get("since") or ""
    if since not in TRACK_SINCE_CHOICES:
        since = ""

    by = request.GET.get("by") or ""
    if by not in IDENTIFY_PROVIDERS:
        by = ""

    queryset = Track.objects.only(*TRACK_LIST_FIELDS)
    if query:
        # `path` is in here because an unidentified track has no title, artist
        # or album to match — the filename is the only handle it has.
        queryset = queryset.filter(
            Q(title__icontains=query)
            | Q(artist__icontains=query)
            | Q(album__icontains=query)
            | Q(path__icontains=query)
        )
    queryset = _filter_state(queryset, state)
    if by:
        queryset = queryset.filter(identified_by=by)
    if since:
        cutoff = timezone.now() - TRACK_SINCE_CHOICES[since][1]
        queryset = queryset.filter(**{f"{TRACK_DATE_FIELDS[on][1]}__gte": cutoff})

    queryset = queryset.order_by(*SORT_CHOICES[sort])
    page = _paginate(queryset, request)

    return {
        "page_obj": page,
        "tracks": page.object_list,
        "is_paginated": page.has_other_pages(),
        "q": query,
        "sort": sort,
        "sort_columns": _sort_columns(sort),
        "sort_options": [
            {"value": value, "label": label, "active": sort == value}
            for value, label in SORT_OPTIONS
        ],
        "state": state,
        "state_filters": _track_state_filters(state),
        "on": on,
        "since": since,
        "by": by,
        "date_fields": [
            {"value": key, "label": label, "active": on == key}
            for key, (label, _) in TRACK_DATE_FIELDS.items()
        ],
        "since_choices": [
            {"value": key, "label": label, "active": since == key}
            for key, (label, _) in TRACK_SINCE_CHOICES.items()
        ],
        "provider_choices": [
            {"value": name, "label": name, "active": by == name}
            for name in IDENTIFY_PROVIDERS
        ],
        #: Drives the "Clear filters" control and the empty-state wording. `on`
        #: is excluded: it selects which date a window applies to and means
        #: nothing on its own.
        "filters_active": bool(query or state or since or by),
    }


#: `_stats()` memoized against the revision counter. One bump wakes every open
#: tab at once and they all refetch the same fragment, so without this the same
#: two aggregates are computed once per tab for identical numbers. Measured at
#: 20k tracks: 76ms per call, so four tabs spent 300ms deriving one answer.
#:
#: The revision is global — any bump invalidates this — and every path that
#: changes a track ends in a job completion, which bumps. A stale entry is
#: therefore not reachable from a change this process made.
_stats_cache: tuple[int, dict] | None = None


def _stats() -> dict:
    """Two grouped queries — never a row dump (A2).

    `_stats.html` is refetched on every SSE update, so its cost is the cost of
    *every* change in the system. Keep it aggregate-only.

    The result is cached until the next `events.bump()`. Treat it as read-only:
    every open tab shares the one dict.
    """
    global _stats_cache

    revision = events.current()
    cached = _stats_cache
    # Read without a lock: the tuple is replaced atomically, so the worst a
    # racing thread can do is compute the same numbers twice.
    if revision and cached is not None and cached[0] == revision:
        return cached[1]

    computed = _compute_stats()
    if revision:
        # Never memoize revision 0. `events.reset_for_tests()` rewinds to 0, so
        # an entry stamped 0 could outlive the reset and answer a later test
        # with an earlier one's numbers. Production leaves 0 at the first bump.
        _stats_cache = (revision, computed)
    return computed


def reset_for_tests() -> None:
    """Drop the memo. `events.reset_for_tests()` rewinds the revision to 0, so a
    cache entry stamped 0 would otherwise outlive the reset and answer with the
    previous test's numbers."""
    global _stats_cache
    _stats_cache = None


def _compute_stats() -> dict:
    counts = {
        row["state"]: row["n"]
        for row in Track.objects.values("state").annotate(n=Count("id"))
    }
    totals = Track.objects.aggregate(
        total=Count("id"),
        pending_moves=Count(
            "id", filter=~Q(planned_path="") & ~Q(planned_path=F("path"))
        ),
    )
    return {
        "state_counts": [
            {"value": value, "label": label, "count": counts.get(value, 0)}
            for value, label in TrackState.choices
        ],
        "track_total": totals["total"] or 0,
        "pending_moves": totals["pending_moves"] or 0,
    }


def _jobs() -> dict:
    """Active jobs plus the last few failures. Two bounded, indexed queries."""
    active = list(
        Job.objects.filter(state__in=JobState.active())
        .order_by("-priority", "id")
        .values("id", "kind", "state", "message", "attempts", "max_attempts")[:20]
    )
    recent_failures = list(
        Job.objects.filter(state=JobState.FAILED)
        .order_by("-finished_at")
        .values("id", "kind", "error", "finished_at")[:5]
    )
    return {
        "active_jobs": active,
        "running_count": sum(1 for j in active if j["state"] == JobState.RUNNING),
        "recent_failures": recent_failures,
    }


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------


def dashboard(request):
    """The library: searchable, sortable, paginated, live via SSE."""
    return render(
        request,
        "music/dashboard.html",
        {
            "nav": "library",
            # One menu is rendered for the whole page and moved to whichever
            # row asked for it. Rendering it per row would repeat this list
            # fifty times for a feature used once in a while.
            "providers": identify_module.available_names(),
            **_tracks_page(request),
            **_stats(),
            **_jobs(),
        },
    )


def fragment_tracks(request):
    return render(request, "music/_tracks.html", _tracks_page(request))


def fragment_stats(request):
    return render(request, "music/_stats.html", _stats())


def fragment_jobs(request):
    return render(request, "music/_jobs.html", _jobs())


#: Everything `_job_row.html` renders. `payload` and `error` are deliberately
#: absent: an error can be 4000 characters and a payload arbitrary JSON, and
#: multiplying that by a page of rows is exactly the kind of pointless transfer
#: this app avoids. Both are fetched per job by `job_detail`.
JOB_LIST_FIELDS = (
    "id", "kind", "state", "attempts", "max_attempts",
    "message", "created_at", "started_at", "finished_at",
)

JOB_STATE_FILTERS = (
    ("", "All"),
    ("active", "Active"),
    (JobState.SUCCEEDED, "Succeeded"),
    (JobState.FAILED, "Failed"),
)


def jobs(request):
    """The job log: what ran, what it said, and what went wrong.

    The `Job` table is the app's audit trail — every scan, identification, move
    and restart passes through it — so this is where you look when something did
    not happen. Rows carry only summary columns; the payload and the full error
    live behind the detail modal, one job at a time.
    """
    state = request.GET.get("state", "")
    queryset = Job.objects.all()
    if state == "active":
        queryset = queryset.filter(state__in=JobState.active())
    elif state in {choice for choice, _ in JOB_STATE_FILTERS} and state:
        queryset = queryset.filter(state=state)

    # -id rather than -created_at: same order (ids are monotonic), but it reads
    # straight off the primary key instead of the created_at index.
    page = _paginate(queryset.only(*JOB_LIST_FIELDS).order_by("-id"), request)

    context = {
        "nav": "jobs",
        "page_obj": page,
        "jobs": page.object_list,
        "state": state,
        "filters": _job_filters(state),
    }
    if request.headers.get("HX-Request"):
        return render(request, "music/_job_list.html", context)
    return render(request, "music/jobs.html", context)


def _job_filters(current: str) -> list[dict]:
    """The filter pills, counts included, resolved here rather than in the
    template — a dictionary lookup by variable key needs a custom filter, and
    the template has no business knowing how the counts are keyed.

    One grouped query covers every pill.
    """
    rows = Job.objects.values("state").annotate(n=Count("id"))
    per_state = {row["state"]: row["n"] for row in rows}
    totals = {
        "": sum(per_state.values()),
        "active": sum(per_state.get(s, 0) for s in JobState.active()),
        **per_state,
    }
    return [
        {
            "value": value,
            "label": label,
            "count": totals.get(value, 0),
            "active": current == value,
        }
        for value, label in JOB_STATE_FILTERS
    ]


def job_detail(request, pk: int):
    """One job in full, loaded into the modal body by htmx.

    Fetched on demand for exactly the job asked about, which is what keeps the
    list page cheap.
    """
    job = get_object_or_404(Job, pk=pk)
    try:
        payload = json.dumps(job.payload, indent=2, sort_keys=True)
    except (TypeError, ValueError):
        payload = str(job.payload)
    return render(
        request,
        "music/_job_detail.html",
        {
            "job": job,
            "payload": payload,
            "duration": job.duration_seconds,
            "spec": _job_spec(job.kind),
        },
    )


def _job_spec(kind: str):
    """The handler's registered description, when it has one."""
    try:
        from music.jobs import registry

        return registry.get(kind)
    except Exception:
        return None


def library_review(request):
    """The organize manifest: every planned move, before any of them happens.

    This page is the reason organizing is safe. A plan writes `planned_path` and
    stops; nothing moves until someone reads this list and presses Apply. Each
    row shows the file's current home and its computed destination, with the
    library root factored out so the part that actually changes is what you read.
    """
    queryset = (
        Track.objects.exclude(planned_path="")
        .exclude(planned_path=F("path"))
        # Exactly what review.html reads, and nothing else.
        .only(
            "id",
            "path",
            "planned_path",
            "plan_note",
            "title",
            "artist",
            "album",
            "duration",
            "state",
            "is_compilation",
        )
        # Grouped by destination, so an album's tracks read as one block.
        .order_by("planned_path", "id")
    )
    page = _paginate(queryset, request)

    root = str(settings.LIBRARY_ROOT)
    moves = [
        {
            "track": track,
            "source": _split_path(track.path, root),
            "destination": _split_path(track.planned_path, root),
            "renames": Path(track.path).name != Path(track.planned_path).name,
        }
        for track in page.object_list
    ]

    return render(
        request,
        "music/review.html",
        {
            "nav": "review",
            "page_obj": page,
            "moves": moves,
            "is_paginated": page.has_other_pages(),
            "pending_moves": page.paginator.count,
            "library_root": root,
        },
    )


def _split_path(raw: str, root: str) -> dict:
    """`{outside_root, directory, name}` — the directory shown relative to root.

    Pure string work on at most one page of rows; no `resolve()`, because that
    stats the filesystem and half the paths in a manifest do not exist yet.
    """
    path = Path(raw)
    try:
        relative = path.parent.relative_to(root)
        directory, outside = str(relative), False
    except ValueError:
        directory, outside = str(path.parent), True
    return {
        "directory": "." if directory == "." else directory,
        "name": path.name,
        "outside_root": outside,
    }


def duplicates(request):
    """Duplicate groups, when the organizer can produce them.

    The detector lives in the library package and may not be built yet; an
    import error renders an empty state instead of a 500, because this page is
    linked from the navbar and a missing module should not break navigation.

    Byte-identical detection needs `content_hash`, and hashing reads every byte
    of every file — far too expensive to fold into a scan on a Pi. So it is
    kicked off from here, the one place the hashes are actually wanted, and
    only while some are still missing. `library.rehash` re-enqueues itself
    batch by batch, and its dedup key means reloading this page cannot stack up
    duplicate work.
    """
    groups, error = _duplicate_groups()

    unhashed = (
        Track.objects.filter(content_hash="")
        .exclude(state=TrackState.MISSING)
        .count()
    )
    if unhashed:
        engine.enqueue("library.rehash", {"limit": 200}, dedup_key="library.rehash")

    return render(
        request,
        "music/duplicates.html",
        {
            "nav": "duplicates",
            "groups": groups,
            "error": error,
            "group_count": len(groups),
            "policy": settings.DUPLICATE_POLICY,
            "unhashed": unhashed,
        },
    )


def _duplicate_groups() -> tuple[list[dict], str]:
    """Normalise whatever `find_duplicates()` returns into rows for a template.

    Accepts a group as either a sequence of Tracks or a mapping carrying a
    `tracks` key, so the page keeps working whichever shape the organizer picks.
    """
    try:
        from music.library.organizer import find_duplicates
    except Exception:
        return [], "unavailable"

    try:
        raw = find_duplicates()
    except Exception:
        log.exception("find_duplicates() failed")
        return [], "failed"

    groups = []
    for entry in raw or ():
        if isinstance(entry, dict):
            tracks = list(entry.get("tracks") or ())
            key = entry.get("key") or entry.get("content_hash") or ""
            reason = entry.get("reason") or ""
        else:
            tracks = list(entry or ())
            key, reason = "", ""
        if len(tracks) > 1:
            groups.append({"key": key, "reason": reason, "tracks": tracks})
    return groups, ""


# --------------------------------------------------------------------------
# Server-Sent Events
# --------------------------------------------------------------------------


def stream_events(request):
    """Push "something changed" to every open tab, at zero idle cost.

    The old endpoint (docs/CODE-AUDIT.md A2) materialised and sorted every row
    of two tables every few seconds, *per connected tab*, and held its thread
    for as long as the tab stayed open — four tabs exhausted the worker pool and
    the dashboard stopped responding.

    This one blocks on `events.wait_for_change`, a condition variable: N tabs
    cost N sleeping threads and **zero queries**, and one writer's `bump()`
    wakes all of them together. The stream then hangs up after
    `SSE_MAX_STREAM_SECONDS` so a thread can never be held indefinitely; the
    browser reconnects by itself thanks to the `retry:` directive below.
    """
    keepalive = float(settings.SSE_KEEPALIVE_SECONDS)
    max_seconds = float(settings.SSE_MAX_STREAM_SECONDS)

    # The stream issues no queries, so it should not sit on a SQLite handle for
    # the next ten minutes. Anything inside a transaction (the test client) is
    # left alone — closing that would break the caller's atomic block.
    for connection in connections.all(initialized_only=True):
        if not connection.in_atomic_block:
            connection.close_if_unusable_or_obsolete()

    def event_stream():
        # Sent first so a client that drops mid-handshake still learns the
        # reconnect delay, and so the recycle below is invisible to the user.
        yield b"retry: 3000\n\n"

        # The page was rendered from the current revision; only push what
        # happens from here on.
        last = events.current()
        deadline = time.monotonic() + max_seconds

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # Hang up. This is the whole of the A2 thread fix: the worker
                # thread returns to the pool on a schedule, not on a whim.
                yield b"event: bye\ndata: recycling\n\n"
                return

            revision, topics = events.wait_for_change(
                since=last, timeout=min(keepalive, remaining)
            )
            if revision == last:
                yield b": keepalive\n\n"
                continue

            last = revision
            yield f"event: update\ndata: {_topic_data(topics)}\n\n".encode()

    response = StreamingHttpResponse(event_stream(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    # nginx is not in this deployment, but a proxy that buffers an event stream
    # turns live updates into a ten-minute silence, so say it anyway.
    response["X-Accel-Buffering"] = "no"
    return response


def _topic_data(topics) -> str:
    """Topics as one SSE `data:` line. A newline here would split the event."""
    cleaned = sorted(t.replace("\n", " ").replace("\r", " ") for t in topics)
    return ",".join(cleaned) or "state"


# --------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------
#
# Every one of these enqueues and returns. None of them touches the filesystem,
# the network or yt-dlp: that is the job engine's work, and a view that waited
# for it would hold a request thread for minutes (A2/A6).


def _notify(message: str, level: str = "success", **extra) -> HttpResponse:
    """204 plus an `HX-Trigger` toast — htmx swaps nothing on a 204.

    The message is JSON-encoded into a header, which escapes quotes and
    newlines but *not* `<`. That is deliberate: escaping here would show
    `&lt;` to the user. The browser-side fix is `textContent` (A11).
    """
    response = HttpResponse(status=204)
    response["HX-Trigger"] = json.dumps(
        {"notify": {"level": level, "message": message}, **extra}
    )
    return response


def _enqueue(kind: str, payload: dict | None = None, **kwargs) -> tuple[Job | None, str]:
    """Enqueue, converting the two expected failures into a message, not a 500."""
    try:
        return engine.enqueue(kind, payload, **kwargs), ""
    except ValueError:
        # No handler registered for this kind — the package that provides it
        # failed to import (registry.load_handlers logs the traceback).
        log.error("no handler registered for job kind %r", kind)
        return None, f"{kind} is unavailable: no handler is registered for it."
    except Exception as exc:
        log.exception("could not enqueue %s", kind)
        return None, f"Could not queue {kind}: {exc}"


def _queued(kind: str, message: str, payload: dict | None = None, **kwargs):
    job, error = _enqueue(kind, payload, **kwargs)
    if job is None:
        return _notify(error, "danger")
    return _notify(message, "info")


@require_POST
def action_scan(request):
    """Walk every ScanRoot and register new audio files."""
    return _queued(
        "library.scan_all",
        "Library scan queued.",
        dedup_key="library.scan_all",
        priority=1,
    )


@require_POST
def action_plan(request):
    """Compute destinations for every identified track. Moves nothing."""
    return _queued(
        "organize.plan_all",
        "Planning moves — check Review when it finishes.",
        dedup_key="organize.plan_all",
        priority=2,
    )


@require_POST
def action_apply(request):
    """Carry out the reviewed manifest. Destructive enough to warrant a confirm."""
    return _queued(
        "organize.apply_all",
        "Applying the planned moves.",
        dedup_key="organize.apply_all",
        priority=2,
    )


@require_POST
def action_identify_track(request, pk: int):
    """Identify one track, optionally through a single named provider.

    `provider` arrives from the row menu (long-press or right-click). It is
    checked against the *running* chain rather than a hardcoded list: a name
    that is configured but unusable — no API key, missing binary — is not
    offered and is not accepted, so the job can never be queued to ask a
    provider that cannot answer.
    """
    track = _track_or_404(pk)
    provider = (request.POST.get("provider") or "").strip()

    if provider and provider not in identify_module.available_names():
        return _notify(f"{provider} is not an available provider.", "warning")

    if provider:
        return _queued(
            "identify.track",
            f"Identifying with {provider}: {track['label']}",
            {"track_id": track["id"], "provider": provider},
            # Distinct from the whole-chain key, so asking for one provider is
            # never handed a queued full-chain job — the toast would name the
            # provider while the chain ran instead.
            dedup_key=f"identify.track:{track['id']}:{provider}",
            priority=3,
        )

    return _queued(
        "identify.track",
        f"Identifying: {track['label']}",
        {"track_id": track["id"]},
        dedup_key=f"identify.track:{track['id']}",
        priority=3,
    )


@require_POST
def action_organize_track(request, pk: int):
    """Plan *and* move this one file. The button is confirmed in the UI.

    `apply` is what separates this from a dry run: without it the handler
    recomputes the destination and stops, which is the plan-all behaviour.

    The dedup key carries `:apply` for that reason. The identify handler queues
    a plan-only `organize.track` for the same track under the plain key, and
    sharing it would let `enqueue` hand this request that plan-only job — the
    toast would say "Organizing" while nothing moved. The two jobs still cannot
    race: the handler takes a per-track lock.
    """
    track = _track_or_404(pk)
    return _queued(
        "organize.track",
        f"Organizing: {track['label']}",
        {"track_id": track["id"], "apply": True},
        dedup_key=f"organize.track:{track['id']}:apply",
        priority=3,
    )


@require_POST
def action_revert_track(request, pk: int):
    """Undo the last move, using the `previous_path` recorded when it was made."""
    track = _track_or_404(pk)
    return _queued(
        "organize.revert",
        f"Reverting: {track['label']}",
        {"track_id": track["id"]},
        dedup_key=f"organize.revert:{track['id']}",
        priority=4,
    )


@require_POST
def action_sync_youtube(request):
    if not settings.PLAYLIST_URL:
        return _notify("No PLAYLIST_URL is configured — set one in Settings.", "warning")
    return _queued(
        "youtube.sync",
        "Playlist sync queued.",
        {"url": settings.PLAYLIST_URL},
        dedup_key="youtube.sync",
        priority=5,
    )


@require_POST
def action_download_video(request, pk: str):
    video = get_object_or_404(
        YoutubeVideo.objects.only("video_id", "title"), pk=pk
    )
    return _queued(
        "youtube.download",
        # Uploader-controlled text. Safe only because the client builds the
        # toast with textContent (A11).
        f"Queued download: {video.title or video.video_id}",
        {"video_id": video.pk},
        dedup_key=f"youtube.download:{video.pk}",
        priority=3,
    )


@require_POST
def action_update_ytdlp(request):
    """Upgrade yt-dlp, then restart the service. Confirmed in the UI."""
    return _queued(
        "maintenance.update_ytdlp",
        "yt-dlp update queued — the service will restart when it finishes.",
        dedup_key="maintenance.update_ytdlp",
        priority=9,
    )


def _track_or_404(pk: int) -> dict:
    """Just the id and a label — an action needs no more of the row than that."""
    row = (
        Track.objects.filter(pk=pk)
        .values("id", "title", "artist", "path")
        .first()
    )
    if row is None:
        raise Http404("no such track")
    if row["artist"] and row["title"]:
        label = f"{row['artist']} - {row['title']}"
    else:
        label = row["title"] or Path(row["path"]).name
    return {"id": row["id"], "label": label}


# --------------------------------------------------------------------------
# Settings (.env editor)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class EnvField:
    """One editable `.env` key, with the validation that keeps it bootable.

    The previous form validated every numeric field with a bare `float()`
    (docs/CODE-AUDIT.md A9), so `0`, `-3` and `2.5` all saved — and
    `SSE_POLL_SECONDS=0` turned every connected stream into a spin loop after
    the next restart. Here each field declares its own type and bounds, which
    mirror the clamps in `music_manager/settings.py`.
    """

    key: str
    label: str
    kind: str  # text | int | number | bool | choice
    help: str = ""
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = ()

    @property
    def is_numeric(self) -> bool:
        return self.kind in ("int", "number")

    @property
    def input_type(self) -> str:
        return "number" if self.is_numeric else "text"

    @property
    def step(self) -> str:
        return "1" if self.kind == "int" else "any"

    @property
    def bounds_hint(self) -> str:
        if not self.is_numeric:
            return ""
        if self.minimum is not None and self.maximum is not None:
            return f"{_plain(self.minimum)}–{_plain(self.maximum)}"
        if self.minimum is not None:
            return f"at least {_plain(self.minimum)}"
        return ""

    def clean(self, raw: str) -> tuple[str | None, str]:
        """Return `(value, error)`. A blank value means "leave unchanged"."""
        value = raw.strip()
        if not value:
            return None, ""
        if "\n" in value or "\r" in value:
            return None, "must be a single line"

        if self.kind == "int":
            try:
                # int() and not float(): "2.5" for a thread count is a typo,
                # and silently truncating it to 2 hides the typo.
                number = int(value, 10)
            except ValueError:
                return None, "must be a whole number"
            return self._check_bounds(number)

        if self.kind == "number":
            try:
                number = float(value)
            except ValueError:
                return None, "must be a number"
            return self._check_bounds(number)

        if self.kind == "bool":
            lowered = value.lower()
            if lowered in ("1", "true", "yes", "on"):
                return "1", ""
            if lowered in ("0", "false", "no", "off"):
                return "0", ""
            return None, "must be on or off"

        if self.kind == "choice":
            if value not in self.choices:
                return None, f"must be one of: {', '.join(self.choices)}"
            return value, ""

        return value, ""

    def _check_bounds(self, number: float) -> tuple[str | None, str]:
        if self.minimum is not None and number < self.minimum:
            return None, f"must be {_plain(self.minimum)} or more"
        if self.maximum is not None and number > self.maximum:
            return None, f"must be {_plain(self.maximum)} or less"
        return _plain(number), ""


def _plain(number: float | int) -> str:
    """Format without a trailing `.0`, so `.env` stays readable."""
    if isinstance(number, int) or float(number).is_integer():
        return str(int(number))
    return repr(float(number))


@dataclass(frozen=True)
class EnvSection:
    title: str
    fields: tuple[EnvField, ...]


#: Deliberately absent: `YTDLP_PATH`, `PIP_PATH`, `FPCALC_PATH`,
#: `FFMPEG_LOCATION`, `SYSTEMD_SERVICE`, `DATABASE_PATH` and every `DJANGO_*`
#: key. This dashboard has no authentication, and those are the keys that decide
#: which binary a background job executes.
ENV_SECTIONS: tuple[EnvSection, ...] = (
    EnvSection(
        "Library",
        (
            EnvField("LIBRARY_ROOT", "Library root", "text",
                     "Destination for organized files."),
            EnvField("SCAN_ROOTS", "Scan roots", "text",
                     "Directories scanned for existing audio, comma-separated."),
            EnvField("DOWNLOAD_STAGING", "Download staging", "text",
                     "Where fresh downloads land before organizing."),
            EnvField("DUPLICATE_POLICY", "Duplicate policy", "choice",
                     "report-only changes nothing.",
                     choices=("report-only", "keep-best", "keep-both")),
            EnvField("AUTO_ORGANIZE", "Organize automatically", "bool",
                     "Off means plans wait for an explicit Apply."),
        ),
    ),
    EnvSection(
        "YouTube",
        (
            EnvField("PLAYLIST_URL", "Playlist URL", "text",
                     "The playlist mirrored by Sync."),
            EnvField("AUDIO_QUALITY", "Audio quality (kbps)", "text",
                     "Passed to the extractor, e.g. 192."),
            EnvField("SYNC_INTERVAL_MINUTES", "Sync every (minutes)", "int",
                     "0 turns the periodic sync off.", minimum=0),
        ),
    ),
    EnvSection(
        "Identification",
        (
            EnvField("IDENTIFY_CHAIN", "Provider chain", "text",
                     "Cheapest first: tags, acoustid, shazam, gemini."),
            EnvField("IDENTIFY_MIN_CONFIDENCE", "Minimum confidence", "number",
                     "Below this a result is discarded and the chain continues.",
                     minimum=0.0, maximum=1.0),
            EnvField("PROVIDER_TIMEOUT_SECONDS", "Provider timeout (s)", "number",
                     "Ceiling on any single network call.", minimum=1.0),
            EnvField("ACOUSTID_RATE_PER_SEC", "AcoustID rate (req/s)", "number",
                     "AcoustID asks for no more than 3.", minimum=0.1),
            EnvField("SHAZAM_ENABLED", "Shazam enabled", "bool",
                     "CPU-heavy; useful for rips and remixes."),
            EnvField("SHAZAM_RATE_PER_MIN", "Shazam rate (req/min)", "number",
                     minimum=0.1),
            EnvField("GEMINI_RATE_PER_MIN", "Gemini rate (req/min)", "number",
                     "Free tier is very limited — keep this low.", minimum=0.1),
            EnvField("GEMINI_DAILY_BUDGET", "Gemini daily budget", "int",
                     "0 disables Gemini entirely.", minimum=0),
        ),
    ),
    EnvSection(
        "Workers",
        (
            EnvField("WORKER_THREADS", "Worker threads", "int",
                     "One ffmpeg transcode saturates a Pi 2 core.",
                     minimum=1, maximum=8),
            EnvField("WORKER_IDLE_WAKE_SECONDS", "Idle wakeup (s)", "number",
                     "Safety net only; workers are event-driven.", minimum=5.0),
            EnvField("WORKER_COOLDOWN_SECONDS", "Cooldown (s)", "number",
                     "Pause after network-heavy jobs. 0 disables it.",
                     minimum=0.0),
            EnvField("JOB_LEASE_SECONDS", "Job lease (s)", "number",
                     "How long before the reaper reclaims a silent job.",
                     minimum=30.0),
            EnvField("JOB_RETENTION_DAYS", "Keep finished jobs (days)", "int",
                     minimum=1),
            EnvField("RESCAN_INTERVAL_MINUTES", "Rescan every (minutes)", "int",
                     "0 turns the periodic rescan off.", minimum=0),
        ),
    ),
    EnvSection(
        "Dashboard",
        (
            EnvField("SSE_KEEPALIVE_SECONDS", "Live-update keepalive (s)", "number",
                     "Idle cost is zero regardless — longer is cheaper.",
                     minimum=1.0),
            EnvField("SSE_MAX_STREAM_SECONDS", "Stream recycle after (s)", "number",
                     "The stream hangs up and the browser reconnects.",
                     minimum=30.0),
            EnvField("PAGE_SIZE", "Rows per page", "int", minimum=5, maximum=500),
            EnvField("LOG_LEVEL", "Log level", "choice",
                     choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")),
        ),
    ),
)

#: Write-only. The stored value never leaves the server — the form reports
#: nothing but "set" or "not set", and a blank submission keeps what is there.
ENV_SECRETS = (
    EnvField("ACOUSTID_API_KEY", "AcoustID API key", "text",
             "Free from acoustid.org; enables bulk identification."),
    EnvField("GEMINI_API_KEY", "Gemini API key", "text",
             "Used only by the last-resort provider."),
)

ENV_FIELDS: dict[str, EnvField] = {
    f.key: f for section in ENV_SECTIONS for f in section.fields
}


def _settings_context(errors: dict | None = None, submitted: dict | None = None) -> dict:
    """Current values from `.env`, falling back to what the process is running."""
    stored = envfile.read_values()
    errors = errors or {}
    submitted = submitted or {}

    sections = []
    for section in ENV_SECTIONS:
        rows = []
        for f in section.fields:
            if f.key in stored:
                value = stored[f.key]
            else:
                value = _running_value(f)
            rows.append(
                {
                    "field": f,
                    "value": submitted.get(f.key, value),
                    "error": errors.get(f.key, ""),
                }
            )
        sections.append({"title": section.title, "rows": rows})

    secrets = [
        {
            "field": f,
            # Presence only. Never the value, not even masked — a mask that is
            # derived from the key is still a leak of its length.
            "is_set": bool(stored.get(f.key) or getattr(settings, f.key, "")),
        }
        for f in ENV_SECRETS
    ]

    return {
        "sections": sections,
        "secrets": secrets,
        "env_path": str(envfile.env_path()),
        "has_errors": bool(errors),
    }


def _running_value(f: EnvField) -> str:
    """What `settings` holds for this key, rendered the way `.env` would store it."""
    value = getattr(settings, f.key, "")
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return _plain(value)
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value)
    return str(value)


def settings_form(request):
    """The settings panel body, loaded into the modal by htmx."""
    return render(request, "music/_settings_form.html", _settings_context())


@require_POST
def update_settings(request):
    """Validate every field, then rewrite `.env` atomically — or nothing at all.

    Validation is all-or-nothing on purpose: a partial save that wrote four good
    values and dropped the fifth would leave the operator's `.env` in a state
    neither they nor the form intended.
    """
    updates: dict[str, str] = {}
    errors: dict[str, str] = {}
    submitted: dict[str, str] = {}

    for key, f in ENV_FIELDS.items():
        raw = request.POST.get(key, "")
        submitted[key] = raw.strip()
        value, error = f.clean(raw)
        if error:
            errors[key] = error
        elif value is not None:
            updates[key] = value

    for f in ENV_SECRETS:
        value, error = f.clean(request.POST.get(f.key, ""))
        if error:
            errors[f.key] = error
        elif value is not None:
            updates[f.key] = value

    if errors:
        # Re-render the panel with the offending fields marked. htmx swaps this
        # into the modal body; a 204 (below) swaps nothing.
        context = _settings_context(errors=errors, submitted=submitted)
        response = render(request, "music/_settings_form.html", context)
        response["HX-Trigger"] = json.dumps(
            {"notify": {"level": "danger",
                        "message": f"{len(errors)} setting(s) need fixing."}}
        )
        return response

    if not updates:
        return _notify("Nothing to update.", "info", closeSettings=True)

    try:
        changed = envfile.set_values(updates)
    except (OSError, ValueError) as exc:
        log.exception("could not write %s", envfile.env_path())
        return _notify(f"Could not save settings: {exc}", "danger")

    if not changed:
        return _notify("No settings changed.", "info", closeSettings=True)

    events.bump("settings")
    return _notify(
        f"Saved {len(changed)} setting(s). They take effect after a restart.",
        "success",
        closeSettings=True,
    )

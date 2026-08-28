"""
Read and rewrite the project `.env` from the running app.

The previous version's writer (docs/CODE-AUDIT.md A8) had four defects, all of
which end with a service that will not boot from an SD card:

* `write_text` truncated in place — a power cut mid-save left `.env` empty;
* no lock — two overlapping settings POSTs interleaved and lost one save;
* it rewrote only the **first** line matching a key, while every reader
  (python-dotenv, systemd) is last-line-wins, so a stale duplicate silently
  shadowed the update;
* it split values at `" #"` and wrote them back unquoted, truncating any value
  that legitimately contained one.

So: one module-level lock, `fileio.atomic_write_text` (temp + fsync + replace),
**every** matching line replaced, and values quoted on write so a `#` or a space
cannot come back as something else. Comments, blank lines and key order survive
a rewrite, because this file is also read and hand-edited by a human.
"""

from __future__ import annotations

import logging
import re
import threading
from pathlib import Path

from django.conf import settings

from music.core.fileio import atomic_write_text

log = logging.getLogger("music.envfile")

#: One process (enforced by core.runtime), so a threading lock is the whole
#: mutual exclusion story for concurrent settings saves.
_lock = threading.Lock()

#: `KEY=`, `export KEY=`, with or without surrounding whitespace. A comment
#: line cannot match: `#` is neither whitespace nor a leading identifier char.
_ASSIGNMENT = re.compile(r"^(\s*)(export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")
_KEY_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: Written bare only when nothing in the value can be misread by any consumer.
_SAFE_BARE = re.compile(r"^[A-Za-z0-9_.,:/@%+=~-]+$")


def env_path() -> Path:
    """The `.env` this app reads. Overridable in tests via settings.ENV_FILE_PATH."""
    override = getattr(settings, "ENV_FILE_PATH", None)
    return Path(override) if override else Path(settings.BASE_DIR) / ".env"


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


def _parse_value(raw: str) -> str:
    """Unwrap one layer of quoting, matching how python-dotenv reads the file.

    Unquoted values lose a trailing ` # comment` (`.env.example` ships several);
    quoted values keep every character, which is the A8 truncation fix.
    """
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        inner = raw[1:-1]
        if raw[0] == '"':
            inner = inner.replace('\\"', '"').replace("\\\\", "\\")
        return inner
    return re.sub(r"\s+#.*$", "", raw).strip()


def read_values(path: str | Path | None = None) -> dict[str, str]:
    """Current `KEY -> value` pairs. Missing or unreadable file yields `{}`.

    Last assignment wins, exactly like python-dotenv and systemd, so a file with
    a duplicated key reports what the app will actually see.
    """
    target = Path(path) if path else env_path()
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        return {}

    values: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = _ASSIGNMENT.match(line)
        if match:
            values[match.group(3)] = _parse_value(match.group(4))
    return values


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------


def quote(value: str) -> str:
    """Render a value so every reader gets back exactly what was typed."""
    if value == "":
        return '""'
    if _SAFE_BARE.match(value):
        return value
    if "'" not in value:
        # Single quotes are literal for dotenv and systemd alike: no escapes to
        # get wrong, which is what we want for paths and URLs.
        return f"'{value}'"
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def set_values(
    updates: dict[str, str], path: str | Path | None = None
) -> list[str]:
    """Apply `updates` to `.env`; return the keys whose stored value changed.

    Existing assignments are rewritten in place (**all** of them, not just the
    first), unknown keys are appended, and everything else — comments, blank
    lines, ordering — is left byte-for-byte alone.
    """
    for key, value in updates.items():
        if not _KEY_NAME.match(key):
            raise ValueError(f"{key!r} is not a valid environment variable name")
        if "\n" in value or "\r" in value or "\0" in value:
            # A newline here would inject an arbitrary second assignment into
            # .env — including keys this form deliberately refuses to expose.
            raise ValueError(f"{key}: value may not contain a line break")

    target = Path(path) if path else env_path()

    with _lock:
        before = read_values(target)
        try:
            lines = target.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []

        pending = dict(updates)
        written: set[str] = set()
        out: list[str] = []

        for line in lines:
            match = _ASSIGNMENT.match(line)
            key = match.group(3) if match else None
            if key is None or key not in pending:
                out.append(line)
                continue
            if key in written:
                # A duplicate assignment further down the file would shadow the
                # one we just rewrote. Drop it rather than leave a stale value.
                log.info("dropping duplicate assignment of %s in %s", key, target)
                continue
            indent, export = match.group(1), match.group(2) or ""
            out.append(f"{indent}{export}{key}={quote(pending[key])}")
            written.add(key)

        for key, value in pending.items():
            if key not in written:
                out.append(f"{key}={quote(value)}")

        atomic_write_text(target, "\n".join(out) + "\n")

        after = read_values(target)
        changed = sorted(k for k in updates if before.get(k) != after.get(k))

    if changed:
        log.info("updated %s in %s", ", ".join(changed), target)
    return changed

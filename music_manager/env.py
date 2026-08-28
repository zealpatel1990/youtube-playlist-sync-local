"""Environment readers with validation and clamping.

Readers raise ImproperlyConfigured with an actionable message rather than let a
traceback escape settings import; numeric readers clamp rather than reject, so a
hand-edited .env cannot turn a loop into a spin.
"""

from __future__ import annotations

import math
import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


def _raw(name: str) -> str | None:
    """Read a variable, stripping whitespace and one layer of stray quoting.

    systemd's EnvironmentFile takes the whole line after '=', so values arrive
    with the quotes the operator meant as shell syntax.
    """
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value


def env_str(
    name: str,
    *,
    default: str | None = None,
    required: bool = False,
    choices: tuple[str, ...] | None = None,
    help: str = "",
) -> str:
    value = _raw(name)
    if value is None or value == "":
        if required:
            hint = f" {help}" if help else ""
            raise ImproperlyConfigured(
                f"{name} is required but not set. Add it to your .env file.{hint}"
            )
        value = default if default is not None else ""
    if choices and value not in choices:
        raise ImproperlyConfigured(
            f"{name}={value!r} is not valid. Choose one of: {', '.join(choices)}."
        )
    return value


def env_bool(name: str, *, default: bool = False) -> bool:
    value = _raw(name)
    if value is None:
        return default
    lowered = value.lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise ImproperlyConfigured(
        f"{name}={value!r} is not a boolean. Use one of: "
        f"{', '.join(sorted(_TRUE))} / {', '.join(sorted(t for t in _FALSE if t))}."
    )


def env_int(
    name: str,
    *,
    default: int,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    value = _raw(name)
    if value is None or value == "":
        parsed = default
    else:
        try:
            # float() first so "2.0" is accepted; int("2.0") would raise.
            # int(inf) raises OverflowError, not ValueError, so catch both.
            parsed = int(_finite(name, float(value), value))
        except (ValueError, OverflowError):
            raise ImproperlyConfigured(
                f"{name}={value!r} is not a number. Expected a whole number."
            ) from None
    return _clamp(parsed, minimum, maximum)


def env_float(
    name: str,
    *,
    default: float,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    value = _raw(name)
    if value is None or value == "":
        parsed = default
    else:
        try:
            # nan defeats clamping outright — every comparison against it is
            # False — so it would sail past minimum= and land in a sleep().
            parsed = _finite(name, float(value), value)
        except ValueError:
            raise ImproperlyConfigured(
                f"{name}={value!r} is not a number."
            ) from None
    return _clamp(parsed, minimum, maximum)


def env_list(
    name: str,
    *,
    default: list[str] | None = None,
    separator: str | None = ",",
) -> list[str]:
    """Split on `separator`, or on both ',' and os.pathsep when separator is None.

    Paths need the second form: a Windows path contains ':' and a POSIX
    directory name may legitimately contain ','.
    """
    value = _raw(name)
    if value is None or value == "":
        return list(default) if default is not None else []
    if separator is None:
        parts = [value]
        for sep in (os.pathsep, ","):
            parts = [chunk for part in parts for chunk in part.split(sep)]
    else:
        parts = value.split(separator)
    return [part.strip() for part in parts if part.strip()]


def env_path(
    name: str, *, default: Path | None = None, required: bool = False
) -> Path:
    value = _raw(name)
    if value is None or value == "":
        if required:
            raise ImproperlyConfigured(
                f"{name} is required but not set. It must be an absolute path "
                f"to a directory (it will be created if missing)."
            )
        if default is None:
            raise ImproperlyConfigured(f"{name} is not set and has no default.")
        return Path(default)
    return Path(value).expanduser()


def _finite(name: str, parsed: float, raw: str) -> float:
    """Reject inf/nan. Raised as ValueError so each caller words its own message."""
    if not math.isfinite(parsed):
        raise ValueError(f"{name}={raw!r} is not a finite number.")
    return parsed


def _clamp(value, minimum, maximum):
    if minimum is not None and value < minimum:
        return minimum
    if maximum is not None and value > maximum:
        return maximum
    return value

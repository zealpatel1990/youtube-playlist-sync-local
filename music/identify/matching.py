"""Sanity checks that compare a provider's answer against the title we know."""

from __future__ import annotations

import re

#: Words that say "this is a different performance of that song". A provider
#: whose answer carries one of these, for a file whose own title does not, has
#: matched a cover rather than the recording asked for — the fingerprint of a
#: faithful cover is close enough that this happens routinely.
DERIVATIVE_MARKERS = (
    "cover", "karaoke", "tribute", "instrumental", "remix", "rework",
    "backing track", "made famous by", "in the style of", "originally performed",
)

#: Dropped before comparing: present in most YouTube titles and in no metadata,
#: so leaving them in makes everything look related to everything.
_NOISE = {
    "official", "video", "audio", "lyrical", "lyrics", "full", "song", "songs",
    "movie", "film", "from", "with", "feat", "featuring", "presenting", "new",
    "latest", "hindi", "punjabi", "gujarati", "version", "quality", "track",
    "music", "released", "release", "original", "soundtrack", "single",
}

#: What YouTube substitutes for a video it will no longer describe. These are
#: not titles, and a hint of "[Deleted video]" tokenises to {"deleted"} — which
#: overlaps nothing, so every guard below fires and the whole chain is vetoed.
#: Observed on a real library: AcoustID, Shazam, Gemini *and* the file's own
#: tags all answered "DrINsaNE - JUST A BOY" and all four were discarded.
#:
#: `ingest.youtube` maps the same two strings to an Availability. Duplicated
#: rather than imported because this module stays free of Django and of every
#: other package, so the naming policy can be unit-tested in isolation.
PLACEHOLDER_HINTS = frozenset(
    {
        "[private video]",
        "[deleted video]",
        "[unavailable video]",
        "[unavailable]",
        "[removed]",
        "[no longer available]",
    }
)

_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)


def usable_hint(hint_title: str) -> str:
    """The hint, or `""` when it carries no information about the recording.

    Every guard runs this first, so a placeholder is treated as "no hint given"
    rather than as a title that agrees with nothing.
    """
    stripped = (hint_title or "").strip()
    if stripped.lower() in PLACEHOLDER_HINTS:
        return ""
    return stripped


def tokens(text: str) -> set[str]:
    """Comparable words: lowercase, at least three letters, noise removed."""
    return {
        word
        for word in (m.group(0).lower() for m in _WORD.finditer(text or ""))
        if len(word) >= 3 and word not in _NOISE
    }


def is_derivative(title: str) -> bool:
    lowered = (title or "").lower()
    return any(marker in lowered for marker in DERIVATIVE_MARKERS)


def looks_like_a_different_recording(
    result_title: str, result_artist: str, hint_title: str
) -> bool:
    """True when the answer is a cover of what was asked for, not the thing itself.

    The case this exists for: a file whose title says "Billie Eilish - when the
    party's over" matched a Poté cover, because the cover's own title contains
    "Billie Eilish Cover" and its duration was one second closer. Word overlap
    alone endorses it. What separates them is that the answer is *marked* as a
    cover while the file's title is not, and the credited artist is someone the
    file never mentions.
    """
    hint_title = usable_hint(hint_title)
    if not hint_title:
        return False
    if not is_derivative(result_title) or is_derivative(hint_title):
        return False
    # The answer says "cover" and the file's title does not. That is only
    # damning if the credited artist is a stranger to the file's title — a
    # self-published cover names its own performer.
    return not shares_a_word(result_artist, hint_title)


def shares_a_word(text: str, hint_title: str) -> bool:
    return bool(tokens(text) & tokens(hint_title))


#: Only a spaced dash separates an artist from a title. "Ziddi-Piddi" and
#: "Bluff-Master" are single words; " - " is the convention uploaders use.
_ARTIST_SPLIT = re.compile(r"\s[-–—]\s")


def hint_artist(hint_title: str) -> str:
    """The artist half of an "Artist - Title" upload, or "" if it has no such shape.

    Only the left side of a spaced dash counts, and only when it looks like a
    name rather than the first half of a song title. Everything else returns ""
    so callers treat the hint as carrying no artist claim at all.
    """
    hint_title = usable_hint(hint_title)
    if not hint_title:
        return ""
    parts = _ARTIST_SPLIT.split(hint_title, 1)
    if len(parts) != 2:
        return ""
    left = parts[0].strip()
    # A trailing colon means the left side is a context, not a performer:
    # "Pushpa: Saami Saami - Lyrical" names a film and a song, no artist.
    if ":" in left:
        return ""
    return left if tokens(left) else ""


def contradicts_hint_artist(result_artist: str, hint_title: str) -> bool:
    """True when the title names an artist and the answer credits a different one.

    The case this exists for: a Billie Eilish download that AcoustID answered
    with "Sons of Serendip" at 0.98. Nothing else catches it — the title matches,
    the answer carries no cover marker, and the score is as high as scores get.
    The only thing wrong is the name on it, and the upload title says so.

    False whenever there is no artist to contradict, so a file titled
    "Ek School Banana Hai" is never demoted for naming nobody.
    """
    artist = hint_artist(hint_title)
    if not artist or not result_artist:
        return False
    return not bool(tokens(result_artist) & tokens(artist))


def is_unrelated(result_title: str, result_artist: str, hint_title: str) -> bool:
    """True when the answer has nothing in common with the title we know.

    Deliberately generous: one shared word anywhere in the title OR the artist
    is enough. Regional titles transliterate unpredictably — "Jai Adhyashakti"
    against "Jay Aadhya Shakti" shares no token at all, and only the artist
    saves it — so anything stricter rejects correct answers.
    """
    hint = tokens(usable_hint(hint_title))
    if not hint:
        return False  # nothing to judge against
    return not (hint & (tokens(result_title) | tokens(result_artist)))

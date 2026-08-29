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

_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)


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


def is_unrelated(result_title: str, result_artist: str, hint_title: str) -> bool:
    """True when the answer has nothing in common with the title we know.

    Deliberately generous: one shared word anywhere in the title OR the artist
    is enough. Regional titles transliterate unpredictably — "Jai Adhyashakti"
    against "Jay Aadhya Shakti" shares no token at all, and only the artist
    saves it — so anything stricter rejects correct answers.
    """
    hint = tokens(hint_title)
    if not hint:
        return False  # nothing to judge against
    return not (hint & (tokens(result_title) | tokens(result_artist)))

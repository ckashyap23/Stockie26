"""Exclusion cues: phrases that say a headline is NOT a fresh event for a seed. They only ever FILTER a candidate out.

They are never an entry requirement: relevance comes from similarity, direction and size from the LLM. A cue is matched against the
headline as a whole word, a phrase (substring), or a prefix when it ends with `*` (e.g. `disrupt*`). Case-insensitive.
"""
from __future__ import annotations

import re


def cue_hit(text: str, cue: str) -> bool:
    cue = cue.strip().lower()
    if not cue:
        return False
    text = text.lower()
    if cue.endswith("*"):
        return re.search(r"\b" + re.escape(cue[:-1]) + r"\w*", text) is not None
    if re.fullmatch(r"[a-z0-9]+", cue):
        return re.search(r"\b" + re.escape(cue) + r"\b", text) is not None
    return cue in text


def first_exclusion(text: str, cues: tuple[str, ...]) -> str | None:
    """The first cue found in `text`, or None."""
    for cue in cues:
        if cue_hit(text, cue):
            return cue
    return None

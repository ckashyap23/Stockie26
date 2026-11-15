"""Prompt construction and strict response parsing for the LLM judge.

PROMPT_VERSION is part of the cache key: change the prompt text => bump it, and old cached judgements are ignored.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass

PROMPT_VERSION = "v2"

SYSTEM_PROMPT = """You are a markets analyst. For each numbered news item you judge whether it matters for the NIFTY 50 index at the \
next Indian market open, in relation to ONE named event type (the "seed").

Rules:
- Use ONLY the text given. Do not invent figures, consensus numbers or market reactions. If the text does not say what was expected, \
set surprise to "unknown" and lower your confidence.
- relates: does this item report an actual new development of the seed's event type? Opinion pieces, forecasts, broker views, \
explainers, previews, interviews, lists and stories about unrelated or much smaller players are NOT events: set relates=false \
(or is_commentary=true for opinion/analysis about an event type).
- Fresh: the development must be NEW (the last day or so). A chronic or ongoing story, a trend recap or a status update with no new
development (e.g. "the weakest monsoon in a decade is straining demand" with no new data or forecast) is not fresh: set relates=false.
- provenance: "official" = stated by the institution, government or company, or by a major wire, with specifics (figures, a vote
count, a named statement); "reported" = a credible outlet reporting something that has happened; "expectation" = a forecast, preview or
'may/could/likely' about a FUTURE event; "unverified" = a social-media post, rumour, clickbait or sensational phrasing with no
specifics or attribution, or a claim that contradicts other information you were given. For central-bank and government decisions,
use official or reported ONLY if the text states the decision with specifics.
- already_priced: true if the text indicates the outcome was fully expected or already reflected in prices.
- direction is the expected effect on NIFTY at the next open: 1 = bullish, -1 = bearish, 0 = no clear effect, ambiguous, or priced in. \
Judge the net effect on India (e.g. higher oil is bearish, a hot US jobs print that lifts yields is bearish), not the tone of the words.
- intensity (0..1) is the expected SIZE of the effect on NIFTY: 0.1 = barely noticeable, 0.3 = minor, 0.5 = notable, 0.8 = major, \
1.0 = a once-a-year shock.
- confidence (0..1): how well the text supports your direction call. relation_confidence (0..1): how sure you are the item is \
really about the seed's event type.
- rationale: at most 25 words, citing what the text says.

Return ONLY a JSON object, no other text:
{"results": [{"id": <item id>, "relates": true|false, "relation_confidence": 0..1, "direction": -1|0|1, "intensity": 0..1, \
"confidence": 0..1, "surprise": "beat"|"miss"|"in_line"|"unknown"|"not_applicable", "is_commentary": true|false, \
"already_priced": true|false, "provenance": "official"|"reported"|"expectation"|"unverified", "rationale": "..."}]}
Include exactly one result per item id."""


@dataclass
class PromptItem:
    key: int  # 1..n inside the batch
    seed_id: str
    event_type: str
    hypothesis: str
    guardrail: str
    guidance: str
    source: str
    published: str
    text_kind: str  # full_text | summary | headline_only
    headline: str
    text: str


@dataclass
class ParsedResult:
    relates: bool
    relation_confidence: float
    direction: int
    intensity: float
    confidence: float
    surprise: str
    is_commentary: bool
    already_priced: bool
    rationale: str
    provenance: str = "reported"  # official | reported | expectation | unverified

    def to_dict(self) -> dict:
        return asdict(self)


def build_user_prompt(items: list[PromptItem]) -> str:
    blocks = []
    for it in items:
        lines = [
            f"### ITEM {it.key}",
            f"SEED {it.seed_id}: {it.event_type}",
            f"HOW IT AFFECTS NIFTY: {it.hypothesis}",
            f"GUARDRAIL: {it.guardrail}",
        ]
        if it.guidance:
            lines.append(f"GUIDANCE: {it.guidance}")
        lines += [
            f"SOURCE: {it.source} | PUBLISHED: {it.published} | TEXT: {it.text_kind}",
            f"HEADLINE: {it.headline}",
            f"BODY: {it.text or '(none: judge from the headline only and keep confidence low)'}",
        ]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + f"\n\nReturn one result for each of the {len(items)} items above (ids {', '.join(str(i.key) for i in items)})."


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def _extract_json(text: str):
    text = _FENCE.sub("", (text or "").strip())
    try:
        return json.loads(text)
    except ValueError:
        pass
    starts = [i for i in (text.find("{"), text.find("[")) if i != -1]
    if not starts:
        raise ValueError("no JSON found")
    start = min(starts)
    end = max(text.rfind("}"), text.rfind("]"))
    if end <= start:
        raise ValueError("unterminated JSON")
    return json.loads(text[start:end + 1])


def _bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1"}
    return bool(value)


def _unit(value) -> float:
    number = float(value)
    return max(0.0, min(1.0, number))


def _direction(value) -> int:
    if isinstance(value, str):
        mapping = {"bullish": 1, "positive": 1, "up": 1, "bearish": -1, "negative": -1, "down": -1, "neutral": 0, "none": 0}
        value = mapping.get(value.strip().lower(), value)
    number = int(float(value))
    return 1 if number > 0 else (-1 if number < 0 else 0)


_SURPRISE = {"beat", "miss", "in_line", "unknown", "not_applicable"}
_PROVENANCE = {"official", "reported", "expectation", "unverified"}


def parse_response(text: str, expected_keys: list[int]) -> tuple[dict[int, ParsedResult], dict[int, str]]:
    """Returns (valid results by item key, error message by item key for the rest). Never raises."""
    results: dict[int, ParsedResult] = {}
    errors: dict[int, str] = {}
    try:
        data = _extract_json(text)
    except ValueError as exc:
        return {}, {k: f"unparseable response ({exc})" for k in expected_keys}
    rows = data.get("results") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return {}, {k: "response has no results list" for k in expected_keys}
    for row in rows:
        try:
            key = int(row["id"])
            surprise = str(row.get("surprise", "unknown")).strip().lower()
            results[key] = ParsedResult(
                relates=_bool(row["relates"]),
                relation_confidence=_unit(row.get("relation_confidence", 1.0 if _bool(row["relates"]) else 0.0)),
                direction=_direction(row.get("direction", 0)),
                intensity=_unit(row.get("intensity", 0.0)),
                confidence=_unit(row.get("confidence", 0.0)),
                surprise=surprise if surprise in _SURPRISE else "unknown",
                is_commentary=_bool(row.get("is_commentary", False)),
                already_priced=_bool(row.get("already_priced", False)),
                rationale=" ".join(str(row.get("rationale", "")).split())[:300],
                provenance=(str(row.get("provenance", "reported")).strip().lower() if str(row.get("provenance", "reported")).strip().lower() in _PROVENANCE else "reported"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(row, dict) and "id" in row:
                try:
                    errors[int(row["id"])] = f"invalid field ({type(exc).__name__}: {str(exc)[:60]})"
                except (TypeError, ValueError):
                    pass
    for key in expected_keys:
        if key not in results and key not in errors:
            errors[key] = "item missing from the response"
    return {k: v for k, v in results.items() if k in expected_keys}, errors

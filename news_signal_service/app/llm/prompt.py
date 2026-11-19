"""Prompt construction and strict response parsing for the LLM judge.

PROMPT_VERSION is part of the cache key: change the prompt text => bump it, and old cached judgements are ignored.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass

PROMPT_VERSION = "v5"

SYSTEM_PROMPT = """You are a markets analyst. For each numbered news item you judge whether it REPORTS an actual, NEW, FACTUAL event \
that matters for the NIFTY 50 index at the next Indian market open, in relation to ONE named seed (an event type, with seed_kind \
"discrete_event" or "market_move"). Judge each item independently. Use ONLY the text given; never invent figures, consensus numbers \
or reactions.

STEP 1 - event_status (decide this first; everything else follows from it):
- "occurred": a decision, data release, filing, corporate action, transaction, attack or incident that HAS happened, reported as \
news with specifics (a figure, vote, named statement, named actor).
- "market_move": a COMPLETED, quantified move in the exact instrument the seed names over the last session ("Dow falls 550 points", \
"rupee closes at 93.44", "Brent settles above $92"). Valid only when seed_kind="market_move".
- "official_remarks": a speech, interview, TV appearance or "says/signals/thinks/sees" by an individual official or executive that is \
not a committee decision or a formal release. Interview transcripts belong here.
- "scheduled_or_expected": a decision/release that has NOT happened ("set to", "expected to", "likely to", "poised to", "will", \
"ahead of", "to await").
- "opinion_or_forecast": analysis, analyst or strategist views, "what analysts are saying", reaction roundups, explainers, technical \
levels, "should you buy/bet", and anything built on could/may/might/looks/outlook.
- "wrap_or_preview": market wraps and mood pieces, pre-market notes ("stocks to watch", "key triggers", "outlook today", \
"what's likely to move the market"), live blogs, futures or GIFT Nifty indications, video/newsletter rundowns covering several \
topics, "latest news and analysis" pages.
- "background": chronic or ongoing stories, recaps, or an event whose underlying release/decision happened before the window start \
(compare published_at and the window; a recap of last week's data is NOT fresh).

Tie-breaks: classify by the MAIN CLAUSE. A headline that pairs a fact with a hedge ("X, but outlook murky"; "markets rebound, but \
inflation clouds rate outlook") is not "occurred" unless the text independently reports a concrete event with specifics. If torn \
between "occurred" and any other status, choose the other status. A price move that is merely mentioned inside a wrap is \
wrap_or_preview, not market_move. Judge by the article's nature, not by who is speaking.

STEP 2 - seed match (strict):
relates=true only if the event is exactly what the seed's NAME describes. Adjacent topics do not match: a defence procurement deal is \
not India-Pakistan escalation; a ministry ASKING for a bigger capex is not a Budget outcome; a duty on one product category is not a \
NIFTY-level tariff event; a US outage story is not an AI-capex repricing. If the best fit is a different seed, relates=false.

STEP 3 - gating (hard):
- Score ONLY if relates=true AND event_status is "occurred" (or "market_move" for market_move seeds).
- "official_remarks": relates=false for market_move seeds; otherwise score only if it is a clear change to formal guidance, and cap \
intensity at 0.2.
- Every other status: relates=false, direction=0, intensity=0, confidence=0, provenance="expectation".
- is_commentary=true for official_remarks, opinion_or_forecast and wrap_or_preview.

STEP 4 - scoring (only if gated in):
- provenance describes the ARTICLE: "official" = the item itself reports the institution's/company's own release or decision with \
specifics; "reported" = a credible outlet reporting something that has happened; "unverified" = rumour, social post, or no \
attribution/specifics. A news story about a statement is "reported" unless it carries the release itself.
- already_priced: true only if the text says the outcome was fully expected. Stale or recap items are "background", not priced in.
- direction: net effect on NIFTY at the next open: 1 bullish, -1 bearish, 0 none/ambiguous/priced in. Judge the net effect on India \
(higher oil is bearish; a hot US jobs print that lifts yields is bearish).
- intensity anchors (NIFTY move expected from THIS item alone): 0.1 under 0.1%; 0.3 about 0.2-0.3%; 0.5 about 0.5%; 0.8 about 1%; \
1.0 2% or more. If the affected companies or sectors are a small share of NIFTY (one product category, one small-cap, one \
procurement contract), intensity is at most 0.1.
- confidence (0..1): how well the text supports the direction. relation_confidence (0..1): how sure you are it fits the seed.
- evidence: up to 12 words quoted from the item showing the event has occurred; "" if none. rationale: at most 25 words.

Examples (event_status -> outcome):
1. "Markets rebound sharply after Diwali, but inflation clouds rate outlook" -> wrap_or_preview; all scores 0.
2. "Fed officials set to raise rates again in December, but the future still looks murky" -> scheduled_or_expected; all scores 0.
3. "Here's what every major Wall Street analyst is saying about Nvidia after its earnings" -> opinion_or_forecast; all scores 0.
4. "Read Jim Cramer's full interview with Nvidia's Colette Kress" -> official_remarks; all scores 0 for the earnings seed.
5. "Lower crude oil prices could bring huge windfall for India in FY28: CEA" -> opinion_or_forecast; all scores 0.
6. "Thursday's big stock stories: What's likely to move the market" -> wrap_or_preview; all scores 0.
7. "Defence stocks in spotlight after ministry seeks 20% hike in FY28 capex" (seed: Union Budget surprise) -> relates=false.
8. "India signs Rs 1,976 crore deal with Israel to upgrade Barak" (seed: India-Pakistan escalation) -> relates=false.
9. "Fed minutes leave door open to larger December rate hike" -> occurred; score it.
10. "Dow Jones falls 550 points" (seed_kind=market_move, R03) -> market_move; score it.

Return ONLY JSON, event_status first in each result:
{"results": [{"id": <item id>, "event_status": "occurred"|"market_move"|"official_remarks"|"scheduled_or_expected"|\
"opinion_or_forecast"|"wrap_or_preview"|"background", "evidence": "...", "relates": true|false, "relation_confidence": 0..1, \
"is_commentary": true|false, "direction": -1|0|1, "intensity": 0..1, "confidence": 0..1, \
"surprise": "beat"|"miss"|"in_line"|"unknown"|"not_applicable", "already_priced": true|false, \
"provenance": "official"|"reported"|"expectation"|"unverified", "rationale": "..."}]}
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
    seed_kind: str = "discrete_event"  # discrete_event | market_move


EVENT_STATUSES = ("occurred", "market_move", "official_remarks", "scheduled_or_expected", "opinion_or_forecast",
                  "wrap_or_preview", "background")


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
    event_status: str = "occurred"  # see EVENT_STATUSES; the parser requires it, the default only serves old cached rows
    evidence: str = ""  # words quoted from the item showing the event has occurred

    def to_dict(self) -> dict:
        return asdict(self)


def build_user_prompt(items: list[PromptItem], window: str = "") -> str:
    blocks = []
    if window:
        blocks.append(f"WINDOW (the news window being scored; published_at before its start is not fresh): {window}")
    for it in items:
        lines = [
            f"### ITEM {it.key}",
            f"SEED {it.seed_id}: {it.event_type}",
            f"SEED_KIND: {it.seed_kind}",
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
            event_status = str(row["event_status"]).strip().lower().replace(" ", "_")
            if event_status not in EVENT_STATUSES:
                raise ValueError(f"event_status {event_status!r}")  # fail closed: an unclassified item is never scored
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
                event_status=event_status,
                evidence=" ".join(str(row.get("evidence", "") or "").split())[:200],
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

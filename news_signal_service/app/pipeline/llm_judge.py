"""LLM judge (every seed): relation to the seed, direction, size and confidence for each matched event.

For every (event, seed) the model sees the seed's event type, transmission hypothesis, guardrail and per-seed guidance plus the
headline and an excerpt of the article. It answers relates / direction / intensity / confidence / surprise / commentary / priced-in
(see app/llm/prompt.py). Anything that is not a clear, related, non-commentary, not-priced-in event scores exactly zero.

Quota-friendly: events are batched, answers are cached (reruns are free and identical), the number of calls per run is capped, and
a fatal error (bad key, unknown model) stops further calls. Items that could not be judged score zero and are reported, never
silently replaced by a weaker method.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from app.domain import ImpactJudgement, RelevantArticle, SeedEvent
from app.llm.cache import LlmCache, cache_key
from app.llm.client import LlmClient, LlmError
from app.pipeline.source_quality import SourceQuality
from app.llm.prompt import SYSTEM_PROMPT, ParsedResult, PromptItem, build_user_prompt, parse_response
from app.tuning import Tuning, load_tuning

log = logging.getLogger(__name__)


@dataclass
class LlmReport:
    calls: int = 0
    cached: int = 0
    judged: int = 0
    unscored: int = 0
    vetoed: int = 0  # answered, but "not about the seed" / commentary / priced in -> zero
    errors: list[str] = field(default_factory=list)
    call_log: list[dict] = field(default_factory=list)  # one entry per request, for the run folder

    def summary(self) -> str:
        return (f"{self.judged} judged ({self.cached} cached, {self.vetoed} vetoed to zero), {self.unscored} unscored, "
                f"{self.calls} calls")


class LlmJudge:
    name = "llm_judge"

    def __init__(
        self,
        client: LlmClient | None,
        cache: LlmCache | None,
        seeds: tuple[SeedEvent, ...],
        tuning: Tuning | None = None,
        max_calls: int = 40,
        not_configured_reason: str = "LLM not configured",
        source_quality: SourceQuality | None = None,
    ) -> None:
        self._client, self._cache = client, cache
        self._seeds = {s.seed_id: s for s in seeds}
        self._tuning = tuning or load_tuning()
        self._max_calls = max_calls
        self._quality = source_quality or SourceQuality()
        self._reason = not_configured_reason

    # -- public -------------------------------------------------------------------------------------------------
    def _seed_kind(self, seed_id: str) -> str:
        return "market_move" if seed_id.upper() in self._tuning.llm_market_move_seeds else "discrete_event"

    def judge(self, items: list[RelevantArticle], window: str = "") -> tuple[list[ImpactJudgement], LlmReport]:
        """`window` describes the news window ("start to end", IST); the model needs it to tell a fresh event from a recap."""
        report = LlmReport()
        out: dict[int, ImpactJudgement] = {}
        pending: list[tuple[int, RelevantArticle, str, str]] = []  # (index, item, cache key, text sent)

        for idx, item in enumerate(items):
            seed = self._seeds[item.seed_id]
            text = self._text_for(item)
            if self._client is None:
                out[idx] = self._unscored(item, self._reason, report)
                continue
            key = cache_key(self._client.model, seed.seed_id, item.article.title, text, seed.llm_guidance, window)
            hit = self._cache.get(key) if self._cache else None
            if hit is not None:
                report.cached += 1
                out[idx] = self._judgement(item, hit, report, cached=True)
            else:
                pending.append((idx, item, key, text))

        batch_size = max(1, self._tuning.llm_batch_size)
        dead = False
        for start in range(0, len(pending), batch_size):
            batch = pending[start:start + batch_size]
            if dead or report.calls >= self._max_calls:
                reason = "stopped after a fatal LLM error" if dead else f"call budget reached ({self._max_calls} per run)"
                for idx, item, _, _ in batch:
                    out[idx] = self._unscored(item, reason, report)
                continue
            dead = self._run_batch(batch, out, report, window)

        if report.unscored and not self._client:
            report.errors.append(self._reason)
        return [out[i] for i in range(len(items))], report

    # -- internals ---------------------------------------------------------------------------------------------------
    def _text_for(self, item: RelevantArticle) -> str:
        article = item.article
        body = article.content if article.content_source == "full_text" and article.content else article.summary
        return (body or "")[: self._tuning.llm_content_chars]

    def _run_batch(self, batch, out: dict[int, ImpactJudgement], report: LlmReport, window: str = "") -> bool:
        """Runs one request. Returns True if a fatal error means no further calls should be made."""
        prompt_items = []
        for n, (_, item, _, text) in enumerate(batch, start=1):
            seed, article = self._seeds[item.seed_id], item.article
            prompt_items.append(PromptItem(
                key=n, seed_id=seed.seed_id, event_type=seed.event_type, hypothesis=seed.hypothesis, guardrail=seed.guardrail,
                guidance=seed.llm_guidance, source=article.source, published=article.published_at.isoformat(timespec="minutes"),
                text_kind=article.content_source if text else "headline_only", headline=article.title, text=text,
                seed_kind=self._seed_kind(seed.seed_id),
            ))
        user = build_user_prompt(prompt_items, window)
        entry = {"call": report.calls + 1, "model": self._client.model,
                 "items": [{"article_id": it.article.article_id, "seed_id": it.seed_id} for _, it, _, _ in batch],
                 "user_prompt": user}
        try:
            response = self._client.complete(SYSTEM_PROMPT, user, max_tokens=800 + 350 * len(batch))
        except LlmError as exc:
            report.calls += 1
            entry["error"] = str(exc)
            report.call_log.append(entry)
            report.errors.append(str(exc))
            for idx, item, _, _ in batch:
                out[idx] = self._unscored(item, f"LLM call failed: {exc}", report)
            return exc.fatal
        report.calls += 1
        entry.update(response_text=response.text, input_tokens=response.input_tokens, output_tokens=response.output_tokens,
                     latency_s=response.latency_s, response_model=response.model)
        parsed, errors = parse_response(response.text, [n for n in range(1, len(batch) + 1)])
        entry["parse_errors"] = {str(k): v for k, v in errors.items()}
        report.call_log.append(entry)
        for n, (idx, item, key, _) in enumerate(batch, start=1):
            if n in parsed:
                if self._cache:
                    self._cache.put(key, self._client.model, parsed[n])
                out[idx] = self._judgement(item, parsed[n], report, cached=False)
            else:
                out[idx] = self._unscored(item, f"LLM answer unusable: {errors.get(n, 'missing')}", report)
        if errors:
            report.errors.append(f"{len(errors)} item(s) in call {report.calls} unusable")
        return False

    def _unscored(self, item: RelevantArticle, reason: str, report: LlmReport) -> ImpactJudgement:
        report.unscored += 1
        return self._build(item, direction=0, confidence=0.0, intensity=0.0, rationale=f"llm: not judged ({reason})",
                           details={"status": "unscored", "reason": reason}, model=self._client.model if self._client else "")

    def _judgement(self, item: RelevantArticle, r: ParsedResult, report: LlmReport, cached: bool) -> ImpactJudgement:
        t, seed = self._tuning, self._seeds[item.seed_id]
        report.judged += 1
        kind, st = self._seed_kind(seed.seed_id), r.event_status
        # Hard gate on what the item IS, before anything else: only a reported event (or, for a market-move seed, a completed move)
        # that really is about this seed can score. Everything else is zeroed with its event_status as the reason.
        gated_in = st == "occurred" or (st == "official_remarks" and kind != "market_move") or (st == "market_move" and kind == "market_move")
        if not gated_in:
            status = st  # scheduled_or_expected, opinion_or_forecast, wrap_or_preview, background, official_remarks or a move on the wrong seed kind
        elif not r.relates or r.relation_confidence < t.llm_min_relation:
            status = "not_about_seed"
        else:
            status = ""  # passes the gate; the checks below decide between scored / priced in / no direction
        if not status:
            if r.is_commentary and st != "official_remarks":
                status = "commentary"
            elif r.already_priced:
                status = "priced_in"
            elif r.direction == 0:
                status = "no_direction"
            elif t.llm_provenance.get(r.provenance, 1.0) <= 0:
                status = r.provenance  # e.g. "unverified": a social post / clickbait claim never scores
            else:
                status = "scored"
        details = {"status": status, "event_status": st, "evidence": r.evidence, "seed_kind": kind,
                   "relates": r.relates, "relation_confidence": r.relation_confidence, "surprise": r.surprise,
                   "is_commentary": r.is_commentary, "already_priced": r.already_priced, "llm_confidence": r.confidence,
                   "llm_intensity": r.intensity, "cached": cached, "provenance": r.provenance,
                   "source_weight": self._quality.weight(item.article.source)}
        if status != "scored":
            report.vetoed += 0 if status == "no_direction" else 1
            return self._build(item, 0, 0.0, 0.0, f"llm: {status.replace('_', ' ')} - {r.rationale}", details, self._client.model)
        confidence = r.confidence * (0.5 + 0.5 * r.relation_confidence) * self._quality.weight(item.article.source)
        confidence = min(1.0, confidence * (1 + 0.05 * min(4, item.cluster_sources - 1)))  # corroboration
        cap = t.llm_intensity_cap.get(seed.priority, t.llm_intensity_cap.get("P2", 1.0))
        if st == "official_remarks":
            cap = min(cap, t.llm_official_remarks_cap)  # one person's remarks never move the score much
        intensity = min(r.intensity, cap) * item.weight_scale * t.llm_provenance.get(r.provenance, 1.0)
        return self._build(item, r.direction, confidence, intensity, f"llm: {r.rationale}", details, self._client.model)

    def _build(self, item: RelevantArticle, direction: int, confidence: float, intensity: float, rationale: str,
               details: dict, model: str) -> ImpactJudgement:
        seed = self._seeds[item.seed_id]
        return ImpactJudgement(
            article_id=item.article.article_id, seed_id=item.seed_id, family=item.family, direction=direction,
            confidence=round(confidence, 4), intensity=round(intensity, 4) if direction else 0.0,
            rationale=f"[{item.article.content_source}] {rationale}", model_name=f"llm:{model}" if model else "llm",
            shock_group=seed.shock_group or seed.seed_id, cluster_size=item.cluster_size, cluster_sources=item.cluster_sources,
            heavyweights=item.heavyweights, weight_scale=item.weight_scale, cluster_id=item.cluster_id, details=details,
        )

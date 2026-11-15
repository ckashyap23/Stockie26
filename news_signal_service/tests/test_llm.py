import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.config import SERVICE_ROOT, Settings
from app.domain import Article, RelevantArticle
from app.llm.cache import LlmCache
from app.llm.client import AnthropicClient, LlmError, LlmResponse, OpenAICompatClient, build_llm_client
from app.llm.prompt import build_user_prompt, parse_response
from app.pipeline.llm_judge import LlmJudge
from app.pipeline.seeds import load_seeds
from app.tuning import build_tuning

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 1, 5, 7, 0, tzinfo=IST)
SEEDS = load_seeds(SERVICE_ROOT / "data" / "event_seeds.csv")
LLM_SEED = next(s for s in SEEDS if s.priority == "P1" and s.llm_guidance)
P2_LLM_SEED = next(s for s in SEEDS if s.priority == "P2")


def _item(i: int, seed_id: str = None, title: str = None, **kw) -> RelevantArticle:
    seed = next(s for s in SEEDS if s.seed_id == (seed_id or LLM_SEED.seed_id))
    article = Article(f"a{i}", "ET", f"http://x/{i}", title or f"HDFC Bank Q2 profit jumps {i}", f"summary {i}", NOW, NOW, "india", "rss")
    return RelevantArticle(article, seed.seed_id, seed.family, seed.geography, 1.0, (), **kw)


def _ok(**over) -> dict:
    base = {"relates": True, "relation_confidence": 0.9, "direction": 1, "intensity": 0.6, "confidence": 0.8,
            "surprise": "beat", "is_commentary": False, "already_priced": False, "rationale": "profit beat"}
    return {**base, **over}


class FakeClient:
    """Answers each call with the next scripted list of per-item results (or raises)."""

    name, model = "fake", "fake-model"

    def __init__(self, script=None, default=None):
        self.script, self.default, self.calls, self.prompts = list(script or []), default or _ok(), 0, []

    def complete(self, system, user, max_tokens=3000):
        self.calls += 1
        self.prompts.append((system, user))
        step = self.script.pop(0) if self.script else None
        if isinstance(step, Exception):
            raise step
        n = user.count("### ITEM ")
        if step is None:
            step = [self.default] * n
        if isinstance(step, str):
            return LlmResponse(step, self.model, 10, 10, 0.1)
        rows = [{"id": i + 1, **r} for i, r in enumerate(step) if r is not None]
        return LlmResponse(json.dumps({"results": rows}), self.model, 10, 10, 0.1)


def _judge(client, tmp_path, tuning=None, **kw):
    return LlmJudge(client, LlmCache(tmp_path / "c.sqlite3"), SEEDS, tuning or build_tuning({}), **kw)


# --- the judge ---------------------------------------------------------------------------------------------------
def test_scored_event_carries_direction_confidence_and_capped_intensity(tmp_path):
    judge = _judge(FakeClient(default=_ok(intensity=0.95)), tmp_path)
    [j], report = judge.judge([_item(1, P2_LLM_SEED.seed_id)])
    assert j.direction == 1 and j.details["status"] == "scored" and j.details["surprise"] == "beat"
    assert j.intensity == pytest.approx(0.7)  # P2 seeds are capped at 0.7 whatever the model says
    assert j.confidence == pytest.approx(0.8 * (0.5 + 0.5 * 0.9), abs=1e-3)
    assert report.calls == 1 and report.judged == 1 and report.unscored == 0


def test_index_weight_scales_intensity(tmp_path):
    judge = _judge(FakeClient(default=_ok(intensity=0.8)), tmp_path)
    big, small = judge.judge([_item(1, weight_scale=1.0), _item(2, weight_scale=0.14)])[0]
    assert small.intensity == pytest.approx(big.intensity * 0.14, rel=0.02)


@pytest.mark.parametrize("answer,status", [
    (_ok(relates=False, relation_confidence=0.9), "not_about_seed"),
    (_ok(relation_confidence=0.3), "not_about_seed"),  # below min_relation_confidence
    (_ok(is_commentary=True), "commentary"),
    (_ok(already_priced=True), "priced_in"),
    (_ok(direction=0), "no_direction"),
])
def test_anything_that_is_not_a_clear_related_event_scores_exactly_zero(tmp_path, answer, status):
    [j], report = _judge(FakeClient(default=answer), tmp_path).judge([_item(1)])
    assert j.direction == 0 and j.confidence == 0.0 and j.intensity == 0.0 and j.details["status"] == status
    assert report.vetoed == (0 if status == "no_direction" else 1)


def test_prompt_contains_the_seed_guidance_headline_and_text(tmp_path):
    client = FakeClient()
    _judge(client, tmp_path).judge([_item(1, title="HDFC Bank Q2 profit beats estimates")])
    system, user = client.prompts[0]
    assert "NIFTY" in system and "JSON" in system
    assert LLM_SEED.event_type in user and LLM_SEED.guardrail in user and "HDFC Bank Q2 profit beats estimates" in user
    assert LLM_SEED.llm_guidance[:40] in user and "summary 1" in user


def test_events_are_batched_and_the_cache_makes_reruns_free_and_identical(tmp_path):
    client = FakeClient()
    judge = _judge(client, tmp_path, build_tuning({"llm": {"batch_size": 4}}))
    items = [_item(i) for i in range(10)]
    first, r1 = judge.judge(items)
    assert client.calls == 3 and r1.calls == 3 and r1.cached == 0  # 4 + 4 + 2
    second, r2 = judge.judge(items)
    assert client.calls == 3 and r2.calls == 0 and r2.cached == 10  # no new requests
    assert [(j.direction, j.confidence, j.intensity) for j in first] == [(j.direction, j.confidence, j.intensity) for j in second]
    assert all(j.details["cached"] for j in second)


def test_without_a_client_llm_events_score_zero_and_say_why(tmp_path):
    judge = _judge(None, tmp_path, not_configured_reason="no API key for provider 'gemini'")
    [j], report = judge.judge([_item(1)])
    assert j.direction == 0 and j.details["status"] == "unscored" and "no API key" in j.rationale
    assert report.unscored == 1 and report.calls == 0 and any("no API key" in e for e in report.errors)


def test_a_fatal_error_stops_further_calls_and_leaves_items_unscored(tmp_path):
    client = FakeClient(script=[LlmError("authentication failed (401)", fatal=True)])
    judge = _judge(client, tmp_path, build_tuning({"llm": {"batch_size": 2}}))
    js, report = judge.judge([_item(i) for i in range(6)])
    assert client.calls == 1 and report.unscored == 6 and all(j.direction == 0 for j in js)
    assert any("authentication" in e for e in report.errors)


def test_a_transient_failure_only_loses_that_batch(tmp_path):
    client = FakeClient(script=[LlmError("rate limited (429) after retries"), None])
    judge = _judge(client, tmp_path, build_tuning({"llm": {"batch_size": 2}}))
    js, report = judge.judge([_item(i) for i in range(4)])
    assert [j.details["status"] for j in js] == ["unscored", "unscored", "scored", "scored"] and report.calls == 2


def test_unparseable_or_partial_answers_are_unscored_not_guessed(tmp_path):
    client = FakeClient(script=[[_ok(), None]])  # item 2 missing from the answer
    js, report = _judge(client, tmp_path).judge([_item(1), _item(2)])
    assert [j.details["status"] for j in js] == ["scored", "unscored"] and "missing" in js[1].rationale
    garbage, _ = _judge(FakeClient(script=["I think this is bullish!"]), tmp_path / "g").judge([_item(9)])
    assert garbage[0].details["status"] == "unscored"


def test_call_budget_per_run_is_enforced(tmp_path):
    client = FakeClient()
    judge = _judge(client, tmp_path, build_tuning({"llm": {"batch_size": 2}}), max_calls=1)
    js, report = judge.judge([_item(i) for i in range(4)])
    assert client.calls == 1 and [j.details["status"] for j in js] == ["scored", "scored", "unscored", "unscored"]
    assert "budget" in js[2].rationale


# --- parsing ---------------------------------------------------------------------------------------------------------
def test_parser_accepts_fenced_json_string_booleans_direction_words_and_clamps():
    text = '```json\n{"results":[{"id":"1","relates":"true","direction":"bearish","intensity":1.7,"confidence":-0.2,"surprise":"MISS"}]}\n```'
    parsed, errors = parse_response(text, [1])
    r = parsed[1]
    assert errors == {} and r.relates is True and r.direction == -1 and r.intensity == 1.0 and r.confidence == 0.0 and r.surprise == "miss"


def test_parser_reports_unusable_items_without_raising():
    parsed, errors = parse_response('{"results":[{"id":1,"relates":true,"direction":"??"}]}', [1, 2])
    assert 1 in errors and errors[2] == "item missing from the response" and parsed == {}
    assert parse_response("not json at all", [1])[1][1].startswith("unparseable")


def test_user_prompt_numbers_every_item():
    from app.llm.prompt import PromptItem

    items = [PromptItem(i, "M01", "RBI", "h", "g", "", "src", "t", "summary", f"head {i}", "body") for i in (1, 2)]
    text = build_user_prompt(items)
    assert "### ITEM 1" in text and "### ITEM 2" in text and "ids 1, 2" in text


# --- HTTP clients ---------------------------------------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body if body is not None else {}, headers or {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


def _openai_body(text="{}"):
    return {"model": "m", "choices": [{"message": {"content": text}}], "usage": {"prompt_tokens": 5, "completion_tokens": 7}}


def test_openai_compatible_client_request_shape_and_parsing():
    seen = {}

    def post(url, headers, json, timeout):
        seen.update(url=url, headers=headers, payload=json)
        return FakeResponse(200, _openai_body('{"ok": 1}'))

    client = OpenAICompatClient("https://host/v1/", "KEY", "some-model", post=post, sleep=lambda s: None)
    out = client.complete("sys", "usr")
    assert seen["url"] == "https://host/v1/chat/completions" and seen["headers"]["Authorization"] == "Bearer KEY"
    assert seen["payload"]["response_format"] == {"type": "json_object"} and seen["payload"]["temperature"] == 0
    assert [m["role"] for m in seen["payload"]["messages"]] == ["system", "user"]
    assert out.text == '{"ok": 1}' and out.input_tokens == 5 and out.output_tokens == 7


def test_json_mode_rejected_by_the_provider_is_retried_without_it():
    payloads = []

    def post(url, headers, json, timeout):
        payloads.append(dict(json))
        return FakeResponse(400, {"error": "response_format unsupported"}) if "response_format" in json else FakeResponse(200, _openai_body("{}"))

    client = OpenAICompatClient("https://h/v1", "K", "m", post=post, sleep=lambda s: None)
    client.complete("s", "u")
    client.complete("s", "u")
    assert "response_format" in payloads[0] and "response_format" not in payloads[1] and "response_format" not in payloads[2]


def test_rate_limit_is_retried_with_backoff_then_succeeds():
    responses = [FakeResponse(429, {"error": "slow down"}, {"Retry-After": "3"}), FakeResponse(200, _openai_body("{}"))]
    sleeps = []
    client = OpenAICompatClient("https://h/v1", "K", "m", post=lambda *a, **k: responses.pop(0), sleep=sleeps.append)
    assert client.complete("s", "u").text == "{}" and 3.0 in sleeps


def test_exhausted_quota_and_bad_key_raise_clear_errors_without_leaking_the_key():
    always_429 = OpenAICompatClient("https://h/v1", "SECRETKEY", "m", post=lambda *a, **k: FakeResponse(429, {"error": "quota"}),
                                    sleep=lambda s: None, max_retries=2)
    with pytest.raises(LlmError) as quota:
        always_429.complete("s", "u")
    assert "rate limited" in str(quota.value) and not quota.value.fatal and "SECRETKEY" not in str(quota.value)
    bad_key = OpenAICompatClient("https://h/v1", "K", "m", post=lambda *a, **k: FakeResponse(401, {"error": "no"}), sleep=lambda s: None)
    with pytest.raises(LlmError) as auth:
        bad_key.complete("s", "u")
    assert auth.value.fatal and "authentication" in str(auth.value)


def test_calls_are_paced_by_min_interval():
    sleeps, now = [], [100.0]
    client = OpenAICompatClient("https://h/v1", "K", "m", post=lambda *a, **k: FakeResponse(200, _openai_body("{}")),
                                min_interval=4.0, sleep=sleeps.append, clock=lambda: now[0])
    client.complete("s", "u")
    client.complete("s", "u")  # clock did not advance: must wait the full interval
    assert sleeps and sleeps[-1] == pytest.approx(4.0)


def test_anthropic_client_request_shape_and_parsing():
    seen = {}

    def post(url, headers, json, timeout):
        seen.update(url=url, headers=headers, payload=json)
        return FakeResponse(200, {"model": "claude-haiku-5-5", "content": [{"type": "text", "text": '{"a":1}'}],
                                  "usage": {"input_tokens": 11, "output_tokens": 3}})

    out = AnthropicClient("KEY", "claude-haiku-5-5", post=post, sleep=lambda s: None).complete("sys", "usr", max_tokens=500)
    assert seen["url"].endswith("/v1/messages") and seen["headers"]["x-api-key"] == "KEY" and "anthropic-version" in seen["headers"]
    assert seen["payload"]["system"] == "sys" and seen["payload"]["messages"] == [{"role": "user", "content": "usr"}]
    assert out.text == '{"a":1}' and out.input_tokens == 11


def test_build_llm_client_presets_and_missing_key_reason():
    none, note = build_llm_client(Settings(llm_provider="gemini", llm_api_key="", gemini_api_key=""))
    assert none is None and "GEMINI_API_KEY" in note
    gem, note = build_llm_client(Settings(llm_provider="gemini", gemini_api_key="k"))
    assert gem.name == "gemini" and "generativelanguage" in gem._url and note.startswith("gemini:")
    groq, _ = build_llm_client(Settings(llm_provider="groq", groq_api_key="k", llm_model="my-model"))
    assert groq.model == "my-model" and "groq.com" in groq._url
    claude, _ = build_llm_client(Settings(llm_provider="anthropic", anthropic_api_key="k"))
    assert claude.name == "anthropic" and claude.model == "claude-haiku-5-5"
    assert build_llm_client(Settings(llm_provider="openai_compat", llm_api_key="k"))[0] is None  # needs base url + model
    assert build_llm_client(Settings(llm_provider="nope", llm_api_key="k"))[0] is None


# --- judge benchmark harness ------------------------------------------------------------------------------------------------------
def test_judge_benchmark_scores_a_model_against_the_golden_set():
    from app.eval import judge_benchmark as jb

    cases = jb.load_cases()
    assert len(cases) >= 30 and {c["seed_id"] for c in cases} <= {s.seed_id for s in SEEDS}

    class Oracle(FakeClient):
        """Answers every item with the expected direction of the matching case (looked up by headline)."""

        def complete(self, system, user, max_tokens=3000):
            self.calls += 1
            rows = []
            for n in range(1, user.count("### ITEM ") + 1):
                block = user.split(f"### ITEM {n}\n")[1].split("### ITEM ")[0]
                case = next(c for c in cases if f"HEADLINE: {c['headline']}" in block)
                d = case["expected_direction"]
                rows.append({"id": n, **_ok(direction=d, relates=d != 0, relation_confidence=0.9 if d else 0.2)})
            return LlmResponse(json.dumps({"results": rows}), "oracle", 1, 1, 0.0)

    perfect = jb.run(Oracle(), cases)
    assert perfect["direction_accuracy"] == 1.0 and perfect["sign_accuracy"] == 1.0 and perfect["false_positive_rate"] == 0.0
    always_bullish = jb.run(FakeClient(default=_ok()), cases)
    assert always_bullish["false_positive_rate"] == 1.0 and always_bullish["direction_accuracy"] < 0.5
    unconfigured = jb.run(None, cases, note="no key")
    assert unconfigured["unscored"] == len(cases)



def test_every_seed_is_judged_by_the_llm_and_the_priority_cap_comes_from_tuning(tmp_path):
    assert all(hasattr(s, "llm_guidance") and not hasattr(s, "judge") for s in SEEDS)  # one judge: the LLM
    capped = _judge(FakeClient(default=_ok(intensity=0.9)), tmp_path, build_tuning({"llm": {"intensity_cap": {"P1": 0.5, "P2": 0.2}}}))
    [j], _ = capped.judge([_item(1)])
    assert j.intensity == pytest.approx(0.5)  # the P1 cap edited in tuning.yaml applies


def test_corroboration_raises_confidence_not_intensity(tmp_path):
    judge = _judge(FakeClient(default=_ok()), tmp_path)
    single, multi = judge.judge([_item(1, cluster_sources=1), _item(2, cluster_sources=4)])[0]
    assert multi.confidence > single.confidence and multi.intensity == single.intensity

"""Verify the LLM setup end to end:   python -m app.llm.check

Sends one real judging prompt (two sample items) through the configured provider and prints what came back: model, latency,
tokens, whether the JSON parsed and whether the answers look sensible. Use it after setting LLM_PROVIDER / the API key / LLM_MODEL.
"""
from __future__ import annotations

import sys

from app.config import Settings
from app.llm.client import LlmError, build_llm_client
from app.llm.prompt import SYSTEM_PROMPT, PromptItem, build_user_prompt, parse_response


def main() -> int:
    settings = Settings()
    client, note = build_llm_client(settings)
    if client is None:
        print(f"LLM not configured: {note}")
        return 2
    items = [
        PromptItem(1, "M01", "RBI repo-rate decision and surprise", "Funding costs, credit and valuation discount rates",
                   "Compare decision with pre-release consensus; rate cut is not automatically bullish", "",
                   "Test wire", "2026-11-09T07:00+05:30", "summary",
                   "RBI cuts repo rate by 25 bps to 5.00%, a bigger cut than the 0 bps economists expected",
                   "The Monetary Policy Committee voted 5-1 to cut the repo rate to 5.00%. A Reuters poll had expected no change."),
        PromptItem(2, "M01", "RBI repo-rate decision and surprise", "Funding costs, credit and valuation discount rates",
                   "Compare decision with pre-release consensus; rate cut is not automatically bullish", "",
                   "Test wire", "2026-11-09T07:05+05:30", "headline_only",
                   "Analysts say RBI may cut rates next year as inflation eases", ""),
    ]
    print(f"provider/model: {note}")
    try:
        response = client.complete(SYSTEM_PROMPT, build_user_prompt(items), max_tokens=1500)
    except LlmError as exc:
        print(f"CALL FAILED: {exc}")
        return 1
    parsed, errors = parse_response(response.text, [1, 2])
    print(f"ok: {response.latency_s}s, tokens in/out = {response.input_tokens}/{response.output_tokens}, model reported: {response.model}")
    for key in (1, 2):
        print(f"  item {key}:", parsed[key] if key in parsed else f"UNUSABLE ({errors.get(key)})")
    print("expected: item 1 relates=true, direction=+1 (surprise cut); item 2 relates=false or is_commentary=true (a forecast, not an event)")
    return 0 if len(parsed) == 2 else 1


if __name__ == "__main__":
    sys.exit(main())

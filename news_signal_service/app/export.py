"""CSV export of a stored signal. UTF-8 with a BOM so Excel opens it correctly.

view=articles (default): one row per seed and news event, with the final score repeated on every row (filter/pivot freely in Excel).
                         A seed with no events still gets one row (blank article columns, seed score 0), so all seeds are visible.
view=seeds             : one row per seed.
"""
from __future__ import annotations

import csv
import io

from app.report import filter_report

FINAL = ["target_date", "window_start", "window_end", "final_score", "final_direction", "final_confidence"]
SEED = ["seed_id", "seed_family", "seed_event_type", "shock_group", "seed_score", "seed_events_judged", "seed_events_scored"]
ARTICLE = ["status", "headline", "source", "url", "published_at", "event_headlines", "event_outlets", "other_outlets", "direction",
           "intensity", "confidence", "signal", "contribution_to_final", "event_weight", "provenance", "surprise", "match_score", "rationale"]
ARTICLE_COLUMNS = FINAL + SEED + ARTICLE + ["run_id"]
SEED_COLUMNS = FINAL + SEED + ["top_headline", "run_id"]


def _final(payload: dict) -> dict:
    return {
        "target_date": payload["target_date"], "window_start": payload["window_start"], "window_end": payload["window_end"],
        "final_score": payload["score"], "final_direction": payload["direction"], "final_confidence": payload["confidence"],
        "run_id": payload.get("run_id", ""),
    }


def signal_to_csv(payload: dict, view: str = "articles", include: str = "relevant") -> bytes:
    if view not in {"articles", "seeds"}:
        raise ValueError("view must be 'articles' or 'seeds'")
    seeds = filter_report(payload.get("seeds") or [], include)
    final = _final(payload)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=ARTICLE_COLUMNS if view == "articles" else SEED_COLUMNS, extrasaction="ignore", lineterminator="\r\n")
    writer.writeheader()
    for seed in seeds:
        base = {
            **final, "seed_id": seed["seed_id"], "seed_family": seed["family"], "seed_event_type": seed["event_type"],
            "shock_group": seed["shock_group"], "seed_score": seed["score"], "seed_events_judged": seed["events_judged"],
            "seed_events_scored": seed["events_scored"],
        }
        if view == "seeds":
            writer.writerow({**base, "top_headline": seed["articles"][0]["headline"] if seed["articles"] else ""})
            continue
        if not seed["articles"]:
            writer.writerow(base)
            continue
        for a in seed["articles"]:
            writer.writerow({
                **base, "status": a["status"], "headline": a["headline"], "source": a["source"], "url": a["url"],
                "published_at": a["published_at"], "event_headlines": a["event_headlines"], "event_outlets": a["event_outlets"],
                "other_outlets": "; ".join(a["other_outlets"]), "direction": a["direction"], "intensity": a["intensity"],
                "confidence": a["confidence"], "signal": a["signal"], "contribution_to_final": a["contribution"],
                "event_weight": a["event_weight"], "provenance": a["provenance"], "surprise": a["surprise"],
                "match_score": a["match_score"], "rationale": a["rationale"],
            })
    return buffer.getvalue().encode("utf-8-sig")

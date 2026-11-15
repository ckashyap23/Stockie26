"""One folder per date, with the Gemini and FinBERT decisions side by side.

    python -m app.backfill.consolidate                   # build var/backfill/by_date/<date>/ and the master CSV

Per date the folder holds the de-duplicated union of every run for that date (newest run wins for the same article+seed):

    by_date/<date>/
        signal.json                          the day's final score (newest run)
        headlines.jsonl, clusters.jsonl      union of all runs, one line per article / event cluster
        Gemini/   judgements.jsonl, matches.jsonl, llm_calls.jsonl
        Finbert/  judgements.jsonl           same article+seed pairs, scored by FinBERT
        news_vs_seed_scores.csv              Date, article, seed, match score, Gemini vs FinBERT side by side
        manifest.json                        which runs were merged

FinBERT here is a tone model, not a NIFTY-impact model:
  match_score = cosine(FinBERT mean-pooled vectors of headline vs the seed's text parts, best part)
  direction   = positive -> +1, negative -> -1, neutral -> 0
  intensity   = |P(positive) - P(negative)|
  confidence  = the winning class probability
The same headline therefore gets the same tone for every seed; only the match score differs per seed.
"""
from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
from pathlib import Path

from app.config import SERVICE_ROOT

FINBERT = "ProsusAI/finbert"
DEFAULT_ROOT = SERVICE_ROOT / "var" / "backfill"


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def _write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _runs_by_date(root: Path) -> dict[str, list[Path]]:
    by_date: dict[str, list[Path]] = {}
    for meta in sorted((root / "runs").glob("*/*/meta.json")):
        by_date.setdefault(meta.parent.parent.name, []).append(meta.parent)

    def recorded(folder: Path) -> str:
        try:
            return json.loads((folder / "meta.json").read_text(encoding="utf-8")).get("recorded_at", "")
        except ValueError:
            return ""

    return {d: sorted(folders, key=recorded) for d, folders in by_date.items()}  # oldest first, so later runs overwrite


class FinbertScorer:
    """Tone (positive/negative/neutral probabilities) and encoder similarity to seed texts, both from ProsusAI/finbert."""

    def __init__(self) -> None:
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        from app.pipeline.embedding import HfMeanPoolEmbedder

        self._torch = torch
        self._tok = AutoTokenizer.from_pretrained(FINBERT)
        self._clf = AutoModelForSequenceClassification.from_pretrained(FINBERT).eval()
        self._labels = {int(i): str(v).lower() for i, v in self._clf.config.id2label.items()}
        self._embedder = HfMeanPoolEmbedder(FINBERT)

    def tone(self, texts: list[str]) -> list[dict]:
        out: list[dict] = []
        for i in range(0, len(texts), 32):
            enc = self._tok(texts[i:i + 32], padding=True, truncation=True, max_length=128, return_tensors="pt")
            with self._torch.no_grad():
                probs = self._torch.softmax(self._clf(**enc).logits, dim=-1)
            for row in probs:
                p = {self._labels[k]: float(row[k]) for k in range(len(row))}
                pos, neg, neu = p.get("positive", 0.0), p.get("negative", 0.0), p.get("neutral", 0.0)
                label = max(p, key=p.get)
                out.append({
                    "label": label, "direction": {"positive": 1, "negative": -1}.get(label, 0),
                    "intensity": round(abs(pos - neg), 4), "confidence": round(p[label], 4),
                    "p_positive": round(pos, 4), "p_negative": round(neg, 4), "p_neutral": round(neu, 4),
                })
        return out

    def match_scores(self, headlines: list[str], seed_parts: list[list[str]]) -> list[float]:
        """cosine(headline, best seed part) for each pair (headlines[i] against seed_parts[i])."""
        flat = [part for parts in seed_parts for part in parts]
        unique = list(dict.fromkeys(headlines + flat))
        vec = dict(zip(unique, self._embedder.embed(unique)))
        scores = []
        for headline, parts in zip(headlines, seed_parts):
            scores.append(round(max(sum(a * b for a, b in zip(vec[headline], vec[p])) for p in parts), 4))
        return scores


def _clean_title(title: str) -> str:
    return " ".join((title or "").split())


COLUMNS = [
    "date", "article", "source", "published_at", "url", "event_headlines", "event_outlets", "seed_id", "seed_event_type", "shock_group",
    "gemini_match_score", "gemini_status", "gemini_direction", "gemini_intensity", "gemini_confidence", "gemini_rationale",
    "finbert_match_score", "finbert_label", "finbert_direction", "finbert_intensity", "finbert_confidence",
    "finbert_p_positive", "finbert_p_negative", "finbert_p_neutral",
]


def build(root: Path, only: set[str] | None = None, scorer: FinbertScorer | None = None) -> dict:
    from app.pipeline.relevance import seed_parts
    from app.pipeline.seeds import load_seeds

    seeds = {s.seed_id: s for s in load_seeds(SERVICE_ROOT / "data" / "event_seeds.csv")}
    parts = {sid: seed_parts(s) for sid, s in seeds.items()}
    scorer = scorer or FinbertScorer()
    out_root, master, summary = root / "by_date", [], {}

    for day, folders in sorted(_runs_by_date(root).items()):
        if only and day not in only:
            continue
        headlines: dict[str, dict] = {}
        clusters: dict[str, dict] = {}
        matches: dict[tuple, dict] = {}
        judgements: dict[tuple, dict] = {}
        calls: list[dict] = []
        for folder in folders:
            for r in _read(folder / "headlines.jsonl"):
                headlines[r["article_id"]] = r
            for r in _read(folder / "clusters.jsonl"):
                clusters[r["representative"]] = r
            for r in _read(folder / "matches.jsonl"):
                matches[(r["article_id"], r["seed_id"])] = r
            for r in _read(folder / "judgements.jsonl"):
                judgements[(r["article_id"], r["seed_id"])] = r
            calls += [{**c, "run": folder.name} for c in _read(folder / "llm_calls.jsonl")]
        pairs = sorted(judgements)
        target = out_root / day
        if target.exists():
            shutil.rmtree(target)
        _write(target / "headlines.jsonl", list(headlines.values()))
        _write(target / "clusters.jsonl", list(clusters.values()))
        _write(target / "Gemini" / "judgements.jsonl", [judgements[k] for k in pairs])
        _write(target / "Gemini" / "matches.jsonl", [matches[k] for k in pairs if k in matches])
        _write(target / "Gemini" / "llm_calls.jsonl", calls)

        titles = [_clean_title(headlines.get(a, {}).get("title", "")) for a, _ in pairs]
        valid = [i for i, (a, s) in enumerate(pairs) if titles[i] and s in parts]
        tone = scorer.tone([titles[i] for i in valid]) if valid else []
        sim = scorer.match_scores([titles[i] for i in valid], [parts[pairs[i][1]] for i in valid]) if valid else []
        finbert = {}
        for i, t, m in zip(valid, tone, sim):
            finbert[pairs[i]] = {"article_id": pairs[i][0], "seed_id": pairs[i][1], "model_name": f"finbert:{FINBERT}",
                                 "match_score": m, **t}
        _write(target / "Finbert" / "judgements.jsonl", list(finbert.values()))

        rows = []
        for key in pairs:
            art, sid = key
            h, j, m, fb = headlines.get(art, {}), judgements[key], matches.get(key, {}), finbert.get(key, {})
            details = j.get("details", {})
            cl = clusters.get(art, {})
            rationale = str(j.get("rationale", "")).replace("[summary] ", "")
            rows.append({
                "date": day, "article": _clean_title(h.get("title", "")), "source": h.get("source", ""),
                "published_at": h.get("published_at", ""), "url": h.get("url", ""),
                "event_headlines": len(cl.get("members", [art])), "event_outlets": len(cl.get("sources", [h.get("source", "")])),
                "seed_id": sid, "seed_event_type": seeds[sid].event_type if sid in seeds else "",
                "shock_group": j.get("shock_group", ""),
                "gemini_match_score": m.get("confidence", ""), "gemini_status": details.get("status", ""),
                "gemini_direction": j.get("direction", ""), "gemini_intensity": j.get("intensity", ""),
                "gemini_confidence": j.get("confidence", ""), "gemini_rationale": rationale,
                "finbert_match_score": fb.get("match_score", ""), "finbert_label": fb.get("label", ""),
                "finbert_direction": fb.get("direction", ""), "finbert_intensity": fb.get("intensity", ""),
                "finbert_confidence": fb.get("confidence", ""), "finbert_p_positive": fb.get("p_positive", ""),
                "finbert_p_negative": fb.get("p_negative", ""), "finbert_p_neutral": fb.get("p_neutral", ""),
            })
        _write_csv(target / "news_vs_seed_scores.csv", rows)
        latest = folders[-1]
        for name in ("signal.json", "aggregation.json"):  # the day's final score and how it was built, from the newest run
            if (latest / name).exists():
                shutil.copy2(latest / name, target / ("Gemini" if name == "aggregation.json" else ".") / name)
        (target / "manifest.json").write_text(json.dumps({
            "date": day, "runs_merged": [f.name for f in folders], "articles": len(headlines), "event_clusters": len(clusters),
            "article_seed_pairs": len(pairs), "llm_calls": len(calls),
        }, indent=2), encoding="utf-8")
        master += rows
        summary[day] = len(pairs)
    if not only:
        _write_csv(root / "news_vs_seed_scores.csv", master)
    return summary


def _write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", default=str(DEFAULT_ROOT))
    parser.add_argument("--dates", nargs="*", help="only these dates (YYYY-MM-DD); skips the master CSV")
    args = parser.parse_args(argv)
    root = Path(args.root)
    summary = build(root, set(args.dates) if args.dates else None)
    print(f"{len(summary)} dates, {sum(summary.values())} article-seed pairs -> {root / 'by_date'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

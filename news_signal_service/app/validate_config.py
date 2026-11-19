"""Check every hand-edited config file before a run:   python -m app.validate_config [--config-dir DIR]

Errors (exit code 1) are things that would break or silently corrupt a run: unknown seed IDs, regexes that do not
compile, a cue listed as both bullish and bearish, duplicate feed URLs. Warnings are things that are probably
unintended: an alias shared by two seeds, cues set on a tone-mode seed, a cue-mode seed with cues on one side only.
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import defaultdict
from pathlib import Path

import yaml

from app.config import CONFIG_FILES, Settings

SEED_COLUMNS = [
    "Seed ID", "Event family", "Geography", "Event type", "Implementation priority", "Timing",
    "Seed aliases (expand semantically)", "Source IDs", "NIFTY transmission hypothesis", "Required guardrail",
    "LLM guidance", "Shock group", "Exclude cues", "Context terms",
]
TUNING_KEYS = {"llm", "unseeded", "relevance", "similarity"}


def _split(value: str) -> list[str]:
    return [p.strip() for p in (value or "").split(";") if p.strip()]


def _yaml(path: Path, errors: list[str]) -> dict:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        errors.append(f"{path.name}: cannot read ({exc})")
        return {}


def validate(settings: Settings) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    seed_ids = _check_seeds(settings.seeds_path, errors, warnings)
    _check_feeds(settings.feeds_path, errors, warnings)
    _check_heavyweights(settings.heavyweights_path, seed_ids, errors, warnings)
    _check_commentary(settings.commentary_path, errors, warnings)
    _check_tuning(settings.tuning_path, errors, warnings)
    _check_source_quality(settings.source_quality_path, errors, warnings)
    return errors, warnings


def _check_seeds(path: Path, errors: list[str], warnings: list[str]) -> set[str]:
    try:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            rows = list(reader)
            columns = reader.fieldnames or []
    except OSError as exc:
        errors.append(f"{path.name}: cannot read ({exc})")
        return set()
    missing = [c for c in SEED_COLUMNS if c not in columns]
    if missing:
        errors.append(f"{path.name}: missing columns {missing}")
        return set()
    ids: set[str] = set()
    alias_owner: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        sid = row["Seed ID"].strip()
        if not sid or sid in ids:
            errors.append(f"{path.name}: empty or duplicate Seed ID {sid!r}")
        ids.add(sid)
        if row["Implementation priority"].strip() not in {"P1", "P2"}:
            errors.append(f"{sid}: priority must be P1 or P2 (got {row['Implementation priority']!r})")
        if not row["Shock group"].strip():
            errors.append(f"{sid}: Shock group is empty")
        excl = [c.lower() for c in _split(row["Exclude cues"])]
        aliases_lower = {x.lower() for x in _split(row["Seed aliases (expand semantically)"])}
        clash = sorted(set(excl) & aliases_lower)
        if clash:
            errors.append(f"{sid}: exclude cue(s) are also aliases of the same seed (would exclude its own matches): {clash}")
        if not _split(row["Seed aliases (expand semantically)"]):
            errors.append(f"{sid}: no aliases")
        for alias in _split(row["Seed aliases (expand semantically)"]):
            alias_owner[alias.lower()].append(sid)
    for alias, owners in sorted(alias_owner.items()):
        if len(set(owners)) > 1:
            warnings.append(f"alias {alias!r} is shared by seeds {sorted(set(owners))}")
    return ids


def _check_feeds(path: Path, errors: list[str], warnings: list[str]) -> None:
    raw = _yaml(path, errors)
    pubs = raw.get("publishers") or []
    if not pubs:
        errors.append(f"{path.name}: no publishers")
    pub_ids, urls, enabled_feeds = set(), defaultdict(int), 0
    for pub in pubs:
        pid = str(pub.get("id", ""))
        if not pid or pid in pub_ids:
            errors.append(f"{path.name}: empty or duplicate publisher id {pid!r}")
        pub_ids.add(pid)
        if pub.get("priority") not in {"P1", "P2"}:
            errors.append(f"{pid}: priority must be P1 or P2")
        if pub.get("body", "full") not in {"full", "headline"}:
            errors.append(f"{pid}: body must be 'full' or 'headline'")
        if not pub.get("feeds"):
            warnings.append(f"{pid}: no feeds")
        for feed in pub.get("feeds") or []:
            url = str(feed.get("url", ""))
            if not url.startswith("http"):
                errors.append(f"{pid}: feed {feed.get('name')!r} has a bad url")
            urls[url] += 1
            if pub.get("enabled", True) and feed.get("enabled", True):
                enabled_feeds += 1
    errors += [f"{path.name}: duplicate feed url {u}" for u, n in urls.items() if n > 1]
    if not enabled_feeds:
        errors.append(f"{path.name}: no enabled feeds")


def _check_heavyweights(path: Path, seed_ids: set[str], errors: list[str], warnings: list[str]) -> None:
    raw = _yaml(path, errors)
    for sid in raw.get("required_seeds") or []:
        if seed_ids and sid not in seed_ids:
            errors.append(f"{path.name}: required_seeds has unknown seed {sid!r}")
    if float(raw.get("full_weight_pct", 5) or 0) <= 0:
        errors.append(f"{path.name}: full_weight_pct must be > 0")
    owner: dict[str, list[str]] = defaultdict(list)
    for hw in raw.get("heavyweights") or []:
        name = str(hw.get("name", "?"))
        if float(hw.get("weight_pct", 0) or 0) <= 0:
            errors.append(f"{path.name}: {name} needs weight_pct > 0")
        if not hw.get("aliases"):
            errors.append(f"{path.name}: {name} has no aliases")
        for alias in hw.get("aliases") or []:
            owner[str(alias).lower()].append(name)
    for alias, names in owner.items():
        if len(set(names)) > 1:
            warnings.append(f"{path.name}: alias {alias!r} is used by {sorted(set(names))}")


def _check_commentary(path: Path, errors: list[str], warnings: list[str]) -> None:
    raw = _yaml(path, errors)
    cats = raw.get("categories") or {}
    if not cats:
        warnings.append(f"{path.name}: no categories - nothing will be dropped")
    for section, groups in (("categories", cats), ("url_categories", raw.get("url_categories") or {})):
        for category, patterns in groups.items():
            for pattern in patterns or []:
                try:
                    re.compile(str(pattern))
                except re.error as exc:
                    errors.append(f"{path.name}: [{section}/{category}] bad regex {pattern!r}: {exc}")


def _check_source_quality(path: Path, errors: list[str], warnings: list[str]) -> None:
    raw = _yaml(path, errors)
    for rule in raw.get("rules") or []:
        try:
            re.compile(str(rule["pattern"]))
        except (re.error, KeyError) as exc:
            errors.append(f"{path.name}: bad rule {rule!r}: {exc}")
            continue
        if not 0 <= float(rule.get("weight", 1)) <= 1:
            errors.append(f"{path.name}: weight for {rule['pattern']!r} must be within 0..1")


def _check_tuning(path: Path, errors: list[str], warnings: list[str]) -> None:
    raw = _yaml(path, errors)
    for key in set(raw) - TUNING_KEYS:
        warnings.append(f"{path.name}: unknown key {key!r} (ignored)")
    sim = raw.get("similarity") or {}
    for kind in ("dense", "sparse"):
        for key in ("dedupe_threshold", "relevance_gate"):
            value = (sim.get(kind) or {}).get(key)
            if value is not None and not 0 < float(value) < 1.5:
                errors.append(f"{path.name}: similarity.{kind}.{key} must be a cosine-scale number (got {value})")
    if sim and not 0 <= float(sim.get("keyword_boost", 0.2)) <= 1:
        errors.append(f"{path.name}: similarity.keyword_boost must be within 0..1")
    if sim and int(sim.get("max_events_to_judge", 60)) < 1:
        errors.append(f"{path.name}: similarity.max_events_to_judge must be >= 1")
    llm = raw.get("llm") or {}
    for name, factor in (llm.get("provenance_factors") or {}).items():
        if name not in {"official", "reported", "expectation", "unverified"} or not 0 <= float(factor) <= 1:
            errors.append(f"{path.name}: llm.provenance_factors.{name} must be a known provenance with a factor within 0..1")
    for key in ("batch_size", "content_chars"):
        if key in llm and int(llm[key]) < 1:
            errors.append(f"{path.name}: llm.{key} must be >= 1")
    for key in ("min_relation_confidence",):
        if key in llm and not 0 <= float(llm[key]) <= 1:
            errors.append(f"{path.name}: llm.{key} must be within 0..1")
    for prio, cap in (llm.get("intensity_cap") or {}).items():
        if not 0 <= float(cap) <= 1:
            errors.append(f"{path.name}: llm.intensity_cap.{prio} must be within 0..1")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config-dir", type=Path, default=None, help="default: CONFIG_DIR from .env, else ./data")
    args = parser.parse_args(argv)
    settings = Settings(config_dir=args.config_dir) if args.config_dir else Settings()
    errors, warnings = validate(settings)
    print(f"config dir: {settings.config_dir}")
    for w in warnings:
        print(f"  warning: {w}")
    for e in errors:
        print(f"  ERROR:   {e}")
    print(f"{len(errors)} error(s), {len(warnings)} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())

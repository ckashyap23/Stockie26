"""Canonical strategy-family registry shared by research and production."""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable

import yaml

CONFIG_PATH = Path(__file__).with_name("strategy_families.yaml")


@dataclass(frozen=True)
class StrategyMeta:
    variant: str
    family: str
    direction: str
    strategy_type: str
    definition: str
    guards: tuple[str, ...] = ()

    @property
    def can_hard_trade(self) -> bool:
        return self.strategy_type == "SIGNAL"

    @property
    def is_diagnostic_only(self) -> bool:
        return self.strategy_type == "RESEARCH"


class StrategyFamilyRegistry:
    def __init__(self, path: str | Path = CONFIG_PATH):
        self.path = Path(path)
        cfg = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        self.config = cfg
        self.families = cfg.get("families", {})
        self.variants = cfg.get("variants", {})
        self.guard_variants = {
            f"{parent}_{guard}": parent
            for parent, item in self.variants.items()
            for guard in item.get("guards", [])
        }

    def get_meta(self, variant: str) -> StrategyMeta:
        try:
            parent = self.guard_variants.get(variant, variant)
            item = self.variants[parent]
        except KeyError as exc:
            raise KeyError(f"Unknown strategy variant: {variant}") from exc
        return StrategyMeta(
            variant=variant,
            family=str(item["family"]),
            direction=str(item["direction"]),
            strategy_type=str(item["strategy_type"]),
            definition=str(item.get("definition", "")),
            guards=tuple(str(guard) for guard in item.get("guards", [])),
        )

    def validate_direction(self, variant: str, emitted: str) -> tuple[bool, str]:
        expected = self.get_meta(variant).direction
        if expected in {"TWO_SIDED", emitted}:
            return True, "OK"
        return False, f"DIRECTION_MISMATCH:{variant}:meta={expected}:emitted={emitted}"

    def validate_complete(self, variants: Iterable[str]) -> None:
        missing = sorted(set(variants) - set(self.variants) - set(self.guard_variants))
        if missing:
            raise ValueError(f"Missing strategy family mappings: {', '.join(missing)}")


@lru_cache(maxsize=1)
def get_strategy_family_registry() -> StrategyFamilyRegistry:
    return StrategyFamilyRegistry()


def collapse_firing_variants_by_family(
    firing_variants: list[dict],
    registry: StrategyFamilyRegistry | None = None,
) -> list[dict]:
    """Return the highest-precision representative per (family, direction)."""
    registry = registry or get_strategy_family_registry()
    grouped: dict[tuple[str, str], dict] = {}
    for item in firing_variants:
        variant = str(item["strategy_variant"])
        meta = registry.get_meta(variant)
        direction = str(item.get("direction") or meta.direction)
        valid, reason = registry.validate_direction(variant, direction)
        if not valid:
            continue
        candidate = {
            **item,
            "strategy_family": meta.family,
            "strategy_type": meta.strategy_type,
        }
        key = (meta.family, direction)
        if key not in grouped or float(candidate.get("historical_precision") or 0) > float(
            grouped[key].get("historical_precision") or 0
        ):
            grouped[key] = candidate
    return list(grouped.values())

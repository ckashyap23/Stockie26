from __future__ import annotations


from importlib import import_module
from pathlib import Path
from types import ModuleType

from .strategies import BUILTIN_UNDERLYING_STRATEGIES, UnderlyingPredictionFunction
from .underlying_prediction_common import DEFAULT_LOOKBACK_DAYS, detect_regime


def _load_custom_underlying_modules() -> list[ModuleType]:
    this_dir = Path(__file__).parent
    modules: list[ModuleType] = []
    for path in sorted(this_dir.glob("underlying_prediction_*.py")):
        if path.name in {"underlying_prediction_common.py"}:
            continue
        mod_name = f"{__package__}.{path.stem}"
        modules.append(import_module(mod_name))
    return modules


def load_underlying_prediction_strategies() -> dict[str, UnderlyingPredictionFunction]:
    registry: dict[str, UnderlyingPredictionFunction] = {
        name: definition.predict
        for name, definition in BUILTIN_UNDERLYING_STRATEGIES.items()
    }
    for module in _load_custom_underlying_modules():
        strategy_name = getattr(module, "STRATEGY_NAME", None)
        predict_fn = getattr(module, "predict", None)
        if isinstance(strategy_name, str) and callable(predict_fn):
            registry[strategy_name] = predict_fn
    return registry


PREDICTION_STRATEGIES = load_underlying_prediction_strategies()

__all__ = [
    "DEFAULT_LOOKBACK_DAYS",
    "PREDICTION_STRATEGIES",
    "UnderlyingPredictionFunction",
    "detect_regime",
    "load_underlying_prediction_strategies",
]

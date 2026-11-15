"""Runtime defaults for legacy commands launched from the original project."""
from pathlib import Path
import os


def configure_legacy_runtime(root: Path) -> None:
    os.environ.setdefault("TECHNICAL_ANALYSIS_ENV_FILE", str(root / ".env"))
    os.environ.setdefault("TECHNICAL_ANALYSIS_OUTPUT_DIR", str(root / "output"))
    from .runtime import load_environment
    load_environment()

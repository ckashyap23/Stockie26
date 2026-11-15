"""Service-owned configuration and writable artifacts, independent of the checkout."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

SERVICE_ROOT = Path(__file__).resolve().parent


def load_environment() -> None:
    # Never search parent directories: a sibling service owns its own credentials.
    path = Path(os.getenv("TECHNICAL_ANALYSIS_ENV_FILE", str(SERVICE_ROOT / ".env")))
    load_dotenv(path, override=False)


def output_dir() -> Path:
    return Path(os.getenv("TECHNICAL_ANALYSIS_OUTPUT_DIR", str(SERVICE_ROOT / "output"))).expanduser().resolve()


def child_environment() -> dict[str, str]:
    """Allow the same commands from an installed wheel or a source checkout."""
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    search_path = [str(SERVICE_ROOT.parent)]
    if env.get("PYTHONPATH"):
        search_path.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(search_path)
    env["TECHNICAL_ANALYSIS_OUTPUT_DIR"] = str(output_dir())
    return env


load_environment()

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# fastapi optionally imports ujson/orjson; some locked-down Windows machines block their DLLs
# ("Application Control policy"). They are optional, so make a blocked import look like "not installed".
for _optional in ("ujson", "orjson"):
    try:
        __import__(_optional)
    except ImportError:
        sys.modules[_optional] = None  # type: ignore[assignment]


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _hermetic_environment(monkeypatch):
    """Tests must not read real keys from the developer's .env or call any network service."""
    for name in ("NEWSAPI_KEY", "LLM_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
                 "OPENROUTER_API_KEY", "LLM_PROVIDER", "LLM_MODEL", "LLM_BASE_URL", "HF_TOKEN"):
        monkeypatch.setenv(name, "")

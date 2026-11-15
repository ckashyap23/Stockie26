from __future__ import annotations

from datetime import time
from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SERVICE_ROOT = Path(__file__).resolve().parents[1]
PACKAGED_CONFIG_DIR = SERVICE_ROOT / "data"
# Every editable config file and its name inside CONFIG_DIR.
CONFIG_FILES = {
    "seeds_path": "event_seeds.csv",
    "feeds_path": "feeds.yaml",
    "heavyweights_path": "nifty_heavyweights.yaml",
    "commentary_path": "commentary_filters.yaml",
    "tuning_path": "tuning.yaml",
    "source_quality_path": "source_quality.yaml",
}


class Settings(BaseSettings):
    """All runtime configuration comes from environment variables (Railway variables)."""

    model_config = SettingsConfigDict(env_file=SERVICE_ROOT / ".env", extra="ignore", populate_by_name=True)

    # --- API security -------------------------------------------------
    # Comma-separated list of accepted keys for the X-API-Key header.
    api_keys: str = ""
    # Hosted scheduler (app/scheduler.py): off by default so a local/dev server never fetches or runs on its own.
    scheduler_enabled: bool = False
    schedule_run_time: time = time(8, 30)  # IST, Monday-Friday
    schedule_ingest_minutes: int = 30
    # Where the daily news score is published (the technical-analysis "NiftyPrediction" table). Empty = do not publish.
    news_score_db_url: str = ""
    supabase_conn_str: str = ""  # fallback for news_score_db_url
    # Local development only. Never set in production.
    allow_unauthenticated: bool = False

    # --- Storage ------------------------------------------------------
    # On Railway, mount a Volume and point this at it (e.g. /data).
    data_dir: Path = SERVICE_ROOT / "var"
    # Editable config (seeds, feeds, heavyweights, commentary filters, tuning). On Railway set CONFIG_DIR=/data/config
    # (on the Volume): the packaged defaults are copied there on first start and your edits then survive redeploys.
    config_dir: Path = PACKAGED_CONFIG_DIR
    # Individual overrides; each defaults to <config_dir>/<standard name>.
    seeds_path: Path | None = None
    feeds_path: Path | None = None
    heavyweights_path: Path | None = None
    commentary_path: Path | None = None
    tuning_path: Path | None = None
    source_quality_path: Path | None = None
    # Written by `python -m app.calibration.fit`; optional.
    calibration_path: Path | None = None  # default: DATA_DIR/calibration.json

    # --- News window (IST) -------------------------------------------
    # Signal for trading day T uses news from previous day's close to the cutoff.
    window_start_time: time = time(15, 30)
    window_end_time: time = time(8, 30)  # news first published after the cutoff is ignored
    # previous_weekday: Monday's window starts Friday 15:30 (covers the weekend). calendar_day: literally the day before (Sunday 15:30).
    window_previous_day: str = "previous_weekday"
    # Feeds only hold their latest 30-60 items; the ingest job fetches this far back and keeps everything in the archive.
    ingest_lookback_hours: int = 36
    # Replay: items first seen up to this long after the cutoff still count as known (the daily run itself starts after 08:30).
    ingest_grace_minutes: int = 60

    # --- Ingestion ----------------------------------------------------
    newsapi_key: str = ""
    http_timeout_seconds: int = 20
    max_articles_per_run: int = 1500  # safety cap; above it the cut is spread fairly over days and sources

    # --- Dedupe / relevance --------------------------------------------
    # tfidf (no dependencies) | sentence_transformers (needs requirements-ml.txt)
    # Benchmarked winner: all-mpnet-base-v2 (python -m app.eval.relevance_benchmark). tfidf is the no-dependency fallback.
    embedder: str = "sentence_transformers"
    embedding_model: str = "sentence-transformers/all-mpnet-base-v2"
    # Optional overrides; by default the thresholds come from data/tuning.yaml (similarity:), per embedder kind.
    dedupe_threshold: float | None = None
    relevance_gate: float | None = None
    unseeded_catch_all: bool = True

    # --- Full text (step 3) -------------------------------------------
    enable_fulltext: bool = True
    fulltext_max_fetches: int = 40
    fulltext_min_chars: int = 400  # below this the page is treated as paywalled/blocked
    fulltext_domain_delay_seconds: float = 1.0

    # --- LLM judge (every seed) -----------------------------------------
    # Free options: gemini (Google AI Studio key) or groq. Others: openai, anthropic, openrouter, openai_compat (+LLM_BASE_URL).
    llm_provider: str = "gemini"
    llm_model: str = ""  # default per provider; the model ids change often, so verify with `python -m app.llm.check`
    llm_base_url: str = ""
    llm_api_key: str = ""  # or the provider-specific variable below
    gemini_api_key: str = ""
    groq_api_key: str = ""
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    openrouter_api_key: str = ""
    llm_timeout_seconds: float = 90
    llm_min_interval_seconds: float = 4.0  # pacing between calls (free tiers allow only a few per minute)
    llm_max_calls_per_run: int = 40

    # --- Relevance / impact ------------------------------------------

    # --- Aggregation --------------------------------------------------
    # score = tanh(raw / aggregate_scale); larger scale = less saturation.
    aggregate_scale: float = 3.0
    neutral_band: float = 0.05
    min_confidence: float = 0.35  # judgements below this contribute zero (confidence is a gate, not a multiplier)
    min_signal: float = 0.05  # |direction x intensity| below this is "insignificant" -> zero
    # One event judged under several seeds counts its strongest signal in full and each further seed at this share (0 = strictly once).
    event_secondary_weight: float = 0.25
    group_decay: float = 0.5  # within one shock group, the k-th strongest channel counts decay**k

    @model_validator(mode="after")
    def _default_config_paths(self) -> "Settings":
        for field, filename in CONFIG_FILES.items():
            if getattr(self, field) is None:
                setattr(self, field, self.config_dir / filename)
        return self

    def calibration_file(self) -> Path:
        return self.calibration_path or self.data_dir / "calibration.json"

    def archive_file(self) -> Path:
        return self.data_dir / "archive.sqlite3"

    def api_key_set(self) -> set[str]:
        return {key.strip() for key in self.api_keys.split(",") if key.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()


def bootstrap_config(settings: Settings) -> list[str]:
    """If CONFIG_DIR is not the packaged data folder, copy any missing default config file into it.

    Existing files are never overwritten, so edits made on the Volume survive redeploys. Returns what was copied.
    """
    import shutil

    copied: list[str] = []
    if settings.config_dir.resolve() == PACKAGED_CONFIG_DIR.resolve():
        return copied
    settings.config_dir.mkdir(parents=True, exist_ok=True)
    for filename in CONFIG_FILES.values():
        target = settings.config_dir / filename
        if not target.exists():
            shutil.copy2(PACKAGED_CONFIG_DIR / filename, target)
            copied.append(filename)
    return copied

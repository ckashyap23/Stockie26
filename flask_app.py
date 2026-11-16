"""Run the Stockie dashboard and technical-analysis API from the repository root."""
from pathlib import Path

from technical_analysis_service.compat import configure_legacy_runtime

# Preserve the current local application's .env and generated report locations.
# Explicit TECHNICAL_ANALYSIS_* environment variables still take precedence.
configure_legacy_runtime(Path(__file__).resolve().parent)

# This launcher binds 127.0.0.1 only, so skip the hosted API-key requirement (the service refuses
# unauthenticated requests unless a key is set or this opt-out is given).
import os

os.environ.setdefault("TECHNICAL_ANALYSIS_ALLOW_UNAUTHENTICATED", "1")

from technical_analysis_service.app import create_app

app = create_app()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)

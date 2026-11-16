FROM python:3.12-slim

# IST for every naive date.today()/datetime.now() in the legacy scripts; the scheduler itself is explicit about IST.
ENV TZ=Asia/Kolkata \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app/service
# broker-login = Playwright + TOTP for the unattended daily Kite token refresh (headless Chromium).
RUN pip install --no-cache-dir "/app/service[broker-login]" \
 && python -m playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/*

# /data is the Railway Volume: Kite token file, output/ (predictions, scheduler status + logs).
ENV HOST=0.0.0.0 PORT=5001 \
    TECHNICAL_ANALYSIS_WORKSPACE=/data \
    TECHNICAL_ANALYSIS_OUTPUT_DIR=/data/output \
    KITE_ACCESS_TOKEN_PATH=/data/kite_access_token.txt \
    KITE_LOGIN_HEADLESS=1
RUN useradd --create-home stockie && mkdir -p /data/output && chown -R stockie:stockie /data
USER stockie
EXPOSE 5001
# Dashboard + API. Set TECHNICAL_ANALYSIS_SCHEDULER=1 to also run the daily job roster in this same service (docs/railway.md).
CMD ["technical-analysis-service", "serve"]

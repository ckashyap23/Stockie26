from __future__ import annotations

import json
import os
import sys
import time
import webbrowser
from datetime import date
from pathlib import Path
from threading import Event, Thread
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv
from flask import Flask, request
from kiteconnect import KiteConnect
from werkzeug.serving import make_server

# Add project root to Python path
project_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(project_root))

from src.common.config import get_settings
from src.data_manager.db.client_factory import get_database_client

load_dotenv(project_root / ".env")

API_KEY = os.getenv("KITE_API_KEY")
API_SECRET = os.getenv("KITE_API_SECRET")
TOKEN_FILE = Path(os.getenv("KITE_ACCESS_TOKEN_PATH", "kite_access_token.txt"))
TOKEN_JSON_FILE = Path(os.getenv("KITE_ACCESS_TOKEN_JSON_PATH", "kite_access_token.json"))

KITE_USER_ID = os.getenv("KITE_USER_ID")
KITE_PASSWORD = os.getenv("KITE_PASSWORD")
KITE_TOTP_SECRET = os.getenv("KITE_TOTP_SECRET")
KITE_TOTP_CODE = os.getenv("KITE_TOTP_CODE")
KITE_PIN = os.getenv("KITE_PIN")
KITE_2FA_MODE = os.getenv("KITE_2FA_MODE", "totp").strip().lower()

AUTO_LOGIN = os.getenv("KITE_AUTO_LOGIN", "1").strip().lower() not in {"0", "false", "no"}
HEADLESS = os.getenv("KITE_LOGIN_HEADLESS", "1").strip().lower() not in {"0", "false", "no"}
if not HEADLESS and os.name != "nt" and not os.getenv("DISPLAY"):
    print("Warning: KITE_LOGIN_HEADLESS=0 but no DISPLAY is available; forcing headless browser mode.")
    HEADLESS = True
REDIRECT_HOST = os.getenv("KITE_REDIRECT_HOST", "127.0.0.1")
REDIRECT_PORT = int(os.getenv("KITE_REDIRECT_PORT", "5000"))
TOKEN_WAIT_SECONDS = int(os.getenv("KITE_TOKEN_WAIT_SECONDS", "180"))
PLAYWRIGHT_STEP_TIMEOUT_MS = int(os.getenv("KITE_PLAYWRIGHT_STEP_TIMEOUT_MS", "60000"))

if not API_KEY or not API_SECRET:
    raise RuntimeError("KITE_API_KEY or KITE_API_SECRET missing in .env")

kite = KiteConnect(api_key=API_KEY)
app = Flask(__name__)

server_instance = None
latest_access_token: str | None = None
token_event = Event()


def save_token(access_token: str) -> None:
    try:
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(access_token, encoding="utf-8")
        print(f"Saved access token to: {TOKEN_FILE.resolve()}")
    except Exception as exc:
        print(f"Warning: failed to save local access token file {TOKEN_FILE}: {exc}")

    try:
        TOKEN_JSON_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_JSON_FILE.write_text(
            json.dumps({"access_token": access_token, "generated_on": str(date.today())}, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        print(f"Warning: failed to save local token metadata file {TOKEN_JSON_FILE}: {exc}")


def load_token() -> str | None:
    if TOKEN_JSON_FILE.exists():
        try:
            data = json.loads(TOKEN_JSON_FILE.read_text(encoding="utf-8"))
            if data.get("generated_on") == str(date.today()) and data.get("access_token"):
                return str(data["access_token"])
        except (OSError, json.JSONDecodeError):
            pass

    if TOKEN_FILE.exists():
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
        return token or None

    return None


def is_token_valid(access_token: str) -> bool:
    try:
        kite.set_access_token(access_token)
        kite.profile()
        return True
    except Exception:
        return False


@app.route("/")
@app.route("/callback")
def callback():
    global latest_access_token

    status = request.args.get("status")
    request_token = request.args.get("request_token")
    error = request.args.get("error")

    if error:
        return f"Login failed: {error}", 400

    if status != "success" or not request_token:
        return "Login failed or request_token missing.", 400

    try:
        session_data = kite.generate_session(request_token=request_token, api_secret=API_SECRET)
        latest_access_token = session_data["access_token"]
        persist_access_token(latest_access_token)
        token_event.set()

        Thread(target=shutdown_server, daemon=True).start()
        return "<h3>Kite access token generated successfully.</h3><p>You can close this tab now.</p>"
    except Exception as exc:
        return f"Token generation failed: {exc}", 500


def shutdown_server() -> None:
    time.sleep(2)
    if server_instance:
        server_instance.shutdown()


class ServerThread(Thread):
    def __init__(self, flask_app: Flask) -> None:
        super().__init__()
        self.server = make_server(REDIRECT_HOST, REDIRECT_PORT, flask_app)
        self.ctx = flask_app.app_context()
        self.ctx.push()

    def run(self) -> None:
        global server_instance
        server_instance = self.server
        self.server.serve_forever()


def persist_access_token(access_token: str) -> None:
    save_token(access_token)

    try:
        settings = get_settings()
        if settings.azure_sql_conn_str or settings.supabase_conn_str:
            db_client = get_database_client(settings)
            db_client.connect()
            db_client.save_kite_access_token(access_token)
            db_client.close()
            print("Saved access token to database.")
        else:
            print("Warning: no database connection string set. Skipping database save.")
    except Exception as exc:
        print(f"Warning: Failed to save token to database: {exc}")
        print("Token was saved to file successfully.")


def extract_request_token(redirect_url: str) -> str | None:
    query = parse_qs(urlparse(redirect_url).query)
    return query.get("request_token", [None])[0]


def exchange_redirect_url(redirect_url: str) -> str:
    request_token = extract_request_token(redirect_url)
    if not request_token:
        raise RuntimeError("Could not find request_token in the provided redirect URL.")

    session_data = kite.generate_session(request_token=request_token, api_secret=API_SECRET)
    access_token = session_data["access_token"]
    persist_access_token(access_token)
    return access_token


def generate_totp() -> str:
    if KITE_TOTP_CODE:
        return KITE_TOTP_CODE.strip()

    if not KITE_TOTP_SECRET:
        raise RuntimeError("Set KITE_TOTP_SECRET or KITE_TOTP_CODE in .env for automated login.")

    try:
        import pyotp
    except ImportError as exc:
        raise RuntimeError("Install pyotp first: pip install pyotp") from exc

    return pyotp.TOTP(KITE_TOTP_SECRET.replace(" ", "")).now()


def automate_kite_login(login_url: str) -> None:
    global latest_access_token

    if not KITE_USER_ID or not KITE_PASSWORD:
        raise RuntimeError("Set KITE_USER_ID and KITE_PASSWORD in .env for automated login.")

    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Install Playwright first: pip install playwright pyotp && python -m playwright install chromium"
        ) from exc

    with sync_playwright() as playwright:
        print(f"Starting automated Kite login with Playwright. Headless={HEADLESS}")
        browser = playwright.chromium.launch(headless=HEADLESS)
        page = browser.new_page()
        page.set_default_timeout(PLAYWRIGHT_STEP_TIMEOUT_MS)

        redirect_base = f"http://{REDIRECT_HOST}:{REDIRECT_PORT}"

        def _handle_token_from_url(url: str, source: str) -> bool:
            global latest_access_token
            if token_event.is_set() or "request_token=" not in url:
                return False
            request_token_val = parse_qs(urlparse(url).query).get("request_token", [None])[0]
            if not request_token_val:
                return False
            try:
                session_data = kite.generate_session(
                    request_token=request_token_val, api_secret=API_SECRET
                )
                latest_access_token = session_data["access_token"]
                persist_access_token(latest_access_token)
                token_event.set()
                print(f"Kite access token obtained via {source}.")
                return True
            except Exception as exc:
                print(f"Token exchange error ({source}): {exc}")
                return False

        # Primary: catch-all route that intercepts the callback redirect before the
        # browser makes a TCP connection (prevents chrome-error://chromewebdata/).
        def _route_handler(route, request):
            if redirect_base in request.url:
                if _handle_token_from_url(request.url, "route intercept"):
                    route.fulfill(
                        status=200,
                        content_type="text/html",
                        body="<h3>Kite access token generated successfully.</h3>",
                    )
                    return
            route.continue_()

        # Fallback: if the route doesn't fire (e.g. Playwright version quirks),
        # requestfailed fires with the original URL (including request_token=) even
        # after the browser shows chrome-error://.
        def _on_request_failed(request):
            _handle_token_from_url(request.url, "requestfailed event")

        page.route("**", _route_handler)
        page.on("requestfailed", _on_request_failed)

        print("Opening Kite login page in browser.")
        page.goto(login_url, wait_until="networkidle", timeout=90_000)

        print("Entering Kite user id and password.")
        page.locator("input[name='user_id'], input[id='userid'], input[type='text'], input[type='email']").first.fill(
            KITE_USER_ID
        )
        page.locator("input[type='password']").first.fill(KITE_PASSWORD)
        page.locator("button[type='submit']").first.click()

        page.wait_for_load_state("domcontentloaded")
        time.sleep(1)

        if KITE_2FA_MODE == "manual":
            print("Manual 2FA mode enabled. Complete the app-code step in the visible browser.")
        else:
            print("Entering Kite 2FA code.")
            two_factor_value = generate_totp() if KITE_TOTP_SECRET or KITE_TOTP_CODE else KITE_PIN
            if not two_factor_value:
                raise RuntimeError("Set KITE_TOTP_SECRET, KITE_TOTP_CODE, or KITE_PIN in .env for 2FA.")

            two_factor_input = page.locator("input[type='text'], input[type='number'], input[type='password']").first
            two_factor_input.wait_for(timeout=20_000)
            two_factor_input.fill(two_factor_value)
            try:
                click_first_available(
                    page,
                    [
                        "button[type='submit']",
                        "button:has-text('Continue')",
                        "button:has-text('Login')",
                        "button:has-text('Submit')",
                        "input[type='submit']",
                    ],
                )
            except PlaywrightTimeoutError:
                # Kite auto-submits when all TOTP digits are entered; the button
                # becomes disabled before click() can complete. This is expected —
                # the form is already submitted, so just proceed to wait for the redirect.
                print("Submit button timed out (likely auto-submitted). Proceeding to wait for redirect.")

        try:
            print("Waiting for Kite callback or browser URL containing request_token.")
            wait_for_token_from_callback_or_url(page)
        except PlaywrightTimeoutError:
            if not token_event.is_set():
                raise RuntimeError(
                    "Timed out waiting for Kite redirect with request_token. "
                    f"Current browser URL: {page.url}. "
                    "Run once with KITE_LOGIN_HEADLESS=0 to see the page state."
                )
        finally:
            browser.close()


def wait_for_token_from_callback_or_url(page) -> None:
    global latest_access_token

    deadline = time.monotonic() + TOKEN_WAIT_SECONDS
    last_reported_url = ""

    while time.monotonic() < deadline:
        if token_event.is_set() and latest_access_token:
            print("Kite callback received access_token.")
            return

        current_url = page.url
        if "request_token=" in current_url:
            print("Kite redirect URL found. Exchanging request_token for access_token.")
            latest_access_token = exchange_redirect_url(current_url)
            token_event.set()
            return

        if current_url != last_reported_url:
            print(f"Still waiting. Current browser URL: {current_url}")
            last_reported_url = current_url

        invalid_app_code = page.get_by_text("Invalid App Code").first
        if invalid_app_code.count() and invalid_app_code.is_visible():
            raise RuntimeError(
                "Kite rejected the 2FA value as an invalid app code. "
                "Stop retrying with the current KITE_TOTP_SECRET because the account can lock. "
                "Use the base32 secret from Zerodha's external TOTP setup, or set "
                "KITE_2FA_MODE=manual and enter the mobile app code yourself."
            )

        time.sleep(1)

    raise RuntimeError(
        "Timed out waiting for Kite callback or request_token URL. "
        f"Current browser URL: {page.url}. "
        "With KITE_LOGIN_HEADLESS=0, check whether Kite is showing an error, captcha, "
        "authorization prompt, or a redirect URL mismatch."
    )


def click_first_available(page, selectors: list[str]) -> None:
    for selector in selectors:
        target = page.locator(selector).first
        if target.count() and target.is_visible() and target.is_enabled():
            target.click(no_wait_after=True)
            return

    # If buttons exist but are all disabled/processing, the form likely auto-submitted
    # (e.g. Kite auto-submits when all 6 TOTP digits are entered). Do nothing and let
    # wait_for_token_from_callback_or_url detect the redirect.
    if not any(page.locator(s).count() for s in selectors):
        page.keyboard.press("Enter")


def start_callback_server() -> ServerThread:
    server = ServerThread(app)
    server.daemon = True
    server.start()
    return server


def get_kite_client() -> KiteConnect:
    existing_token = load_token()
    if existing_token and is_token_valid(existing_token):
        print("Using existing valid Kite access token.")
        kite.set_access_token(existing_token)
        return kite

    print("No valid access token found for today.")
    login_url = kite.login_url()

    if len(sys.argv) > 1:
        access_token = exchange_redirect_url(sys.argv[1].strip())
        kite.set_access_token(access_token)
        return kite

    start_callback_server()

    print(f"Login URL: {login_url}")
    print(f"Listening for Kite redirect on http://{REDIRECT_HOST}:{REDIRECT_PORT}/ and /callback")

    if AUTO_LOGIN:
        automate_kite_login(login_url)
    else:
        print("Opening Kite login page for manual login.")
        webbrowser.open(login_url)

    if not token_event.wait(TOKEN_WAIT_SECONDS):
        raise RuntimeError("Timed out waiting for Kite access token generation.")

    if not latest_access_token:
        raise RuntimeError("Kite access token was not generated.")

    kite.set_access_token(latest_access_token)
    print("Kite access token generated and saved successfully.")
    return kite


def main() -> None:
    kite_client = get_kite_client()
    profile = kite_client.profile()
    print("Logged in as:", profile.get("user_name"))


if __name__ == "__main__":
    main()

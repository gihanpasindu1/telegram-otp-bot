import os
import json
import re
import asyncio
import logging
import time
import threading
from typing import Optional
from datetime import datetime
from pathlib import Path

import httpx
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ----------- ENV -----------
TG_TOKEN = os.getenv("TG_TOKEN")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "6356573938").split(",")]
ALLOWED_DOMAIN = "code-gmail.com"  # <-- hard-coded domain
MAX_REQUESTS_PER_USER = int(os.getenv("MAX_REQUESTS_PER_USER", "10"))
DELAY_SECONDS = int(os.getenv("DELAY_SECONDS", "30"))
STATE_FILE = os.getenv("STATE_FILE", "state.json")
COOLDOWN_SECONDS = 91  # 3 minutes cooldown after success OR "no OTP"

# Self-healing knobs (optional)
RESTART_EVERY_MIN = int(os.getenv("RESTART_EVERY_MIN", "0"))          # 0 = disabled
ERROR_RESTART_THRESHOLD = int(os.getenv("ERROR_RESTART_THRESHOLD", "6"))  # restart if this many network errors in a row
# ---------------------------

OTP_PATTERN = re.compile(r"\b(\d{6})\b")

# Track consecutive network-ish errors for auto-restart
_CONSEC_ERRORS = 0

class StateManager:
    def __init__(self, state_file: str):
        self.state_file = state_file
        self.state = self._load_state()

    def _load_state(self) -> dict:
        Path(self.state_file).parent.mkdir(parents=True, exist_ok=True)
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r") as f:
                    data = json.load(f)
            except Exception as e:
                logger.error(f"Error loading state: {e}")
                data = {}
        else:
            data = {}
        # normalize structure
        data.setdefault("user_requests", {})
        data.setdefault("cached_otps", {})
        data.setdefault("cooldowns", {})  # user_id -> next_allowed_ts
        data.setdefault("blocked_emails", {})  # email -> {timestamp, by}
        return data

    def _save_state(self):
        try:
            with open(self.state_file, "w") as f:
                json.dump(self.state, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving state: {e}")

    # ---- quotas ----
    def get_user_requests(self, user_id: int) -> int:
        return self.state["user_requests"].get(str(user_id), 0)

    def increment_user_requests(self, user_id: int):
        uid = str(user_id)
        self.state["user_requests"][uid] = self.state["user_requests"].get(uid, 0) + 1
        self._save_state()

    def reset_user_limit(self, user_id: int):
        uid = str(user_id)
        if uid in self.state["user_requests"]:
            del self.state["user_requests"][uid]
        self._save_state()

    # ---- otp cache ----
    def cache_otp(self, email: str, otp: str):
        self.state["cached_otps"][email] = {
            "otp": otp,
            "timestamp": datetime.now().isoformat(),
        }
        self._save_state()

    def clear_email(self, email: str):
        if email in self.state["cached_otps"]:
            del self.state["cached_otps"][email]
            self._save_state()
            return True
        return False

    # ---- cooldowns ----
    def set_cooldown(self, user_id: int, seconds: int):
        next_allowed = int(time.time()) + seconds
        self.state["cooldowns"][str(user_id)] = next_allowed
        self._save_state()

    def remaining_cooldown(self, user_id: int) -> int:
        now = int(time.time())
        next_allowed = int(self.state["cooldowns"].get(str(user_id), 0))
        if next_allowed > now:
            return next_allowed - now
        return 0

    # ---- blocked emails ----
    def is_blocked(self, email: str) -> bool:
        return email in self.state.get("blocked_emails", {})

    def block_email(self, email: str, by_user_id: int):
        self.state["blocked_emails"][email] = {
            "timestamp": datetime.now().isoformat(),
            "by": by_user_id,
        }
        self._save_state()

    def unblock_email(self, email: str) -> bool:
        if email in self.state.get("blocked_emails", {}):
            del self.state["blocked_emails"][email]
            self._save_state()
            return True
        return False


state_manager = StateManager(STATE_FILE)

async def fetch_otp_from_generator(email: str) -> Optional[str]:
    """
    Fetch the inbox HTML and extract a 6-digit OTP.
    """
    inbox_url = f"https://generator.email/{email}"

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Cache-Control": "max-age=0",
        "Referer": "https://generator.email/",
    }

    max_retries = 3
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        for attempt in range(max_retries):
            try:
                logger.info(f"Fetching {inbox_url} (attempt {attempt + 1}/{max_retries})")
                response = await client.get(inbox_url, headers=headers)
                response.raise_for_status()

                soup = BeautifulSoup(response.text, "html.parser")

                # Scan common text containers for a 6-digit code
                email_bodies = soup.find_all(["div", "p", "span", "td"])
                for element in email_bodies:
                    text = element.get_text()
                    matches = OTP_PATTERN.findall(text)
                    if matches:
                        otp = matches[0]
                        logger.info(f"Found OTP: {otp}")
                        return otp

                logger.warning(f"No OTP found in inbox for {email}")
                return None

            except httpx.HTTPError as e:
                logger.error(f"Request error (attempt {attempt + 1}): {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(2)
                else:
                    raise

    return None

# ---------------- Self-healing helpers ----------------
def _start_timed_restart_thread():
    """Exit the process after RESTART_EVERY_MIN minutes (if enabled)."""
    if RESTART_EVERY_MIN <= 0:
        return

    def _worker():
        logger.warning(f"Timed restart enabled. Will restart every {RESTART_EVERY_MIN} minutes.")
        while True:
            time.sleep(RESTART_EVERY_MIN * 60)
            logger.warning("Restarting bot now...")
            import sys
            os.execv(sys.executable, ["python"] + sys.argv)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()


def _note_net_success():
    global _CONSEC_ERRORS
    _CONSEC_ERRORS = 0

def _note_net_error_and_maybe_restart():
    """Increment error counter; if threshold reached, exit for Railway to restart."""
    global _CONSEC_ERRORS
    _CONSEC_ERRORS += 1
    if ERROR_RESTART_THRESHOLD > 0 and _CONSEC_ERRORS >= ERROR_RESTART_THRESHOLD:
        logger.error(
            f"Consecutive network errors reached {ERROR_RESTART_THRESHOLD}. "
            "Exiting for Railway to auto-restart."
        )
        os._exit(1)

# ---------------- Commands ----------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    user = update.effective_user
    if not user:
        return

    welcome_text = (
        f"✨ Welcome to Digital Creed OTP Service ✨\n\n"
        f"🔹 Need a quick OTP? Just send:\n"
        f"/otp yourname@{ALLOWED_DOMAIN}\n\n"
        f"⏱️ I’ll wait {DELAY_SECONDS} seconds before checking your inbox to make sure your code arrives.\n\n"
        f"👤 Each user can make up to {MAX_REQUESTS_PER_USER} requests in total.\n\n"
        f"🚫 After every check — whether an OTP is found or not — please wait 3 minutes before making another request.\n\n"
        f"💡 Tip: Double-check your email spelling for faster results!\n\n"
        f"📩 Example:\n"
        f"/otp yourname@{ALLOWED_DOMAIN}"
    )

    await update.message.reply_text(welcome_text)

async def otp_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    user = update.effective_user
    if not user:
        return

    is_admin = user.id in ADMIN_IDS

    # cooldown gate (NON-ADMIN only)
    if not is_admin:
        cd = state_manager.remaining_cooldown(user.id)
        if cd > 0:
            await update.message.reply_text(
                f"⏳ Please wait {cd} seconds before requesting again."
            )
            return

    if not context.args:
        await update.message.reply_text(
            "❌ Please provide an email address.\n"
            f"Example: /otp yourname@{ALLOWED_DOMAIN}"
        )
        return

    email = context.args[0].strip().lower()

    if not email.endswith(f"@{ALLOWED_DOMAIN}"):
        await update.message.reply_text(
            f"❌ Invalid email domain. Only @{ALLOWED_DOMAIN} is supported."
        )
        return

    # blocked email behaves like "no otp right now" (do not reveal it's blocked)
    if state_manager.is_blocked(email):
        if not is_admin:
            state_manager.set_cooldown(user.id, COOLDOWN_SECONDS)

        # --- WRITE LOG: blocked treated as no otp ---
        try:
            with open("otp_log.txt", "a") as lf:
                lf.write(f"[{datetime.now()}] user={user.id} email={email} result=NO_OTP\n")
        except Exception as _:
            pass

        await update.message.reply_text(
            "❌ No OTP found right now. Please try again later."
        )
        return

    # do not count yet; only count on success (NON-ADMIN only quota)
    if not is_admin:
        current_requests = state_manager.get_user_requests(user.id)
        if current_requests >= MAX_REQUESTS_PER_USER:
            await update.message.reply_text(
                f"⛔ You reached your limit ({MAX_REQUESTS_PER_USER})."
            )
            return
        remaining_if_success = MAX_REQUESTS_PER_USER - (current_requests + 1)
    else:
        remaining_if_success = "∞"

    await update.message.reply_text(
        f"⏳ Waiting {DELAY_SECONDS} seconds before checking…\n"
        f"📧 {email}\n"
        f"📊 Remaining (if success): {remaining_if_success}"
    )

    # initial delay (NON-ADMIN only)
    if not is_admin:
        await asyncio.sleep(DELAY_SECONDS)

    # ------- RETRY LOOP with user-visible attempt messages on NETWORK errors -------
    max_rounds = 5
    for round_idx in range(1, max_rounds + 1):
        try:
            otp = await fetch_otp_from_generator(email)

            if otp:
                # Count ONLY on success (NON-ADMIN only)
                if not is_admin:
                    state_manager.increment_user_requests(user.id)
                state_manager.cache_otp(email, otp)

                # cooldown after success (NON-ADMIN only)
                if not is_admin:
                    state_manager.set_cooldown(user.id, COOLDOWN_SECONDS)

                # --- WRITE LOG: success ---
                try:
                    with open("otp_log.txt", "a") as lf:
                        lf.write(f"[{datetime.now()}] user={user.id} email={email} result=OTP:{otp}\n")
                except Exception as _:
                    pass

                _note_net_success()

                if not is_admin:
                    now_used = state_manager.get_user_requests(user.id)
                    remaining = MAX_REQUESTS_PER_USER - now_used
                else:
                    remaining = "∞"

                await update.message.reply_text(
                    f"✅ OTP Found!\n\n"
                    f"🔢 Code: `{otp}`\n"
                    f"📧 {email}\n"
                    f"📊 Remaining: {remaining}",
                    parse_mode="Markdown",
                )
                return
            else:
                # no OTP found; do NOT decrement quota
                # cooldown after no-otp (NON-ADMIN only)
                if not is_admin:
                    state_manager.set_cooldown(user.id, COOLDOWN_SECONDS)

                # --- WRITE LOG: no otp ---
                try:
                    with open("otp_log.txt", "a") as lf:
                        lf.write(f"[{datetime.now()}] user={user.id} email={email} result=NO_OTP\n")
                except Exception as _:
                    pass

                _note_net_success()
                await update.message.reply_text(
                    "❌ No OTP found right now. Please try again later."
                )
                return

        except httpx.HTTPError:
            # --- WRITE LOG: network error (per attempt) ---
            try:
                with open("otp_log.txt", "a") as lf:
                    lf.write(f"[{datetime.now()}] user={user.id} email={email} result=NETWORK_ERROR attempt={round_idx}\n")
            except Exception as _:
                pass

            # Network error: retry up to 5 rounds, 5s between attempts.
            if round_idx < max_rounds:
                await update.message.reply_text(
                    f"⚠️ Network issue (attempt {round_idx}/{max_rounds}). Retrying in 5 seconds..."
                )
                await asyncio.sleep(5)
                continue
            # After 5 network-error rounds, give up politely.
            _note_net_error_and_maybe_restart()
            await update.message.reply_text(
                "⚠️ Network issue. Please wait a few minutes and try again."
            )
            return

        except Exception as e:
            logger.error(f"Unexpected error in otp_command: {e}")

            # --- WRITE LOG: unexpected error ---
            try:
                with open("otp_log.txt", "a") as lf:
                    lf.write(f"[{datetime.now()}] user={user.id} email={email} result=UNEXPECTED_ERROR:{str(e)[:120]}\n")
            except Exception as _:
                pass

            _note_net_error_and_maybe_restart()
            await update.message.reply_text(
                "❌ An unexpected error occurred. Please try again."
            )
            return
    # ------------------------------------------------------------------------------

async def remaining_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    user = update.effective_user
    if not user:
        return

    current_requests = state_manager.get_user_requests(user.id)
    remaining = MAX_REQUESTS_PER_USER - current_requests
    cd = state_manager.remaining_cooldown(user.id)

    if cd > 0:
        text = (
            f"📊 Used: {current_requests}/{MAX_REQUESTS_PER_USER}\n"
            f"⏱️ Cooldown: {cd} seconds left"
        )
    else:
        text = (
            f"📊 Used: {current_requests}/{MAX_REQUESTS_PER_USER}\n"
            f"✅ No cooldown active"
        )
    await update.message.reply_text(text)

async def resetlimit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    user = update.effective_user
    if not user:
        return

    if user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Admin only.")
        return

    if not context.args:
        await update.message.reply_text("❌ Usage: /resetlimit <user_id>")
        return

    try:
        target_user_id = int(context.args[0])
        state_manager.reset_user_limit(target_user_id)
        await update.message.reply_text(f"✅ Reset done for user {target_user_id}")
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID (must be a number).")

async def clearemail_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    user = update.effective_user
    if not user:
        return

    if user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Admin only.")
        return

    if not context.args:
        await update.message.reply_text(
            "❌ Usage: /clearemail <email>\n"
            f"Example: /clearemail user@{ALLOWED_DOMAIN}"
        )
        return

    email = context.args[0].lower()
    if state_manager.clear_email(email):
        await update.message.reply_text(f"✅ Cached OTP cleared for {email}")
    else:
        await update.message.reply_text(f"ℹ️ No cached OTP found for {email}")

# ---------------- Admin Block/Unblock ----------------
async def block_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    user = update.effective_user
    if not user:
        return

    if user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Admin only.")
        return

    if not context.args:
        await update.message.reply_text(
            "❌ Usage: /block <email>\n"
            f"Example: /block user@{ALLOWED_DOMAIN}"
        )
        return

    email = context.args[0].strip().lower()
    if not email.endswith(f"@{ALLOWED_DOMAIN}"):
        await update.message.reply_text(
            f"❌ Invalid email domain. Only @{ALLOWED_DOMAIN} is supported."
        )
        return

    state_manager.block_email(email, user.id)

    try:
        with open("otp_log.txt", "a") as lf:
            lf.write(f"[{datetime.now()}] user={user.id} email={email} action=BLOCK\n")
    except Exception as _:
        pass

    await update.message.reply_text("✅ Done.")

async def unblock_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    user = update.effective_user
    if not user:
        return

    if user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Admin only.")
        return

    if not context.args:
        await update.message.reply_text(
            "❌ Usage: /unblock <email>\n"
            f"Example: /unblock user@{ALLOWED_DOMAIN}"
        )
        return

    email = context.args[0].strip().lower()
    if not email.endswith(f"@{ALLOWED_DOMAIN}"):
        await update.message.reply_text(
            f"❌ Invalid email domain. Only @{ALLOWED_DOMAIN} is supported."
        )
        return

    ok = state_manager.unblock_email(email)

    try:
        with open("otp_log.txt", "a") as lf:
            lf.write(f"[{datetime.now()}] user={user.id} email={email} action=UNBLOCK ok={ok}\n")
    except Exception as _:
        pass

    await update.message.reply_text("✅ Done." if ok else "ℹ️ Not found.")

# ---------------- Admin Log Viewer (/log) ----------------
async def showlog_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return

    if user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ This command is restricted to admins only.")
        return

    log_file = "otp_log.txt"
    try:
        with open(log_file, "r") as f:
            lines = f.readlines()

        if not lines:
            await update.message.reply_text("📭 Log file is empty.")
            return

        full_log = "".join(lines)

        # Telegram message length safety
        if len(full_log) > 4000:
            chunks = [full_log[i:i+4000] for i in range(0, len(full_log), 4000)]
            for i, chunk in enumerate(chunks, start=1):
                await update.message.reply_text(f"📜 Log Part {i}:\n\n{chunk}")
        else:
            await update.message.reply_text(f"🧾 Full Log:\n\n{full_log}")

    except FileNotFoundError:
        await update.message.reply_text("⚠️ No log file found yet.")
    except Exception as e:
        await update.message.reply_text(f"❌ Error reading log: {e}")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error {context.error}")

def main():
    if not TG_TOKEN:
        logger.error("TG_TOKEN environment variable is not set!")
        print("❌ ERROR: TG_TOKEN environment variable is required.")
        return

    logger.info("Starting OTP bot...")
    # ... your logs ...

    _start_timed_restart_thread()

    application = (
        Application
        .builder()
        .token(TG_TOKEN)
        .concurrent_updates(True)   # enable parallel handling
        .build()
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("otp", otp_command))
    application.add_handler(CommandHandler("remaining", remaining_command))
    application.add_handler(CommandHandler("resetlimit", resetlimit_command))
    application.add_handler(CommandHandler("clearemail", clearemail_command))
    application.add_handler(CommandHandler("block", block_command))
    application.add_handler(CommandHandler("unblock", unblock_command))
    application.add_handler(CommandHandler("log", showlog_command))
    application.add_error_handler(error_handler)

    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

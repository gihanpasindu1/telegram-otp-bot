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
ALLOWED_DOMAIN = os.getenv("ALLOWED_DOMAIN", "yotomail.com")
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
        data.setdefault("user_requests", {})
        data.setdefault("cached_otps", {})
        data.setdefault("cooldowns", {})
        return data

    def _save_state(self):
        try:
            with open(self.state_file, "w") as f:
                json.dump(self.state, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving state: {e}")

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


state_manager = StateManager(STATE_FILE)

async def fetch_otp_from_generator(email: str) -> Optional[str]:
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

def _start_timed_restart_thread():
    if RESTART_EVERY_MIN <= 0:
        return
    def _worker():
        import sys
        logger.warning(f"Timed restart enabled. Will restart every {RESTART_EVERY_MIN} minutes.")
        while True:
            time.sleep(RESTART_EVERY_MIN * 60)
            logger.warning("Restarting bot now...")
            os.execv(sys.executable, ["python"] + sys.argv)
    import sys
    t = threading.Thread(target=_worker, daemon=True)
    t.start()

def _note_net_success():
    global _CONSEC_ERRORS
    _CONSEC_ERRORS = 0

def _note_net_error_and_maybe_restart():
    global _CONSEC_ERRORS
    _CONSEC_ERRORS += 1
    if ERROR_RESTART_THRESHOLD > 0 and _CONSEC_ERRORS >= ERROR_RESTART_THRESHOLD:
        logger.error(
            f"Consecutive network errors reached {ERROR_RESTART_THRESHOLD}. Exiting for Railway to auto-restart."
        )
        os._exit(1)

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
        f"⏱️ I’ll wait {DELAY_SECONDS} seconds before checking your inbox.\n\n"
        f"👤 Each user can make up to {MAX_REQUESTS_PER_USER} requests.\n\n"
        f"🚫 After every check — found or not — please wait 3 minutes before trying again.\n\n"
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
    cd = state_manager.remaining_cooldown(user.id)
    if cd > 0:
        await update.message.reply_text(f"⏳ Please wait {cd} seconds before requesting again.")
        return
    if not context.args:
        await update.message.reply_text(f"❌ Usage: /otp yourname@{ALLOWED_DOMAIN}")
        return
    email = context.args[0].strip().lower()
    if not email.endswith(f"@{ALLOWED_DOMAIN}"):
        await update.message.reply_text(f"❌ Only @{ALLOWED_DOMAIN} supported.")
        return
    current_requests = state_manager.get_user_requests(user.id)
    if current_requests >= MAX_REQUESTS_PER_USER:
        await update.message.reply_text(f"⛔ Limit {MAX_REQUESTS_PER_USER} reached.")
        return
    remaining_if_success = MAX_REQUESTS_PER_USER - (current_requests + 1)
    await update.message.reply_text(
        f"⏳ Waiting {DELAY_SECONDS} seconds...\n📧 {email}\n📊 Remaining (if success): {remaining_if_success}"
    )
    await asyncio.sleep(DELAY_SECONDS)
    try:
        otp = await fetch_otp_from_generator(email)
        with open("otp_log.txt", "a") as f:
            f.write(f"[{datetime.now()}] User:{user.id} Email:{email} OTP:{otp or 'None'}\n")

        if otp:
            state_manager.increment_user_requests(user.id)
            state_manager.cache_otp(email, otp)
            state_manager.set_cooldown(user.id, COOLDOWN_SECONDS)
            _note_net_success()
            now_used = state_manager.get_user_requests(user.id)
            remaining = MAX_REQUESTS_PER_USER - now_used
            await update.message.reply_text(
                f"✅ OTP Found!\n\n🔢 Code: `{otp}`\n📧 {email}\n📊 Remaining: {remaining}",
                parse_mode="Markdown",
            )
        else:
            state_manager.set_cooldown(user.id, COOLDOWN_SECONDS)
            _note_net_success()
            await update.message.reply_text("❌ No OTP found right now.")
    except httpx.HTTPError:
        _note_net_error_and_maybe_restart()
        await update.message.reply_text("⚠️ Network error while checking your mailbox. Please try again.")
    except Exception as e:
        logger.error(f"Unexpected error in otp_command: {e}")
        _note_net_error_and_maybe_restart()
        await update.message.reply_text("❌ Unexpected error. Please try again.")

# ---------- ADMIN /log COMMAND ----------
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
        if len(full_log) > 4000:  # Telegram message limit ~4096 chars
            # Send in chunks if it's long
            chunks = [full_log[i:i+4000] for i in range(0, len(full_log), 4000)]
            for i, chunk in enumerate(chunks, start=1):
                await update.message.reply_text(f"📜 Log Part {i}:\n\n{chunk}")
        else:
            await update.message.reply_text(f"🧾 Full Log:\n\n{full_log}")
    except FileNotFoundError:
        await update.message.reply_text("⚠️ No log file found yet.")
    except Exception as e:
        await update.message.reply_text(f"❌ Error reading log: {e}")

def main():
    if not TG_TOKEN:
        print("❌ TG_TOKEN missing!")
        return
    _start_timed_restart_thread()
    application = Application.builder().token(TG_TOKEN).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("otp", otp_command))
    application.add_handler(CommandHandler("log", showlog_command))
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

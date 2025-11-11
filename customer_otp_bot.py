import os
import json
import re
import asyncio
import logging
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
# ---------------------------

OTP_PATTERN = re.compile(r"\b(\d{6})\b")

class StateManager:
    def __init__(self, state_file: str):
        self.state_file = state_file
        self.state = self._load_state()

    def _load_state(self) -> dict:
        Path(self.state_file).parent.mkdir(parents=True, exist_ok=True)
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Error loading state: {e}")
                return {"user_requests": {}, "cached_otps": {}}
        return {"user_requests": {}, "cached_otps": {}}

    def _save_state(self):
        try:
            with open(self.state_file, "w") as f:
                json.dump(self.state, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving state: {e}")

    def get_user_requests(self, user_id: int) -> int:
        return self.state["user_requests"].get(str(user_id), 0)

    def increment_user_requests(self, user_id: int):
        user_id_str = str(user_id)
        self.state["user_requests"][user_id_str] = (
            self.state["user_requests"].get(user_id_str, 0) + 1
        )
        self._save_state()

    def reset_user_limit(self, user_id: int):
        user_id_str = str(user_id)
        if user_id_str in self.state["user_requests"]:
            del self.state["user_requests"][user_id_str]
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

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    user = update.effective_user
    if not user:
        return

    welcome_text = (
        f"👋 Welcome to OTP Fetcher Bot!\n\n"
        f"📧 I can fetch OTP codes from @{ALLOWED_DOMAIN} email addresses.\n\n"
        f"📝 Commands:\n"
        f"• /otp <email> — Fetch OTP\n"
        f"• /remaining — Check remaining requests\n\n"
        f"⚠️ Limit: {MAX_REQUESTS_PER_USER} per user\n"
        f"⏱️ Delay: {DELAY_SECONDS}s\n\n"
        f"Example: /otp yourname@{ALLOWED_DOMAIN}"
    )

    if user.id in ADMIN_IDS:
        welcome_text += (
            f"\n\n👑 Admin:\n"
            f"• /resetlimit <user_id>\n"
            f"• /clearemail <email>"
        )

    await update.message.reply_text(welcome_text)

async def otp_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    user = update.effective_user
    if not user:
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
            f"❌ Invalid email domain.\nOnly @{ALLOWED_DOMAIN} is supported."
        )
        return

    # --- CHANGED LOGIC: don't count yet; only count after success ---
    current_requests = state_manager.get_user_requests(user.id)
    if current_requests >= MAX_REQUESTS_PER_USER:
        await update.message.reply_text(
            f"⛔ You reached your limit ({MAX_REQUESTS_PER_USER})."
        )
        return
    remaining_if_success = MAX_REQUESTS_PER_USER - (current_requests + 1)
    # ---------------------------------------------------------------

    await update.message.reply_text(
        f"⏳ Waiting {DELAY_SECONDS} seconds for OTP to arrive...\n"
        f"📧 Email: {email}\n"
        f"📊 Remaining (if success): {remaining_if_success}"
    )

    await asyncio.sleep(DELAY_SECONDS)

    try:
        otp = await fetch_otp_from_generator(email)

        if otp:
            # Count ONLY on success:
            state_manager.increment_user_requests(user.id)
            state_manager.cache_otp(email, otp)

            now_used = state_manager.get_user_requests(user.id)
            remaining = MAX_REQUESTS_PER_USER - now_used

            await update.message.reply_text(
                f"✅ OTP Found!\n\n"
                f"🔢 Code: `{otp}`\n"
                f"📧 Email: {email}\n"
                f"📊 Remaining: {remaining}",
                parse_mode="Markdown",
            )
        else:
            # No increment here
            await update.message.reply_text(
                f"❌ No OTP found.\n"
                f"📧 Email: {email}\n"
                f"📊 Used: {current_requests}/{MAX_REQUESTS_PER_USER}"
            )

    except httpx.HTTPError as e:
        # No increment here
        await update.message.reply_text(
            f"⚠️ Network error contacting generator.email.\n"
            f"Error: {str(e)[:100]}\n\n"
            f"📊 Used: {current_requests}/{MAX_REQUESTS_PER_USER}"
        )
    except Exception as e:
        logger.error(f"Unexpected error in otp_command: {e}")
        # No increment here
        await update.message.reply_text(
            f"❌ Unexpected error. Try again later.\n"
            f"📊 Used: {current_requests}/{MAX_REQUESTS_PER_USER}"
        )

async def remaining_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    user = update.effective_user
    if not user:
        return

    current_requests = state_manager.get_user_requests(user.id)
    remaining = MAX_REQUESTS_PER_USER - current_requests

    await update.message.reply_text(
        f"📊 Request status:\n"
        f"Used: {current_requests}/{MAX_REQUESTS_PER_USER}\n"
        f"Remaining: {remaining}"
    )

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

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error {context.error}")

def main():
    if not TG_TOKEN:
        logger.error("TG_TOKEN environment variable is not set!")
        print("❌ ERROR: TG_TOKEN environment variable is required.")
        return

    logger.info("Starting Telegram OTP Fetcher Bot...")
    logger.info(f"Admin IDs: {ADMIN_IDS}")
    logger.info(f"Allowed domain: {ALLOWED_DOMAIN}")
    logger.info(f"Max requests per user: {MAX_REQUESTS_PER_USER}")
    logger.info(f"Delay: {DELAY_SECONDS} seconds")
    logger.info(f"State file: {STATE_FILE}")

    application = Application.builder().token(TG_TOKEN).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("otp", otp_command))
    application.add_handler(CommandHandler("remaining", remaining_command))
    application.add_handler(CommandHandler("resetlimit", resetlimit_command))
    application.add_handler(CommandHandler("clearemail", clearemail_command))

    application.add_error_handler(error_handler)

    logger.info("Bot is running. Ctrl+C to stop.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

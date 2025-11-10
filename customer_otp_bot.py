import os, re, json, requests, asyncio, time
from pathlib import Path
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ================== ENVIRONMENT CONFIG ==================
TG_TOKEN = os.getenv("TG_TOKEN", "")
ALLOWED_DOMAIN = os.getenv("ALLOWED_DOMAIN", "yotomail.com").lower()
MAX_REQUESTS_PER_USER = int(os.getenv("MAX_REQUESTS_PER_USER", "10"))
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "6356573938").split(",") if x.strip()}
DELAY_SECONDS = int(os.getenv("DELAY_SECONDS", "30"))
STATE_FILE = os.getenv("STATE_FILE", "state.json")
# ========================================================

GENEMAIL_BASE = "https://generator.email/"
OTP_REGEX = r"\b(\d{6})\b"
TIMEOUT = 20

# ---------- Persistence (Save limits + last OTPs) ----------
def load_state():
    p = Path(STATE_FILE)
    if not p.exists():
        return {"usage": {}, "last_codes": {}}
    try:
        data = json.loads(p.read_text())
        data.setdefault("usage", {})
        data.setdefault("last_codes", {})
        return data
    except Exception:
        return {"usage": {}, "last_codes": {}}

def save_state(state):
    try:
        Path(STATE_FILE).write_text(json.dumps(state))
    except Exception:
        pass

STATE = load_state()

def get_user_count(uid): return int(STATE["usage"].get(str(uid), {}).get("count", 0))
def inc_user_count(uid):
    rec = STATE["usage"].get(str(uid), {"count": 0})
    rec["count"] = int(rec.get("count", 0)) + 1
    STATE["usage"][str(uid)] = rec; save_state(STATE)
def reset_user_count(uid): STATE["usage"][str(uid)] = {"count": 0}; save_state(STATE)
def get_last_code(email): return (STATE["last_codes"].get(email, {}) or {}).get("code")
def set_last_code(email, code):
    STATE["last_codes"][email] = {"code": code, "ts": int(time.time())}; save_state(STATE)
def clear_last_code(email): STATE["last_codes"].pop(email, None); save_state(STATE)

# ---------- Email + OTP Helpers ----------
def is_allowed_email(email: str) -> bool:
    if "@" not in email:
        return False
    local, domain = email.strip().rsplit("@", 1)
    return bool(local) and domain.lower() == ALLOWED_DOMAIN

# Shared requests session with retries and fake browser headers
session = requests.Session()
retry_strategy = Retry(total=3, backoff_factor=1, status_forcelist=[403, 429, 500, 502, 503, 504])
adapter = HTTPAdapter(max_retries=retry_strategy)
session.mount("https://", adapter)
session.mount("http://", adapter)

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/127.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://generator.email/",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

def fetch_inbox_html(email: str) -> str:
    """Fetch the generator.email inbox page."""
    url = f"{GENEMAIL_BASE.rstrip('/')}/{email.strip()}"
    try:
        r = session.get(url, headers=BROWSER_HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        return r.text
    except requests.exceptions.RequestException as e:
        raise Exception(f"Could not fetch inbox: {e}")

def extract_latest_otp(html: str):
    """Parse the inbox table and find the newest 6-digit OTP."""
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.select("table tr")
    for row in rows:
        tds = row.find_all("td")
        text = tds[1].get_text(" ") if len(tds) >= 2 else row.get_text(" ")
        m = re.search(OTP_REGEX, text)
        if m:
            return m.group(1)
    return None

# ---------- Telegram Commands ----------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"👋 Hey {update.effective_user.first_name}!\n"
        f"Send `/otp yourname@{ALLOWED_DOMAIN}`\n"
        f"I’ll wait {DELAY_SECONDS}s then check your inbox.\n"
        f"Limit: {MAX_REQUESTS_PER_USER} requests per user.",
        parse_mode="Markdown",
    )

async def otp(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not ctx.args:
        return await update.message.reply_text(
            f"Usage: `/otp yourname@{ALLOWED_DOMAIN}`", parse_mode="Markdown"
        )

    used = get_user_count(uid)
    if used >= MAX_REQUESTS_PER_USER:
        return await update.message.reply_text(f"❌ Limit reached ({used}/{MAX_REQUESTS_PER_USER}).")

    email = ctx.args[0].strip()
    if not is_allowed_email(email):
        return await update.message.reply_text(f"❌ Only @{ALLOWED_DOMAIN} emails are allowed.")

    await update.message.reply_text(f"⏳ Waiting {DELAY_SECONDS}s before checking your inbox…")
    await asyncio.sleep(DELAY_SECONDS)

    try:
        html = fetch_inbox_html(email)
        code = extract_latest_otp(html)
        if not code:
            return await update.message.reply_text("⚠️ No OTP found yet. Try again shortly.")
        if get_last_code(email) == code:
            await update.message.reply_text("⚠️ Same OTP as last time (may be old).")
        inc_user_count(uid)
        set_last_code(email, code)
        remaining = MAX_REQUESTS_PER_USER - get_user_count(uid)
        await update.message.reply_text(
            f"✅ OTP for `{email}` → *`{code}`*\nRemaining: {remaining}", parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error: {e}")

async def remaining(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    used = get_user_count(uid)
    await update.message.reply_text(f"📊 Used {used}/{MAX_REQUESTS_PER_USER}. Remaining {MAX_REQUESTS_PER_USER - used}.")

async def resetlimit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return await update.message.reply_text("❌ Admin only.")
    if not ctx.args:
        return await update.message.reply_text("Usage: /resetlimit <user_id>")
    reset_user_count(ctx.args[0])
    await update.message.reply_text(f"✅ Reset for {ctx.args[0]}.")

async def clearemail(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return await update.message.reply_text("❌ Admin only.")
    if not ctx.args:
        return await update.message.reply_text("Usage: /clearemail <email>")
    clear_last_code(ctx.args[0])
    await update.message.reply_text(f"✅ Cleared OTP cache for {ctx.args[0]}.")

def main():
    if not TG_TOKEN:
        raise SystemExit("⚠️ TG_TOKEN env variable missing.")
    app = ApplicationBuilder().token(TG_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("otp", otp))
    app.add_handler(CommandHandler("remaining", remaining))
    app.add_handler(CommandHandler("resetlimit", resetlimit))
    app.add_handler(CommandHandler("clearemail", clearemail))
    print("✅ Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()

import os, re, json, requests, asyncio, time
from pathlib import Path
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

TG_TOKEN = os.getenv("TG_TOKEN", "")
ALLOWED_DOMAIN = os.getenv("ALLOWED_DOMAIN", "yotomail.com").lower()
MAX_REQUESTS_PER_USER = int(os.getenv("MAX_REQUESTS_PER_USER", "10"))
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "6356573938").split(",") if x.strip()}
DELAY_SECONDS = int(os.getenv("DELAY_SECONDS", "30"))
STATE_FILE = os.getenv("STATE_FILE", "state.json")

GENEMAIL_BASE = "https://generator.email/"
OTP_REGEX = r"\b(\d{6})\b"
TIMEOUT = 12

# ---------- persistence ----------
def load_state():
    p = Path(STATE_FILE)
    if not p.exists():
        return {"usage": {}, "last_codes": {}}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {"usage": {}, "last_codes": {}}

def save_state(state): Path(STATE_FILE).write_text(json.dumps(state))
STATE = load_state()

def get_user_count(uid): return STATE["usage"].get(str(uid), {}).get("count", 0)
def inc_user_count(uid):
    rec = STATE["usage"].get(str(uid), {"count": 0})
    rec["count"] = rec.get("count", 0) + 1
    STATE["usage"][str(uid)] = rec; save_state(STATE)
def reset_user_count(uid): STATE["usage"][str(uid)] = {"count": 0}; save_state(STATE)
def get_last_code(email): return STATE["last_codes"].get(email, {}).get("code")
def set_last_code(email, code):
    STATE["last_codes"][email] = {"code": code, "ts": int(time.time())}; save_state(STATE)
def clear_last_code(email): STATE["last_codes"].pop(email, None); save_state(STATE)

def is_allowed_email(email):
    if "@" not in email: return False
    local, domain = email.strip().rsplit("@", 1)
    return bool(local) and domain.lower() == ALLOWED_DOMAIN

def fetch_inbox_html(email):
    r = requests.get(f"{GENEMAIL_BASE.rstrip('/')}/{email}", timeout=TIMEOUT)
    r.raise_for_status(); return r.text

def extract_latest_otp(html):
    soup = BeautifulSoup(html, "html.parser")
    for row in soup.select("table tr"):
        cells = row.find_all("td")
        text = cells[1].get_text(" ") if len(cells) >= 2 else row.get_text(" ")
        m = re.search(OTP_REGEX, text)
        if m: return m.group(1)
    return None

# ---------- commands ----------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Send `/otp yourname@{ALLOWED_DOMAIN}`\n"
        f"I'll wait {DELAY_SECONDS}s then check your inbox.\n"
        f"Limit: {MAX_REQUESTS_PER_USER} per user.",
        parse_mode="Markdown",
    )

async def otp(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user, uid = update.effective_user, update.effective_user.id
    if not ctx.args:
        return await update.message.reply_text(f"Usage: `/otp yourname@{ALLOWED_DOMAIN}`", parse_mode="Markdown")
    used = get_user_count(uid)
    if used >= MAX_REQUESTS_PER_USER:
        return await update.message.reply_text(f"Limit reached ({used}/{MAX_REQUESTS_PER_USER}).")
    email = ctx.args[0].strip()
    if not is_allowed_email(email):
        return await update.message.reply_text(f"Only @{ALLOWED_DOMAIN} addresses allowed.")
    await update.message.reply_text(f"Waiting {DELAY_SECONDS}s for your OTP…")
    await asyncio.sleep(DELAY_SECONDS)
    try:
        html = fetch_inbox_html(email)
        code = extract_latest_otp(html)
        if not code:
            return await update.message.reply_text("No OTP found yet.")
        if get_last_code(email) == code:
            await update.message.reply_text("Same as last OTP, might be old.")
        inc_user_count(uid); set_last_code(email, code)
        remaining = MAX_REQUESTS_PER_USER - get_user_count(uid)
        await update.message.reply_text(
            f"Your OTP for `{email}` is: *`{code}`*\nRemaining: {remaining}",
            parse_mode="Markdown",
        )
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

async def remaining(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    used = get_user_count(uid)
    await update.message.reply_text(f"Used {used}/{MAX_REQUESTS_PER_USER}. Remaining {MAX_REQUESTS_PER_USER - used}.")

async def resetlimit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return await update.message.reply_text("Admin only.")
    if not ctx.args:
        return await update.message.reply_text("Usage: /resetlimit <user_id>")
    reset_user_count(ctx.args[0]); await update.message.reply_text(f"Reset for {ctx.args[0]}.")

async def clearemail(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return await update.message.reply_text("Admin only.")
    if not ctx.args:
        return await update.message.reply_text("Usage: /clearemail <email>")
    clear_last_code(ctx.args[0]); await update.message.reply_text(f"Cleared OTP memory for {ctx.args[0]}.")

def main():
    app = ApplicationBuilder().token(TG_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("otp", otp))
    app.add_handler(CommandHandler("remaining", remaining))
    app.add_handler(CommandHandler("resetlimit", resetlimit))
    app.add_handler(CommandHandler("clearemail", clearemail))
    print("Bot running. Ctrl+C to stop.")
    app.run_polling()

if __name__ == "__main__":
    main()

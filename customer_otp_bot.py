# customer_otp_bot.py
import os, re, json, requests, time
from pathlib import Path
from telegram.ext import Updater, CommandHandler
from telegram import ParseMode
from bs4 import BeautifulSoup

# =============== CONFIG (read from environment) ===============
TG_TOKEN = os.environ.get("TG_TOKEN", "")
ALLOWED_DOMAIN = os.environ.get("ALLOWED_DOMAIN", "yotomail.com").lower()
MAX_REQUESTS_PER_USER = int(os.environ.get("MAX_REQUESTS_PER_USER", "10"))
ADMIN_IDS = set(int(x) for x in os.environ.get("ADMIN_IDS", "6356573938").split(",") if x.strip())
DELAY_SECONDS = int(os.environ.get("DELAY_SECONDS", "30"))
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
# ================================================================

GENEMAIL_BASE = "https://generator.email/"
OTP_REGEX = r"\b(\d{6})\b"
TIMEOUT = 12

# ---------- persistence ----------
def load_state():
    p = Path(STATE_FILE)
    if not p.exists():
        return {"usage": {}, "last_codes": {}}
    try:
        data = json.loads(p.read_text())
        if not isinstance(data, dict):
            return {"usage": {}, "last_codes": {}}
        if "usage" not in data or "last_codes" not in data:
            return {"usage": {}, "last_codes": {}}
        return data
    except Exception:
        return {"usage": {}, "last_codes": {}}

def save_state(state):
    try:
        Path(STATE_FILE).write_text(json.dumps(state))
    except Exception:
        pass

STATE = load_state()

def get_user_count(user_id):
    return int(STATE["usage"].get(str(user_id), {}).get("count", 0))

def inc_user_count(user_id):
    rec = STATE["usage"].get(str(user_id), {"count": 0})
    rec["count"] = rec.get("count", 0) + 1
    STATE["usage"][str(user_id)] = rec
    save_state(STATE)

def reset_user_count(user_id):
    STATE["usage"][str(user_id)] = {"count": 0}
    save_state(STATE)

def get_last_code(email):
    return STATE["last_codes"].get(email, {}).get("code")

def set_last_code(email, code):
    STATE["last_codes"][email] = {"code": code, "ts": int(time.time())}
    save_state(STATE)

def clear_last_code(email):
    STATE["last_codes"].pop(email, None)
    save_state(STATE)

# ---------- helpers ----------
def is_allowed_email(email):
    email = email.strip()
    if "@" not in email:
        return False
    local, domain = email.rsplit("@", 1)
    return bool(local) and domain.lower() == ALLOWED_DOMAIN

def fetch_inbox_html(email):
    url = GENEMAIL_BASE.rstrip("/") + "/" + email.strip()
    r = requests.get(url, headers={"User-Agent": "OTPFetcher/1.0"}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.text

def extract_latest_otp(html):
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.select("table tr")
    if not rows:
        return None
    for row in rows:
        tds = row.find_all("td")
        if len(tds) >= 2:
            text = tds[1].get_text(" ")
        else:
            text = row.get_text(" ")
        m = re.search(OTP_REGEX, text)
        if m:
            return m.group(1)
    return None

# ---------- commands ----------
def start_cmd(update, context):
    msg = (
        f"Paste your temp email like:\n"
        f"`/otp yourname@{ALLOWED_DOMAIN}`\n\n"
        f"I'll wait *{DELAY_SECONDS}s* before checking your inbox.\n"
        f"Limit: *{MAX_REQUESTS_PER_USER}* requests per user."
    )
    update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

def otp_cmd(update, context):
    user = update.effective_user
    uid = user.id

    if not context.args:
        update.message.reply_text(
            f"Usage: `/otp yourname@{ALLOWED_DOMAIN}`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    used = get_user_count(uid)
    if used >= MAX_REQUESTS_PER_USER:
        update.message.reply_text(
            f"Limit reached. You used *{used}/{MAX_REQUESTS_PER_USER}* requests.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    email = context.args[0].strip()
    if not is_allowed_email(email):
        update.message.reply_text(
            f"Invalid email. Must end with `@{ALLOWED_DOMAIN}`.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    update.message.reply_text(f"Waiting {DELAY_SECONDS} seconds for your OTP…")
    time.sleep(DELAY_SECONDS)

    try:
        html = fetch_inbox_html(email)
        code = extract_latest_otp(html)
        if not code:
            update.message.reply_text("No OTP found yet.")
            return

        last = get_last_code(email)
        if last == code:
            update.message.reply_text("Warning: same as last OTP, might be old.")

        inc_user_count(uid)
        remaining = MAX_REQUESTS_PER_USER - get_user_count(uid)
        set_last_code(email, code)

        update.message.reply_text(
            f"Your OTP for `{email}` is: *`{code}`*\n"
            f"_Remaining requests: {remaining}_",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        update.message.reply_text(f"Error: {e}")

def remaining_cmd(update, context):
    uid = update.effective_user.id
    used = get_user_count(uid)
    remaining = max(0, MAX_REQUESTS_PER_USER - used)
    update.message.reply_text(
        f"You used *{used}* of *{MAX_REQUESTS_PER_USER}*. Remaining: *{remaining}*.",
        parse_mode=ParseMode.MARKDOWN,
    )

def resetlimit_cmd(update, context):
    if update.effective_user.id not in ADMIN_IDS:
        update.message.reply_text("Admin only.")
        return
    if not context.args:
        update.message.reply_text("Usage: /resetlimit <user_id>")
        return
    uid = context.args[0]
    reset_user_count(uid)
    update.message.reply_text(f"Reset done for {uid}.")

def clearemail_cmd(update, context):
    if update.effective_user.id not in ADMIN_IDS:
        update.message.reply_text("Admin only.")
        return
    if not context.args:
        update.message.reply_text("Usage: /clearemail <email>")
        return
    email = context.args[0]
    clear_last_code(email)
    update.message.reply_text(f"Cleared last OTP memory for {email}.")

def main():
    updater = Updater(TG_TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", start_cmd))
    dp.add_handler(CommandHandler("otp", otp_cmd, pass_args=True))
    dp.add_handler(CommandHandler("remaining", remaining_cmd))
    dp.add_handler(CommandHandler("resetlimit", resetlimit_cmd, pass_args=True))
    dp.add_handler(CommandHandler("clearemail", clearemail_cmd, pass_args=True))

    updater.start_polling()
    print("Bot running. Ctrl+C to stop.")
    updater.idle()

if __name__ == "__main__":
    main()

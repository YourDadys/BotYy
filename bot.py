# bot.py
# Referral + Private-channel verification bot
# - Uses Pyrogram (bot client).
# - Optionally uses a Pyrogram "user" client (if API_ID/API_HASH provided)
#   to detect pending join-requests. Without API_ID/API_HASH, only membership
#   (accepted) can be detected via Bot API.

import os
import sqlite3
import time
import uuid
import traceback

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# ------------------------
# CONFIG FROM ENV
# ------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")                      # required
CHANNEL_INVITE = os.getenv("CHANNEL_INVITE")            # required: the private invite link (e.g. https://t.me/+v448J7mpR8liMmRl)
CHANNEL_ID = os.getenv("CHANNEL_ID")                    # optional numeric chat id like -100123...
API_ID = os.getenv("API_ID")                            # optional (for user client)
API_HASH = os.getenv("API_HASH")                        # optional (for user client)
PORT = int(os.getenv("PORT", "8080"))

if not BOT_TOKEN:
    raise SystemExit("Missing BOT_TOKEN environment variable")
if not CHANNEL_INVITE:
    raise SystemExit("Missing CHANNEL_INVITE environment variable (private invite link)")

# Normalize CHANNEL_ID if provided
if CHANNEL_ID is not None and CHANNEL_ID.strip() == "":
    CHANNEL_ID = None

# ------------------------
# DB setup
# ------------------------
DB_PATH = "referral.db"
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.row_factory = sqlite3.Row
cur = conn.cursor()
cur.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    referrer_id INTEGER,
    verified INTEGER DEFAULT 0,
    started_at INTEGER
);
""")
cur.execute("""
CREATE TABLE IF NOT EXISTS referrals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    referrer_id INTEGER,
    referred_id INTEGER,
    ts INTEGER,
    UNIQUE(referrer_id, referred_id)
);
""")
cur.execute("""
CREATE TABLE IF NOT EXISTS rewards (
    user_id INTEGER PRIMARY KEY,
    rewards INTEGER DEFAULT 0
);
""")
conn.commit()

# ------------------------
# Pyrogram bot client
# ------------------------
app = Client("ref-bot", bot_token=BOT_TOKEN)

# Optional: pyrogram user client (MTProto) to inspect join-requests (if API_ID/API_HASH provided)
user_client = None
use_user_client = False
if API_ID and API_HASH:
    try:
        user_client = Client("ref-user", api_id=int(API_ID), api_hash=API_HASH)
        # We won't start it yet; we'll start after bot starts
        use_user_client = True
    except Exception as e:
        print("Could not initialize user client (API_ID/API_HASH invalid?):", e)
        user_client = None
        use_user_client = False

# ------------------------
# Helpers
# ------------------------
def gen_reward_code():
    return "REWARD-" + uuid.uuid4().hex[:8].upper()

def get_bot_username_sync():
    me = app.get_me()
    return me.username if me and me.username else "bot"

def register_user(user_obj, ref_param=None):
    """Insert user if new and register referral"""
    uid = user_obj.id
    username = user_obj.username
    first_name = user_obj.first_name

    cur.execute("SELECT * FROM users WHERE user_id=?", (uid,))
    existing = cur.fetchone()
    if existing:
        # update names
        cur.execute("UPDATE users SET username=?, first_name=? WHERE user_id=?", (username, first_name, uid))
        conn.commit()
        return False  # not new

    ref_id = None
    if ref_param:
        # param can be "ref_123" or just number
        if isinstance(ref_param, str) and ref_param.startswith("ref_"):
            ref_param = ref_param.replace("ref_", "")
        if str(ref_param).isdigit():
            candidate = int(ref_param)
            if candidate != uid:
                # verify referrer exists OR we'll still accept and create one (optional)
                cur.execute("SELECT 1 FROM users WHERE user_id=?", (candidate,))
                if cur.fetchone():
                    ref_id = candidate
                else:
                    # allow even if not present (still count)
                    ref_id = candidate

    ts = int(time.time())
    cur.execute("INSERT INTO users (user_id, username, first_name, referrer_id, verified, started_at) VALUES (?,?,?,?,0,?)",
                (uid, username, first_name, ref_id, ts))
    conn.commit()

    # Register referral mapping if referrer exists
    if ref_id:
        try:
            cur.execute("INSERT OR IGNORE INTO referrals (referrer_id, referred_id, ts) VALUES (?,?,?)",
                        (ref_id, uid, ts))
            conn.commit()
            # notify referrer
            try:
                app.send_message(ref_id,
                                 f"🎯 Your referral joined: {first_name if first_name else username} (id: {uid})")
            except Exception:
                pass

            # check reward threshold (5)
            cur.execute("SELECT COUNT(*) as c FROM referrals WHERE referrer_id=?", (ref_id,))
            c = cur.fetchone()["c"]
            if c >= 5:
                # grant reward: increment rewards and subtract 5 referrals (or keep refs, but grant)
                cur.execute("SELECT rewards FROM rewards WHERE user_id=?", (ref_id,))
                row = cur.fetchone()
                if row:
                    newr = row["rewards"] + 1
                    cur.execute("UPDATE rewards SET rewards=? WHERE user_id=?", (newr, ref_id))
                else:
                    cur.execute("INSERT INTO rewards (user_id, rewards) VALUES (?,?)", (ref_id, 1))
                conn.commit()
                # remove any 5 referrals if you want to reset counters: optional
                # (here we keep referrals but reward is granted per every 5 total)
                try:
                    app.send_message(ref_id, f"🎉 Congratulations — you reached 5 referrals and earned 1 reward!")
                except Exception:
                    pass
        except Exception as e:
            print("Referral insert error:", e)

    return True  # new user registered

# ------------------------
# Keyboards
# ------------------------
def keyboard_before_verify():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔗 Join Channel", url=CHANNEL_INVITE)],
            [InlineKeyboardButton("✅ Verify (I sent request)", callback_data="verify")]
        ]
    )

def keyboard_after_verify(user_id):
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("👥 Check My Referrals", callback_data="check_refs")],
            [InlineKeyboardButton("🎁 Claim Reward (5 refs = 1)", callback_data="claim_reward")]
        ]
    )

# ------------------------
# Utility: check membership via Bot API (works if accepted)
# ------------------------
def bot_check_membership(user_id):
    try:
        # if CHANNEL_ID provided use it; else try to resolve via invite link (not always possible)
        chat_identifier = CHANNEL_ID if CHANNEL_ID else CHANNEL_INVITE
        member = app.get_chat_member(chat_identifier, user_id)
        status = member.status  # 'member','left','administrator','creator','restricted','kicked'
        return status in ("member", "administrator", "creator")
    except Exception:
        return False

# ------------------------
# Utility: check pending requests via user_client (MTProto)
# NOTE: This requires user_client to be started and bot account to have access to view join requests.
# Pyrogram provides get_chat_join_requests only on user client (if available).
# ------------------------
async def userclient_check_request(user_id):
    """
    Returns:
      - 'joined' if member
      - 'pending' if a join request exists
      - 'not' if no request found
    """
    # require user_client to be running
    try:
        # prefer numeric CHANNEL_ID if provided
        if CHANNEL_ID:
            chat = int(CHANNEL_ID)
        else:
            # resolve invite link -> chat id: pyrogram can resolve via get_chat
            resolved = await user_client.get_chat(CHANNEL_INVITE)
            chat = resolved.id
    except Exception as e:
        print("Could not resolve channel invite via user_client:", e)
        return "not"

    # check membership first
    try:
        member = await user_client.get_chat_member(chat, user_id)
        if member and member.status in ("member", "administrator", "creator"):
            return "joined"
    except Exception:
        pass

    # try to get join requests (Pyrogram exposes get_chat_join_requests on newer versions)
    try:
        # This may work if library exposes get_chat_join_requests
        reqs = await user_client.get_chat_join_requests(chat)
        if reqs:
            for r in reqs:
                if getattr(r, "user", None) and r.user.id == user_id:
                    return "pending"
    except Exception as e:
        # Not all pyrogram versions expose this; fallback:
        print("get_chat_join_requests error or not available:", e)

    return "not"

# ------------------------
# Handlers
# ------------------------
@app.on_message(filters.command("start"))
def on_start(client, message):
    # register and maybe referral
    arg = None
    parts = message.text.split()
    if len(parts) > 1:
        arg = parts[1]

    new = register_user(message.from_user, arg)

    # show join + verify only
    username = get_bot_username_sync()
    ref_link = f"https://t.me/{username}?start={message.from_user.id}"
    text = ("👋 Welcome!\n\n"
            "Please join our private channel using the button below.\n"
            "After you send a join request, come back and press Verify.\n\n"
            f"🔗 Your referral link:\n{ref_link}\n\n")
    message.reply(text, reply_markup=keyboard_before_verify())

@app.on_callback_query(filters.regex("^verify$"))
def on_verify(client, callback_query):
    uid = callback_query.from_user.id
    user_mention = callback_query.from_user.mention
    # First try user_client method if available
    if use_user_client and user_client is not None:
        # run async check via user_client
        try:
            res = user_client.loop.run_until_complete(userclient_check_request(uid))
            if res == "joined":
                # mark verified
                cur.execute("UPDATE users SET verified=1 WHERE user_id=?", (uid,))
                conn.commit()
                callback_query.message.edit_text("✅ Verified: you are a channel member.", reply_markup=keyboard_after_verify(uid))
            elif res == "pending":
                # mark verified as pending true? we can mark verified=0 but inform user
                callback_query.answer("✅ Request found (pending). Verification will succeed once admin approves.", show_alert=True)
                # Optionally set a flag pending; here we keep verified=0 until actual membership
            else:
                callback_query.answer("❌ No request found. Please open channel link and tap 'Join' (send request), then press Verify.", show_alert=True)
        except Exception as e:
            print("user_client verify error:", e)
            traceback.print_exc()
            callback_query.answer("❌ Could not check join requests (user client error).", show_alert=True)
    else:
        # fallback: check membership via Bot API (detects only accepted members)
        is_member = bot_check_membership(uid)
        if is_member:
            cur.execute("UPDATE users SET verified=1 WHERE user_id=?", (uid,))
            conn.commit()
            callback_query.message.edit_text("✅ Verified: you are a channel member.", reply_markup=keyboard_after_verify(uid))
        else:
            callback_query.answer(
                "❌ Could not detect join. If you have only sent a join request (pending) the bot cannot detect it without API_ID/API_HASH.\nPlease join the channel, send the join request, and if you provided API_ID/API_HASH the bot will detect pending requests; otherwise wait until admin accepts and then press Verify.",
                show_alert=True
            )

@app.on_callback_query(filters.regex("^check_refs$"))
def on_check_refs(client, callback_query):
    uid = callback_query.from_user.id
    cur.execute("SELECT COUNT(*) as c FROM referrals WHERE referrer_id=?", (uid,))
    row = cur.fetchone()
    count = row["c"] if row else 0
    cur.execute("SELECT rewards FROM rewards WHERE user_id=?", (uid,))
    row2 = cur.fetchone()
    rewards = row2["rewards"] if row2 else 0
    callback_query.answer()
    callback_query.message.reply_text(f"👥 Your referrals: {count}\n🎁 Rewards: {rewards}")

@app.on_callback_query(filters.regex("^claim_reward$"))
def on_claim_reward(client, callback_query):
    uid = callback_query.from_user.id
    cur.execute("SELECT rewards FROM rewards WHERE user_id=?", (uid,))
    row = cur.fetchone()
    if not row or row["rewards"] <= 0:
        callback_query.answer("❌ You have no rewards to claim.", show_alert=True)
        return
    # decrement one reward and send reward code
    code = gen_reward_code()
    newr = row["rewards"] - 1
    cur.execute("UPDATE rewards SET rewards=? WHERE user_id=?", (newr, uid))
    conn.commit()
    try:
        app.send_message(uid, f"🎁 Reward claimed! Your code: <b>{code}</b>")
    except Exception:
        pass
    callback_query.answer("✅ Reward claimed — check your messages.", show_alert=True)

# Map callback data names used above to actual regex strings used
# Note: our callback_data strings are "verify", "check_refs", "claim_reward"
# Ensure inline buttons use these exact strings.

# ------------------------
# Start user_client if available and then start bot
# ------------------------
def start_both():
    # If user_client available, start it (needed for join-request detection)
    if use_user_client and user_client is not None:
        try:
            print("Starting user_client (MTProto) for pending-request checks...")
            user_client.start()
            print("user_client started.")
        except Exception as e:
            print("Could not start user_client:", e)

    print("Starting bot (bot client)...")
    app.run()

if __name__ == "__main__":
    start_both()

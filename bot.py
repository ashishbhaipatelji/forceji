import logging
import re
import os
from telethon.utils import get_display_name
from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
from decouple import config
from telethon.tl.functions.users import GetFullUserRequest
from telethon.errors.rpcerrorlist import UserNotParticipantError
from telethon.tl.functions.channels import GetParticipantRequest
from telethon.tl.types import ChannelParticipantsAdmins
from telethon.errors import ChatAdminRequiredError, UserAdminInvalidError
import sqlite3
import psycopg2
from datetime import datetime, timezone

logging.basicConfig(
    format="[%(levelname) 5s/%(asctime)s] %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("BotzHub")


def parse_bool(value, default=True):
    """Robustly parse a bool from any string value."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    if not isinstance(value, str):
        return default
    v = value.strip().lower()
    if v in ("true", "yes", "1", "on", "enable", "enabled"):
        return True
    if v in ("false", "no", "0", "off", "disable", "disabled"):
        return False
    log.warning("Could not parse '%s' as bool, defaulting to %s", value, default)
    return default


log.info("Starting...")
try:
    bottoken = config("BOT_TOKEN")
    xchannel = config("CHANNEL")
    welcome_msg = config("WELCOME_MSG", default="Welcome {mention}! Glad to see you in {title}. 🎉")
    welcome_not_joined = config(
        "WELCOME_NOT_JOINED",
        default="Hey {mention}, please join {channel} first to chat here. 👇",
    )
    on_join = parse_bool(config("ON_JOIN", default="True"))
    on_new_msg = parse_bool(config("ON_NEW_MSG", default="True"))
    api_id = config("API_ID", cast=int)
    api_hash = config("API_HASH")
    session_string = config("SESSION_STRING", default=None)
    database_url = config("DATABASE_URL", default=None)
except Exception as e:
    log.error("Config error: %s", e)
    log.info("Bot is quitting...")
    exit(1)

if session_string and len(session_string.strip()) > 20:
    try:
        session = StringSession(session_string.strip())
        log.info("Using string session")
    except Exception as e:
        log.warning("SESSION_STRING is invalid (%s) – falling back to file session", e)
        session = "BotzHub"
else:
    session = "BotzHub"
    log.info("Using file session (BotzHub.session)")

try:
    BotzHub = TelegramClient(session, api_id, api_hash).start(bot_token=bottoken)
except Exception as e:
    log.error("Failed to start TelegramClient: %s", e)
    exit(1)

channel = xchannel.replace("@", "")
bot_self = BotzHub.loop.run_until_complete(BotzHub.get_me())

# ── Database helpers ────────────────────────────────────────────────────────

def _get_db_conn():
    if database_url:
        return psycopg2.connect(database_url), "pg"
    db_path = os.path.join("data", "shield.db")
    os.makedirs("data", exist_ok=True)
    return sqlite3.connect(db_path), "sqlite"


def db_record_mute(user_id: int, chat_id: int):
    try:
        conn, kind = _get_db_conn()
        cur = conn.cursor()
        if kind == "pg":
            cur.execute(
                "INSERT INTO muted_users (user_id, chat_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (user_id, chat_id),
            )
        else:
            cur.execute(
                "INSERT OR IGNORE INTO muted_users (user_id, chat_id) VALUES (?, ?)",
                (user_id, chat_id),
            )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        log.error("db_record_mute error: %s", e)


def db_record_unmute(user_id: int, chat_id: int):
    try:
        conn, kind = _get_db_conn()
        cur = conn.cursor()
        ts = datetime.now(timezone.utc).isoformat()
        if kind == "pg":
            cur.execute(
                "UPDATE muted_users SET unmuted_at=%s WHERE user_id=%s AND chat_id=%s",
                (ts, user_id, chat_id),
            )
        else:
            cur.execute(
                "UPDATE muted_users SET unmuted_at=? WHERE user_id=? AND chat_id=?",
                (ts, user_id, chat_id),
            )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        log.error("db_record_unmute error: %s", e)


def db_log_stat(event_name: str, chat_id=None, user_id=None):
    try:
        conn, kind = _get_db_conn()
        cur = conn.cursor()
        if kind == "pg":
            cur.execute(
                "INSERT INTO stats (event, chat_id, user_id) VALUES (%s, %s, %s)",
                (event_name, chat_id, user_id),
            )
        else:
            cur.execute(
                "INSERT INTO stats (event, chat_id, user_id) VALUES (?, ?, ?)",
                (event_name, chat_id, user_id),
            )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        log.error("db_log_stat error: %s", e)


def db_get_stats():
    try:
        conn, kind = _get_db_conn()
        cur = conn.cursor()
        if kind == "pg":
            cur.execute("SELECT COUNT(*) FROM muted_users WHERE unmuted_at IS NULL")
            currently_muted = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM muted_users WHERE unmuted_at IS NOT NULL")
            total_unmuted = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM stats")
            total_events = cur.fetchone()[0]
        else:
            cur.execute("SELECT COUNT(*) FROM muted_users WHERE unmuted_at IS NULL")
            currently_muted = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM muted_users WHERE unmuted_at IS NOT NULL")
            total_unmuted = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM stats")
            total_events = cur.fetchone()[0]
        cur.close()
        conn.close()
        return currently_muted, total_unmuted, total_events
    except Exception as e:
        log.error("db_get_stats error: %s", e)
        return 0, 0, 0


# ── Helpers ─────────────────────────────────────────────────────────────────

async def get_user_join(user_id):
    try:
        await BotzHub(GetParticipantRequest(channel=channel, participant=user_id))
        return True
    except UserNotParticipantError:
        return False
    except Exception as e:
        log.warning("get_user_join error for %s: %s", user_id, e)
        return False


async def is_admin(chat_id, user_id):
    try:
        admins = await BotzHub.get_participants(chat_id, filter=ChannelParticipantsAdmins())
        return any(a.id == user_id for a in admins)
    except Exception:
        return False


def build_user_vars(user, chat, count):
    mention = f"[{get_display_name(user)}](tg://user?id={user.id})"
    name = user.first_name or ""
    last = user.last_name or ""
    fullname = f"{name} {last}".strip()
    username = f"@{user.username}" if user.username else mention
    title = chat.title if hasattr(chat, "title") and chat.title else "this chat"
    return dict(
        mention=mention, title=title, fullname=fullname,
        username=username, name=name, last=last,
        channel=f"@{channel}", count=count,
    )


# ── /start ───────────────────────────────────────────────────────────────────

@BotzHub.on(events.NewMessage(pattern="^/start$"))
async def cmd_start(event):
    bot_name = bot_self.first_name or "Shield"
    bot_username = f"@{bot_self.username}" if bot_self.username else "Shield Bot"
    text = (
        f"👋 **Welcome to {bot_name}!**\n\n"
        f"🛡️ I'm a **Force Subscribe Bot** that keeps your groups organised by ensuring "
        f"all members join **@{channel}** before they can chat.\n\n"
        f"**✨ What I do:**\n"
        f"• 🔇 Mute new members until they join the channel\n"
        f"• ✅ Automatically unmute once they've joined\n"
        f"• 👋 Send customised welcome messages\n"
        f"• 📊 Track mute/unmute statistics\n"
        f"• 🔒 Keep your group spam-free\n\n"
        f"**⚙️ Current Settings:**\n"
        f"• Channel: @{channel}\n"
        f"• Check on join: {'✅' if on_join else '❌'}\n"
        f"• Check on message: {'✅' if on_new_msg else '❌'}\n\n"
        f"**🚀 To use me:**\n"
        f"1. Add me to your group as **Admin**\n"
        f"2. Give me **Ban Users** permission\n"
        f"3. That's it — I'll handle the rest!\n\n"
        f"_Need help? Use /help for all available commands._"
    )
    buttons = [
        [
            Button.url("📢 Channel", url=f"https://t.me/{channel}"),
            Button.url("➕ Add to Group", url=f"https://t.me/{bot_self.username}?startgroup=true"),
        ],
        [
            Button.inline("📊 Stats", data=b"stats"),
            Button.inline("ℹ️ Help", data=b"help"),
        ],
        [
            Button.url("⭐ GitHub", url="https://github.com/xditya/ForceSub"),
        ],
    ]
    await event.reply(text, buttons=buttons, link_preview=False)
    db_log_stat("start_command", user_id=event.sender_id)


# ── /help ────────────────────────────────────────────────────────────────────

@BotzHub.on(events.NewMessage(pattern="^/help$"))
async def cmd_help(event):
    text = (
        "**🛡️ Shield Bot — Commands**\n\n"
        "**User Commands:**\n"
        "• `/start` — Welcome message & bot info\n"
        "• `/help` — Show this help message\n"
        "• `/status` — Check if you've joined the channel\n\n"
        "**Admin Commands** _(group admins only)_:\n"
        "• `/stats` — View bot statistics\n"
        "• `/unmute <reply>` — Manually unmute a user\n"
        "• `/mute <reply>` — Manually mute a user\n"
        "• `/settings` — View current bot settings\n\n"
        "**Setup:**\n"
        "Add me to your group as Admin with **Ban Users** permission.\n"
        "I'll automatically enforce channel subscription for all members."
    )
    buttons = [
        [Button.inline("◀️ Back", data=b"back_start"), Button.inline("📊 Stats", data=b"stats")],
    ]
    await event.reply(text, buttons=buttons, link_preview=False)


# ── /status ──────────────────────────────────────────────────────────────────

@BotzHub.on(events.NewMessage(pattern="^/status$"))
async def cmd_status(event):
    joined = await get_user_join(event.sender_id)
    if joined:
        await event.reply(
            f"✅ You **have joined** @{channel}.\nYou can speak freely in all groups using this bot!",
            buttons=[Button.url(f"📢 @{channel}", url=f"https://t.me/{channel}")],
        )
    else:
        await event.reply(
            f"❌ You **haven't joined** @{channel} yet.\nJoin the channel to unlock chat access in protected groups.",
            buttons=[
                Button.url(f"📢 Join @{channel}", url=f"https://t.me/{channel}"),
                Button.inline("🔄 Check Again", data=b"recheck"),
            ],
        )


# ── /settings (admin) ────────────────────────────────────────────────────────

@BotzHub.on(events.NewMessage(pattern="^/settings$"))
async def cmd_settings(event):
    if event.is_group or event.is_channel:
        if not await is_admin(event.chat_id, event.sender_id):
            await event.reply("⛔ This command is for group admins only.")
            return
    db_type = "PostgreSQL" if database_url else "SQLite"
    session_type = "String Session ☁️" if session_string else "File Session 💾"
    text = (
        "**⚙️ Shield Bot — Settings**\n\n"
        f"• **Channel:** @{channel}\n"
        f"• **Check on join:** {'✅ Enabled' if on_join else '❌ Disabled'}\n"
        f"• **Check on message:** {'✅ Enabled' if on_new_msg else '❌ Disabled'}\n"
        f"• **Database:** {db_type}\n"
        f"• **Session:** {session_type}\n"
        f"• **Bot:** @{bot_self.username}\n"
    )
    await event.reply(text)


# ── /stats (admin) ───────────────────────────────────────────────────────────

@BotzHub.on(events.NewMessage(pattern="^/stats$"))
async def cmd_stats(event):
    if event.is_group or event.is_channel:
        if not await is_admin(event.chat_id, event.sender_id):
            await event.reply("⛔ This command is for group admins only.")
            return
    currently_muted, total_unmuted, total_events = db_get_stats()
    text = (
        "**📊 Shield Bot — Statistics**\n\n"
        f"• 🔇 Currently muted: **{currently_muted}**\n"
        f"• ✅ Total unmuted: **{total_unmuted}**\n"
        f"• 📈 Total events logged: **{total_events}**\n"
        f"• 📢 Force-subscribe channel: @{channel}\n"
    )
    buttons = [[Button.inline("🔄 Refresh", data=b"stats")]]
    await event.reply(text, buttons=buttons)


# ── /unmute (admin) ───────────────────────────────────────────────────────────

@BotzHub.on(events.NewMessage(pattern="^/unmute$"))
async def cmd_unmute(event):
    if not (event.is_group or event.is_channel):
        await event.reply("⚠️ Use this command in a group.")
        return
    if not await is_admin(event.chat_id, event.sender_id):
        await event.reply("⛔ Admins only.")
        return
    if not event.reply_to_msg_id:
        await event.reply("↩️ Reply to a user's message with /unmute to unmute them.")
        return
    target_msg = await event.get_reply_message()
    target_user = await target_msg.get_sender()
    try:
        await BotzHub.edit_permissions(event.chat_id, target_user.id, send_messages=True)
        db_record_unmute(target_user.id, event.chat_id)
        db_log_stat("admin_unmute", chat_id=event.chat_id, user_id=target_user.id)
        await event.reply(f"✅ {get_display_name(target_user)} has been unmuted.")
    except (ChatAdminRequiredError, UserAdminInvalidError):
        await event.reply("⛔ I don't have permission to unmute users here.")
    except Exception as e:
        log.error("unmute error: %s", e)
        await event.reply(f"⚠️ Failed to unmute: {e}")


# ── /mute (admin) ─────────────────────────────────────────────────────────────

@BotzHub.on(events.NewMessage(pattern="^/mute$"))
async def cmd_mute(event):
    if not (event.is_group or event.is_channel):
        await event.reply("⚠️ Use this command in a group.")
        return
    if not await is_admin(event.chat_id, event.sender_id):
        await event.reply("⛔ Admins only.")
        return
    if not event.reply_to_msg_id:
        await event.reply("↩️ Reply to a user's message with /mute to mute them.")
        return
    target_msg = await event.get_reply_message()
    target_user = await target_msg.get_sender()
    try:
        await BotzHub.edit_permissions(event.chat_id, target_user.id, send_messages=False)
        db_record_mute(target_user.id, event.chat_id)
        db_log_stat("admin_mute", chat_id=event.chat_id, user_id=target_user.id)
        await event.reply(f"🔇 {get_display_name(target_user)} has been muted.")
    except (ChatAdminRequiredError, UserAdminInvalidError):
        await event.reply("⛔ I don't have permission to mute users here.")
    except Exception as e:
        log.error("mute error: %s", e)
        await event.reply(f"⚠️ Failed to mute: {e}")


# ── New member join handler ───────────────────────────────────────────────────

@BotzHub.on(events.ChatAction)
async def on_chat_action(event):
    if not on_join:
        return
    if not event.is_group:
        return
    if event.action_message:
        return
    if not (event.user_joined or event.user_added):
        return

    user = await event.get_user()
    if user.bot:
        return

    chat = await event.get_chat()
    try:
        pp = await BotzHub.get_participants(chat)
        count = len(pp)
    except Exception:
        count = 0

    uv = build_user_vars(user, chat, count)
    joined = await get_user_join(user.id)

    if joined:
        try:
            msg = welcome_msg.format(**uv)
        except KeyError:
            msg = welcome_msg
        butt = [Button.url("📢 Channel", url=f"https://t.me/{channel}")]
        db_log_stat("join_welcomed", chat_id=event.chat.id, user_id=user.id)
    else:
        try:
            msg = welcome_not_joined.format(**uv)
        except KeyError:
            msg = welcome_not_joined
        butt = [
            Button.url("📢 Join Channel", url=f"https://t.me/{channel}"),
            Button.inline("✅ I Joined — Unmute Me", data=f"unmute_{user.id}".encode()),
        ]
        try:
            await BotzHub.edit_permissions(event.chat.id, user.id, send_messages=False)
            db_record_mute(user.id, event.chat.id)
            db_log_stat("join_muted", chat_id=event.chat.id, user_id=user.id)
        except Exception as e:
            log.error("Mute on join failed for %s: %s", user.id, e)

    try:
        await event.reply(msg, buttons=butt)
    except Exception as e:
        log.error("Reply on join failed: %s", e)


# ── New message handler ───────────────────────────────────────────────────────

@BotzHub.on(events.NewMessage(incoming=True))
async def on_new_message(event):
    if event.is_private:
        return
    if not on_new_msg:
        return

    try:
        sender = await event.get_sender()
    except Exception:
        return

    if not sender or sender.bot:
        return

    joined = await get_user_join(event.sender_id)
    if joined:
        return

    try:
        await BotzHub.edit_permissions(event.chat_id, event.sender_id, send_messages=False)
        db_record_mute(event.sender_id, event.chat_id)
        db_log_stat("msg_muted", chat_id=event.chat_id, user_id=event.sender_id)
    except Exception as e:
        log.error("Mute on message failed for %s: %s", event.sender_id, e)
        return

    chat = await event.get_chat()
    try:
        pp = await BotzHub.get_participants(chat)
        count = len(pp)
    except Exception:
        count = 0

    uv = build_user_vars(sender, chat, count)
    try:
        reply_msg = welcome_not_joined.format(**uv)
    except KeyError:
        reply_msg = welcome_not_joined

    butt = [
        Button.url("📢 Join Channel", url=f"https://t.me/{channel}"),
        Button.inline("✅ I Joined — Unmute Me", data=f"unmute_{event.sender_id}".encode()),
    ]
    try:
        await event.reply(reply_msg, buttons=butt)
    except Exception as e:
        log.error("Reply on new message failed: %s", e)


# ── Callback: unmute button ───────────────────────────────────────────────────

@BotzHub.on(events.callbackquery.CallbackQuery(data=re.compile(b"unmute_(.*)")))
async def cb_unmute(event):
    uid = int(event.data_match.group(1).decode("UTF-8"))
    if uid != event.sender_id:
        await event.answer(
            "⚠️ This button is not for you — you can already chat freely!",
            cache_time=0, alert=True,
        )
        return

    joined = await get_user_join(uid)
    if not joined:
        await event.answer(
            f"❌ You haven't joined @{channel} yet!\nJoin the channel then tap this button again.",
            cache_time=0, alert=True,
        )
        return

    try:
        await BotzHub.edit_permissions(event.chat_id, uid, send_messages=True)
        db_record_unmute(uid, event.chat_id)
        db_log_stat("self_unmuted", chat_id=event.chat_id, user_id=uid)
    except (ChatAdminRequiredError, UserAdminInvalidError):
        await event.answer("⛔ I don't have permission to unmute you. Contact a group admin.", cache_time=0, alert=True)
        return
    except Exception as e:
        log.error("cb_unmute error: %s", e)
        await event.answer("⚠️ Something went wrong. Try again later.", cache_time=0, alert=True)
        return

    nm = event.sender.first_name or "there"
    chat_title = (await event.get_chat()).title or "this group"
    try:
        await event.edit(
            f"✅ Welcome to **{chat_title}**, {nm}!\nYou can now chat freely. Enjoy! 🎉",
            buttons=[Button.url("📢 Channel", url=f"https://t.me/{channel}")],
        )
    except Exception as e:
        log.warning("Could not edit unmute message: %s", e)
        await event.answer(f"✅ You're unmuted! Welcome, {nm}!", cache_time=0)


# ── Callback: inline stats ────────────────────────────────────────────────────

@BotzHub.on(events.callbackquery.CallbackQuery(data=b"stats"))
async def cb_stats(event):
    currently_muted, total_unmuted, total_events = db_get_stats()
    text = (
        "**📊 Shield Bot — Statistics**\n\n"
        f"• 🔇 Currently muted: **{currently_muted}**\n"
        f"• ✅ Total unmuted: **{total_unmuted}**\n"
        f"• 📈 Total events: **{total_events}**\n"
        f"• 📢 Channel: @{channel}\n"
    )
    try:
        await event.edit(text, buttons=[[Button.inline("◀️ Back", data=b"back_start"), Button.inline("🔄 Refresh", data=b"stats")]])
    except Exception:
        await event.answer(f"🔇 Muted: {currently_muted} | ✅ Unmuted: {total_unmuted}", cache_time=0)


# ── Callback: inline help ─────────────────────────────────────────────────────

@BotzHub.on(events.callbackquery.CallbackQuery(data=b"help"))
async def cb_help(event):
    text = (
        "**🛡️ Shield Bot — Commands**\n\n"
        "**User Commands:**\n"
        "• `/start` — Welcome message & bot info\n"
        "• `/help` — Show this help message\n"
        "• `/status` — Check your subscription status\n\n"
        "**Admin Commands** _(group admins only)_:\n"
        "• `/stats` — View bot statistics\n"
        "• `/unmute` — Manually unmute a user (reply)\n"
        "• `/mute` — Manually mute a user (reply)\n"
        "• `/settings` — View current bot settings\n\n"
        "**Setup:** Add me to your group as Admin with **Ban Users** permission."
    )
    try:
        await event.edit(text, buttons=[[Button.inline("◀️ Back", data=b"back_start")]])
    except Exception:
        await event.answer("Use /help for commands.", cache_time=0)


# ── Callback: recheck subscription ────────────────────────────────────────────

@BotzHub.on(events.callbackquery.CallbackQuery(data=b"recheck"))
async def cb_recheck(event):
    joined = await get_user_join(event.sender_id)
    if joined:
        await event.answer("✅ Great! You've joined the channel.", cache_time=0, alert=True)
        try:
            await event.edit(
                f"✅ You have joined @{channel}!\nYou can now speak in protected groups.",
                buttons=[Button.url(f"📢 @{channel}", url=f"https://t.me/{channel}")],
            )
        except Exception:
            pass
    else:
        await event.answer(f"❌ Not yet! Please join @{channel} first.", cache_time=0, alert=True)


# ── Callback: back to start ────────────────────────────────────────────────────

@BotzHub.on(events.callbackquery.CallbackQuery(data=b"back_start"))
async def cb_back_start(event):
    bot_name = bot_self.first_name or "Shield"
    text = (
        f"👋 **Welcome to {bot_name}!**\n\n"
        f"🛡️ I'm a **Force Subscribe Bot** that keeps your groups organised by ensuring "
        f"all members join **@{channel}** before they can chat.\n\n"
        f"**✨ What I do:**\n"
        f"• 🔇 Mute new members until they join the channel\n"
        f"• ✅ Automatically unmute once they've joined\n"
        f"• 👋 Send customised welcome messages\n"
        f"• 📊 Track mute/unmute statistics\n"
        f"• 🔒 Keep your group spam-free\n\n"
        f"**⚙️ Current Settings:**\n"
        f"• Channel: @{channel}\n"
        f"• Check on join: {'✅' if on_join else '❌'}\n"
        f"• Check on message: {'✅' if on_new_msg else '❌'}\n\n"
        f"_Use /help for all available commands._"
    )
    buttons = [
        [
            Button.url("📢 Channel", url=f"https://t.me/{channel}"),
            Button.url("➕ Add to Group", url=f"https://t.me/{bot_self.username}?startgroup=true"),
        ],
        [
            Button.inline("📊 Stats", data=b"stats"),
            Button.inline("ℹ️ Help", data=b"help"),
        ],
        [
            Button.url("⭐ GitHub", url="https://github.com/xditya/ForceSub"),
        ],
    ]
    try:
        await event.edit(text, buttons=buttons, link_preview=False)
    except Exception:
        await event.answer("Navigate to /start", cache_time=0)


log.info("Shield Bot started as @%s", bot_self.username)
BotzHub.run_until_disconnected()

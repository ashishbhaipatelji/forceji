import logging
import re
import os
import time
from telethon.utils import get_display_name
from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
from decouple import config
from telethon.errors.rpcerrorlist import UserNotParticipantError
from telethon.tl.functions.channels import GetParticipantRequest
from telethon.tl.types import ChannelParticipantsAdmins
from telethon.errors import (
    ChatAdminRequiredError, UserAdminInvalidError,
    UserIdInvalidError, PeerIdInvalidError,
)
import sqlite3
import psycopg2
from datetime import datetime, timezone

logging.basicConfig(
    format="[%(levelname) 5s/%(asctime)s] %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("BotzHub")

START_TIME = time.time()


# ── Config helpers ────────────────────────────────────────────────────────────

def parse_bool(value, default=True):
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
    bottoken     = config("BOT_TOKEN")
    xchannel     = config("CHANNEL")
    welcome_msg  = config("WELCOME_MSG",
                          default="Welcome {mention}! 🎉 Glad to see you in **{title}**.")
    welcome_not_joined = config(
        "WELCOME_NOT_JOINED",
        default="Hey {mention}, please join {channel} first to chat here. 👇",
    )
    on_join      = parse_bool(config("ON_JOIN",      default="True"))
    on_new_msg   = parse_bool(config("ON_NEW_MSG",   default="True"))
    api_id       = config("API_ID", cast=int)
    api_hash     = config("API_HASH")
    session_string = config("SESSION_STRING", default=None)
    database_url   = config("DATABASE_URL",   default=None)
except Exception as e:
    log.error("Config error: %s", e)
    exit(1)

# ── Session ───────────────────────────────────────────────────────────────────

if session_string and len(session_string.strip()) > 20:
    try:
        session = StringSession(session_string.strip())
        log.info("Using string session")
    except Exception as e:
        log.warning("SESSION_STRING invalid (%s) – falling back to file session", e)
        session = "BotzHub"
else:
    session = "BotzHub"
    log.info("Using file session (BotzHub.session)")

try:
    BotzHub = TelegramClient(session, api_id, api_hash).start(bot_token=bottoken)
except Exception as e:
    log.error("Failed to start TelegramClient: %s", e)
    exit(1)

channel  = xchannel.replace("@", "")
bot_self = BotzHub.loop.run_until_complete(BotzHub.get_me())


# ── Database helpers ──────────────────────────────────────────────────────────

def _db():
    if database_url:
        return psycopg2.connect(database_url), "pg"
    os.makedirs("data", exist_ok=True)
    return sqlite3.connect(os.path.join("data", "shield.db")), "sqlite"


def _ph(kind):
    return "%s" if kind == "pg" else "?"


def db_record_mute(user_id: int, chat_id: int):
    try:
        conn, k = _db()
        p = _ph(k)
        q = (f"INSERT INTO muted_users (user_id, chat_id) VALUES ({p},{p}) ON CONFLICT DO NOTHING"
             if k == "pg" else
             f"INSERT OR IGNORE INTO muted_users (user_id, chat_id) VALUES ({p},{p})")
        conn.cursor().execute(q, (user_id, chat_id))
        conn.commit(); conn.close()
    except Exception as e:
        log.error("db_record_mute: %s", e)


def db_record_unmute(user_id: int, chat_id: int):
    try:
        conn, k = _db()
        p = _ph(k)
        ts = datetime.now(timezone.utc).isoformat()
        conn.cursor().execute(
            f"UPDATE muted_users SET unmuted_at={p} WHERE user_id={p} AND chat_id={p}",
            (ts, user_id, chat_id))
        conn.commit(); conn.close()
    except Exception as e:
        log.error("db_record_unmute: %s", e)


def db_log_stat(event_name: str, chat_id=None, user_id=None):
    try:
        conn, k = _db()
        p = _ph(k)
        conn.cursor().execute(
            f"INSERT INTO stats (event, chat_id, user_id) VALUES ({p},{p},{p})",
            (event_name, chat_id, user_id))
        conn.commit(); conn.close()
    except Exception as e:
        log.error("db_log_stat: %s", e)


def db_upsert_user(user_id: int, first_name: str, username: str):
    try:
        conn, k = _db()
        cur = conn.cursor()
        p = _ph(k)
        ts = datetime.now(timezone.utc).isoformat()
        if k == "pg":
            cur.execute(
                f"INSERT INTO users (user_id, first_name, username, last_seen) "
                f"VALUES ({p},{p},{p},{p}) "
                f"ON CONFLICT (user_id) DO UPDATE SET first_name={p}, username={p}, last_seen={p}",
                (user_id, first_name, username, ts, first_name, username, ts))
        else:
            cur.execute(
                f"INSERT OR IGNORE INTO users (user_id, first_name, username) VALUES ({p},{p},{p})",
                (user_id, first_name, username))
            cur.execute(
                f"UPDATE users SET first_name={p}, username={p}, last_seen={p} WHERE user_id={p}",
                (first_name, username, ts, user_id))
        conn.commit(); conn.close()
    except Exception as e:
        log.error("db_upsert_user: %s", e)


def db_get_stats():
    try:
        conn, k = _db()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM muted_users WHERE unmuted_at IS NULL")
        muted = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM muted_users WHERE unmuted_at IS NOT NULL")
        unmuted = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM stats")
        events_total = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM users")
        total_users = cur.fetchone()[0]
        conn.close()
        return muted, unmuted, events_total, total_users
    except Exception as e:
        log.error("db_get_stats: %s", e)
        return 0, 0, 0, 0


def db_get_all_user_ids():
    try:
        conn, _ = _db()
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM users")
        rows = [r[0] for r in cur.fetchall()]
        conn.close()
        return rows
    except Exception as e:
        log.error("db_get_all_user_ids: %s", e)
        return []


# ── Misc helpers ──────────────────────────────────────────────────────────────

async def get_user_join(user_id):
    try:
        await BotzHub(GetParticipantRequest(channel=channel, participant=user_id))
        return True
    except UserNotParticipantError:
        return False
    except Exception as e:
        log.warning("get_user_join(%s): %s", user_id, e)
        return False


async def is_admin(chat_id, user_id):
    try:
        admins = await BotzHub.get_participants(chat_id, filter=ChannelParticipantsAdmins())
        return any(a.id == user_id for a in admins)
    except Exception:
        return False


def build_user_vars(user, chat, count):
    mention  = f"[{get_display_name(user)}](tg://user?id={user.id})"
    name     = user.first_name or ""
    last     = user.last_name  or ""
    fullname = f"{name} {last}".strip()
    username = f"@{user.username}" if user.username else mention
    title    = (chat.title if hasattr(chat, "title") and chat.title else "this chat")
    return dict(mention=mention, title=title, fullname=fullname,
                username=username, name=name, last=last,
                channel=f"@{channel}", count=count)


def uptime_str():
    secs = int(time.time() - START_TIME)
    h, rem = divmod(secs, 3600)
    m, s   = divmod(rem, 60)
    return f"{h}h {m}m {s}s"


def _start_text():
    return (
        f"👋 **Welcome to Shield Bot!**\n\n"
        f"🛡️ I'm a **Force Subscribe Bot** — I keep your groups clean by ensuring "
        f"every member joins **@{channel}** before they can send messages.\n\n"
        f"**✨ Features:**\n"
        f"› 🔇 Auto-mute members who haven't joined the channel\n"
        f"› ✅ One-tap self-unmute after joining\n"
        f"› 👋 Custom welcome messages with smart placeholders\n"
        f"› 📊 Live statistics & event logging\n"
        f"› 🔒 Admin panel with full group controls\n"
        f"› 📣 Broadcast messages to all bot users\n\n"
        f"**⚙️ Active Settings:**\n"
        f"› Channel: **@{channel}**\n"
        f"› Check on join: {'✅' if on_join else '❌'}\n"
        f"› Check on message: {'✅' if on_new_msg else '❌'}\n\n"
        f"**🚀 Quick Setup:**\n"
        f"1. Add me to your group\n"
        f"2. Make me **Admin** with _Ban Users_ permission\n"
        f"3. Done — I'll handle everything automatically!\n\n"
        f"_Use the buttons below to explore all features._"
    )


def _start_buttons():
    return [
        [
            Button.url(f"📢 @{channel}", url=f"https://t.me/{channel}"),
            Button.url("➕ Add to Group",
                       url=f"https://t.me/{bot_self.username}?startgroup=true"),
        ],
        [
            Button.inline("📊 Statistics",   data=b"stats"),
            Button.inline("ℹ️ Help",          data=b"help"),
        ],
        [
            Button.inline("🔐 Admin Panel",  data=b"admin_panel"),
            Button.inline("⚙️ Settings",     data=b"settings_cb"),
        ],
        [
            Button.inline("🔍 My Status",    data=b"my_status"),
            Button.inline("🏓 Ping",          data=b"ping"),
        ],
        [
            Button.url("⭐ GitHub", url="https://github.com/xditya/ForceSub"),
        ],
    ]


# ═══════════════════════════════════════════════════════════════════════════════
# COMMANDS
# ═══════════════════════════════════════════════════════════════════════════════

# ── /start ────────────────────────────────────────────────────────────────────

@BotzHub.on(events.NewMessage(pattern="^/start$"))
async def cmd_start(event):
    sender = await event.get_sender()
    db_upsert_user(
        event.sender_id,
        sender.first_name or "",
        sender.username  or "",
    )
    db_log_stat("start_command", user_id=event.sender_id)
    await event.reply(_start_text(), buttons=_start_buttons(), link_preview=False)


# ── /help ─────────────────────────────────────────────────────────────────────

@BotzHub.on(events.NewMessage(pattern="^/help$"))
async def cmd_help(event):
    await event.reply(_help_text(), buttons=_help_buttons(), link_preview=False)


def _help_text():
    return (
        "**🛡️ Shield Bot — Command Reference**\n\n"
        "**👤 User Commands:**\n"
        "› `/start`  — Welcome screen & bot info\n"
        "› `/help`   — This help message\n"
        "› `/status` — Check your channel subscription\n"
        "› `/ping`   — Bot health & uptime\n"
        "› `/about`  — About this bot\n\n"
        "**🔐 Admin Commands** _(group admins only)_:\n"
        "› `/admin`    — Open the admin panel\n"
        "› `/stats`    — Live mute/unmute statistics\n"
        "› `/settings` — View current bot configuration\n"
        "› `/mute`     — Mute a user _(reply to their message)_\n"
        "› `/unmute`   — Unmute a user _(reply to their message)_\n"
        "› `/ban`      — Ban a user from the group _(reply)_\n"
        "› `/unban`    — Unban a user _(reply)_\n"
        "› `/kick`     — Kick a user _(reply)_\n"
        "› `/broadcast`— Broadcast a message to all bot users _(reply)_\n\n"
        "**📌 Placeholders** for welcome messages:\n"
        "`{mention}` `{name}` `{last}` `{fullname}`\n"
        "`{username}` `{title}` `{channel}` `{count}`"
    )


def _help_buttons():
    return [
        [
            Button.inline("◀️ Back",        data=b"back_start"),
            Button.inline("📊 Stats",        data=b"stats"),
        ],
        [
            Button.inline("🔐 Admin Panel", data=b"admin_panel"),
        ],
    ]


# ── /about ────────────────────────────────────────────────────────────────────

@BotzHub.on(events.NewMessage(pattern="^/about$"))
async def cmd_about(event):
    text = (
        "**🛡️ About Shield Bot**\n\n"
        "Shield Bot is an open-source **Force Subscribe Bot** for Telegram groups.\n\n"
        "**Tech Stack:**\n"
        "› Python 3.12\n"
        "› Telethon (MTProto)\n"
        "› PostgreSQL / SQLite\n\n"
        "**Features:**\n"
        "› Force channel subscription enforcement\n"
        "› Automatic mute/unmute with one-tap buttons\n"
        "› Full admin panel\n"
        "› Persistent statistics\n"
        "› Railway & Replit compatible\n\n"
        f"**Bot:** @{bot_self.username}\n"
        f"**Uptime:** {uptime_str()}"
    )
    buttons = [
        [
            Button.url("⭐ GitHub", url="https://github.com/xditya/ForceSub"),
            Button.inline("◀️ Back", data=b"back_start"),
        ],
    ]
    await event.reply(text, buttons=buttons, link_preview=False)


# ── /ping ─────────────────────────────────────────────────────────────────────

@BotzHub.on(events.NewMessage(pattern="^/ping$"))
async def cmd_ping(event):
    start = time.time()
    msg   = await event.reply("🏓 Pong!")
    delta = round((time.time() - start) * 1000)
    await msg.edit(
        f"🏓 **Pong!**\n\n"
        f"› Latency: **{delta} ms**\n"
        f"› Uptime:  **{uptime_str()}**\n"
        f"› Status:  ✅ Running",
        buttons=[[Button.inline("◀️ Back", data=b"back_start")]],
    )


# ── /status ───────────────────────────────────────────────────────────────────

@BotzHub.on(events.NewMessage(pattern="^/status$"))
async def cmd_status(event):
    joined = await get_user_join(event.sender_id)
    if joined:
        await event.reply(
            f"✅ **You have joined @{channel}!**\n"
            f"You can speak freely in all groups protected by this bot.",
            buttons=[[Button.url(f"📢 @{channel}", url=f"https://t.me/{channel}"),
                      Button.inline("◀️ Back", data=b"back_start")]],
        )
    else:
        await event.reply(
            f"❌ **You haven't joined @{channel} yet.**\n"
            f"Join the channel to unlock chat access in protected groups.",
            buttons=[
                [Button.url(f"📢 Join @{channel}", url=f"https://t.me/{channel}")],
                [Button.inline("🔄 Check Again", data=b"recheck"),
                 Button.inline("◀️ Back",        data=b"back_start")],
            ],
        )


# ── /admin ────────────────────────────────────────────────────────────────────

@BotzHub.on(events.NewMessage(pattern="^/admin$"))
async def cmd_admin(event):
    if (event.is_group or event.is_channel) and not await is_admin(event.chat_id, event.sender_id):
        await event.reply("⛔ This command is for group admins only.")
        return
    await event.reply(_admin_text(), buttons=_admin_buttons(), link_preview=False)


def _admin_text():
    muted, unmuted, ev_total, total_users = db_get_stats()
    db_type      = "PostgreSQL ☁️" if database_url else "SQLite 💾"
    session_type = "String Session ☁️" if (session_string and len((session_string or "").strip()) > 20) else "File Session 💾"
    return (
        "**🔐 Admin Panel — Shield Bot**\n\n"
        f"**📊 Live Stats:**\n"
        f"› 🔇 Currently muted: **{muted}**\n"
        f"› ✅ Total unmuted:   **{unmuted}**\n"
        f"› 👥 Total users:     **{total_users}**\n"
        f"› 📈 Events logged:   **{ev_total}**\n\n"
        f"**⚙️ Configuration:**\n"
        f"› Channel:    **@{channel}**\n"
        f"› On join:    {'✅ Enabled' if on_join    else '❌ Disabled'}\n"
        f"› On message: {'✅ Enabled' if on_new_msg else '❌ Disabled'}\n"
        f"› Database:   **{db_type}**\n"
        f"› Session:    **{session_type}**\n"
        f"› Uptime:     **{uptime_str()}**\n\n"
        "_Select an action below:_"
    )


def _admin_buttons():
    return [
        [
            Button.inline("📊 Refresh Stats",   data=b"admin_stats"),
            Button.inline("⚙️ Settings",         data=b"settings_cb"),
        ],
        [
            Button.inline("🔇 Mute User",        data=b"admin_help_mute"),
            Button.inline("🔊 Unmute User",       data=b"admin_help_unmute"),
        ],
        [
            Button.inline("🚫 Ban User",          data=b"admin_help_ban"),
            Button.inline("✅ Unban User",         data=b"admin_help_unban"),
        ],
        [
            Button.inline("👢 Kick User",         data=b"admin_help_kick"),
            Button.inline("📣 Broadcast",         data=b"admin_help_broadcast"),
        ],
        [
            Button.inline("🔍 Channel Status",   data=b"channel_status"),
            Button.inline("🏓 Ping",              data=b"ping"),
        ],
        [
            Button.inline("◀️ Back to Menu",     data=b"back_start"),
        ],
    ]


# ── /settings ─────────────────────────────────────────────────────────────────

@BotzHub.on(events.NewMessage(pattern="^/settings$"))
async def cmd_settings(event):
    if (event.is_group or event.is_channel) and not await is_admin(event.chat_id, event.sender_id):
        await event.reply("⛔ This command is for group admins only.")
        return
    await event.reply(_settings_text(), buttons=_settings_buttons())


def _settings_text():
    db_type      = "PostgreSQL ☁️" if database_url else "SQLite 💾"
    session_type = "String Session ☁️" if (session_string and len((session_string or "").strip()) > 20) else "File Session 💾"
    return (
        "**⚙️ Shield Bot — Current Settings**\n\n"
        f"› **Force-subscribe channel:** @{channel}\n"
        f"› **Check on join:**    {'✅ Enabled' if on_join    else '❌ Disabled'}\n"
        f"› **Check on message:** {'✅ Enabled' if on_new_msg else '❌ Disabled'}\n"
        f"› **Database backend:** {db_type}\n"
        f"› **Session type:**     {session_type}\n"
        f"› **Bot username:**     @{bot_self.username}\n"
        f"› **Uptime:**           {uptime_str()}\n\n"
        "_Settings are configured via environment variables._"
    )


def _settings_buttons():
    return [
        [
            Button.inline("🔐 Admin Panel", data=b"admin_panel"),
            Button.inline("📊 Stats",        data=b"stats"),
        ],
        [
            Button.inline("◀️ Back",         data=b"back_start"),
        ],
    ]


# ── /stats ────────────────────────────────────────────────────────────────────

@BotzHub.on(events.NewMessage(pattern="^/stats$"))
async def cmd_stats(event):
    if (event.is_group or event.is_channel) and not await is_admin(event.chat_id, event.sender_id):
        await event.reply("⛔ This command is for group admins only.")
        return
    muted, unmuted, ev_total, total_users = db_get_stats()
    text = (
        "**📊 Shield Bot — Statistics**\n\n"
        f"› 🔇 Currently muted:  **{muted}**\n"
        f"› ✅ Total unmuted:    **{unmuted}**\n"
        f"› 👥 Bot users:        **{total_users}**\n"
        f"› 📈 Events logged:    **{ev_total}**\n"
        f"› 📢 Channel:          @{channel}\n"
        f"› ⏱️ Uptime:           {uptime_str()}"
    )
    buttons = [
        [Button.inline("🔄 Refresh", data=b"stats"),
         Button.inline("🔐 Admin",   data=b"admin_panel")],
        [Button.inline("◀️ Back",    data=b"back_start")],
    ]
    await event.reply(text, buttons=buttons)


# ── /mute ─────────────────────────────────────────────────────────────────────

@BotzHub.on(events.NewMessage(pattern="^/mute$"))
async def cmd_mute(event):
    if not (event.is_group or event.is_channel):
        await event.reply("⚠️ Use this command inside a group.")
        return
    if not await is_admin(event.chat_id, event.sender_id):
        await event.reply("⛔ Admins only.")
        return
    if not event.reply_to_msg_id:
        await event.reply("↩️ Reply to a user's message with `/mute` to mute them.")
        return
    target_msg  = await event.get_reply_message()
    target_user = await target_msg.get_sender()
    try:
        await BotzHub.edit_permissions(event.chat_id, target_user.id, send_messages=False)
        db_record_mute(target_user.id, event.chat_id)
        db_log_stat("admin_mute", chat_id=event.chat_id, user_id=target_user.id)
        await event.reply(f"🔇 **{get_display_name(target_user)}** has been muted.")
    except (ChatAdminRequiredError, UserAdminInvalidError):
        await event.reply("⛔ I don't have permission to mute users here.")
    except Exception as e:
        log.error("cmd_mute: %s", e)
        await event.reply(f"⚠️ Failed: {e}")


# ── /unmute ───────────────────────────────────────────────────────────────────

@BotzHub.on(events.NewMessage(pattern="^/unmute$"))
async def cmd_unmute(event):
    if not (event.is_group or event.is_channel):
        await event.reply("⚠️ Use this command inside a group.")
        return
    if not await is_admin(event.chat_id, event.sender_id):
        await event.reply("⛔ Admins only.")
        return
    if not event.reply_to_msg_id:
        await event.reply("↩️ Reply to a user's message with `/unmute` to unmute them.")
        return
    target_msg  = await event.get_reply_message()
    target_user = await target_msg.get_sender()
    try:
        await BotzHub.edit_permissions(event.chat_id, target_user.id, send_messages=True)
        db_record_unmute(target_user.id, event.chat_id)
        db_log_stat("admin_unmute", chat_id=event.chat_id, user_id=target_user.id)
        await event.reply(f"🔊 **{get_display_name(target_user)}** has been unmuted.")
    except (ChatAdminRequiredError, UserAdminInvalidError):
        await event.reply("⛔ I don't have permission to unmute users here.")
    except Exception as e:
        log.error("cmd_unmute: %s", e)
        await event.reply(f"⚠️ Failed: {e}")


# ── /ban ──────────────────────────────────────────────────────────────────────

@BotzHub.on(events.NewMessage(pattern="^/ban$"))
async def cmd_ban(event):
    if not (event.is_group or event.is_channel):
        await event.reply("⚠️ Use this command inside a group.")
        return
    if not await is_admin(event.chat_id, event.sender_id):
        await event.reply("⛔ Admins only.")
        return
    if not event.reply_to_msg_id:
        await event.reply("↩️ Reply to a user's message with `/ban` to ban them.")
        return
    target_msg  = await event.get_reply_message()
    target_user = await target_msg.get_sender()
    try:
        await BotzHub.edit_permissions(
            event.chat_id, target_user.id,
            view_messages=False,
        )
        db_log_stat("admin_ban", chat_id=event.chat_id, user_id=target_user.id)
        await event.reply(f"🚫 **{get_display_name(target_user)}** has been banned.")
    except (ChatAdminRequiredError, UserAdminInvalidError):
        await event.reply("⛔ I don't have permission to ban users here.")
    except Exception as e:
        log.error("cmd_ban: %s", e)
        await event.reply(f"⚠️ Failed: {e}")


# ── /unban ────────────────────────────────────────────────────────────────────

@BotzHub.on(events.NewMessage(pattern="^/unban$"))
async def cmd_unban(event):
    if not (event.is_group or event.is_channel):
        await event.reply("⚠️ Use this command inside a group.")
        return
    if not await is_admin(event.chat_id, event.sender_id):
        await event.reply("⛔ Admins only.")
        return
    if not event.reply_to_msg_id:
        await event.reply("↩️ Reply to a user's message with `/unban` to unban them.")
        return
    target_msg  = await event.get_reply_message()
    target_user = await target_msg.get_sender()
    try:
        await BotzHub.edit_permissions(
            event.chat_id, target_user.id,
            view_messages=True,
        )
        db_log_stat("admin_unban", chat_id=event.chat_id, user_id=target_user.id)
        await event.reply(f"✅ **{get_display_name(target_user)}** has been unbanned.")
    except (ChatAdminRequiredError, UserAdminInvalidError):
        await event.reply("⛔ I don't have permission to unban users here.")
    except Exception as e:
        log.error("cmd_unban: %s", e)
        await event.reply(f"⚠️ Failed: {e}")


# ── /kick ─────────────────────────────────────────────────────────────────────

@BotzHub.on(events.NewMessage(pattern="^/kick$"))
async def cmd_kick(event):
    if not (event.is_group or event.is_channel):
        await event.reply("⚠️ Use this command inside a group.")
        return
    if not await is_admin(event.chat_id, event.sender_id):
        await event.reply("⛔ Admins only.")
        return
    if not event.reply_to_msg_id:
        await event.reply("↩️ Reply to a user's message with `/kick` to kick them.")
        return
    target_msg  = await event.get_reply_message()
    target_user = await target_msg.get_sender()
    try:
        # Kick = ban then immediately unban
        await BotzHub.edit_permissions(event.chat_id, target_user.id, view_messages=False)
        await BotzHub.edit_permissions(event.chat_id, target_user.id, view_messages=True)
        db_log_stat("admin_kick", chat_id=event.chat_id, user_id=target_user.id)
        await event.reply(f"👢 **{get_display_name(target_user)}** has been kicked.")
    except (ChatAdminRequiredError, UserAdminInvalidError):
        await event.reply("⛔ I don't have permission to kick users here.")
    except Exception as e:
        log.error("cmd_kick: %s", e)
        await event.reply(f"⚠️ Failed: {e}")


# ── /broadcast ────────────────────────────────────────────────────────────────

@BotzHub.on(events.NewMessage(pattern="^/broadcast$"))
async def cmd_broadcast(event):
    if (event.is_group or event.is_channel) and not await is_admin(event.chat_id, event.sender_id):
        await event.reply("⛔ Admins only.")
        return
    if not event.reply_to_msg_id:
        await event.reply(
            "📣 **How to broadcast:**\n"
            "Reply to any message with `/broadcast` and I'll forward it to all bot users.\n\n"
            f"Currently tracking **{len(db_get_all_user_ids())}** users."
        )
        return

    bcast_msg = await event.get_reply_message()
    user_ids  = db_get_all_user_ids()
    sent = failed = 0

    status_msg = await event.reply(f"📣 Broadcasting to {len(user_ids)} users…")

    for uid in user_ids:
        try:
            await BotzHub.forward_messages(uid, bcast_msg)
            sent += 1
        except Exception:
            failed += 1

    db_log_stat("broadcast", user_id=event.sender_id)
    await status_msg.edit(
        f"📣 **Broadcast Complete!**\n\n"
        f"› ✅ Sent:   **{sent}**\n"
        f"› ❌ Failed: **{failed}**\n"
        f"› 👥 Total:  **{len(user_ids)}**"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# GROUP EVENT HANDLERS
# ═══════════════════════════════════════════════════════════════════════════════

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
        count = len(await BotzHub.get_participants(chat))
    except Exception:
        count = 0

    uv     = build_user_vars(user, chat, count)
    joined = await get_user_join(user.id)

    if joined:
        try:
            msg = welcome_msg.format(**uv)
        except KeyError:
            msg = welcome_msg
        butt = [[Button.url("📢 Channel", url=f"https://t.me/{channel}")]]
        db_log_stat("join_welcomed", chat_id=event.chat.id, user_id=user.id)
    else:
        try:
            msg = welcome_not_joined.format(**uv)
        except KeyError:
            msg = welcome_not_joined
        butt = [
            [Button.url("📢 Join Channel", url=f"https://t.me/{channel}")],
            [Button.inline("✅ I Joined — Unmute Me", data=f"unmute_{user.id}".encode())],
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
        count = len(await BotzHub.get_participants(chat))
    except Exception:
        count = 0

    uv = build_user_vars(sender, chat, count)
    try:
        reply_msg = welcome_not_joined.format(**uv)
    except KeyError:
        reply_msg = welcome_not_joined

    butt = [
        [Button.url("📢 Join Channel", url=f"https://t.me/{channel}")],
        [Button.inline("✅ I Joined — Unmute Me",
                       data=f"unmute_{event.sender_id}".encode())],
    ]
    try:
        await event.reply(reply_msg, buttons=butt)
    except Exception as e:
        log.error("Reply on new message failed: %s", e)


# ═══════════════════════════════════════════════════════════════════════════════
# CALLBACK QUERY HANDLERS
# ═══════════════════════════════════════════════════════════════════════════════

# ── Self-unmute ───────────────────────────────────────────────────────────────

@BotzHub.on(events.callbackquery.CallbackQuery(data=re.compile(b"unmute_(.*)")))
async def cb_unmute(event):
    uid = int(event.data_match.group(1).decode())
    if uid != event.sender_id:
        await event.answer(
            "⚠️ This button is not meant for you — you're already free to chat!",
            cache_time=0, alert=True)
        return

    joined = await get_user_join(uid)
    if not joined:
        await event.answer(
            f"❌ You haven't joined @{channel} yet!\n"
            "Join the channel, then tap this button again.",
            cache_time=0, alert=True)
        return

    try:
        await BotzHub.edit_permissions(event.chat_id, uid, send_messages=True)
        db_record_unmute(uid, event.chat_id)
        db_log_stat("self_unmuted", chat_id=event.chat_id, user_id=uid)
    except (ChatAdminRequiredError, UserAdminInvalidError):
        await event.answer(
            "⛔ I don't have permission to unmute you. Ask a group admin.",
            cache_time=0, alert=True)
        return
    except Exception as e:
        log.error("cb_unmute: %s", e)
        await event.answer("⚠️ Something went wrong. Try again later.", cache_time=0, alert=True)
        return

    nm = event.sender.first_name or "there"
    chat_title = (await event.get_chat()).title or "the group"
    try:
        await event.edit(
            f"✅ **Welcome to {chat_title}, {nm}!**\n"
            "You can now chat freely. Enjoy! 🎉",
            buttons=[[Button.url("📢 Channel", url=f"https://t.me/{channel}")]],
        )
    except Exception:
        await event.answer(f"✅ You're unmuted! Welcome, {nm}!", cache_time=0)


# ── Back to start ─────────────────────────────────────────────────────────────

@BotzHub.on(events.callbackquery.CallbackQuery(data=b"back_start"))
async def cb_back_start(event):
    try:
        await event.edit(_start_text(), buttons=_start_buttons(), link_preview=False)
    except Exception:
        await event.answer("Send /start", cache_time=0)


# ── Statistics ────────────────────────────────────────────────────────────────

@BotzHub.on(events.callbackquery.CallbackQuery(data=b"stats"))
async def cb_stats(event):
    muted, unmuted, ev_total, total_users = db_get_stats()
    text = (
        "**📊 Shield Bot — Statistics**\n\n"
        f"› 🔇 Currently muted:  **{muted}**\n"
        f"› ✅ Total unmuted:    **{unmuted}**\n"
        f"› 👥 Bot users:        **{total_users}**\n"
        f"› 📈 Events logged:    **{ev_total}**\n"
        f"› 📢 Channel:          @{channel}\n"
        f"› ⏱️ Uptime:           {uptime_str()}"
    )
    try:
        await event.edit(text, buttons=[
            [Button.inline("🔄 Refresh", data=b"stats"),
             Button.inline("🔐 Admin",   data=b"admin_panel")],
            [Button.inline("◀️ Back",    data=b"back_start")],
        ])
    except Exception:
        await event.answer(f"🔇 {muted} muted | ✅ {unmuted} unmuted", cache_time=0)


# ── Help ──────────────────────────────────────────────────────────────────────

@BotzHub.on(events.callbackquery.CallbackQuery(data=b"help"))
async def cb_help(event):
    try:
        await event.edit(_help_text(), buttons=_help_buttons(), link_preview=False)
    except Exception:
        await event.answer("Send /help", cache_time=0)


# ── Admin panel ───────────────────────────────────────────────────────────────

@BotzHub.on(events.callbackquery.CallbackQuery(data=b"admin_panel"))
async def cb_admin_panel(event):
    try:
        await event.edit(_admin_text(), buttons=_admin_buttons(), link_preview=False)
    except Exception:
        await event.answer("Send /admin", cache_time=0)


# ── Admin stats refresh ───────────────────────────────────────────────────────

@BotzHub.on(events.callbackquery.CallbackQuery(data=b"admin_stats"))
async def cb_admin_stats(event):
    muted, unmuted, ev_total, total_users = db_get_stats()
    await event.answer(
        f"🔇 Muted: {muted} | ✅ Unmuted: {unmuted} | 👥 Users: {total_users}",
        cache_time=0, alert=False)
    try:
        await event.edit(_admin_text(), buttons=_admin_buttons(), link_preview=False)
    except Exception:
        pass


# ── Settings (inline) ─────────────────────────────────────────────────────────

@BotzHub.on(events.callbackquery.CallbackQuery(data=b"settings_cb"))
async def cb_settings(event):
    try:
        await event.edit(_settings_text(), buttons=_settings_buttons())
    except Exception:
        await event.answer("Send /settings", cache_time=0)


# ── Ping (inline) ─────────────────────────────────────────────────────────────

@BotzHub.on(events.callbackquery.CallbackQuery(data=b"ping"))
async def cb_ping(event):
    await event.answer(
        f"🏓 Pong! Uptime: {uptime_str()}",
        cache_time=0, alert=False)


# ── My status (inline) ────────────────────────────────────────────────────────

@BotzHub.on(events.callbackquery.CallbackQuery(data=b"my_status"))
async def cb_my_status(event):
    joined = await get_user_join(event.sender_id)
    if joined:
        await event.answer(f"✅ You have joined @{channel}!", cache_time=0, alert=True)
    else:
        await event.answer(
            f"❌ You haven't joined @{channel} yet.\nJoin to unlock group chat access.",
            cache_time=0, alert=True)


# ── Recheck subscription ──────────────────────────────────────────────────────

@BotzHub.on(events.callbackquery.CallbackQuery(data=b"recheck"))
async def cb_recheck(event):
    joined = await get_user_join(event.sender_id)
    if joined:
        await event.answer("✅ Great! You've joined the channel.", cache_time=0, alert=True)
        try:
            await event.edit(
                f"✅ **You have joined @{channel}!**\n"
                "You can now speak in all groups protected by this bot.",
                buttons=[[Button.url(f"📢 @{channel}", url=f"https://t.me/{channel}"),
                          Button.inline("◀️ Back", data=b"back_start")]],
            )
        except Exception:
            pass
    else:
        await event.answer(
            f"❌ Not yet! Please join @{channel} first.",
            cache_time=0, alert=True)


# ── Channel status ────────────────────────────────────────────────────────────

@BotzHub.on(events.callbackquery.CallbackQuery(data=b"channel_status"))
async def cb_channel_status(event):
    try:
        entity = await BotzHub.get_entity(channel)
        try:
            members = len(await BotzHub.get_participants(entity))
            members_str = f"**{members}**"
        except Exception:
            members_str = "_unavailable_"
        title = getattr(entity, "title", channel)
        await event.answer(f"📢 {title}: {members_str} members", cache_time=0, alert=False)
        try:
            await event.edit(
                f"**📢 Channel Info: @{channel}**\n\n"
                f"› Title:   **{title}**\n"
                f"› Members: {members_str}\n"
                f"› Link:    https://t.me/{channel}",
                buttons=[
                    [Button.url(f"📢 Open @{channel}", url=f"https://t.me/{channel}")],
                    [Button.inline("◀️ Back to Admin",  data=b"admin_panel")],
                ],
            )
        except Exception:
            pass
    except Exception as e:
        await event.answer(f"⚠️ Could not fetch channel info: {e}", cache_time=0, alert=True)


# ── Admin action help popups (teach admins how to use each command) ────────────

_ADMIN_HELP = {
    b"admin_help_mute":      ("🔇 Mute a User",      "In your group, reply to a user's message and send:\n/mute"),
    b"admin_help_unmute":    ("🔊 Unmute a User",     "In your group, reply to a user's message and send:\n/unmute"),
    b"admin_help_ban":       ("🚫 Ban a User",        "In your group, reply to a user's message and send:\n/ban"),
    b"admin_help_unban":     ("✅ Unban a User",       "In your group, reply to a user's message and send:\n/unban"),
    b"admin_help_kick":      ("👢 Kick a User",       "In your group, reply to a user's message and send:\n/kick"),
    b"admin_help_broadcast": ("📣 Broadcast",         "Reply to any message and send:\n/broadcast\nI'll forward it to all bot users."),
}

for _cb_data, (_title, _desc) in _ADMIN_HELP.items():
    def _make_handler(title, desc):
        async def _handler(event):
            await event.answer(f"{title}\n\n{desc}", cache_time=0, alert=True)
        return _handler
    BotzHub.on(events.callbackquery.CallbackQuery(data=_cb_data))(
        _make_handler(_title, _desc)
    )


# ═══════════════════════════════════════════════════════════════════════════════

log.info("Shield Bot started as @%s", bot_self.username)
BotzHub.run_until_disconnected()

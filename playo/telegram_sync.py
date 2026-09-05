import asyncio
import os
import re

from telethon import TelegramClient, events
from telethon.tl.types import DocumentAttributeAudio, DocumentAttributeFilename

from . import config as cfgmod

EXT_MAP = {
    "audio/mpeg": ".mp3", "audio/mp4": ".m4a", "audio/x-m4a": ".m4a",
    "audio/flac": ".flac", "audio/x-flac": ".flac", "audio/ogg": ".ogg",
    "audio/opus": ".opus", "audio/wav": ".wav", "audio/x-wav": ".wav",
}


def _sanitize(name):
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name).strip().strip(".")
    return name or "audio"


def _channel(v):
    v = str(v).strip()
    return int(v) if re.fullmatch(r"-?\d+", v) else v


def _filename(msg):
    doc = msg.document
    if doc:
        for a in doc.attributes:
            if isinstance(a, DocumentAttributeFilename) and a.file_name:
                return _sanitize(a.file_name)
        audio = next((a for a in doc.attributes
                      if isinstance(a, DocumentAttributeAudio)), None)
        if audio and getattr(audio, "title", None):
            performer = getattr(audio, "performer", "") or ""
            base = (f"{performer} - {audio.title}"
                    if performer and performer.lower() not in audio.title.lower()
                    else audio.title)
            ext = EXT_MAP.get((doc.mime_type or "").lower(), ".mp3")
            return _sanitize(f"{base}{ext}")
    return _sanitize(f"audio_{msg.id}.mp3")


def _audio_meta(msg):
    """Metadata only — nothing is downloaded here."""
    doc = getattr(msg, "document", None)
    if not doc:
        return None
    mime = (doc.mime_type or "").lower()
    if not mime.startswith("audio") and not msg.audio:
        return None
    audio = next((a for a in doc.attributes
                  if isinstance(a, DocumentAttributeAudio)), None)
    title = getattr(audio, "title", None) if audio else None
    performer = getattr(audio, "performer", None) if audio else None
    duration = getattr(audio, "duration", 0) or 0 if audio else 0
    name = _filename(msg)
    if not title:
        stem = os.path.splitext(name)[0]
        if " - " in stem:
            p, t = stem.split(" - ", 1)
            title, performer = t, performer or p
        else:
            title = stem
    return {
        "msg_id": msg.id,
        "file": name,
        "title": title or os.path.splitext(name)[0],
        "artist": performer or "",
        "duration": float(duration or 0),
        "size": getattr(doc, "size", 0) or 0,
    }


def _session_base(cfg, dl=False):
    """Separate session files so the bot watch and user login never conflict."""
    if cfg.get("user_session"):
        return cfgmod.session_path(cfg) + "_user"
    return cfgmod.session_path(cfg) + ("_bot_dl" if dl else "_bot")


def _remove_session_files(base):
    for suffix in (".session", ".session-journal", ".session-wal", ".session-shm"):
        p = base + suffix
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass


class LoginRequired(Exception):
    """Interactive personal login needed — must happen in the main thread."""


async def _connect_user(cfg, interactive=True):
    """Personal-account client (Telegram code only — no API registration).

    Self-heals: if a bot token was accidentally saved in the user session,
    the session is wiped and the phone login starts over."""
    base = _session_base(cfg)
    client = TelegramClient(base, cfg["api_id"], cfg["api_hash"])
    await client.connect()
    if await client.is_user_authorized():
        me = await client.get_me()
        if not getattr(me, "bot", False):
            return client
        await client.disconnect()
        _remove_session_files(base)
        client = TelegramClient(base, cfg["api_id"], cfg["api_hash"])
        await client.connect()
    if not interactive:
        await client.disconnect()
        raise LoginRequired()
    print("One-time personal login (a Telegram code will be sent).")
    phone = input("Phone (international format, e.g. +989123456789): ").strip()
    if not phone:
        raise RuntimeError("A phone number is required to index channel history.")
    await client.start(phone=phone)
    me = await client.get_me()
    if getattr(me, "bot", False):
        raise RuntimeError("That was a bot token — enter your personal phone number.")
    return client


async def _connect_bot(cfg, dl=False):
    client = TelegramClient(_session_base(cfg, dl), cfg["api_id"], cfg["api_hash"])
    await client.start(bot_token=cfg["bot_token"])
    return client


def _connect(cfg, dl=False, interactive=True):
    if cfg.get("user_session"):
        return _connect_user(cfg, interactive=interactive)
    return _connect_bot(cfg, dl=dl)


# ---------- catalog (index only, no downloads) ----------

async def _resolve_channel(client, cfg):
    """Resolve the channel to an input entity, warming the entity cache if needed."""
    v = _channel(cfg["channel"])
    try:
        return await client.get_input_entity(v)
    except Exception:
        pass
    if cfg.get("user_session"):
        try:
            async for _dlg in client.iter_dialogs():
                pass
        except Exception:
            pass
        try:
            return await client.get_input_entity(v)
        except Exception as e:
            raise RuntimeError(
                "Channel not found in this account's dialogs. "
                "Open the channel once in Telegram with this account (join it), "
                "then run sync again.") from e
    raise RuntimeError("Can't resolve the channel. Is the bot an admin of the channel?")


async def _catalog_history(cfg, interactive=True, min_id=0):
    client = await _connect(cfg, interactive=interactive)
    entries = {}
    try:
        entity = await _resolve_channel(client, cfg)
        # telethon 1.44 breaks with min_id=None — always pass an int
        async for msg in client.iter_messages(entity, min_id=int(min_id)):
            m = _audio_meta(msg)
            if m:
                entries[msg.id] = m
    finally:
        await client.disconnect()
    return list(entries.values())


def build_catalog_history(cfg, interactive=True, min_id=0):
    return asyncio.run(_catalog_history(cfg, interactive=interactive, min_id=min_id))


# ---------- one-time personal login (phone + code, TUI-driven) ----------

def personal_login(cfg, phone, code_q, pwd_q, status_cb=None):
    """Log the personal account in from the settings screen.

    Runs in its own thread. When Telegram asks for the login code (or the
    2FA password) this call blocks on the given queues until the UI pushes
    the answer — no console prompts. Returns 'ok' or 'already'."""
    return asyncio.run(_personal_login(cfg, phone, code_q, pwd_q, status_cb))


async def _personal_login(cfg, phone, code_q, pwd_q, status_cb=None):
    base = _session_base(cfg)
    client = TelegramClient(base, cfg["api_id"], cfg["api_hash"])
    await client.connect()
    if await client.is_user_authorized():
        me = await client.get_me()
        if not getattr(me, "bot", False):
            await client.disconnect()
            return "already"
        # a bot token got saved here by mistake — wipe and start over
        await client.disconnect()
        _remove_session_files(base)
        client = TelegramClient(base, cfg["api_id"], cfg["api_hash"])
        await client.connect()

    def _code():
        if status_cb:
            status_cb("code sent — check Telegram")
        return code_q.get()

    def _pwd():
        if status_cb:
            status_cb("2FA password required")
        return pwd_q.get()

    await client.start(phone=phone, code_callback=_code,
                       password_callback=_pwd)
    me = await client.get_me()
    bot = bool(getattr(me, "bot", False))
    await client.disconnect()
    if bot:
        raise RuntimeError("that was a bot token — enter your phone number")
    return "ok"


# ---------- on-demand download of selected songs ----------

async def _download(cfg, entries, progress_cb, on_file, dl):
    os.makedirs(cfg["download_dir"], exist_ok=True)
    # interactive=False: downloads also run in background threads (TUI) —
    # same rule as streaming: LoginRequired, never a hidden input()
    client = await _connect(cfg, dl=dl, interactive=False)
    paths = []
    try:
        chat = await _resolve_channel(client, cfg)
        for e in entries:
            msg = await client.get_messages(chat, ids=e["msg_id"])
            if not msg or not getattr(msg, "document", None):
                continue
            dest = os.path.join(cfg["download_dir"], e["file"])
            if os.path.exists(dest):
                paths.append(dest)
                if on_file:
                    on_file(dest)
                continue
            cb = None
            if progress_cb:
                cb = lambda cur, tot, n=e["file"]: progress_cb(n, cur, tot)
            await client.download_media(msg, file=dest, progress_callback=cb)
            paths.append(dest)
            if on_file:
                on_file(dest)
    finally:
        await client.disconnect()
    return paths


def download_entries(cfg, entries, progress_cb=None, on_file=None, dl=False):
    return asyncio.run(_download(cfg, entries, progress_cb, on_file, dl))


async def _download_streaming(cfg, entry, on_progress, cancel=None):
    """Download to <file>.part while the player reads a separate copy.

    on_progress(cur, total) fires as chunks land. When `cancel` is set
    (user picked another song) the download stops and cleans up."""
    os.makedirs(cfg["download_dir"], exist_ok=True)
    # interactive=False: streaming always runs in a BACKGROUND thread —
    # an input() prompt there would hang the app invisibly; a missing
    # session must raise LoginRequired instead
    client = await _connect(cfg, dl=True, interactive=False)
    dest = os.path.join(cfg["download_dir"], entry["file"])
    part = dest + ".part"
    cancelled = False
    try:
        chat = await _resolve_channel(client, cfg)
        msg = await client.get_messages(chat, ids=entry["msg_id"])
        if not msg or not getattr(msg, "document", None):
            raise RuntimeError("message not found")
        total = getattr(msg.document, "size", 0) or 0
        if os.path.exists(part):
            os.remove(part)                       # restart interrupted parts
        loops = 0
        async for chunk in client.iter_download(msg, file_size=total,
                                                chunk_size=512 * 1024):
            if cancel is not None and cancel.is_set():
                cancelled = True
                break
            with open(part, "ab") as f:
                f.write(chunk)
            loops += 1
            if on_progress and loops % 2 == 0:
                on_progress(os.path.getsize(part), total)
        if cancelled:
            try:
                os.remove(part)
            except OSError:
                pass
            return None
        # The mixer may still hold the .part file open — on Windows the rename
        # then fails with PermissionError. Leave the promotion to the caller.
        try:
            os.replace(part, dest)
            promoted = True
        except PermissionError:
            promoted = False
        if on_progress:
            on_progress(total, total)
        return dest if promoted else part
    finally:
        await client.disconnect()


def download_streaming(cfg, entry, on_progress=None, cancel=None):
    return asyncio.run(_download_streaming(cfg, entry, on_progress, cancel))


# ---------- live watch (indexes new posts, does NOT download) ----------

async def _watch(cfg, on_new):
    os.makedirs(cfg["download_dir"], exist_ok=True)
    client = await _connect(cfg)
    entity = await _resolve_channel(client, cfg)

    @client.on(events.NewMessage(chats=entity))
    async def _handler(event):
        m = _audio_meta(event.message)
        if m:
            on_new(m)

    print("Listening to the channel... (Ctrl+C to exit)")
    await client.run_until_disconnected()


def run_watch(cfg, on_new):
    asyncio.run(_watch(cfg, on_new))

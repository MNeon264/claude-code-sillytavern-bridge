"""Character Memory: recaps of what happened in SillyTavern chats, carried
into other chats only where the user asks for them.

A memory belongs to the chat it happened in. The bridge reads SillyTavern's
chat files from its data folder and has Sonnet recap them, part by part, from
one character's point of view where the character is in that part (has a
message in it), and as a plain account where they aren't: an account names
no point of view, so everyone who wasn't there shares it, and nobody is
written into a scene they weren't in. Nothing is recorded per turn.

What a character remembers in a chat (their "sources"):
  - Earlier in the same chat: recaps of the messages SillyTavern no longer
    sends because they don't fit the context, in whole parts, so the last
    part overlaps the start of the transcript.
  - Connected chats (links): any chat the user connected to this chat on
    the Memory tab, for everyone in it (character_id "") or for chosen
    characters. A chat the character wasn't in comes as background.
  - A persona binding: once a character is bound to a persona, every chat
    the user picks for the binding, plus each new (non-branch) chat with that
    character and persona, is remembered in the other chats with that
    persona. A chat can be left out of bindings everywhere (encounter
    `excluded`), or in one chat (exclusions).
Chats of the same lineage as the current one (its parent, branches of it)
are never brought in through a binding: they are alternate timelines.

Requests identify the chat, persona and speaking card through the
`bridge_memory` field the SillyTavern extension (st-extension/) adds to every
chat completion request. Without it memory skips the turn.

Recaps are cached by the text of the part they recap (SEGMENT_SIZE messages
per part), so a branch shares its parent's recaps up to the branch point and
an edit only invalidates the part it's in. They're built in the background
by one worker thread, when a chat that needs them is used or opened, or when
the user asks on the Memory tab; a turn uses what's built so far.

A part's recap is written scene by scene, each scene's paragraph opening
with the number of the message it starts at ("[#52]"). What another chat
gets of a remembered chat is written from those scene recaps (passed_on),
in the mode the Memory tab sets: a synopsis of the whole chat of about a
set number of words, or chapters, one per event, each of about a set
number of words. Sonnet divides a chat of at least CHAPTER_MIN_PARTS parts
into chapters at its scenes, so a chapter can start inside a part, once
per chat for every character, and when the chat grows only its last
chapter is divided again, so earlier chapters keep their place and the
choices made about them. Each connection and each binding can leave
chapters out. Scene recaps short enough already are passed on as they are.

When a chat with sources starts, Sonnet compares its opening (and Author's
Note) with the memories of chats the character was in. A conflict becomes
an alert the extension shows in SillyTavern, where the user keeps the
memories or drops some from the chat.

The bridge passes the memory to the model as a <character_memory> block once
per CLI session, then only the parts that changed (see claude_bridge).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
import struct
import subprocess
import threading
import time
import urllib.parse
from collections import OrderedDict, deque
from datetime import datetime
from typing import Callable, NamedTuple, Optional

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
MEMORY_ROOT = os.path.join(_THIS_DIR, "character_memory")
DB_PATH = os.path.join(MEMORY_ROOT, "memory.db")

# Chat messages per recapped part. Parts start at fixed message indices, so
# a branch and its parent share every part before the branch point.
SEGMENT_SIZE = 40
# Characters of other chats' memories per turn. Newest chats first get
# theirs; a chat's earliest chapters are left out when its others fit, and
# chats that don't fit at all are left out.
SOURCE_BUDGET = 24000
# What another chat gets of a remembered chat (the memory_mode setting):
# one synopsis, or chapters. Word targets: memory_synopsis_words and
# memory_chapter_words.
MODES = ("synopsis", "chapters")
DEFAULT_WORDS = {"synopsis": 400, "chapters": 250}
WORDS_MIN, WORDS_MAX = 50, 2000
# Part recaps up to this much over the word target are passed on as they
# are, without a Sonnet call to shorten them.
WORDS_SLACK = 1.25
# A chat with fewer parts than this is one chapter.
CHAPTER_MIN_PARTS = 3
# Most messages of a chat's opening (its greetings up to the user's first
# message) compared with its memories for conflicts.
OPENING_MESSAGES = 6
# Longest message text sent to a recap call.
_MESSAGE_CAP = 12000
RECAP_TIMEOUT = 300
CHECK_TIMEOUT = 120

# Generation types that aren't a character's reply: background prompts from
# extensions, and the user's own message written by the model.
_SKIP_TYPES = {"quiet", "impersonate"}

# ---------------------------------------------------------------------------
# Wiring (set by the bridge)
# ---------------------------------------------------------------------------

_log_fn: Optional[Callable] = None
_claude_exe = os.environ.get("CLAUDE_EXE", "claude")
_call_recorder: Optional[Callable] = None
_st_dir_fn: Optional[Callable[[], Optional[str]]] = None
_settings_fn: Optional[Callable[[], dict]] = None


def configure(log=None, claude_exe=None, call_recorder=None, st_user_dir=None, settings=None):
    """log(message, level); claude_exe: path to the CLI; call_recorder:
    prompt_history.start_call; st_user_dir: callable returning SillyTavern's
    data/default-user folder or None; settings: callable returning the
    bridge's settings (memory_mode and the word targets)."""
    global _log_fn, _claude_exe, _call_recorder, _st_dir_fn, _settings_fn
    if log:
        _log_fn = log
    if claude_exe:
        _claude_exe = claude_exe
    if call_recorder:
        _call_recorder = call_recorder
    if st_user_dir:
        _st_dir_fn = st_user_dir
    if settings:
        _settings_fn = settings


def pass_mode() -> str:
    """What another chat gets of a remembered chat: "synopsis" or "chapters"."""
    m = (_settings_fn() if _settings_fn else {}).get("memory_mode")
    return m if m in MODES else MODES[0]


def word_target(m: Optional[str] = None) -> int:
    """The word target of a mode: the synopsis's length, or each chapter's."""
    m = m or pass_mode()
    v = (_settings_fn() if _settings_fn else {}).get(f"memory_{'synopsis' if m == 'synopsis' else 'chapter'}_words")
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return DEFAULT_WORDS[m]
    return int(min(WORDS_MAX, max(WORDS_MIN, v)))


def log(msg: str, level: str = "INFO"):
    if _log_fn:
        _log_fn(f"[memory] {msg}", level)
    else:
        print(f"[memory] {level}: {msg}")


def _hash(text: str, n: int = 24) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:n]


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
-- A character is one or more cards (aliases); its id is its first card's avatar.
CREATE TABLE IF NOT EXISTS characters (id TEXT PRIMARY KEY, name TEXT NOT NULL, created_ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS cards (avatar TEXT PRIMARY KEY, character_id TEXT NOT NULL, name TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS cards_character ON cards(character_id);
-- Chat files seen live or scanned on disk. chat_id is the file's path in
-- SillyTavern's user folder (rel): SillyTavern's integrity id isn't unique,
-- as older versions copied it into branches. It's kept to follow a renamed
-- chat. personas: {avatar: {n, name}}. created_ts: when a branch named after
-- its parent was made (_created), else the first message's time; last_msg:
-- {name, text} of its last message, for the Memory tab.
CREATE TABLE IF NOT EXISTS chats (
    chat_id TEXT PRIMARY KEY, rel TEXT NOT NULL, integrity TEXT, group_id TEXT, group_name TEXT, main_chat TEXT,
    title TEXT, first_ts REAL, last_ts REAL, n INTEGER, personas TEXT, speakers TEXT,
    mtime REAL, size INTEGER, live_ts REAL, created_ts REAL, last_msg TEXT);
CREATE INDEX IF NOT EXISTS chats_rel ON chats(rel);
-- One character's memory of one chat: flags. The recaps of its parts are
-- in segments, keyed by the card and the text. (Older databases also have
-- short, short_key and short_edited, unused since synopses replaced short
-- recaps, and chapters, moved to plans.)
CREATE TABLE IF NOT EXISTS encounters (
    character_id TEXT NOT NULL, chat_id TEXT NOT NULL,
    excluded INTEGER NOT NULL DEFAULT 0, chapters TEXT,
    built_ts REAL, error TEXT,
    PRIMARY KEY (character_id, chat_id));
-- A chat's division into chapters, the same for every character:
-- {"chapters": [{start, title}], "last"}, see _plan_chapters.
CREATE TABLE IF NOT EXISTS plans (chat_id TEXT PRIMARY KEY, chapters TEXT NOT NULL);
-- pov: the card the part is recapped from (a recap stays valid when cards
-- are added to or removed from a character), or "" for an account of a
-- part the character isn't in, shared by all who weren't there.
CREATE TABLE IF NOT EXISTS segments (
    key TEXT PRIMARY KEY, pov TEXT NOT NULL, recap TEXT NOT NULL,
    edited INTEGER NOT NULL DEFAULT 0, created_ts REAL NOT NULL);
-- Synopses and chapter recaps (kind), written from part recaps. key: a hash
-- of the kind, the card ("" when the character is in no part), the word
-- target and those part recaps, so they're shared like parts and written
-- again when a part changes.
CREATE TABLE IF NOT EXISTS condensed (
    key TEXT PRIMARY KEY, kind TEXT NOT NULL, pov TEXT NOT NULL, recap TEXT NOT NULL,
    edited INTEGER NOT NULL DEFAULT 0, created_ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS bindings (
    character_id TEXT NOT NULL, persona TEXT NOT NULL, persona_name TEXT, created_ts REAL NOT NULL,
    PRIMARY KEY (character_id, persona));
-- how: chosen (picked on the Memory tab), auto (a new chat), removed (taken
-- out; never auto-added again). chapters_off, here and in links: JSON list
-- of the starts of the chat's chapters that this binding or connection
-- leaves out.
CREATE TABLE IF NOT EXISTS binding_chats (
    character_id TEXT NOT NULL, persona TEXT NOT NULL, chat_id TEXT NOT NULL,
    how TEXT NOT NULL, added_ts REAL NOT NULL, chapters_off TEXT,
    PRIMARY KEY (character_id, persona, chat_id));
-- A connection: target_chat remembers source_chat. character_id: the
-- character who remembers it, or "" for everyone in the target chat (a
-- character's own row wins over that one).
CREATE TABLE IF NOT EXISTS links (
    character_id TEXT NOT NULL, target_chat TEXT NOT NULL, source_chat TEXT NOT NULL, created_ts REAL NOT NULL,
    chapters_off TEXT,
    PRIMARY KEY (character_id, target_chat, source_chat));
CREATE TABLE IF NOT EXISTS exclusions (
    character_id TEXT NOT NULL, target_chat TEXT NOT NULL, source_chat TEXT NOT NULL, created_ts REAL NOT NULL,
    PRIMARY KEY (character_id, target_chat, source_chat));
-- The latest conflict check of a chat's opening against its memories.
-- status: clear, open (a conflict to show), kept, applied, failed. sig:
-- hash of the opening; sources: every chat checked so far; shown: the ones
-- the open conflict is about.
CREATE TABLE IF NOT EXISTS checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT, character_id TEXT NOT NULL, chat_id TEXT NOT NULL,
    sig TEXT NOT NULL, status TEXT NOT NULL, explanation TEXT, sources TEXT, shown TEXT,
    created_ts REAL NOT NULL,
    UNIQUE (character_id, chat_id));
CREATE TABLE IF NOT EXISTS ext_tokens (hash TEXT PRIMARY KEY, last_ts REAL NOT NULL);
"""

_db_lock = threading.RLock()
_conn: Optional[sqlite3.Connection] = None


def _db() -> sqlite3.Connection:
    """The shared connection. Use it with _db_lock held."""
    global _conn
    if _conn is None:
        os.makedirs(MEMORY_ROOT, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_SCHEMA)
        for table, col, kind in (("chats", "created_ts", "REAL"), ("chats", "last_msg", "TEXT"),
                                 ("encounters", "chapters", "TEXT"), ("links", "chapters_off", "TEXT"),
                                 ("binding_chats", "chapters_off", "TEXT")):
            if col not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {kind}")
        # Chapters used to be divided per character; a chat keeps the first.
        conn.execute("INSERT OR IGNORE INTO plans (chat_id, chapters) SELECT chat_id, chapters FROM encounters "
                     "WHERE chapters IS NOT NULL ORDER BY built_ts")
        conn.execute("UPDATE encounters SET chapters = NULL WHERE chapters IS NOT NULL")
        _conn = conn
    return _conn


def _q(sql: str, args=()) -> list[sqlite3.Row]:
    with _db_lock:
        return _db().execute(sql, args).fetchall()


def _q1(sql: str, args=()) -> Optional[sqlite3.Row]:
    with _db_lock:
        return _db().execute(sql, args).fetchone()


def _x(sql: str, args=()) -> int:
    with _db_lock:
        return _db().execute(sql, args).rowcount


def _meta_set(key: str, value):
    _x("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
       (key, json.dumps(value)))


def _meta_get(key: str, default=None):
    row = _q1("SELECT value FROM meta WHERE key = ?", (key,))
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# SillyTavern's data folder
# ---------------------------------------------------------------------------

def st_dir() -> Optional[str]:
    return _st_dir_fn() if _st_dir_fn else None


def _stem(name: str) -> str:
    return name[:-4] if name.lower().endswith(".png") else os.path.splitext(name)[0]


def _safe_rel(rel: str) -> Optional[str]:
    """`rel` if it names a file inside SillyTavern's folder."""
    rel = (rel or "").replace("\\", "/")
    if not rel or rel.startswith("/") or any(p in ("", "..") for p in rel.split("/")):
        return None
    return rel


def chat_rel(chat_file: str, group_id: str, card_avatar: str) -> Optional[str]:
    """Path of a chat file relative to SillyTavern's user folder: group chats
    are in "group chats/", a card's chats in "chats/<avatar without .png>/"."""
    if not chat_file or "/" in chat_file or "\\" in chat_file:
        return None
    if group_id:
        return _safe_rel(f"group chats/{chat_file}.jsonl")
    if not card_avatar or "/" in card_avatar:
        return None
    return _safe_rel(f"chats/{_stem(card_avatar)}/{chat_file}.jsonl")


def _abs(rel: str) -> Optional[str]:
    base = st_dir()
    rel = _safe_rel(rel)
    return os.path.join(base, *rel.split("/")) if base and rel else None


_TAG_RE = re.compile(r"<(style|script)\b[\s\S]*?</\1\s*>|</?[A-Za-z][^<>]{0,500}>", re.IGNORECASE)


def _clean(text: str) -> str:
    """Message text for a recap call: HTML styling removed, capped."""
    text = _TAG_RE.sub("", str(text or ""))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:_MESSAGE_CAP] + (" [...]" if len(text) > _MESSAGE_CAP else "")


def _parse_ts(value) -> Optional[float]:
    """SillyTavern's send_date: ISO, epoch milliseconds, or the old
    "September 10, 2025 1:10pm"."""
    if isinstance(value, (int, float)):
        return value / 1000 if value > 1e11 else float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    v = value.strip()
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
    except ValueError:
        pass
    for fmt in ("%B %d, %Y %I:%M%p", "%B %d, %Y %I:%M %p", "%Y-%m-%d @%Hh %Mm %Ss %fms", "%Y-%m-%d@%Hh%Mm%Ss"):
        try:
            return datetime.strptime(v, fmt).timestamp()
        except ValueError:
            continue
    return None


def _persona_of(msg: dict) -> str:
    """The persona avatar a user message was sent with ("" if unknown)."""
    fa = msg.get("force_avatar")
    if not isinstance(fa, str) or not fa:
        return ""
    if "file=" in fa:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(fa).query)
        return (q.get("file") or [""])[0]
    if fa.startswith("User Avatars/"):
        return fa.split("/", 1)[1]
    return ""


class ChatFile:
    """A parsed chat file. messages: dicts with i (index in the chat, as
    SillyTavern numbers them), name, user, hidden, text, persona, avatar (a
    character's message: its card; group chats record it per message, a
    card's own chats are in its folder), ts. Its chat id is `rel`."""
    __slots__ = ("rel", "integrity", "main_chat", "note", "messages", "mtime", "size", "segs")


_chat_cache: "OrderedDict[str, tuple]" = OrderedDict()
_chat_cache_lock = threading.Lock()


def read_chat(rel: str) -> Optional[ChatFile]:
    """Parse a chat file (cached while its mtime and size are unchanged)."""
    path = _abs(rel)
    if not path:
        return None
    try:
        st = os.stat(path)
    except OSError:
        return None
    with _chat_cache_lock:
        hit = _chat_cache.get(rel)
        if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
            _chat_cache.move_to_end(rel)
            return hit[2]
    cf = ChatFile()
    cf.rel, cf.mtime, cf.size = rel, st.st_mtime, st.st_size
    cf.integrity, cf.main_chat, cf.note, cf.messages, cf.segs = None, None, "", [], {}
    folder_card = f"{rel.split('/')[1]}.png" if rel.startswith("chats/") else ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for n, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(obj, dict):
                    continue
                if n == 0 and "mes" not in obj:
                    meta = obj.get("chat_metadata") if isinstance(obj.get("chat_metadata"), dict) else {}
                    cf.integrity = meta.get("integrity") or None
                    cf.main_chat = meta.get("main_chat") or None
                    cf.note = str(meta.get("note_prompt") or "").strip()
                    continue
                if "mes" not in obj:
                    continue
                cf.messages.append({
                    "i": len(cf.messages),
                    "name": str(obj.get("name") or ""),
                    "user": bool(obj.get("is_user")),
                    "hidden": bool(obj.get("is_system")),
                    "text": str(obj.get("mes") or ""),
                    "persona": _persona_of(obj) if obj.get("is_user") else "",
                    "avatar": "" if obj.get("is_user") else str(obj.get("original_avatar") or folder_card),
                    "ts": _parse_ts(obj.get("send_date")),
                })
    except OSError as e:
        log(f"couldn't read {rel}: {e}", "WARN")
        return None
    with _chat_cache_lock:
        _chat_cache[rel] = (cf.mtime, cf.size, cf)
        while len(_chat_cache) > 48:
            _chat_cache.popitem(last=False)
    return cf


_NAME_DATE_RE = re.compile(r"(?:^|\s-\s)(\d{4})-(\d{1,2})-(\d{1,2}) ?@(\d{1,2})h ?(\d{1,2})m ?(\d{1,2})s(?: ?\d{1,3}ms)?$")
_BRANCH_END_RE = re.compile(r" - Branch #(\d+)$")
_BRANCH_START_RE = re.compile(r"^Branch #(\d+) - ")
_PREVIEW_CAP = 300


def _stem_parts(stem: str) -> tuple[str, str, Optional[datetime], bool]:
    """A chat file's name taken apart: (the text before its date, "Branch #N"
    or "", the date, whether the date is the file's own). SillyTavern names
    chats "<card name> - <date>", checkpoints "<name> - <date>", and branches
    "Branch #N - <date>" (older) or "<parent's name> - Branch #N", whose date
    is the parent's. The date is the browser's local time, so it's returned
    naive; older ones read "2025-9-4 @03h 21m 44s 317ms"."""
    s = re.sub(r" imported$", "", stem)
    branch, own = "", True
    m = _BRANCH_END_RE.search(s)
    if m:
        branch, own, s = f"Branch #{m[1]}", False, s[:m.start()]
    m = _BRANCH_START_RE.match(s)
    if m:
        branch, s = branch or f"Branch #{m[1]}", s[m.end():]
    when = None
    m = _NAME_DATE_RE.search(s)
    if m:
        try:
            when = datetime(*(int(g) for g in m.groups()))
        except ValueError:
            pass
        s = s[:m.start()]
    return s.strip(), branch, when, own and when is not None


def _created(cf: ChatFile) -> Optional[float]:
    """When a branch named after its parent was made: when it parted from
    the parent (its first message the parent doesn't have). Other chats have
    the date in their name, or none."""
    _, _, _, own = _stem_parts(os.path.splitext(os.path.basename(cf.rel))[0])
    if own or not cf.main_chat:
        return None
    parent = read_chat(f"{os.path.dirname(cf.rel)}/{cf.main_chat}.jsonl")
    if not parent:
        return None
    pm = parent.messages
    for m in cf.messages:
        p = pm[m["i"]] if m["i"] < len(pm) else None
        if not p or p["text"] != m["text"] or p["ts"] != m["ts"]:
            return m["ts"]
    stamps = [m["ts"] for m in cf.messages if m["ts"]]
    return stamps[-1] if stamps else None


def _last_message(cf: ChatFile) -> dict:
    """The chat's last message with text (an empty one is a reply that
    failed or was stopped), plain, to tell chats and branches apart."""
    for m in reversed(cf.messages):
        text = " ".join(_TAG_RE.sub("", m["text"]).replace("*", "").split())
        if text:
            return {"name": m["name"], "text": text[:_PREVIEW_CAP] + ("…" if len(text) > _PREVIEW_CAP else "")}
    return {}


def _summarize(cf: ChatFile, group: Optional[dict]) -> dict:
    """The chats-table row for a parsed chat file."""
    personas: dict = {}
    speakers: list = []
    for m in cf.messages:
        if m["hidden"]:
            continue
        if m["user"]:
            p = personas.setdefault(m["persona"], {"n": 0, "name": m["name"]})
            p["n"] += 1
            p["name"] = m["name"] or p["name"]
        if m["name"] and m["name"] not in speakers:
            speakers.append(m["name"])
    stem = os.path.splitext(os.path.basename(cf.rel))[0]
    stamps = [m["ts"] for m in cf.messages if m["ts"]]
    return {
        "chat_id": cf.rel, "rel": cf.rel, "integrity": cf.integrity,
        "group_id": group.get("id") if group else None,
        "group_name": group.get("name") if group else None,
        "main_chat": cf.main_chat,
        "title": f"{group.get('name') or 'Group'}: {stem}" if group else stem,
        "first_ts": stamps[0] if stamps else cf.mtime,
        "last_ts": stamps[-1] if stamps else cf.mtime,
        "n": len(cf.messages),
        "personas": json.dumps(personas, ensure_ascii=False),
        "speakers": json.dumps(speakers[:30], ensure_ascii=False),
        "mtime": cf.mtime, "size": cf.size,
        "created_ts": _created(cf) or (stamps[0] if stamps else cf.mtime),
        "last_msg": json.dumps(_last_message(cf), ensure_ascii=False),
    }


_CHAT_COLS = ("chat_id", "rel", "integrity", "group_id", "group_name", "main_chat", "title", "first_ts", "last_ts",
              "n", "personas", "speakers", "mtime", "size", "created_ts", "last_msg")


# Old chat id -> new, for chats renamed while the bridge runs (get_chat).
_renamed: dict = {}


def _store_chat(row: dict):
    """Insert or refresh a chats row. A file new to the bridge whose
    integrity id belongs to exactly one known chat whose file is gone is
    that chat renamed, and takes over its memory."""
    with _db_lock:
        known = _q1("SELECT 1 FROM chats WHERE chat_id = ?", (row["chat_id"],))
        if not known and row.get("integrity"):
            gone = [r["chat_id"] for r in _q("SELECT chat_id, rel FROM chats WHERE integrity = ?", (row["integrity"],))
                    if not os.path.exists(_abs(r["rel"]) or "")]
            if len(gone) == 1:
                _rename_chat(gone[0], row["chat_id"])
        cols = ", ".join(_CHAT_COLS)
        marks = ", ".join("?" * len(_CHAT_COLS))
        updates = ", ".join(f"{c} = excluded.{c}" for c in _CHAT_COLS if c != "chat_id")
        _x(f"INSERT INTO chats ({cols}) VALUES ({marks}) ON CONFLICT(chat_id) DO UPDATE SET {updates}",
           tuple(row[c] for c in _CHAT_COLS))


def _rename_chat(old: str, new: str):
    with _db_lock:
        conn = _db()
        conn.execute("BEGIN")
        try:
            conn.execute("DELETE FROM chats WHERE chat_id = ?", (old,))
            for table, cols in (("encounters", ("chat_id",)), ("binding_chats", ("chat_id",)), ("plans", ("chat_id",)),
                                ("links", ("target_chat", "source_chat")),
                                ("exclusions", ("target_chat", "source_chat")), ("checks", ("chat_id",))):
                for col in cols:
                    conn.execute(f"UPDATE OR IGNORE {table} SET {col} = ? WHERE {col} = ?", (new, old))
                    conn.execute(f"DELETE FROM {table} WHERE {col} = ?", (old,))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    _renamed[old] = new
    log(f"chat {old} was renamed to {new}")


def scan_chat(rel: str, group: Optional[dict] = None) -> Optional[dict]:
    """The chats row for a file, re-read only when it changed."""
    path = _abs(rel)
    if not path:
        return None
    try:
        st = os.stat(path)
    except OSError:
        return None
    row = _q1("SELECT * FROM chats WHERE chat_id = ?", (rel,))
    if row and row["mtime"] == st.st_mtime and row["size"] == st.st_size and row["last_msg"] is not None:
        return dict(row)
    cf = read_chat(rel)
    if not cf:
        return None
    if group is None and rel.startswith("group chats/"):
        stem = os.path.splitext(os.path.basename(rel))[0]
        group = next((g for g in list_groups() if stem in g["chats"]), None)
    data = _summarize(cf, group)
    _store_chat(data)
    return dict(_q1("SELECT * FROM chats WHERE chat_id = ?", (data["chat_id"],)))


_missing: dict = {}  # chat_id -> when a rescan last failed to find it


def get_chat(chat_id: str, rescan_for: Optional[str] = None) -> Optional[dict]:
    """The chats row for an id, refreshed from its file (read for the first
    time if need be: a chat's id is its file's path). When the file has
    moved (renamed chat), the character `rescan_for`'s chats are scanned to
    find it again, at most every 10 minutes per chat."""
    row = _q1("SELECT * FROM chats WHERE chat_id = ?", (chat_id,))
    fresh = scan_chat(row["rel"] if row else chat_id)
    if fresh and fresh["chat_id"] == chat_id:
        return fresh
    if rescan_for and time.time() - _missing.get(chat_id, 0) > 600:
        chats = character_chats(rescan_for)
        new_id = _renamed.get(chat_id, chat_id)
        for c in chats:
            if c["chat_id"] == new_id:
                _missing.pop(chat_id, None)
                return c
        _missing[chat_id] = time.time()
    return dict(row) if row else None


_group_cache: dict = {"mtime": None, "groups": [], "checked": 0.0}


def list_groups() -> list[dict]:
    """SillyTavern's groups: id, name, members (card avatars), chats (file
    stems). Re-checked on disk at most every few seconds: listing a
    character's chats asks for every one of their group chats."""
    base = st_dir()
    if not base:
        return []
    if time.time() - _group_cache["checked"] < 5:
        return _group_cache["groups"]
    _group_cache["checked"] = time.time()
    gdir = os.path.join(base, "groups")
    try:
        names = sorted(os.listdir(gdir))
        stamp = (os.stat(gdir).st_mtime, tuple((n, os.stat(os.path.join(gdir, n)).st_mtime) for n in names))
    except OSError:
        return []
    if _group_cache["mtime"] == stamp:
        return _group_cache["groups"]
    groups = []
    for n in names:
        if not n.endswith(".json"):
            continue
        try:
            with open(os.path.join(gdir, n), "r", encoding="utf-8") as f:
                g = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(g, dict) and g.get("id"):
            groups.append({
                "id": str(g["id"]), "name": str(g.get("name") or ""),
                "members": [str(m) for m in g.get("members") or []],
                "chats": [str(c) for c in g.get("chats") or []],
            })
    _group_cache.update(mtime=stamp, groups=groups)
    return groups


def _group(group_id: str) -> Optional[dict]:
    return next((g for g in list_groups() if g["id"] == str(group_id)), None)


_card_cache: dict = {}  # avatar -> (mtime, name)


def _card_name(path: str) -> Optional[str]:
    """The character name stored in a card PNG's chara/ccv3 text chunk."""
    try:
        with open(path, "rb") as f:
            if f.read(8) != b"\x89PNG\r\n\x1a\n":
                return None
            while True:
                head = f.read(8)
                if len(head) < 8:
                    return None
                length, ctype = struct.unpack(">I4s", head)
                if ctype == b"tEXt":
                    key, _, val = f.read(length).partition(b"\0")
                    f.seek(4, 1)
                    if key in (b"chara", b"ccv3"):
                        try:
                            card = json.loads(base64.b64decode(val))
                            name = ((card.get("data") or card).get("name") or "").strip()
                            if name:
                                return name
                        except (ValueError, AttributeError):
                            continue
                elif ctype == b"IEND":
                    return None
                else:
                    f.seek(length + 4, 1)
    except OSError:
        return None


def list_cards() -> list[dict]:
    """Every character card in SillyTavern: avatar and name."""
    base = st_dir()
    if not base:
        return []
    cdir = os.path.join(base, "characters")
    try:
        names = os.listdir(cdir)
    except OSError:
        return []
    out = []
    for n in names:
        if not n.lower().endswith(".png"):
            continue
        p = os.path.join(cdir, n)
        try:
            mtime = os.stat(p).st_mtime
        except OSError:
            continue
        hit = _card_cache.get(n)
        if not hit or hit[0] != mtime:
            hit = (mtime, _card_name(p) or _stem(n))
            _card_cache[n] = hit
        out.append({"avatar": n, "name": hit[1]})
    out.sort(key=lambda c: (c["name"].lower(), c["avatar"]))
    return out


def list_personas() -> list[dict]:
    """SillyTavern's personas: avatar, name, title, the start of the
    description with {{user}} filled in and its brackets dropped."""
    base = st_dir()
    if not base:
        return []
    try:
        with open(os.path.join(base, "settings.json"), "r", encoding="utf-8") as f:
            pu = (json.load(f).get("power_user") or {})
    except (OSError, ValueError):
        return []
    descs = pu.get("persona_descriptions") or {}
    out = []
    for avatar, name in (pu.get("personas") or {}).items():
        d = descs.get(avatar) if isinstance(descs.get(avatar), dict) else {}
        name = str(name or "")
        desc = " ".join(str(d.get("description") or "").split())
        if desc.startswith("[") and desc.endswith("]") and "[" not in desc[1:] and "]" not in desc[:-1]:
            desc = desc[1:-1].strip()
        desc = re.sub(r"\{\{user\}\}", lambda _: name or "{{user}}", desc, flags=re.IGNORECASE)
        out.append({"avatar": avatar, "name": name, "title": str(d.get("title") or "").strip(),
                    "description": desc[:_PREVIEW_CAP] + ("…" if len(desc) > _PREVIEW_CAP else "")})
    out.sort(key=lambda p: (p["name"].lower(), p["avatar"]))
    return out


def _persona_name(avatar: str) -> str:
    return next((p["name"] for p in list_personas() if p["avatar"] == avatar), "") or _stem(avatar)


# ---------------------------------------------------------------------------
# Characters and cards
# ---------------------------------------------------------------------------

def ensure_card(avatar: str, name: str = "") -> str:
    """The character a card belongs to, creating both on first sight."""
    name = (name or "").strip() or _stem(avatar)
    with _db_lock:
        row = _q1("SELECT character_id, name FROM cards WHERE avatar = ?", (avatar,))
        if row:
            if name != row["name"]:
                _x("UPDATE cards SET name = ? WHERE avatar = ?", (name, avatar))
                _x("UPDATE characters SET name = ? WHERE id = ? AND id = ?", (name, row["character_id"], avatar))
            return row["character_id"]
        _x("INSERT OR IGNORE INTO characters (id, name, created_ts) VALUES (?, ?, ?)", (avatar, name, time.time()))
        _x("INSERT INTO cards (avatar, character_id, name) VALUES (?, ?, ?)", (avatar, avatar, name))
        return avatar


def character_cards(cid: str) -> list[dict]:
    return [dict(r) for r in _q("SELECT avatar, name FROM cards WHERE character_id = ? ORDER BY avatar = ? DESC, avatar",
                                (cid, cid))]


def character_name(cid: str) -> str:
    row = _q1("SELECT name FROM characters WHERE id = ?", (cid,))
    return row["name"] if row else _stem(cid)


def character_chats(cid: str) -> list[dict]:
    """Every chat of the character's cards on disk: their own chats and the
    group chats of groups they're in."""
    base = st_dir()
    if not base:
        return []
    cards = [c["avatar"] for c in character_cards(cid)] or [cid]
    rows, seen = [], set()
    for avatar in cards:
        folder = os.path.join(base, "chats", _stem(avatar))
        try:
            files = sorted(f for f in os.listdir(folder) if f.endswith(".jsonl"))
        except OSError:
            files = []
        for f in files:
            row = scan_chat(f"chats/{_stem(avatar)}/{f}")
            if row and row["chat_id"] not in seen:
                seen.add(row["chat_id"])
                rows.append(row)
    for g in list_groups():
        if not any(a in g["members"] for a in cards):
            continue
        for stem in g["chats"]:
            row = scan_chat(f"group chats/{stem}.jsonl", g)
            if row and row["chat_id"] not in seen:
                seen.add(row["chat_id"])
                rows.append(row)
    return rows


def _move_character_rows(old: str, new: str, chat_ids: Optional[set] = None):
    """Move a character's memory rows to another character: all of them, or
    those of the chats in `chat_ids`."""
    def cond(col):
        if chat_ids is None:
            return "", ()
        ids = list(chat_ids) or [""]
        return f" AND {col} IN ({','.join('?' * len(ids))})", tuple(ids)

    with _db_lock:
        conn = _db()
        conn.execute("BEGIN")
        try:
            for table, col in (("encounters", "chat_id"), ("links", "target_chat"),
                               ("exclusions", "target_chat"), ("checks", "chat_id")):
                extra, args = cond(col)
                conn.execute(f"UPDATE OR IGNORE {table} SET character_id = ? WHERE character_id = ?{extra}",
                             (new, old) + args)
                conn.execute(f"DELETE FROM {table} WHERE character_id = ?{extra}", (old,) + args)
            if chat_ids is None:
                conn.execute("UPDATE OR IGNORE bindings SET character_id = ? WHERE character_id = ?", (new, old))
                conn.execute("DELETE FROM bindings WHERE character_id = ?", (old,))
                conn.execute("UPDATE OR IGNORE binding_chats SET character_id = ? WHERE character_id = ?", (new, old))
                conn.execute("DELETE FROM binding_chats WHERE character_id = ?", (old,))
            else:
                extra, args = cond("chat_id")
                conn.execute(f"DELETE FROM binding_chats WHERE character_id = ?{extra}", (old,) + args)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def add_card(cid: str, avatar: str) -> tuple[bool, str]:
    """Make another card part of this character (the same person on several
    cards). Its memory moves over. Only a card that is a character on its
    own can be added."""
    if avatar == cid:
        return False, "that card is this character"
    names = {c["avatar"]: c["name"] for c in list_cards()}
    if avatar not in names:
        return False, "no such card in SillyTavern"
    if not _q1("SELECT 1 FROM characters WHERE id = ?", (cid,)):
        if cid not in names:
            return False, "no such character"
        cid = ensure_card(cid, names[cid])
    old = ensure_card(avatar, names[avatar])
    if old == cid:
        return False, "that card is already part of this character"
    if len(character_cards(old)) > 1:
        return False, "that card belongs to a character with several cards; remove it there first"
    _move_character_rows(old, cid)
    with _db_lock:
        _x("UPDATE cards SET character_id = ? WHERE avatar = ?", (cid, avatar))
        _x("DELETE FROM characters WHERE id = ?", (old,))
    log(f"card {avatar} is now part of {character_name(cid)}")
    return True, ""


def remove_card(avatar: str) -> tuple[bool, str]:
    """Make a card a character of its own again. The memory of its chats
    goes with it; it leaves the persona bindings of the character."""
    row = _q1("SELECT character_id, name FROM cards WHERE avatar = ?", (avatar,))
    if not row:
        return False, "unknown card"
    cid = row["character_id"]
    if cid == avatar:
        return False, "the character's first card can't be removed"
    folder_chats = set()
    for c in character_chats(cid):
        if c["rel"].startswith(f"chats/{_stem(avatar)}/"):
            folder_chats.add(c["chat_id"])
        elif c.get("group_id"):
            g = _group(c["group_id"])
            if g and avatar in g["members"] and not any(
                    a["avatar"] in g["members"] for a in character_cards(cid) if a["avatar"] != avatar):
                folder_chats.add(c["chat_id"])
    with _db_lock:
        _x("INSERT OR IGNORE INTO characters (id, name, created_ts) VALUES (?, ?, ?)", (avatar, row["name"], time.time()))
        _x("UPDATE cards SET character_id = ? WHERE avatar = ?", (avatar, avatar))
    _move_character_rows(cid, avatar, folder_chats)
    return True, ""


# ---------------------------------------------------------------------------
# Recaps
# ---------------------------------------------------------------------------

def _pov(cid: str, row: dict) -> tuple[str, str]:
    """(avatar, name) of the character's card in a chat: the card whose
    folder it's in, or the character's card among the group's members."""
    cards = character_cards(cid) or [{"avatar": cid, "name": character_name(cid)}]
    for c in cards:
        if row["rel"].startswith(f"chats/{_stem(c['avatar'])}/"):
            return c["avatar"], c["name"]
    g = _group(row["group_id"]) if row.get("group_id") else None
    for c in cards:
        if g and c["avatar"] in g["members"]:
            return c["avatar"], c["name"]
    return cards[0]["avatar"], cards[0]["name"]


class _Who(NamedTuple):
    """A character as one chat's recaps see them: pov, the card a part they're
    in is recapped from, and its name; their cards and names, to find their
    messages (by the card, or the name where a message doesn't record it)."""
    pov: str
    name: str
    avatars: frozenset
    names: frozenset


def _who(cid: str, row: dict) -> _Who:
    cards = character_cards(cid) or [{"avatar": cid, "name": character_name(cid)}]
    pov, name = _pov(cid, row)
    return _Who(pov, name, frozenset(c["avatar"] for c in cards), frozenset(c["name"] for c in cards))


def _speaks(who: _Who, msgs: list[dict]) -> bool:
    return any(not m["user"] and (m["avatar"] in who.avatars or m["name"] in who.names) for m in msgs)


def _segments(cf: ChatFile, who: _Who, upto: Optional[int] = None) -> list[dict]:
    """The chat's parts: SEGMENT_SIZE messages each from the start, cut at
    `upto` (a message index) when given. Hidden and empty messages are left
    out of the text; a part with none left is skipped. Each message is
    headed by its index in the chat ("#52 Name: ..."), so a recap can mark
    where its scenes start. A part the character has a message in is
    recapped from their card (pov), any other as a plain account (pov ""),
    which everyone who isn't in it shares: whoever reads it was in other
    parts or none. Keyed by pov and text."""
    cache_key = (who, upto)
    if cache_key in cf.segs:
        return cf.segs[cache_key]
    n = len(cf.messages) if upto is None else max(0, min(upto, len(cf.messages)))
    out = []
    for start in range(0, n, SEGMENT_SIZE):
        end = min(start + SEGMENT_SIZE, n)
        part = [(i, m) for i, m in enumerate(cf.messages[start:end], start) if not m["hidden"] and m["text"].strip()]
        if not part:
            continue
        text = "\n\n".join(f"#{i} {m['name'] or ('User' if m['user'] else 'Character')}: {_clean(m['text'])}"
                           for i, m in part)
        pov = who.pov if _speaks(who, [m for _, m in part]) else ""
        out.append({"start": start, "end": end, "text": text, "pov": pov, "first": part[0][0],
                    "key": _hash(f"{pov}\n{text}"), "full": end - start == SEGMENT_SIZE})
    cf.segs[cache_key] = out
    return out


def _marked(seg: dict, recap: str, name: Optional[str]) -> str:
    """A part's recap (or a scene of it) where it's read with the
    character's others: an account of a part they weren't in says so."""
    return f"({name} wasn't there.) {recap}" if name and not seg["pov"] else recap


# Where a scene starts in a part's recap: "[#52]" opening a paragraph.
_SCENE_MARK = re.compile(r"^[ \t]*\[#\s*(\d+)\s*\][ \t]*", re.M)


def _scenes(seg: dict, recap: str) -> list[dict]:
    """A part's recap as its scenes, [{start, end, text}]: start is the
    message a scene starts at, as the recap marks it ("[#52]" opening a
    paragraph), end the one after its last. The first scene starts with the
    part; a mark outside the part or out of order joins its text to the
    scene before. A recap without marks (edited without them) is one scene."""
    marks = list(_SCENE_MARK.finditer(recap))
    pieces = [(None, recap[:marks[0].start()] if marks else recap)]
    pieces += [(int(m.group(1)), recap[m.end():marks[k + 1].start() if k + 1 < len(marks) else len(recap)])
               for k, m in enumerate(marks)]
    scenes = []
    for n, text in pieces:
        text = text.strip()
        if not text:
            continue
        if not scenes:
            scenes.append({"start": seg["start"], "text": text})
        elif n is not None and scenes[-1]["start"] < n < seg["end"]:
            scenes.append({"start": n, "text": text})
        else:
            scenes[-1]["text"] += "\n\n" + text
    scenes = scenes or [{"start": seg["start"], "text": recap.strip()}]
    for k, s in enumerate(scenes):
        s["end"] = scenes[k + 1]["start"] if k + 1 < len(scenes) else seg["end"]
    return scenes


def _plain(recap: str) -> str:
    """A part's recap without its scene marks, as the model reads it."""
    return _SCENE_MARK.sub("", recap).strip()


def _units(segs: list[dict], have: dict) -> list[dict]:
    """The scenes of these parts in order, what a chat is divided into
    chapters by and its synopsis or chapters are written from: [{seg,
    start, end, text}], one with text None for a part not recapped yet."""
    out = []
    for s in segs:
        r = have.get(s["key"])
        if r:
            out += [{"seg": s, **x} for x in _scenes(s, r["recap"])]
        else:
            out.append({"seg": s, "start": s["start"], "end": s["end"], "text": None})
    return out


def _inchat_upto(cf: ChatFile, floor: int) -> int:
    """Where the recap of a chat's earlier part ends, for a CLI session whose
    transcript starts at message `floor`: at the end of the part the floor
    falls in. So it's made of whole parts, the ones the chat's own recap and
    its branches use, and a floor that moves within a part needs no new
    recap; the last part overlaps the start of the transcript. While that
    part isn't complete (the chat doesn't reach its end yet), at the floor."""
    end = -(-floor // SEGMENT_SIZE) * SEGMENT_SIZE
    return end if end <= len(cf.messages) else floor


def _segment_recaps(keys: list[str]) -> dict:
    if not keys:
        return {}
    rows = []
    for i in range(0, len(keys), 500):
        chunk = keys[i:i + 500]
        rows += _q(f"SELECT key, recap, edited FROM segments WHERE key IN ({','.join('?' * len(chunk))})", chunk)
    return {r["key"]: dict(r) for r in rows}


def _encounter(cid: str, chat_id: str) -> dict:
    row = _q1("SELECT * FROM encounters WHERE character_id = ? AND chat_id = ?", (cid, chat_id))
    return dict(row) if row else {"character_id": cid, "chat_id": chat_id, "excluded": 0, "chapters": None}


def _ensure_encounter(cid: str, chat_id: str):
    _x("INSERT OR IGNORE INTO encounters (character_id, chat_id) VALUES (?, ?)", (cid, chat_id))


_keys_cache: "OrderedDict[tuple, list]" = OrderedDict()


def recap_state(cid: str, chat_id: str, upto: Optional[int] = None, row: Optional[dict] = None) -> dict:
    """How much of a chat is recapped for a character: parts, done, missing,
    and there (the parts they're in). The parts are cached per version of
    the file, so listing a character's chats doesn't parse every file again."""
    row = row or get_chat(chat_id, rescan_for=cid)
    if not row:
        return {"parts": 0, "done": 0, "missing": 0, "there": 0, "available": False}
    who = _who(cid, row)
    ck = (row["rel"], row.get("mtime"), row.get("size"), who, upto)
    parts = _keys_cache.get(ck)
    if parts is None:
        cf = read_chat(row["rel"])
        if not cf:
            return {"parts": 0, "done": 0, "missing": 0, "there": 0, "available": False}
        parts = [(s["key"], bool(s["pov"])) for s in _segments(cf, who, upto)]
        _keys_cache[ck] = parts
        while len(_keys_cache) > 5000:
            _keys_cache.popitem(last=False)
    have = _segment_recaps([k for k, _ in parts])
    done = sum(1 for k, _ in parts if k in have)
    return {"parts": len(parts), "done": done, "missing": len(parts) - done,
            "there": sum(1 for _, there in parts if there), "available": True}


def _chat_people(cf: ChatFile, char_name: str) -> tuple[str, list[str]]:
    """(the user's name in the chat, the other speakers)."""
    users = [m["name"] for m in cf.messages if m["user"] and not m["hidden"] and m["name"]]
    user = max(set(users), key=users.count) if users else "the user"
    others = []
    for m in cf.messages:
        if not m["user"] and not m["hidden"] and m["name"] and m["name"] != char_name and m["name"] not in others:
            others.append(m["name"])
    return user, others


# A part the character is in.
_RECAP_PROMPT = """You keep {char}'s memory of a roleplay. Below is part {part} of {total} of a chat{where}, each message headed by its number. {user} is the user's character.{others}
{before}
Write a recap of this part as {char} would remember it:
- What happened, in order: what {char} did, saw and learned; who was there; what was said that matters; promises, revelations, decisions and how things stand at the end of the part.
- One paragraph per scene. A new scene starts where the story moves to another place or time, or turns to a new event; a part is often one scene, sometimes a few. Open each paragraph with the number of the message its scene starts at, in brackets: the first one [#{first}].
- Past tense, third person, plain prose, at most {words} words in all. Use names, not "the user".
- Only events. Don't describe anyone's looks or personality, and don't restate the setting or world lore unless something happened to it.
- {char}'s own messages are the ones under {char}'s name. {char} was only where the text puts {char}: leave out what {char} wasn't there for and couldn't know, and don't place {char} in a scene the text doesn't.
- Write only the recap: no heading, no preamble, no commentary.

=== CHAT PART {part} ===
{text}
=== END OF PART ===
"""

# A part the character isn't in: an account from no one's point of view,
# shared by everyone who wasn't there.
_ACCOUNT_PROMPT = """You keep the record of a roleplay. Below is part {part} of {total} of a chat{where}, each message headed by its number. {user} is the user's character.{others}
{before}
Write a recap of this part as a plain account of what happened:
- What happened, in order: who did what, saw and learned; who was there; what was said that matters; promises, revelations, decisions and how things stand at the end of the part.
- One paragraph per scene. A new scene starts where the story moves to another place or time, or turns to a new event; a part is often one scene, sometimes a few. Open each paragraph with the number of the message its scene starts at, in brackets: the first one [#{first}].
- Past tense, third person, plain prose, at most {words} words in all. Use names, not "the user".
- Only events. Don't describe anyone's looks or personality, and don't restate the setting or world lore unless something happened to it.
- Put people only where the text puts them: don't add anyone to a scene.
- Write only the recap: no heading, no preamble, no commentary.

=== CHAT PART {part} ===
{text}
=== END OF PART ===
"""

# Synopses and chapters are written as the character remembers the chat
# (whose: from their point of view), or as a plain account of a chat they
# weren't in at all. absent: the note on the scenes they weren't in.
_SYNOPSIS_PROMPT = """Below are recaps of the consecutive scenes of a roleplay chat{where}{whose}. {user} is the user's character.{absent}

Write one synopsis of the whole chat from them, in about {words} words, {as_whom}:
- What happened, in order: the key events, {deeds}, revelations, decisions and promises, and how things stood at the end.
- Keep names, places and turning points; drop moment-to-moment detail first.
- Past tense, third person, plain prose. Use names, not "the user".
- Write only the synopsis: no heading, no preamble, no commentary.

{recaps}
"""

_PLAN_PROMPT = """Below are recaps of a roleplay chat{where}, part by part, each part about {size} messages and recapped scene by scene: a scene starts with the number of its first message, in brackets. {user} is the user's character.{earlier}

Divide the chat into chapters: stretches of the story that each cover one event or episode, such as a journey, a fight, a festival, a night together or an investigation. Start a new chapter where the story moves on: a new place or day, a new goal or conflict, a time skip. A chapter is one or more whole scenes, and can start in the middle of a part. Most chapters span about 40 to 200 messages, but follow the story. Give each chapter a short title, at most eight words, naming its event.

Answer with only a JSON object: {{"chapters": [{{"first_message": <the bracketed number of its first scene>, "title": "..."}}]}}, in order. The first chapter starts at message {first}.

{recaps}
"""

_CHAPTER_PROMPT = """Below are recaps of the consecutive scenes of one chapter of a roleplay chat{where}{whose}: chapter {number} of {total}, "{title}". {user} is the user's character.{before}{absent}

Write one recap of this chapter from them, in about {words} words, {as_whom}:
- What happened, in order: the key events, {deeds}, revelations, decisions and promises, and how things stood at the end of the chapter.
- Keep names, places and turning points; drop moment-to-moment detail first.
- Past tense, third person, plain prose. Use names, not "the user".
- Write only the recap: no heading, no preamble, no commentary.

{recaps}
"""


def _voice(char: Optional[str], segs: list[dict]) -> dict:
    """The fields of _SYNOPSIS_PROMPT and _CHAPTER_PROMPT that say whose
    memory it is: `char`'s, or a plain account (char None: a chat they
    weren't in at all). segs: the parts the scenes are from."""
    if not char:
        return {"whose": "", "absent": "", "as_whom": "as a plain account of what happened", "deeds": "who did what"}
    absent = (f" {char} wasn't in the scenes marked so: they're plain accounts. Keep what happened in them as events "
              f"{char} wasn't part of, and don't put {char} in them.") if any(not s["pov"] for s in segs) else ""
    return {"whose": f", from {char}'s point of view", "absent": absent, "as_whom": f"as {char} would remember it",
            "deeds": f"what {char} did, saw and learned"}


def _build_recaps(job: dict) -> None:
    """Recap every missing part of a chat for a character, then write what
    other chats get of it when the chat is recapped whole."""
    cid, chat_id, upto = job["cid"], job["chat_id"], job.get("upto")
    row = get_chat(chat_id, rescan_for=cid)
    cf = read_chat(row["rel"]) if row else None
    if not cf:
        job["error"] = "chat file not found"
        return
    who = _who(cid, row)
    char = who.name
    user, others = _chat_people(cf, char)
    _, everyone = _chat_people(cf, "")
    where = f' ("{row["title"]}")' if row.get("title") else ""
    segs = _segments(cf, who, upto)
    have = _segment_recaps([s["key"] for s in segs])
    missing = [s for s in segs if s["key"] not in have]
    job["stage"], job["total"], job["done"] = "parts", len(missing), 0
    _ensure_encounter(cid, chat_id)
    prev = None
    for idx, s in enumerate(segs):
        if job.get("cancel"):
            return
        if s["key"] in have:
            prev = _plain(have[s["key"]]["recap"])
            continue
        before = f"\nWhat happened before this part, as recapped earlier:\n{prev}\n" if prev else ""
        common = dict(part=idx + 1, total=len(segs), user=user, where=where, before=before, first=s["first"],
                      words=250 if s["full"] else 180, text=s["text"])
        if s["pov"]:
            prompt = _RECAP_PROMPT.format(char=char, others=(f" Others in the chat: {', '.join(others[:12])}."
                                                             if others else ""), **common)
            label = f"Character Memory: recap {char} — {row['title']} ({idx + 1}/{len(segs)})"
        else:
            prompt = _ACCOUNT_PROMPT.format(others=(f" Characters in the chat: {', '.join(everyone[:12])}."
                                                    if everyone else ""), **common)
            label = f"Character Memory: account (without {char}) — {row['title']} ({idx + 1}/{len(segs)})"
        recap = _call_sonnet(prompt, timeout=RECAP_TIMEOUT, label=label)
        if not recap or not recap.strip():
            job["error"] = "the recap call failed (see the History tab)"
            _x("UPDATE encounters SET error = ? WHERE character_id = ? AND chat_id = ?", (job["error"], cid, chat_id))
            return
        recap = recap.strip()
        _x("INSERT OR REPLACE INTO segments (key, pov, recap, edited, created_ts) VALUES (?, ?, ?, 0, ?)",
           (s["key"], s["pov"], recap, time.time()))
        have[s["key"]] = {"recap": recap, "edited": 0}
        prev = _plain(recap)
        job["done"] += 1
    _x("UPDATE encounters SET built_ts = ?, error = NULL WHERE character_id = ? AND chat_id = ?",
       (time.time(), cid, chat_id))
    if upto is None and not job.get("cancel"):
        _write_passed_on(job, cid, row, cf, who, user)


def _scene_list(units: list[dict], char: Optional[str] = None) -> str:
    """Scene recaps for a synopsis or chapter prompt: "Messages 80–95:" and
    the recap; with `char`, the scenes of parts they aren't in are marked."""
    return "\n\n".join(f"Messages {u['start']}–{u['end'] - 1}"
                       + (f" ({char} wasn't there)" if char and not u["seg"]["pov"] else "") + ":\n"
                       + (u["text"] or "(not recapped)") for u in units)


def _plan_list(units: list[dict], numbers: dict) -> str:
    """Scene recaps for the division call, under their parts: "Part 3
    (messages 80–119):", then "[#80] ..." for each scene. numbers: each
    part's number, by its start."""
    out, seg = [], None
    for u in units:
        if u["seg"] is not seg:
            seg = u["seg"]
            out.append(f"\nPart {numbers[seg['start']]} (messages {u['start']}–{seg['end'] - 1}):")
        out.append(f"[#{u['start']}] {u['text'] or '(not recapped)'}")
    return "\n".join(out).strip()


def _write_passed_on(job: dict, cid: str, row: dict, cf: ChatFile, who: _Who, user: str):
    """Write what another chat gets of this chat in the current mode, once
    every part is recapped: the synopsis, or the chapters (dividing the chat
    first). What's written already, or short enough as it is, is kept. A
    chat the character isn't in at all is written as a plain account."""
    m = pass_mode()
    where = f' ("{row["title"]}")' if row.get("title") else ""
    if m == "chapters" and not _plan_chapters(job, row, cf, who, user):
        return
    mem = passed_on(cid, row, cf, m=m)
    secs = mem["sections"]
    char = who.name if mem["there"] else None
    todo = [s for s in secs if not s["ready"] and not s["parts_missing"]]
    job["stage"], job["total"], job["done"] = ("chapters" if m == "chapters" else "synopsis"), len(todo), 0
    whose = who.name if char else f"account (without {who.name})"
    for s in todo:
        if job.get("cancel"):
            return
        voice = _voice(char, [u["seg"] for u in s["units"]])
        if not s["title"]:
            # The synopsis, which is also what chapters mode passes on of a
            # chat too short for chapters.
            prompt = _SYNOPSIS_PROMPT.format(where=where, user=user, words=s["words"],
                                             recaps=_scene_list(s["units"], char), **voice)
            label = f"Character Memory: synopsis {whose} — {row['title']}"
        else:
            n = secs.index(s)
            title = s["title"]
            prev = secs[n - 1]["title"] if n else None
            prompt = _CHAPTER_PROMPT.format(
                where=where, user=user, words=s["words"], number=n + 1, total=len(secs), title=title,
                before=f' The chapter before it was "{prev}".' if prev else "",
                recaps=_scene_list(s["units"], char), **voice)
            label = f"Character Memory: chapter {whose} — {row['title']} ({n + 1}/{len(secs)}: {title})"
        text = _call_sonnet(prompt, timeout=RECAP_TIMEOUT, label=label)
        if not text or not text.strip():
            job["error"] = f"writing the {s['kind']} failed (see the History tab)"
            _x("UPDATE encounters SET error = ? WHERE character_id = ? AND chat_id = ?", (job["error"], cid, row["chat_id"]))
            return
        _x("INSERT OR REPLACE INTO condensed (key, kind, pov, recap, edited, created_ts) VALUES (?, ?, ?, ?, 0, ?)",
           (s["key"], s["kind"], who.pov if char else "", text.strip(), time.time()))
        job["done"] += 1


def _plan(chat_id: str) -> Optional[dict]:
    """The chat's division into chapters, if it has one."""
    row = _q1("SELECT chapters FROM plans WHERE chat_id = ?", (chat_id,))
    try:
        plan = json.loads(row["chapters"]) if row else None
    except ValueError:
        return None
    return plan if isinstance(plan, dict) and plan.get("chapters") else None


def _plan_chapters(job: dict, row: dict, cf: ChatFile, who: _Who, user: str) -> bool:
    """Divide a chat into chapters at its scenes, from this character's
    recaps; every character uses the same division. A plan records each
    chapter's start (the message its first scene starts at, which can be
    in the middle of a part) and title, and `last`, the start of the chat's
    last part then. When the chat has grown (or shrunk) since, its last
    chapter is divided again with the scenes after it, and the earlier
    chapters stay as they are. False when the call failed."""
    segs = _segments(cf, who)
    if len(segs) < CHAPTER_MIN_PARTS:
        return True
    plan = _plan(row["chat_id"])
    last = segs[-1]["start"]
    if plan and plan.get("last") == last:
        return True
    chapters = [c for c in (plan or {}).get("chapters", []) if c["start"] <= last]
    kept = chapters[:-1]
    units = _units(segs, _segment_recaps([s["key"] for s in segs]))
    first = chapters[-1]["start"] if chapters else units[0]["start"]
    todo = [u for u in units if u["start"] >= first] if kept else units
    if len(todo) <= 1 and chapters:
        new = chapters[-1:]
    else:
        job["stage"], job["total"], job["done"] = "plan", 1, 0
        earlier = ""
        if kept:
            earlier = ("\nThe chat's earlier chapters, already divided: "
                       + "; ".join(f'{i + 1}. "{c["title"]}"' for i, c in enumerate(kept))
                       + ". Divide only the scenes below, which come after them.")
        reply = _call_sonnet(_PLAN_PROMPT.format(
            where=f' ("{row["title"]}")' if row.get("title") else "", user=user, size=SEGMENT_SIZE,
            earlier=earlier, first=todo[0]["start"],
            recaps=_plan_list(todo, {s["start"]: i + 1 for i, s in enumerate(segs)})),
            timeout=RECAP_TIMEOUT, label=f"Character Memory: chapters — {row['title']}")
        new = _parse_plan(_extract_json(reply or ""), todo)
        if new is None:
            job["error"] = "dividing the chat into chapters failed (see the History tab)"
            _x("UPDATE encounters SET error = ? WHERE character_id = ? AND chat_id = ?",
               (job["error"], job["cid"], row["chat_id"]))
            return False
        # The chapter divided again keeps its start, and with it the
        # connections' and bindings' choice about it.
        new[0]["start"] = first
        job["done"] = 1
    _x("INSERT OR REPLACE INTO plans (chat_id, chapters) VALUES (?, ?)",
       (row["chat_id"], json.dumps({"chapters": kept + new, "last": last}, ensure_ascii=False)))
    return True


def _whole(v) -> Optional[int]:
    """A whole number from a JSON answer: 52, 52.0, "52" or "#52"."""
    if isinstance(v, bool):
        return None
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str) and v.strip().lstrip("#").isdigit():
        return int(v.strip().lstrip("#"))
    return v if isinstance(v, int) else None


def _parse_plan(obj: Optional[dict], units: list[dict]) -> Optional[list[dict]]:
    """[{start, title}] from the division call's answer: chapters in order,
    each starting at one of these scenes. A number inside a scene counts as
    its start, and the same scene named twice as one chapter. None when
    it's unusable."""
    items = (obj or {}).get("chapters")
    if not isinstance(items, list) or not items:
        return None
    starts = [u["start"] for u in units]
    out = []
    for item in items:
        n = _whole(item.get("first_message")) if isinstance(item, dict) else None
        if n is None or n >= units[-1]["end"]:
            return None
        at = max([x for x in starts if x <= n], default=starts[0])
        if out and at < out[-1]["start"]:
            return None
        if out and at == out[-1]["start"]:
            continue
        title = " ".join(str(item.get("title") or "").split())[:80]
        out.append({"start": at, "title": title or f"Messages from #{at}"})
    return out


def _condensed(keys: list[str]) -> dict:
    keys = [k for k in keys if k]
    rows = []
    for i in range(0, len(keys), 500):
        chunk = keys[i:i + 500]
        rows += _q(f"SELECT key, recap, edited FROM condensed WHERE key IN ({','.join('?' * len(chunk))})", chunk)
    return {r["key"]: dict(r) for r in rows}


def passed_on(cid: str, row: dict, cf: Optional[ChatFile] = None, off=(), m: Optional[str] = None) -> dict:
    """What another chat gets of this chat for the character, in mode `m`
    (default: the current one): {mode, words, chaptered, complete, there,
    sections}. A section is the synopsis, or a chapter: start (its id: the
    message its first scene starts at), title (None for the synopsis, which
    is also what chapters mode passes on of a chat too short for chapters
    or not divided yet), kind ("synopsis" or "chapter"), words (its word
    target), from/to (message numbers), keys (its parts; a part can be
    split between two chapters), units (its scenes, see _units), key (of
    its condensed text), text, ready (False while a part or its condensed
    text is missing; text is then the scene recaps there are), as_is (the
    scene recaps, short enough), edited, parts_missing, on (not in `off`, a
    connection's or binding's choice). chaptered: the chat is divided into
    chapters, so they can be chosen. complete: nothing is left to build.
    there: the character is in some part of it; if not, it's all plain
    accounts, which everyone else who wasn't there shares, and if so,
    those of the parts they aren't in say so."""
    cf = cf or read_chat(row["rel"])
    m = m or pass_mode()
    out = {"mode": m, "words": word_target(m), "chaptered": False, "complete": True, "there": False, "sections": []}
    if not cf:
        out["complete"] = False
        return out
    who = _who(cid, row)
    segs = _segments(cf, who)
    if not segs:
        return out
    out["there"] = any(s["pov"] for s in segs)
    pov, name = (who.pov, who.name) if out["there"] else ("", None)
    have = _segment_recaps([s["key"] for s in segs])
    units = _units(segs, have)
    groups = [(segs[0]["start"], None, units)]
    if m == "chapters" and len(segs) >= CHAPTER_MIN_PARTS:
        plan = _plan(row["chat_id"])
        if not plan or plan.get("last") != segs[-1]["start"]:
            out["complete"] = False
        if plan:
            # A scene belongs to the last chapter starting at or before it;
            # scenes before the first chapter (unhidden since) to the first.
            starts = sorted({c["start"] for c in plan["chapters"]})
            titles = {c["start"]: c["title"] for c in plan["chapters"]}
            by_start: dict = {}
            for u in units:
                at = max([x for x in starts if x <= u["start"]] or starts[:1])
                by_start.setdefault(at, []).append(u)
            groups = [(at, titles[at], by_start[at]) for at in starts if at in by_start]
            out["chaptered"] = True
    for start, title, us in groups:
        parts = list({id(u["seg"]): u["seg"] for u in us}.values())
        missing = sum(1 for s in parts if s["key"] not in have)
        full = "\n\n".join(_marked(u["seg"], u["text"], name) for u in us if u["text"])
        kind = "chapter" if title else "synopsis"
        words = word_target("chapters" if title else "synopsis")
        out["sections"].append({
            "start": start, "title": title, "kind": kind, "words": words,
            "from": us[0]["start"], "to": us[-1]["end"] - 1, "keys": [s["key"] for s in parts], "units": us,
            "key": None if missing else _hash(f"{kind}\n{pov}\n{words}\n{full}"),
            "text": full, "ready": False, "as_is": False, "edited": False, "parts_missing": missing,
            "on": not (out["chaptered"] and start in off)})
    done = _condensed([s["key"] for s in out["sections"]])
    for s in out["sections"]:
        if s["key"] in done:
            s.update(text=done[s["key"]]["recap"], ready=True, edited=bool(done[s["key"]]["edited"]))
        elif s["key"] and len(s["text"].split()) <= s["words"] * WORDS_SLACK:
            s.update(ready=True, as_is=True)
        if not s["ready"]:
            out["complete"] = False
    return out


def render_passed_on(sections: list[dict]) -> str:
    """The sections another chat gets, as the text of its <memory>."""
    return "\n\n".join(s["text"] if not s["title"] else f'<chapter title="{_attr(s["title"])}">\n{s["text"]}\n</chapter>'
                       for s in sections)


def _uses_off(cid: str, chat_id: str, target: Optional[str] = None, persona: Optional[str] = None) -> Optional[list]:
    """The chapters a connection to `target` (the character's own, else the
    one for everyone there) or the binding with `persona` leaves out of
    this chat; None when there's no such connection or binding chat."""
    if target:
        row = _q1("SELECT chapters_off FROM links WHERE character_id IN (?, '') AND target_chat = ? AND source_chat = ? "
                  "ORDER BY character_id = '' LIMIT 1", (cid, target, chat_id))
    else:
        row = _q1("SELECT chapters_off FROM binding_chats WHERE character_id = ? AND persona = ? AND chat_id = ?",
                  (cid, persona or "", chat_id))
    return None if row is None else _off_list(row["chapters_off"])


def _off_list(raw) -> list:
    try:
        v = json.loads(raw or "[]")
    except ValueError:
        return []
    return [x for x in v if isinstance(x, int) and not isinstance(x, bool)] if isinstance(v, list) else []


def set_chapters_off(cid: str, chat_id: str, off: list, target: Optional[str] = None,
                     persona: Optional[str] = None) -> bool:
    """Which chapters of a chat a connection (to `target`; cid: its
    character, "" for everyone there) or a binding (`cid` with `persona`)
    leaves out, by their starts."""
    value = json.dumps(sorted({int(x) for x in off})) if off else None
    if target:
        return _x("UPDATE links SET chapters_off = ? WHERE character_id = ? AND target_chat = ? AND source_chat = ?",
                  (value, cid, target, chat_id)) > 0
    return _x("UPDATE binding_chats SET chapters_off = ? WHERE character_id = ? AND persona = ? AND chat_id = ?",
              (value, cid, persona or "", chat_id)) > 0


def set_segment_recap(key: str, recap: Optional[str]) -> bool:
    """Rewrite one part's recap (None: delete it, so it's recapped again)."""
    if recap is None:
        return _x("DELETE FROM segments WHERE key = ?", (key,)) > 0
    return _x("UPDATE segments SET recap = ?, edited = 1 WHERE key = ?", (recap.strip(), key)) > 0


def _section(cid: str, row: dict, key: str) -> Optional[tuple[dict, dict]]:
    """(the passed_on() it's in, the section) of a synopsis or chapter by
    its key, in either mode."""
    cf = read_chat(row["rel"])
    for m in MODES:
        mem = passed_on(cid, row, cf, m=m)
        sec = next((s for s in mem["sections"] if s["key"] == key), None)
        if sec:
            return mem, sec
    return None


def set_condensed(cid: str, chat_id: str, key: str, recap: Optional[str]) -> bool:
    """Rewrite the chat's synopsis or one of its chapters (by its key; see
    passed_on), or (None) drop it: a written one is written again, an edit
    of scene recaps passed on as they are goes back to them. An edit lasts
    while the part recaps it's made from stay the same."""
    row = get_chat(chat_id, rescan_for=cid)
    found = _section(cid, row, key) if row else None
    if not found:
        return False
    if recap is None:
        _x("DELETE FROM condensed WHERE key = ?", (key,))
        return True
    mem, sec = found
    _x("INSERT OR REPLACE INTO condensed (key, kind, pov, recap, edited, created_ts) VALUES (?, ?, ?, ?, 1, ?)",
       (key, sec["kind"], _pov(cid, row)[0] if mem["there"] else "", recap.strip(), time.time()))
    return True


def clear_recap(cid: str, chat_id: str):
    """Forget a chat's recaps for a character: its parts (the accounts of
    those they aren't in too, which others who weren't there share) and
    synopsis and chapters. They're built again when needed. The division
    into chapters is the chat's, and stays."""
    row = get_chat(chat_id, rescan_for=cid)
    cf = read_chat(row["rel"]) if row else None
    if cf:
        keys = [s["key"] for m in MODES for s in passed_on(cid, row, cf, m=m)["sections"] if s["key"]]
        keys += [s["key"] for s in _segments(cf, _who(cid, row))]
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            for table in ("segments", "condensed"):
                _x(f"DELETE FROM {table} WHERE key IN ({','.join('?' * len(chunk))})", chunk)
    _x("UPDATE encounters SET built_ts = NULL, error = NULL WHERE character_id = ? AND chat_id = ?", (cid, chat_id))


# ---------------------------------------------------------------------------
# Sonnet calls
# ---------------------------------------------------------------------------

def _call_sonnet(prompt: str, timeout: int = 120, label: str = "Character Memory") -> Optional[str]:
    """One `claude -p --model sonnet` call; the reply text, or None."""
    cmd = [
        _claude_exe, "-p",
        "--output-format", "stream-json", "--verbose",
        "--model", "sonnet", "--effort", "low",
        # No tools, and no CLAUDE.md files from the bridge directory or its parents.
        "--tools", "",
        "--setting-sources", "user",
    ]
    record = _call_recorder("memory", label, argv=cmd, prompt=prompt) if _call_recorder else None
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8")
    except FileNotFoundError:
        log(f"claude CLI not found at {_claude_exe!r}", "ERROR")
        if record:
            record.finish(status="error", error=f"claude CLI not found at {_claude_exe!r}")
        return None
    try:
        stdout, stderr = proc.communicate(input=prompt, timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        log(f"{label}: timed out after {timeout}s", "WARN")
        if record:
            record.finish(status="error", error=f"timed out after {timeout}s")
        return None
    if record:
        record.observe_stdout(stdout, proc.returncode, stderr)
        record.finish()
    text = ""
    for line in (stdout or "").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "result":
            if event.get("is_error"):
                return None
            text = event.get("result", "") or text
            break
        if event.get("type") == "assistant":
            for block in (event.get("message") or {}).get("content", []):
                if block.get("type") == "text":
                    text = block.get("text", "") or text
    return text or None


def _extract_json(text: str) -> Optional[dict]:
    """The first JSON object in a reply."""
    if not text:
        return None
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                esc = (ch == "\\") and not esc
                if ch == '"' and not esc:
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start:i + 1])
                        if isinstance(obj, dict):
                            return obj
                    except ValueError:
                        pass
                    break
        start = text.find("{", start + 1)
    return None


# ---------------------------------------------------------------------------
# Background jobs
# ---------------------------------------------------------------------------

class _Jobs:
    """One worker thread running recap builds and conflict checks in order."""

    def __init__(self):
        self.cv = threading.Condition()
        self.queue: deque = deque()
        self.current: Optional[dict] = None
        self.recent: deque = deque(maxlen=20)
        self.thread: Optional[threading.Thread] = None

    def submit(self, job: dict) -> bool:
        with self.cv:
            if (self.current and self.current["key"] == job["key"]) or any(j["key"] == job["key"] for j in self.queue):
                return False
            job.setdefault("queued_ts", time.time())
            self.queue.append(job)
            if not self.thread or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._run, daemon=True, name="memory-jobs")
                self.thread.start()
            self.cv.notify()
            return True

    def cancel(self, cid: Optional[str] = None, chat_id: Optional[str] = None) -> int:
        def match(j):
            return (cid is None or j.get("cid") == cid) and (chat_id is None or j.get("chat_id") == chat_id)
        with self.cv:
            n = len(self.queue)
            self.queue = deque(j for j in self.queue if not match(j))
            n -= len(self.queue)
            if self.current and match(self.current):
                self.current["cancel"] = True
                n += 1
            return n

    def pending(self, cid: str, chat_id: str) -> Optional[dict]:
        with self.cv:
            for j in ([self.current] if self.current else []) + list(self.queue):
                if j.get("cid") == cid and j.get("chat_id") == chat_id and j["kind"] == "recap":
                    return dict(j)
        return None

    def status(self) -> dict:
        def view(j):
            return {k: j.get(k) for k in ("kind", "cid", "chat_id", "title", "stage", "done", "total", "error", "upto")}
        with self.cv:
            return {"current": view(self.current) if self.current else None,
                    "queued": [view(j) for j in self.queue],
                    "recent": [view(j) for j in self.recent]}

    def _run(self):
        while True:
            with self.cv:
                while not self.queue:
                    self.cv.wait()
                self.current = self.queue.popleft()
            job = self.current
            try:
                if job["kind"] == "recap":
                    _build_recaps(job)
                elif job["kind"] == "check":
                    _run_check(job)
            except Exception as e:
                job["error"] = f"{type(e).__name__}: {e}"
                log(f"{job['kind']} job failed: {job['error']}", "ERROR")
            with self.cv:
                self.recent.appendleft(job)
                self.current = None


_jobs = _Jobs()


def schedule_recap(cid: str, chat_id: str, upto: Optional[int] = None, title: str = "") -> bool:
    return _jobs.submit({"kind": "recap", "cid": cid, "chat_id": chat_id, "upto": upto, "title": title,
                         "key": f"recap\0{cid}\0{chat_id}\0{upto}"})


def cancel_jobs(cid: Optional[str] = None, chat_id: Optional[str] = None) -> int:
    return _jobs.cancel(cid, chat_id)


def jobs_status() -> dict:
    st = _jobs.status()
    names = {}
    for j in [st["current"]] + st["queued"] + st["recent"]:
        if j and j.get("cid"):
            j["character"] = names.setdefault(j["cid"], character_name(j["cid"]))
    return st


# ---------------------------------------------------------------------------
# Sources: what a character remembers in a chat
# ---------------------------------------------------------------------------

def _lineage(row: dict) -> tuple:
    """Chats with the same lineage are a chat and its branches, theirs
    included: same folder or group, same root chat. A branch's main_chat
    names the chat it was made from, so the chain is followed up."""
    folder = os.path.dirname(row["rel"])
    stem = os.path.splitext(os.path.basename(row["rel"]))[0]
    parent, seen = row.get("main_chat"), {stem}
    while parent and parent not in seen and len(seen) < 30:
        seen.add(parent)
        stem = parent
        prow = scan_chat(f"{folder}/{parent}.jsonl")
        parent = prow.get("main_chat") if prow else None
    return row.get("group_id") or folder, stem


def binding(cid: str, persona: str) -> Optional[dict]:
    row = _q1("SELECT * FROM bindings WHERE character_id = ? AND persona = ?", (cid, persona)) if persona else None
    return dict(row) if row else None


def sources(cid: str, chat_id: str, persona: str, chat_row: Optional[dict] = None) -> list[dict]:
    """The other chats a character remembers in this chat: [{chat_id, how,
    off, who}], how being "link" or "binding", off the starts of the
    chapters that connection or binding leaves out, and who, for a link,
    the character it's for ("" for everyone in this chat)."""
    off, whose = {}, {}
    # A character's own connection wins over the one for everyone.
    for r in _q("SELECT character_id, source_chat, chapters_off FROM links WHERE character_id IN (?, '') "
                "AND target_chat = ? ORDER BY character_id = '' DESC", (cid, chat_id)):
        off[("link", r["source_chat"])] = _off_list(r["chapters_off"])
        whose[r["source_chat"]] = r["character_id"]
    links = {c for how, c in off if how == "link"}
    bound = set()
    if binding(cid, persona):
        for r in _q("SELECT chat_id, chapters_off FROM binding_chats WHERE character_id = ? AND persona = ? "
                    "AND how != 'removed'", (cid, persona)):
            off[("binding", r["chat_id"])] = _off_list(r["chapters_off"])
        bound = {c for how, c in off if how == "binding"}
        bound -= {r["chat_id"] for r in _q(
            "SELECT chat_id FROM encounters WHERE character_id = ? AND excluded = 1", (cid,))}
        if chat_row:
            mine = _lineage(chat_row)
            for other in list(bound):
                r = _q1("SELECT rel, group_id, main_chat FROM chats WHERE chat_id = ?", (other,))
                if r and _lineage(dict(r)) == mine:
                    bound.discard(other)
    excluded = {r["source_chat"] for r in _q(
        "SELECT source_chat FROM exclusions WHERE character_id = ? AND target_chat = ?", (cid, chat_id))}
    out = [{"chat_id": c, "how": "link", "off": off[("link", c)], "who": whose[c]}
           for c in sorted(links - excluded - {chat_id})]
    out += [{"chat_id": c, "how": "binding", "off": off[("binding", c)]}
            for c in sorted(bound - links - excluded - {chat_id})]
    return out


def _date(ts: Optional[float]) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else ""


def _attr(v: str) -> str:
    return str(v).replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")


class TurnMemory:
    """Memory for one character's turn in one chat."""

    def __init__(self, cid, pov, name, chat_id, chat_row, persona, persona_name, is_group, first_in_context, srcs):
        """pov: the speaking card's avatar (a session's memory is for it),
        name: its name."""
        self.cid, self.pov, self.name, self.chat_id, self.chat_row = cid, pov, name, chat_id, chat_row
        self.persona, self.persona_name, self.is_group = persona, persona_name, is_group
        self.first_in_context = first_in_context
        self.sources = srcs
        # Ids of pieces (from the last pieces() call) with parts not yet
        # recapped. A session that has an earlier version keeps it until
        # they are: a chat that grew loses its last part's recap meanwhile.
        self.incomplete: set = set()

    def pieces(self, floor: Optional[int]) -> list[tuple[str, str, str]]:
        """[(piece id, title, text)]: the recap of this chat up to `floor`
        (the first message the CLI session has; see _inchat_upto), then what
        each other chat passes on (passed_on: a synopsis, or the chapters its
        connection or binding keeps). Missing recaps are scheduled; what
        exists is used."""
        out = []
        self.incomplete = set()
        cf = read_chat(self.chat_row["rel"]) if self.chat_row else None
        who = _who(self.cid, self.chat_row) if cf else None
        # Parts of this chat aren't repeated from a chat it shares them with
        # (a connected parent of a branch).
        own_keys = {s["key"] for s in _segments(cf, who)} if cf else set()
        if cf and floor and floor > 0:
            upto = _inchat_upto(cf, floor)
            segs = _segments(cf, who, upto)
            have = _segment_recaps([s["key"] for s in segs])
            if any(s["key"] not in have for s in segs):
                schedule_recap(self.cid, self.chat_id, upto, self.chat_row.get("title") or "")
                self.incomplete.add("inchat")
            texts = [_marked(s, _plain(have[s["key"]]["recap"]), self.name) for s in segs if s["key"] in have]
            own_keys |= {s["key"] for s in segs}
            if texts:
                overlap = (" Its end can overlap the first messages of the transcript; where the two differ, "
                           "the transcript is right.") if upto > floor else ""
                out.append(("inchat", "earlier in this chat",
                            '<memory source="earlier in this chat">\nWhat happened in this chat before the '
                            f'part of the transcript shown here.{overlap}\n\n' + "\n\n".join(texts) + "\n</memory>"))
        # Other chats, newest first while allocating the budget, oldest first in the text.
        rows = []
        for s in self.sources:
            row = get_chat(s["chat_id"], rescan_for=self.cid)
            src = read_chat(row["rel"]) if row else None
            if src:
                rows.append((row, src, s.get("off") or ()))
        rows.sort(key=lambda rs: rs[0].get("last_ts") or 0, reverse=True)
        budget, used_keys, chosen, left_out = SOURCE_BUDGET, set(own_keys), [], 0
        for row, src, off in rows:
            mem = passed_on(self.cid, row, src, off)
            if not mem["complete"]:
                schedule_recap(self.cid, row["chat_id"], None, row.get("title") or "")
                self.incomplete.add(f"chat:{row['chat_id']}")
            # Sections the connection or binding passes on, minus those whose
            # parts this chat already has (a connected parent of a branch).
            secs = [s for s in mem["sections"] if s["on"] and s["text"].strip() and not set(s["keys"]) <= used_keys]
            # The latest chapters that fit; the earliest go first.
            fit, size = [], 0
            for s in reversed(secs):
                size += len(render_passed_on([s])) + 2
                if size > budget:
                    break
                fit.insert(0, s)
            if not fit:
                left_out += 1 if secs else 0
                continue
            text = render_passed_on(fit)
            budget -= len(text)
            used_keys |= {k for s in fit for k in s["keys"]}
            chosen.append((row, src, text, len(fit) < len(secs), mem["there"]))
        for row, src, text, cut, there in sorted(chosen, key=lambda c: c[0].get("first_ts") or 0):
            user, others = _chat_people(src, self.name)
            if row.get("group_id"):
                present = [n for n in json.loads(row.get("speakers") or "[]") if n != self.name]
            else:
                present = (others if not there else []) + [user]
            if there:
                note = f"Present: {', '.join(([self.name] + present)[:12])}."
            else:
                note = (f"{self.name} wasn't there: this is background on what happened. Present: "
                        f"{', '.join(present[:12])}. {self.name} knows of it only what {self.name} would have heard "
                        f"or been told.")
            if self.is_group and there:
                note += " The others in this chat know about it only if they were there or were told."
            if cut:
                note += " (Its earlier chapters are left out for length.)"
            title = row.get("title") or row["chat_id"]
            date = _date(row.get("first_ts"))
            out.append((f"chat:{row['chat_id']}", title,
                        f'<memory source="{_attr(title)}"' + (f' date="{date}"' if date else "")
                        + f">\n{note}\n\n{text}\n</memory>"))
        if left_out:
            log(f"{left_out} older chat(s) didn't fit {self.name}'s memory budget", "INFO")
        return out


def render_block(name: str, pieces: list[tuple[str, str, str]]) -> str:
    if not pieces:
        return ""
    intro = (f"{name}'s memories of events that aren't in the transcript: from earlier in this chat, or from "
             f"other chats. They happened. Where the character card's description or scenario says something "
             f"different about {name}'s history or relationships, these memories win.")
    return (f'<character_memory character="{_attr(name)}">\n{intro}\n\n'
            + "\n\n".join(p[2] for p in pieces) + "\n</character_memory>")


def render_update(name: str, changed: list[tuple[str, str, str]], removed_titles: list[str]) -> str:
    if not changed and not removed_titles:
        return ""
    parts = [f"Changes to {name}'s memories. Each <memory> replaces the one with the same source."]
    parts += [p[2] for p in changed]
    parts += [f'<memory source="{_attr(t)}" removed="yes">This memory no longer applies to this chat. '
              f"Disregard it.</memory>" for t in removed_titles]
    return f'<character_memory_update character="{_attr(name)}">\n' + "\n\n".join(parts) + "\n</character_memory_update>"


def piece_hash(piece: tuple[str, str, str]) -> str:
    return _hash(piece[2], 16)


# ---------------------------------------------------------------------------
# The request's identity (bridge_memory field from the extension)
# ---------------------------------------------------------------------------

def parse_request_field(raw) -> Optional[dict]:
    """Validate the `bridge_memory` field; None when it's missing or unusable."""
    if not isinstance(raw, dict):
        return None

    def s(key, limit=300):
        v = raw.get(key)
        return v.strip()[:limit] if isinstance(v, str) else ""

    def n(key):
        v = raw.get(key)
        return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 else None

    info = {
        "token": s("token", 200), "version": s("v", 20) or str(raw.get("v") or ""),
        "chat_id": s("chat_id", 100), "chat_file": s("chat_file", 400), "group_id": s("group_id", 100),
        "main_chat": s("main_chat", 400), "char_avatar": s("char_avatar", 300), "char_name": s("char_name", 200),
        "persona": s("persona", 300), "persona_name": s("persona_name", 200), "type": s("type", 40) or "normal",
        "first_in_context": n("first_in_context"), "chat_length": n("chat_length"),
    }
    if not info["chat_file"] or not info["char_avatar"]:
        return info if info["token"] else None
    return info


def register_token(token: str):
    if token and 16 <= len(token) <= 200:
        _x("INSERT INTO ext_tokens (hash, last_ts) VALUES (?, ?) ON CONFLICT(hash) DO UPDATE SET last_ts = excluded.last_ts",
           (_hash(token, 64), time.time()))
        _x("DELETE FROM ext_tokens WHERE hash NOT IN (SELECT hash FROM ext_tokens ORDER BY last_ts DESC LIMIT 10)")


def token_ok(token: str) -> bool:
    return bool(token) and _q1("SELECT 1 FROM ext_tokens WHERE hash = ?", (_hash(token, 64),)) is not None


def note_request(info: dict):
    """A chat request carried the extension's field: learn its token and
    when it was last heard from (for the Memory tab). Memory on or off."""
    if info.get("token"):
        register_token(info["token"])
    _meta_set("extension", {"version": info.get("version"), "ts": time.time()})


def _register_live_chat(info: dict) -> Optional[dict]:
    """The chats row of the request's chat, refreshed from its file."""
    rel = chat_rel(info["chat_file"], info["group_id"], info["char_avatar"])
    if not rel:
        return None
    group = _group(info["group_id"]) if info["group_id"] else None
    row = scan_chat(rel, group)
    if not row:
        # Not saved yet (a brand-new chat): remember what the request says.
        cid = rel
        _store_chat({"chat_id": cid, "rel": rel, "integrity": info["chat_id"] or None, "group_id": info["group_id"] or None,
                     "group_name": group["name"] if group else None, "main_chat": info["main_chat"] or None,
                     "title": f"{group['name']}: {info['chat_file']}" if group else info["chat_file"],
                     "first_ts": time.time(), "last_ts": time.time(), "n": info["chat_length"] or 0,
                     "personas": "{}", "speakers": "[]", "mtime": 0, "size": 0,
                     "created_ts": time.time(), "last_msg": None})
        row = dict(_q1("SELECT * FROM chats WHERE chat_id = ?", (cid,)))
    _x("UPDATE chats SET live_ts = ? WHERE chat_id = ?", (time.time(), row["chat_id"]))
    return row


def _auto_bind(cid: str, persona: str, row: dict):
    """A new chat with a bound persona joins the binding. Branches don't:
    they're alternate timelines of a chat the user may or may not want."""
    b = binding(cid, persona)
    if not b or row.get("main_chat"):
        return
    if _q1("SELECT 1 FROM binding_chats WHERE character_id = ? AND persona = ? AND chat_id = ?",
           (cid, persona, row["chat_id"])):
        return
    if (row.get("first_ts") or 0) >= b["created_ts"] - 60:
        _x("INSERT OR IGNORE INTO binding_chats (character_id, persona, chat_id, how, added_ts) VALUES (?, ?, ?, 'auto', ?)",
           (cid, persona, row["chat_id"], time.time()))
        log(f"{character_name(cid)}: {row.get('title')} joins the binding with {b.get('persona_name') or persona}")


def prepare_turn(info: dict) -> Optional[TurnMemory]:
    """Memory for a roleplay turn, or None when there's none to give."""
    if info.get("type") in _SKIP_TYPES or not info.get("chat_file") or not info.get("char_avatar"):
        return None
    if not st_dir():
        log("SillyTavern's data folder wasn't found, so memory can't read chats", "WARN")
        return None
    cid = ensure_card(info["char_avatar"], info["char_name"])
    row = _register_live_chat(info)
    if not row:
        return None
    persona = info.get("persona") or ""
    if persona:
        _auto_bind(cid, persona, row)
    srcs = sources(cid, row["chat_id"], persona, row)
    tm = TurnMemory(cid, info["char_avatar"], (info.get("char_name") or "").strip() or character_name(cid),
                    row["chat_id"], row, persona, info.get("persona_name") or "", bool(info.get("group_id")),
                    info.get("first_in_context"), srcs)
    if srcs:
        _maybe_check(tm)
    return tm


# ---------------------------------------------------------------------------
# Conflict checks
# ---------------------------------------------------------------------------

_CHECK_PROMPT = """{char} is a character in a roleplay. {char} remembers earlier chats, recapped below, and a new chat has started{with_whom}. Decide whether the way the new chat starts contradicts those memories about {char}'s relationships or shared history with the people in them.

Contradictions: the new chat treats people who share history in the memories as strangers meeting for the first time; it gives them a different relationship (siblings in one, rivals or lovers in the other); it says something happened, or didn't, against the memories.
Not contradictions: a new place, setting or time; a time skip; a different mood; things the memories don't cover; the character card's own background.

Answer with only a JSON object: {{"conflict": true or false, "explanation": "one or two sentences for the user saying what clashes, naming the chat"}}

=== MEMORIES ===
{memories}

=== HOW THE NEW CHAT STARTS ===
{opening}
"""


def _opening(cf: ChatFile) -> str:
    """How a chat starts: its greeting(s) and the user's first message, and
    its Author's Note. Empty until the user has written: it's checked once
    the chat has its premise, and doesn't change as the chat goes on."""
    msgs = [m for m in cf.messages if not m["hidden"] and m["text"].strip()]
    first_user = next((i for i, m in enumerate(msgs) if m["user"]), None)
    if first_user is None:
        return ""
    msgs = msgs[:min(first_user + 1, OPENING_MESSAGES)]
    text = "\n\n".join(f"{m['name'] or 'Character'}: {_clean(m['text'])[:3000]}" for m in msgs)
    if cf.note:
        text = f"Author's Note for this chat: {cf.note[:2000]}\n\n{text}"
    return text


def _check_sig(tm: TurnMemory, cf: ChatFile) -> tuple[str, list[str]]:
    """What the check depends on: a hash of the chat's opening, and the
    sources that have recaps. Chats the character wasn't in are background,
    with no history of theirs to clash with, and aren't checked."""
    ready = []
    for s in tm.sources:
        row = _q1("SELECT rel, group_id FROM chats WHERE chat_id = ?", (s["chat_id"],))
        src = read_chat(row["rel"]) if row else None
        segs = _segments(src, _who(tm.cid, dict(row))) if src else []
        if any(x["pov"] for x in segs) and _segment_recaps([x["key"] for x in segs]):
            ready.append(s["chat_id"])
    return _hash(_opening(cf)), sorted(ready)


def _maybe_check(tm: TurnMemory):
    """Check the chat's opening against its memories when either changed.
    After the user kept memories or dropped some, only new ones (or a new
    opening) are checked again, so the ones they chose to keep don't keep
    raising the same conflict."""
    cf = read_chat(tm.chat_row["rel"]) if tm.chat_row else None
    if not cf:
        return
    sig, ready = _check_sig(tm, cf)
    if not ready or not _opening(cf):
        return
    row = _q1("SELECT sig, status, sources FROM checks WHERE character_id = ? AND chat_id = ?", (tm.cid, tm.chat_id))
    if row and row["sig"] == sig:
        checked = set(json.loads(row["sources"] or "[]"))
        if row["status"] in ("kept", "applied") and set(ready) <= checked:
            return
        if set(ready) == checked:
            return
    _jobs.submit({"kind": "check", "cid": tm.cid, "chat_id": tm.chat_id, "persona": tm.persona,
                  "title": tm.chat_row.get("title") or "", "key": f"check\0{tm.cid}\0{tm.chat_id}"})


def _run_check(job: dict):
    """Ask Sonnet whether the chat's opening contradicts its memories. Once
    the user has decided (kept or dropped memories), only chats added since
    are checked, and a decision made while the check ran wins."""
    cid, chat_id = job["cid"], job["chat_id"]
    row = get_chat(chat_id, rescan_for=cid)
    cf = read_chat(row["rel"]) if row else None
    if not cf:
        return
    pov, name = _pov(cid, row)
    tm = TurnMemory(cid, pov, name, chat_id, row, job.get("persona") or "", "",
                    bool(row.get("group_id")), None, sources(cid, chat_id, job.get("persona") or "", row))
    sig, ready = _check_sig(tm, cf)
    prev = _q1("SELECT id, sig, status, sources FROM checks WHERE character_id = ? AND chat_id = ?", (cid, chat_id))
    decided = bool(prev) and prev["status"] in ("kept", "applied") and prev["sig"] == sig
    checked = set(json.loads(prev["sources"] or "[]")) if decided else set()
    to_check = [c for c in ready if c not in checked]
    if not to_check:
        return
    mems = []
    off = {s["chat_id"]: s.get("off") or () for s in tm.sources}
    for src_id in to_check:
        srow = get_chat(src_id, rescan_for=cid)
        src = read_chat(srow["rel"]) if srow else None
        if not src:
            continue
        # What this chat gets of it: the synopsis, or the chapters passed on.
        text = render_passed_on([s for s in passed_on(cid, srow, src, off.get(src_id, ()))["sections"]
                                 if s["on"] and s["text"].strip()])
        mems.append(f"--- {srow.get('title') or src_id} ---\n{text[:5000]}")
    user, _ = _chat_people(cf, tm.name)
    reply = _call_sonnet(_CHECK_PROMPT.format(
        char=tm.name, with_whom=f" with {user}" if user != "the user" else "",
        memories="\n\n".join(mems), opening=_opening(cf)),
        timeout=CHECK_TIMEOUT, label=f"Character Memory: conflict check {tm.name} — {row.get('title')}")
    parsed = _extract_json(reply or "")
    if not parsed or not isinstance(parsed.get("conflict"), bool):
        status, explanation = "failed", "The conflict check didn't return an answer."
    elif parsed["conflict"]:
        status, explanation = "open", str(parsed.get("explanation") or "").strip()[:1000]
    else:
        status, explanation = (prev["status"] if decided else "clear"), str(parsed.get("explanation") or "").strip()[:1000]
    with _db_lock:
        now = _q1("SELECT id, status FROM checks WHERE character_id = ? AND chat_id = ?", (cid, chat_id))
        if (tuple(now) if now else None) != ((prev["id"], prev["status"]) if prev else None):
            return  # the user decided meanwhile; the next turn checks again if needed
        _x("DELETE FROM checks WHERE character_id = ? AND chat_id = ?", (cid, chat_id))
        _x("INSERT INTO checks (character_id, chat_id, sig, status, explanation, sources, shown, created_ts) "
           "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
           (cid, chat_id, sig, status, explanation, json.dumps(sorted(checked | set(ready))),
            json.dumps(to_check), time.time()))
    if status == "open":
        log(f"{tm.name}'s memories conflict with how {row.get('title')} starts: {explanation}", "WARN")


def get_check(cid: str, chat_id: str) -> Optional[dict]:
    row = _q1("SELECT * FROM checks WHERE character_id = ? AND chat_id = ?", (cid, chat_id))
    if not row:
        return None
    out = dict(row)
    # The chats the conflict is about, for the popup and the Memory tab.
    names = _card_names(cid)
    out["sources"] = [_brief(cid, c, names) for c in json.loads(out.pop("shown", None) or out.get("sources") or "[]")]
    return out


def resolve_check(check_id: int, action: str, drop: list[str]) -> tuple[bool, str]:
    """keep: the memories stay. drop: the listed chats' memories leave this
    chat (a connection is removed; a binding's chat is excluded here)."""
    row = _q1("SELECT * FROM checks WHERE id = ?", (int(check_id),))
    if not row:
        return False, "no such check"
    cid, chat_id = row["character_id"], row["chat_id"]
    if action == "drop":
        for src in drop:
            if not isinstance(src, str):
                continue
            if _x("DELETE FROM links WHERE character_id = ? AND target_chat = ? AND source_chat = ?", (cid, chat_id, src)):
                continue
            set_exclusion(cid, chat_id, src, True)
    _x("UPDATE checks SET status = ? WHERE id = ?", ("applied" if action == "drop" else "kept", int(check_id)))
    return True, ""


# ---------------------------------------------------------------------------
# Changes from the Memory tab and the extension
# ---------------------------------------------------------------------------

def _chat_title(chat_id: str) -> str:
    row = _q1("SELECT title FROM chats WHERE chat_id = ?", (chat_id,))
    return row["title"] if row and row["title"] else chat_id


def bind(cid: str, persona: str) -> dict:
    _x("INSERT OR IGNORE INTO bindings (character_id, persona, persona_name, created_ts) VALUES (?, ?, ?, ?)",
       (cid, persona, _persona_name(persona), time.time()))
    return binding(cid, persona)


def unbind(cid: str, persona: str):
    with _db_lock:
        _x("DELETE FROM bindings WHERE character_id = ? AND persona = ?", (cid, persona))
        _x("DELETE FROM binding_chats WHERE character_id = ? AND persona = ?", (cid, persona))


def set_binding_chats(cid: str, persona: str, add: list[str], remove: list[str], build: bool = True) -> int:
    """Pick chats for a binding (and recap them), or take them out."""
    if not binding(cid, persona):
        return 0
    now, n = time.time(), 0
    for c in add:
        n += _x("INSERT INTO binding_chats (character_id, persona, chat_id, how, added_ts) VALUES (?, ?, ?, 'chosen', ?) "
                "ON CONFLICT(character_id, persona, chat_id) DO UPDATE SET how = 'chosen'", (cid, persona, c, now))
        if build:
            schedule_recap(cid, c, None, _chat_title(c))
    for c in remove:
        n += _x("INSERT INTO binding_chats (character_id, persona, chat_id, how, added_ts) VALUES (?, ?, ?, 'removed', ?) "
                "ON CONFLICT(character_id, persona, chat_id) DO UPDATE SET how = 'removed'", (cid, persona, c, now))
    return n


def rememberers(target: str) -> list[dict]:
    """Who remembers a connection to a chat that's for everyone there: a
    group chat's members, or a card's own chat's character. [{id, avatar,
    name}], one per character."""
    row = get_chat(target)
    if not row:
        return []
    names = _all_card_names()
    if row.get("group_id"):
        g = _group(row["group_id"])
        avatars = list(g["members"]) if g else []
    else:
        avatars = [f"{row['rel'].split('/')[1]}.png"] if row["rel"].startswith("chats/") else []
    out, seen = [], set()
    for a in avatars:
        cid = ensure_card(a, names.get(a, ""))
        if cid not in seen:
            seen.add(cid)
            out.append({"id": cid, "avatar": a, "name": names.get(a) or character_name(cid)})
    return out


def _link_cids(target: str, cid: str) -> list[str]:
    """The characters a connection's row is for: `cid`, or everyone there."""
    return [r["id"] for r in rememberers(target)] if cid == "" else [cid]


def set_link(cid: str, target: str, source: str, on: bool, build: bool = True):
    """Connect `source` to `target` for one character (cid "": everyone in
    the target chat), or take that connection away. The others it's for
    keep theirs."""
    if target == source:
        return
    if on:
        _x("INSERT OR IGNORE INTO links (character_id, target_chat, source_chat, created_ts) VALUES (?, ?, ?, ?)",
           (cid, target, source, time.time()))
        if cid:
            _x("DELETE FROM exclusions WHERE character_id = ? AND target_chat = ? AND source_chat = ?",
               (cid, target, source))
        else:
            _x("DELETE FROM exclusions WHERE target_chat = ? AND source_chat = ?", (target, source))
        if build:
            for c in _link_cids(target, cid):
                schedule_recap(c, source, None, _chat_title(source))
    else:
        _x("DELETE FROM links WHERE character_id = ? AND target_chat = ? AND source_chat = ?", (cid, target, source))


def connect(target: str, source: str, who: Optional[list] = None, build: bool = True):
    """Connect `source` to `target`, for everyone there (who None) or the
    characters in `who`, replacing whoever it was for. Chapters left out of
    it stay out."""
    if target == source:
        return
    cids = [""] if who is None else list(dict.fromkeys(who))
    with _db_lock:
        old = _q1("SELECT chapters_off FROM links WHERE target_chat = ? AND source_chat = ? "
                  "ORDER BY character_id = '' DESC LIMIT 1", (target, source))
        _x("DELETE FROM links WHERE target_chat = ? AND source_chat = ?", (target, source))
        for cid in cids:
            set_link(cid, target, source, True, build=False)
        if old and old["chapters_off"]:
            _x("UPDATE links SET chapters_off = ? WHERE target_chat = ? AND source_chat = ?",
               (old["chapters_off"], target, source))
    if build:
        for cid in dict.fromkeys(c for x in cids for c in _link_cids(target, x)):
            schedule_recap(cid, source, None, _chat_title(source))


def disconnect(target: str, source: str):
    """Take a connection away, for everyone it was for."""
    _x("DELETE FROM links WHERE target_chat = ? AND source_chat = ?", (target, source))


def estimate(cids: list[str], chat_id: str) -> int:
    """About how many Sonnet calls recapping a chat for these characters
    takes: its parts not recapped yet (an account of a part, shared by those
    not in it, counts once), then for each version of it not written yet a
    synopsis, or a division into chapters (once per chat) and about one call
    per three parts."""
    row = get_chat(chat_id)
    cf = read_chat(row["rel"]) if row else None
    if not cf:
        return 0
    missing, written, calls = set(), set(), 0
    m = pass_mode()
    for cid in cids:
        segs = _segments(cf, _who(cid, row))
        have = _segment_recaps([s["key"] for s in segs])
        missing |= {s["key"] for s in segs if s["key"] not in have}
        mem = passed_on(cid, row, cf, m=m)
        version = tuple(s["key"] for s in segs)
        if mem["complete"] or version in written or len(segs) < 2:
            continue
        written.add(version)
        calls += -(-len(segs) // 3) if m == "chapters" and len(segs) >= CHAPTER_MIN_PARTS else 1
    if m == "chapters" and written:
        segs = _segments(cf, _who(cids[0], row))
        plan = _plan(row["chat_id"])
        calls += 1 if len(segs) >= CHAPTER_MIN_PARTS and (not plan or plan.get("last") != segs[-1]["start"]) else 0
    return len(missing) + calls


def set_exclusion(cid: str, target: str, source: str, on: bool):
    if on:
        _x("INSERT OR IGNORE INTO exclusions (character_id, target_chat, source_chat, created_ts) VALUES (?, ?, ?, ?)",
           (cid, target, source, time.time()))
    else:
        _x("DELETE FROM exclusions WHERE character_id = ? AND target_chat = ? AND source_chat = ?", (cid, target, source))


def set_excluded(cid: str, chat_id: str, on: bool):
    """Leave a chat out of every binding (connections still bring it in)."""
    _ensure_encounter(cid, chat_id)
    _x("UPDATE encounters SET excluded = ? WHERE character_id = ? AND chat_id = ?", (1 if on else 0, cid, chat_id))


# ---------------------------------------------------------------------------
# Views for the Memory tab and the extension
# ---------------------------------------------------------------------------

def _norm_name(s: str) -> str:
    """A name compared with a file name: letters and digits, as SillyTavern
    drops characters like | and : from file names."""
    return re.sub(r"[\W_]+", "", (s or "").casefold())


_names_cache: dict = {"at": 0.0, "names": {}}


def _all_card_names() -> dict:
    """Card avatar -> name, for every card (re-read every few seconds)."""
    if time.time() - _names_cache["at"] > 5:
        _names_cache.update(at=time.time(), names={c["avatar"]: c["name"] for c in list_cards()})
    return _names_cache["names"]


def _card_names(cid: Optional[str] = None) -> dict:
    """Chat folder -> card name, for the character's cards, or every card."""
    if not cid:
        return {_stem(a): n for a, n in _all_card_names().items()}
    return {_stem(c["avatar"]): c["name"] for c in character_cards(cid)}


def _chat_view(row: dict, cid: Optional[str], recaps: bool = True, names: Optional[dict] = None) -> dict:
    """A chat for the Memory tab. name: its card's or group's name; label:
    its branch ("Branch #3") or its own title (a checkpoint's name, a renamed
    file). A chat's file name otherwise starts with the card's name when the
    chat began, which may be an old one. created: the day the file was made,
    as its name says it in the user's time, or else created_ts (the
    dashboard shows it in the browser's time zone); parent: the chat id of
    the chat it's a branch of; members: a group chat's cards. With `cid`
    and `recaps`, how far that character's recap of it is."""
    personas = json.loads(row.get("personas") or "{}")
    names = _card_names(cid) if names is None else names
    folder = os.path.basename(os.path.dirname(row["rel"]))
    name = (row.get("group_name") or "Group") if row["rel"].startswith("group chats/") else names.get(folder, folder)
    main = row.get("main_chat") or None
    prefix, branch, when, own = _stem_parts(os.path.splitext(os.path.basename(row["rel"]))[0])
    known = {_norm_name(n) for n in (name, folder, *names, *names.values())}
    own_title = (main or when is None) and _norm_name(prefix) not in known
    created_ts = None if own else (row.get("created_ts") or row.get("first_ts"))
    out = {
        "chat_id": row["chat_id"], "title": row.get("title") or row["chat_id"], "rel": row["rel"],
        "name": name, "label": branch or (prefix if own_title else ""),
        "group": row.get("group_name") or None, "branch_of": main,
        "parent": f"{os.path.dirname(row['rel'])}/{main}.jsonl" if main else None,
        "created": when.strftime("%Y-%m-%d") if own else _date(created_ts), "created_ts": created_ts,
        "last_msg": json.loads(row.get("last_msg") or "{}"),
        "first": _date(row.get("first_ts")), "last": _date(row.get("last_ts")),
        "last_ts": row.get("last_ts") or 0, "messages": row.get("n") or 0,
        "personas": [{"avatar": a, "name": p.get("name") or "", "messages": p.get("n", 0)}
                     for a, p in sorted(personas.items(), key=lambda kv: -kv[1].get("n", 0))],
        "members": [],
    }
    g = _group(row["group_id"]) if row.get("group_id") else None
    if g:
        cards = _all_card_names()
        out["members"] = [{"avatar": a, "name": cards.get(a) or _stem(a)} for a in g["members"]]
    if recaps and cid:
        st = recap_state(cid, row["chat_id"], row=row)
        enc = _encounter(cid, row["chat_id"])
        job = _jobs.pending(cid, row["chat_id"])
        out.update(recap=st, excluded=bool(enc.get("excluded")), error=enc.get("error"),
                   building={k: job.get(k) for k in ("stage", "done", "total")} if job else None)
    return out


_BRIEF_KEYS = ("chat_id", "title", "name", "label", "created", "created_ts", "last_msg", "group", "branch_of",
               "members", "messages", "last_ts")


def _brief(cid: Optional[str], chat_id: str, names: Optional[dict] = None) -> dict:
    """A chat as a list names it (see _chat_view), from what's stored."""
    row = _q1("SELECT * FROM chats WHERE chat_id = ?", (chat_id,))
    if not row:
        return {"chat_id": chat_id, "title": chat_id}
    view = _chat_view(dict(row), cid, recaps=False, names=names)
    return {k: view[k] for k in _BRIEF_KEYS}


def _recap_parts(cid: str, row: dict) -> list[dict]:
    """A character's recap of a chat, part by part: key, from, to (message
    numbers), there (the character is in it; if not, it's an account),
    recap (None until recapped), edited."""
    cf = read_chat(row["rel"])
    if not cf:
        return []
    segs = _segments(cf, _who(cid, row))
    have = _segment_recaps([s["key"] for s in segs])
    return [{"key": s["key"], "from": s["start"], "to": s["end"] - 1, "there": bool(s["pov"]),
             "recap": (have.get(s["key"]) or {}).get("recap"), "edited": bool((have.get(s["key"]) or {}).get("edited"))}
            for s in segs]


_SECTION_KEYS = ("start", "title", "kind", "words", "from", "to", "key", "text", "ready", "as_is", "edited",
                 "parts_missing", "on")


def _passed_view(mem: dict) -> dict:
    """passed_on() for the Memory tab and the extension."""
    return {**{k: mem[k] for k in ("mode", "words", "chaptered", "complete", "there")},
            "sections": [{k: x[k] for k in _SECTION_KEYS} for x in mem["sections"]]}


def recap_view(cid: str, chat_id: str, target: Optional[str] = None, persona: Optional[str] = None) -> dict:
    """A character's recap of a chat, to read where another chat uses it:
    its parts, and what's passed on, with the chapters a connection to
    `target` or the binding with `persona` leaves out marked."""
    row = get_chat(chat_id, rescan_for=cid)
    if not row:
        return {}
    off = (_uses_off(cid, chat_id, target, persona) if target or persona else None) or []
    return {**_chat_view(row, cid, recaps=False), "parts": _recap_parts(cid, row),
            "passed": _passed_view(passed_on(cid, row, off=off))}


def overview() -> dict:
    """Characters with any memory setup, the extension's last contact, jobs."""
    chars = []
    for r in _q("SELECT id, name FROM characters ORDER BY name COLLATE NOCASE"):
        cid = r["id"]
        n_b = _q1("SELECT COUNT(*) AS n FROM bindings WHERE character_id = ?", (cid,))["n"]
        n_l = _q1("SELECT COUNT(*) AS n FROM links WHERE character_id = ?", (cid,))["n"]
        cards = [c["avatar"] for c in character_cards(cid)]
        n_s = _q1(f"SELECT COUNT(*) AS n FROM segments WHERE pov IN ({','.join('?' * len(cards))})",
                  cards)["n"] if cards else 0
        n_o = _q1("SELECT COUNT(*) AS n FROM checks WHERE character_id = ? AND status = 'open'", (cid,))["n"]
        chars.append({"id": cid, "name": r["name"], "cards": cards,
                      "bindings": n_b, "links": n_l, "recaps": n_s, "alerts": n_o})
    n_c = _q1("SELECT COUNT(*) AS n FROM (SELECT DISTINCT target_chat, source_chat FROM links)")["n"]
    # Chats remembered somewhere: each is written again (synopsis or
    # chapters) when the mode or word target changes and it's next needed.
    in_use = _q1("SELECT COUNT(*) AS n FROM (SELECT character_id, source_chat FROM links UNION "
                 "SELECT character_id, chat_id FROM binding_chats WHERE how != 'removed')")["n"]
    return {"characters": chars, "extension": _meta_get("extension"), "jobs": jobs_status(),
            "st_found": bool(st_dir()), "in_use": in_use, "connections": n_c,
            "settings": {"mode": pass_mode(), "synopsis_words": word_target("synopsis"),
                         "chapter_words": word_target("chapters")}}


def character_view(cid: str) -> dict:
    """Everything the Memory tab shows for a character."""
    if not _q1("SELECT 1 FROM characters WHERE id = ?", (cid,)):
        card = next((c for c in list_cards() if c["avatar"] == cid), None)
        if not card:
            return {}
        cid = ensure_card(card["avatar"], card["name"])
    chats = character_chats(cid)
    chats.sort(key=lambda r: r.get("last_ts") or 0, reverse=True)
    names = _card_names(cid)
    views = [_chat_view(r, cid, names=names) for r in chats]
    by_id = {v["chat_id"]: v for v in views}
    bindings = []
    for b in _q("SELECT * FROM bindings WHERE character_id = ? ORDER BY created_ts", (cid,)):
        members = {r["chat_id"]: r["how"] for r in _q(
            "SELECT chat_id, how FROM binding_chats WHERE character_id = ? AND persona = ?", (cid, b["persona"]))}
        bindings.append({"persona": b["persona"], "persona_name": b["persona_name"] or _persona_name(b["persona"]),
                         "created": _date(b["created_ts"]), "chats": members})
    # Connections for this character or everyone, among their chats.
    links = [{"target_chat": r["target_chat"], "source_chat": r["source_chat"], "who": r["character_id"]}
             for r in _q("SELECT character_id, target_chat, source_chat FROM links WHERE character_id IN (?, '')", (cid,))
             if r["character_id"] or r["target_chat"] in by_id or r["source_chat"] in by_id]
    exclusions = [dict(r) for r in _q("SELECT target_chat, source_chat FROM exclusions WHERE character_id = ?", (cid,))]
    checks = {r["chat_id"]: get_check(cid, r["chat_id"]) for r in _q(
        "SELECT chat_id FROM checks WHERE character_id = ? AND status IN ('open', 'failed')", (cid,))}
    for c in by_id.values():
        c["check"] = checks.get(c["chat_id"])
    # The chats at the other end of those connections, by id, to name them.
    others = {c for lk in links for c in (lk["target_chat"], lk["source_chat"]) if c not in by_id}
    all_names = _card_names()
    return {"id": cid, "name": character_name(cid), "cards": character_cards(cid), "bindings": bindings,
            "links": links, "link_chats": {c: _brief(None, c, all_names) for c in others},
            "exclusions": exclusions, "chats": views}


def chat_detail(cid: str, chat_id: str) -> dict:
    """One chat for a character: its recap, part by part, and the memories
    it uses."""
    row = get_chat(chat_id, rescan_for=cid)
    if not row:
        return {}
    view = _chat_view(row, cid)
    parts = _recap_parts(cid, row)
    personas = [p["avatar"] for p in view["personas"] if p["avatar"]]
    used = []
    names = _card_names()
    for persona in personas or [""]:
        for s in sources(cid, chat_id, persona, row):
            if all(u["chat_id"] != s["chat_id"] for u in used):
                srow = get_chat(s["chat_id"], rescan_for=cid)
                mem = passed_on(cid, srow, off=s["off"]) if srow else None
                chapters = ({"on": sum(x["on"] for x in mem["sections"]), "total": len(mem["sections"])}
                            if mem and mem["chaptered"] else None)
                used.append({**_brief(None, s["chat_id"], names), **s, "chapters": chapters,
                             "there": bool(mem and mem["there"]),
                             "persona": persona if s["how"] == "binding" else None})
    excluded = [{"chat_id": r["source_chat"], "title": _chat_title(r["source_chat"])} for r in _q(
        "SELECT source_chat FROM exclusions WHERE character_id = ? AND target_chat = ?", (cid, chat_id))]
    # Where this chat is remembered (by this character, or everyone there),
    # each with the chapters it leaves out.
    used_in = [{**_brief(None, r["target_chat"], names), "who": r["character_id"], "off": _off_list(r["chapters_off"])}
               for r in _q("SELECT character_id, target_chat, chapters_off FROM links WHERE character_id IN (?, '') "
                           "AND source_chat = ? ORDER BY created_ts", (cid, chat_id))]
    in_bindings = [{"persona": r["persona"], "how": r["how"], "off": _off_list(r["chapters_off"])} for r in _q(
        "SELECT b.persona, b.how, b.chapters_off FROM binding_chats b JOIN bindings USING (character_id, persona) "
        "WHERE b.character_id = ? AND b.chat_id = ?", (cid, chat_id))]
    return {**view, "parts": parts, "passed": _passed_view(passed_on(cid, row)),
            "sources": used, "excluded_sources": excluded, "used_in": used_in,
            "in_bindings": in_bindings, "check": get_check(cid, chat_id)}


def extension_status(info: dict, recaps: bool = False) -> dict:
    """For the extension, when a chat is opened or a reply arrives: per
    character, the memories this chat uses and any open conflict; with
    `recaps`, their recap texts too. Starts missing recaps and the conflict
    check, so they're ready for the first reply."""
    out = []
    if not st_dir():
        return {"characters": out, "st_found": False}
    group = _group(info["group_id"]) if info.get("group_id") else None
    cards = list(group["members"]) if group else ([info["char_avatar"]] if info.get("char_avatar") else [])
    names = {c["avatar"]: c["name"] for c in list_cards()} if group else {info["char_avatar"]: info.get("char_name")}
    persona = info.get("persona") or ""
    for avatar in cards:
        rel = chat_rel(info["chat_file"], info.get("group_id") or "", avatar)
        if not rel:
            continue
        row = scan_chat(rel, group)
        if not row:
            continue
        cid = ensure_card(avatar, names.get(avatar) or "")
        if persona:
            _auto_bind(cid, persona, row)
        srcs = sources(cid, row["chat_id"], persona, row)
        tm = TurnMemory(cid, avatar, names.get(avatar) or character_name(cid), row["chat_id"], row, persona,
                        info.get("persona_name") or "", bool(group), None, srcs)
        items = []
        card_names = _card_names(cid)
        for s in srcs:
            srow = get_chat(s["chat_id"], rescan_for=cid)
            if not srow:
                continue
            st = recap_state(cid, s["chat_id"], row=srow)
            mem = passed_on(cid, srow, off=s["off"])
            if not mem["complete"]:
                schedule_recap(cid, s["chat_id"], None, srow.get("title") or "")
            view = _chat_view(srow, cid, recaps=False, names=card_names)
            item = {**{k: view[k] for k in _BRIEF_KEYS}, "how": s["how"], "who": s.get("who"), "recap": st,
                    "there": mem["there"]}
            if recaps:
                item.update(parts=[{k: p[k] for k in ("from", "to", "there", "recap")} for p in _recap_parts(cid, srow)],
                            passed=_passed_view(mem))
            items.append(item)
        if srcs:
            _maybe_check(tm)
        check = get_check(cid, row["chat_id"])
        out.append({"avatar": avatar, "character_id": cid, "name": tm.name,
                    "bound": bool(binding(cid, persona)), "sources": items,
                    "check": check if check and check["status"] == "open" else None})
        if recaps:
            out[-1]["own"] = {"parts": [{k: p[k] for k in ("from", "to", "there", "recap")} for p in _recap_parts(cid, row)]}
    return {"characters": out, "st_found": True}


# ---------------------------------------------------------------------------
# Any chat, and the connections between chats (the Memory tab)
# ---------------------------------------------------------------------------

def all_chats() -> list[dict]:
    """Every chat file in SillyTavern, from the folder listings alone: rel,
    group (a group chat's group: the chats its group lists), avatar (a
    card's own chat's card), mtime."""
    base = st_dir()
    if not base:
        return []
    out = []
    cdir = os.path.join(base, "chats")
    try:
        folders = sorted(os.listdir(cdir))
    except OSError:
        folders = []
    for folder in folders:
        try:
            with os.scandir(os.path.join(cdir, folder)) as it:
                for e in it:
                    if e.name.endswith(".jsonl") and e.is_file():
                        out.append({"rel": f"chats/{folder}/{e.name}", "group": None, "avatar": f"{folder}.png",
                                    "mtime": e.stat().st_mtime})
        except OSError:
            continue
    for g in list_groups():
        for stem in g["chats"]:
            rel = _safe_rel(f"group chats/{stem}.jsonl")
            try:
                mtime = os.stat(_abs(rel)).st_mtime if rel else None
            except OSError:
                continue
            if mtime is not None:
                out.append({"rel": rel, "group": g, "avatar": "", "mtime": mtime})
    return out


def find_chats(q: str = "", limit: int = 30) -> dict:
    """Any chats in SillyTavern matching a search, last written first: by
    card or group name, group member, file name, and for a chat read before,
    its last message and personas. Only the ones returned are read.
    {chats: [view], more: how many others match}."""
    words = (q or "").casefold().split()
    cards = _all_card_names()
    stored = {r["chat_id"]: r for r in _q("SELECT chat_id, last_msg, personas FROM chats")} if words else {}
    hits = []
    for f in all_chats():
        if words:
            g = f["group"]
            text = [os.path.basename(f["rel"])[:-len(".jsonl")]]
            if g:
                text += [g["name"]] + [cards.get(a) or _stem(a) for a in g["members"]]
            else:
                text.append(cards.get(f["avatar"]) or _stem(f["avatar"]))
            r = stored.get(f["rel"])
            if r:
                text += [r["last_msg"] or "", r["personas"] or ""]
            hay = " ".join(text).casefold()
            if not all(w in hay for w in words):
                continue
        hits.append(f)
    hits.sort(key=lambda f: f["mtime"], reverse=True)
    names = _card_names()
    views = []
    for f in hits[:limit]:
        row = scan_chat(f["rel"], f["group"])
        if row:
            views.append(_chat_view(row, None, recaps=False, names=names))
    return {"chats": views, "more": max(0, len(hits) - limit)}


def _connection_state(cids: list[str], srow: Optional[dict]) -> dict:
    """How far the recaps of a connected chat are for the characters who
    remember it: parts, recapped (characters with every part done), of,
    absent (the names of those not in it: it's background for them),
    building."""
    if not srow:
        return {"available": False}
    out = {"available": True, "parts": 0, "recapped": 0, "of": len(cids), "absent": [], "building": False}
    for cid in cids:
        st = recap_state(cid, srow["chat_id"], row=srow)
        if not st["available"]:
            return {"available": False}
        out["parts"] = st["parts"]
        out["recapped"] += not st["missing"]
        if st["parts"] and not st["there"]:
            out["absent"].append(character_name(cid))
        out["building"] = out["building"] or bool(_jobs.pending(cid, srow["chat_id"]))
    return out


def connections() -> list[dict]:
    """Every connection, by the chat that remembers it, the most recently
    connected first: [{target (chat view), members (who "everyone" is
    there: rememberers), sources: [{chat, everyone, who: [{id, name}] the
    characters with a connection of their own, state}]}]."""
    targets: "OrderedDict[str, OrderedDict]" = OrderedDict()
    newest: dict = {}
    for r in _q("SELECT character_id, target_chat, source_chat, created_ts FROM links ORDER BY created_ts"):
        src = targets.setdefault(r["target_chat"], OrderedDict()).setdefault(
            r["source_chat"], {"everyone": False, "who": []})
        if r["character_id"]:
            src["who"].append(r["character_id"])
        else:
            src["everyone"] = True
        newest[r["target_chat"]] = r["created_ts"]
    names = _card_names()
    out = []
    for target in sorted(targets, key=lambda t: newest[t], reverse=True):
        trow = get_chat(target)
        members = rememberers(target)
        items = []
        for source, info in targets[target].items():
            srow = get_chat(source)
            cids = ([m["id"] for m in members] if info["everyone"] else []) + info["who"]
            items.append({"chat": _chat_view(srow, None, recaps=False, names=names) if srow
                          else {"chat_id": source, "title": source, "name": source},
                          "everyone": info["everyone"],
                          "who": [{"id": c, "name": character_name(c)} for c in info["who"]],
                          "state": _connection_state(list(dict.fromkeys(cids)), srow)})
        out.append({"target": _chat_view(trow, None, recaps=False, names=names) if trow
                    else {"chat_id": target, "title": target, "name": target},
                    "members": members, "sources": items})
    return out


def connect_preview(target: str, source: str, who: Optional[list] = None) -> dict:
    """Before connecting `source` to `target`: who'd remember it there
    (members: rememberers, each with there, whether they're in it), the
    current connection if any (everyone, who), and about how many Sonnet
    calls recapping it takes for everyone there, or for `who`."""
    trow, srow = get_chat(target), get_chat(source)
    if not trow or not srow or target == source:
        return {}
    members = rememberers(target)
    for m in members:
        m["there"] = bool(recap_state(m["id"], source, row=srow)["there"])
    rows = _q("SELECT character_id FROM links WHERE target_chat = ? AND source_chat = ?", (target, source))
    cids = [m["id"] for m in members] if who is None else who
    names = _card_names()
    return {"target": _chat_view(trow, None, recaps=False, names=names),
            "source": _chat_view(srow, None, recaps=False, names=names),
            "members": members, "everyone": any(r["character_id"] == "" for r in rows),
            "who": [r["character_id"] for r in rows if r["character_id"]],
            "calls": estimate(cids, source) if cids else 0}

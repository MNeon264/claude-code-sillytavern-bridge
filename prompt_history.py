"""Prompt history and subscription usage, for the dashboard's History and
Usage tabs.

Every `claude -p` call the bridge makes is recorded in a SQLite file: the
argv, the system prompt and stdin text the CLI was given, the reply, and the
token usage from the CLI's `result` event. Roleplay turns also record the
account's usage windows (5-hour session, weekly) just before the turn and a
few seconds after it, so each turn shows how many percent of each window it
took.

Usage comes from the endpoint behind Claude Code's /usage
(GET /api/oauth/usage), called with the access token the CLI keeps in
~/.claude/.credentials.json. It costs no quota. The bridge only reads that
file: when the token has expired, the next `claude` call renews it. The
endpoint reports whole percents, so a per-turn figure is approximate, and it
counts everything that used the account in that time (other Claude apps,
background memory calls).

The bridge calls set_logger() and init() once at startup. Every function
here is thread-safe and never raises into the caller's code path.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Optional

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
USER_AGENT = "claude-code-sillytavern-bridge"

# Full text (argv, prompts, replies) is kept for this many recent calls; older
# rows keep only their token and usage numbers, for the Usage tab's totals.
TEXT_KEEP_CALLS = 500
ROW_KEEP_DAYS = 30

# A turn reuses the last usage reading as its "before" value when it is this
# recent; otherwise the turn starts a fresh read.
BEFORE_MAX_AGE = 120
# The "after" read runs this long after a turn ends, giving the server time
# to count it. A read later than AFTER_MAX_LAG after the turn is not used.
AFTER_DELAY = 5
AFTER_MAX_LAG = 180
# Never call the endpoint more often than this.
MIN_FETCH_INTERVAL = 10

# Labels for the endpoint's `limits[].kind`. Unknown kinds are shown as-is.
_LIMIT_LABELS = {
    "session": "Current session (5-hour)",
    "weekly_all": "Weekly · all models",
    "weekly_opus": "Weekly · Opus",
    "weekly_sonnet": "Weekly · Sonnet",
}
# Windows read from the endpoint's top-level keys when it sends no `limits`.
_WINDOW_KEYS = {
    "five_hour": "Current session (5-hour)",
    "seven_day": "Weekly · all models",
    "seven_day_opus": "Weekly · Opus",
    "seven_day_sonnet": "Weekly · Sonnet",
}

_log_fn = None
_db_path: Optional[str] = None
_db_lock = threading.Lock()
_inserts_since_prune = 0


def set_logger(fn):
    """Inject the bridge's log() so messages land in the same console."""
    global _log_fn
    _log_fn = fn


def log(msg: str, level: str = "INFO"):
    if _log_fn is not None:
        try:
            _log_fn(msg, level)
            return
        except Exception:
            pass
    print(f"[history:{level}] {msg}")


# =============================================================================
# Storage
# =============================================================================

_SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started REAL NOT NULL,
    finished REAL,
    kind TEXT NOT NULL,
    label TEXT,
    char_key TEXT,
    model TEXT,
    effort TEXT,
    session_id TEXT,
    resumed INTEGER,
    note TEXT,
    status TEXT,
    error TEXT,
    stop_reason TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_creation_tokens INTEGER,
    cost_usd REAL,
    usage_json TEXT,
    rate_limit_json TEXT,
    usage_before TEXT,
    usage_after TEXT,
    argv TEXT,
    system_prompt_hash TEXT,
    system_prompt_chars INTEGER,
    prompt TEXT,
    prompt_chars INTEGER,
    response TEXT,
    response_chars INTEGER,
    thinking TEXT
);
CREATE INDEX IF NOT EXISTS calls_started ON calls(started);
CREATE TABLE IF NOT EXISTS system_prompts (hash TEXT PRIMARY KEY, text TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def _connect():
    conn = sqlite3.connect(_db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init(db_path: str):
    """Open (or create) the history DB and restore the last usage reading."""
    global _db_path
    _db_path = db_path
    try:
        with _db_lock:
            conn = _connect()
            try:
                conn.execute("PRAGMA journal_mode = WAL")
                conn.executescript(_SCHEMA)
                row = conn.execute("SELECT value FROM meta WHERE key = 'usage_latest'").fetchone()
                _prune(conn)
                conn.commit()
            finally:
                conn.close()
        if row:
            _usage.restore(json.loads(row["value"]))
    except (sqlite3.Error, OSError, ValueError) as e:
        log(f"Prompt history disabled: can't open {db_path}: {e}", "ERROR")
        _db_path = None


def _prune(conn):
    conn.execute("DELETE FROM calls WHERE started < ?", (time.time() - ROW_KEEP_DAYS * 86400,))
    conn.execute(
        "UPDATE calls SET argv = NULL, prompt = NULL, response = NULL, thinking = NULL, "
        "system_prompt_hash = NULL WHERE prompt IS NOT NULL AND id NOT IN "
        "(SELECT id FROM calls ORDER BY id DESC LIMIT ?)",
        (TEXT_KEEP_CALLS,),
    )
    conn.execute(
        "DELETE FROM system_prompts WHERE hash NOT IN "
        "(SELECT system_prompt_hash FROM calls WHERE system_prompt_hash IS NOT NULL)"
    )


def _db_write(fn):
    """Run fn(conn) in a transaction. Returns its result, or None on error."""
    if not _db_path:
        return None
    try:
        with _db_lock:
            conn = _connect()
            try:
                result = fn(conn)
                conn.commit()
                return result
            finally:
                conn.close()
    except sqlite3.Error as e:
        log(f"Prompt history write failed: {e}", "WARN")
        return None


def _db_read(fn, default=None):
    if not _db_path:
        return default
    try:
        with _db_lock:
            conn = _connect()
            try:
                return fn(conn)
            finally:
                conn.close()
    except sqlite3.Error as e:
        log(f"Prompt history read failed: {e}", "WARN")
        return default


def _json_or_none(value):
    return json.dumps(value) if value is not None else None


def _argv_for_storage(argv):
    """argv as a JSON list, with an inline --system-prompt value moved out.
    Returns (argv_json, system_prompt_or_None)."""
    if not argv:
        return None, None
    argv = [str(a) for a in argv]
    system_prompt = None
    if "--system-prompt" in argv:
        i = argv.index("--system-prompt")
        if i + 1 < len(argv):
            system_prompt = argv[i + 1]
            argv[i + 1] = f"<system prompt, {len(system_prompt):,} chars>"
    return json.dumps(argv), system_prompt


# =============================================================================
# One recorded CLI call
# =============================================================================

class Call:
    """A `claude -p` call being recorded. Feed it the CLI's stream-json events
    with observe() / observe_stdout(), then call finish() once. Used as a
    context manager, an exception inside the block is recorded as an error
    (and still raised)."""

    def __init__(self, kind, label=None, *, char_key=None, track_usage=False,
                 argv=None, prompt=None, system_prompt=None):
        self.kind = kind
        self.label = label
        self.char_key = char_key
        self.track_usage = track_usage
        self.argv = argv
        self.prompt = prompt
        self.system_prompt = system_prompt
        self.started = time.time()
        self.finished = None
        self.id = None
        self.model = None
        self.effort = None
        self.session_id = None
        self.resumed = None
        self.note = None
        self.stop_reason = None
        self.usage = None
        self.cost = None
        self.extra_usage = {}
        self.rate_limit = None
        self.usage_before = None
        self._result_text = None
        self._result_error = None
        self._last_text = None
        self._done = False
        if track_usage:
            _usage.begin(self)

    def observe(self, event: dict):
        try:
            etype = event.get("type")
            if etype == "system" and event.get("subtype") == "init":
                self.model = event.get("model") or self.model
                self.session_id = event.get("session_id") or self.session_id
            elif etype == "assistant":
                message = event.get("message") or {}
                self.stop_reason = message.get("stop_reason") or self.stop_reason
                for block in message.get("content") or []:
                    if block.get("type") == "text" and block.get("text"):
                        self._last_text = block["text"]
            elif etype == "rate_limit_event":
                info = event.get("rate_limit_info")
                if isinstance(info, dict):
                    self.rate_limit = info
                    _usage.note_cli_event(info)
            elif etype == "result":
                self.session_id = event.get("session_id") or self.session_id
                self.stop_reason = event.get("stop_reason") or self.stop_reason
                if isinstance(event.get("usage"), dict):
                    self.usage = event["usage"]
                self.cost = event.get("total_cost_usd", self.cost)
                for key in ("modelUsage", "num_turns", "duration_ms", "duration_api_ms"):
                    if key in event:
                        self.extra_usage[key] = event[key]
                if event.get("is_error"):
                    self._result_error = str(event.get("result") or event.get("subtype") or "error")
                elif event.get("structured_output") is not None:
                    self._result_text = json.dumps(event["structured_output"])
                elif isinstance(event.get("result"), str):
                    self._result_text = event["result"]
        except Exception as e:
            log(f"Prompt history: couldn't read a CLI event: {e}", "WARN")

    def observe_stdout(self, stdout: str, returncode=None, stderr=None):
        """Read a finished process's whole stream-json output. A non-zero
        returncode marks the call as failed."""
        if returncode and not self._result_error:
            self._result_error = (stderr or "").strip()[:2000] or f"exit {returncode}"
        for line in (stdout or "").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                self.observe(event)

    def finish(self, response=None, thinking=None, status=None, error=None):
        if self._done:
            return
        self._done = True
        try:
            self.finished = time.time()
            error = error or self._result_error
            status = status or ("error" if error else "ok")
            if response is None:
                response = self._result_text if self._result_text is not None else self._last_text
            if self.track_usage:
                _usage.detach(self)
            self._insert(response, thinking or None, status, error)
            if self.track_usage and self.id is not None:
                _usage.end(self)
        except Exception as e:
            log(f"Prompt history: couldn't record a call: {e}", "WARN")

    def _insert(self, response, thinking, status, error):
        argv_json, inline_sp = _argv_for_storage(self.argv)
        system_prompt = self.system_prompt or inline_sp
        sp_hash = hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()[:20] if system_prompt else None
        usage = self.usage or {}
        extra = dict(self.extra_usage)
        if self.usage:
            extra["usage"] = self.usage

        def write(conn):
            global _inserts_since_prune
            if sp_hash:
                conn.execute(
                    "INSERT OR IGNORE INTO system_prompts (hash, text) VALUES (?, ?)",
                    (sp_hash, system_prompt),
                )
            cur = conn.execute(
                """INSERT INTO calls (started, finished, kind, label, char_key, model, effort,
                    session_id, resumed, note, status, error, stop_reason, input_tokens,
                    output_tokens, cache_read_tokens, cache_creation_tokens, cost_usd,
                    usage_json, rate_limit_json, usage_before, argv, system_prompt_hash,
                    system_prompt_chars, prompt, prompt_chars, response, response_chars, thinking)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    self.started, self.finished, self.kind, self.label, self.char_key,
                    self.model, self.effort, self.session_id,
                    None if self.resumed is None else int(bool(self.resumed)),
                    self.note, status, error, self.stop_reason,
                    usage.get("input_tokens"), usage.get("output_tokens"),
                    usage.get("cache_read_input_tokens"), usage.get("cache_creation_input_tokens"),
                    self.cost, _json_or_none(extra or None), _json_or_none(self.rate_limit),
                    _json_or_none(self.usage_before), argv_json, sp_hash,
                    len(system_prompt) if system_prompt else None,
                    self.prompt, len(self.prompt) if self.prompt is not None else None,
                    response, len(response) if response is not None else None, thinking,
                ),
            )
            _inserts_since_prune += 1
            if _inserts_since_prune >= 25:
                _inserts_since_prune = 0
                _prune(conn)
            return cur.lastrowid

        self.id = _db_write(write)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc is not None:
            self.finish(status="error", error=f"{exc_type.__name__}: {exc}")
        else:
            self.finish()
        return False


def start_call(kind, label=None, **kwargs) -> Call:
    """Start recording a CLI call. kind is one of chat, utility, summary,
    image, memory. track_usage=True also records the account's
    usage windows before and after the call (roleplay turns)."""
    return Call(kind, label, **kwargs)


# =============================================================================
# Subscription usage
# =============================================================================

def _credentials_path():
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    return os.path.join(base, ".credentials.json")


def _read_login():
    """(access_token, plan, problem). problem is a message when there is no
    usable token."""
    path = _credentials_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            oauth = (json.load(f) or {}).get("claudeAiOauth") or {}
    except FileNotFoundError:
        return None, None, (
            f"Claude Code's login file ({path}) wasn't found. The bridge reads usage with "
            "the CLI's claude.ai login; on macOS the CLI keeps it in the Keychain instead."
        )
    except (OSError, ValueError) as e:
        return None, None, f"Couldn't read {path}: {e}"
    token = oauth.get("accessToken")
    plan = oauth.get("subscriptionType")
    if not token:
        return None, plan, "Claude Code isn't logged in with a claude.ai account, so there are no plan limits to read."
    expires = oauth.get("expiresAt")
    if isinstance(expires, (int, float)) and expires / 1000 < time.time() + 30:
        return None, plan, "Claude Code's login token has expired. The CLI renews it on its next call."
    return token, plan, None


def _parse_time(value):
    """ISO-8601 string or epoch number -> epoch seconds, or None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _num(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _normalize(data: dict, at: float, plan) -> dict:
    """The endpoint's JSON -> the snapshot the dashboard and deltas use."""
    windows = []
    for lim in data.get("limits") or []:
        if not isinstance(lim, dict) or _num(lim.get("percent")) is None:
            continue
        kind = str(lim.get("kind") or "")
        label = _LIMIT_LABELS.get(kind) or kind.replace("_", " ").capitalize() or "Limit"
        if lim.get("scope"):
            label += f" ({lim['scope']})"
        windows.append({
            "key": kind,
            "label": label,
            "percent": _num(lim.get("percent")),
            "resets_at": _parse_time(lim.get("resets_at")),
            "severity": lim.get("severity") or "normal",
            "active": bool(lim.get("is_active")),
        })
    if not windows:
        for key, label in _WINDOW_KEYS.items():
            w = data.get(key)
            if isinstance(w, dict) and _num(w.get("utilization")) is not None:
                windows.append({
                    "key": key,
                    "label": label,
                    "percent": _num(w.get("utilization")),
                    "resets_at": _parse_time(w.get("resets_at")),
                    "severity": "normal",
                    "active": False,
                })

    snap = {"at": at, "plan": plan, "windows": windows}
    for key in ("five_hour", "seven_day"):
        w = data.get(key)
        if isinstance(w, dict) and _num(w.get("utilization")) is not None:
            snap[key] = _num(w.get("utilization"))
            snap[key + "_resets"] = _parse_time(w.get("resets_at"))

    extra = data.get("extra_usage")
    if isinstance(extra, dict) and extra.get("is_enabled"):
        snap["extra_usage"] = {
            "monthly_limit": extra.get("monthly_limit"),
            "used_credits": extra.get("used_credits"),
            "utilization": extra.get("utilization"),
            "currency": extra.get("currency"),
        }

    breakdown = data.get("seven_day_breakdown")
    if isinstance(breakdown, dict):
        rows = [
            {"name": r.get("display_name") or r.get("key"), "percent": _num(r.get("percent"))}
            for r in breakdown.get("rows") or []
            if isinstance(r, dict) and _num(r.get("percent")) is not None
        ]
        if rows:
            snap["breakdown"] = rows
    return snap


def _compact(snap: dict) -> dict:
    """The part of a snapshot stored with each call."""
    return {k: snap.get(k) for k in ("at", "five_hour", "five_hour_resets", "seven_day", "seven_day_resets")}


def _window_delta(before, after, key):
    """{before, after, delta} for one window; delta is None when the window
    reset in between."""
    if not before or not after or before.get(key) is None or after.get(key) is None:
        return None
    out = {"before": before[key], "after": after[key], "delta": None}
    rb, ra = before.get(key + "_resets"), after.get(key + "_resets")
    if rb is None or ra is None or abs(rb - ra) < 120:
        out["delta"] = round(after[key] - before[key], 2)
    else:
        out["reset"] = True
    return out


class _UsageTracker:
    def __init__(self):
        self.lock = threading.Condition()
        self.fetch_lock = threading.Lock()
        self.latest = None
        self.error = None
        self.plan = None
        self.cli = None
        self.last_attempt = 0.0
        self.backoff_until = 0.0
        self.rejected_token = None
        self.waiting_before = []  # in-flight tracked calls without a "before"
        self.waiting_after = []   # (call id, finished, label) without an "after"
        self.last_finished = 0.0  # when the last tracked call ended
        self.due = None
        self.thread = None

    def restore(self, snap):
        with self.lock:
            if isinstance(snap, dict) and self.latest is None:
                self.latest = snap

    # -- tracked calls ------------------------------------------------------

    def begin(self, call):
        with self.lock:
            latest = self.latest
            # Reuse the last reading only if no tracked turn has ended since
            # it was taken (a swipe right after a turn needs a fresh one).
            if latest and latest["at"] >= self.last_finished and time.time() - latest["at"] < BEFORE_MAX_AGE:
                call.usage_before = _compact(latest)
                return
            self.waiting_before.append(call)
            self._schedule(time.time())

    def detach(self, call):
        with self.lock:
            if call in self.waiting_before:
                self.waiting_before.remove(call)

    def end(self, call):
        with self.lock:
            self.last_finished = max(self.last_finished, call.finished)
            self.waiting_after.append((call.id, call.finished, call.label or call.kind))
            self._schedule(call.finished + AFTER_DELAY)

    def note_cli_event(self, info):
        with self.lock:
            self.cli = {"at": time.time(), "info": info}

    # -- polling ------------------------------------------------------------

    def _schedule(self, when):
        # Caller holds self.lock.
        if self.due is None or when < self.due:
            self.due = when
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, name="usage-poller", daemon=True)
            self.thread.start()
        self.lock.notify()

    def _run(self):
        while True:
            with self.lock:
                while self.due is None:
                    self.lock.wait()
                start = max(self.due, self.last_attempt + MIN_FETCH_INTERVAL, self.backoff_until)
                wait = start - time.time()
                if wait > 0:
                    self.lock.wait(wait)
                    continue
                self.due = None
            self.fetch()

    def fetch(self):
        """Read the usage endpoint once and hand the result to waiting calls."""
        with self.fetch_lock:
            at = time.time()
            # Another thread read it while this one waited for the lock.
            if at - self.last_attempt < 2:
                return
            with self.lock:
                self.last_attempt = at
            snap = None
            token, plan, problem = _read_login()
            if token and token == self.rejected_token:
                problem = "The usage endpoint rejected Claude Code's login token. The CLI renews it on its next call."
            if not problem:
                snap, problem = self._request(token, at, plan)
            with self.lock:
                self.plan = plan or self.plan
                self.error = problem
                ready = []
                if snap:
                    self.latest = snap
                    for call in self.waiting_before:
                        call.usage_before = _compact(snap)
                    self.waiting_before = []
                    ready = [w for w in self.waiting_after if w[1] <= at]
                    self.waiting_after = [w for w in self.waiting_after if w[1] > at]
                # Give up on calls that waited too long: one that never
                # finished (a crash before the CLI ran), or a turn whose
                # "after" reading would now include later usage.
                self.waiting_before = [c for c in self.waiting_before if at - c.started < 600]
                self.waiting_after = [w for w in self.waiting_after if at - w[1] <= AFTER_MAX_LAG]
                if not snap and (self.waiting_before or self.waiting_after):
                    self._schedule(at + 30)
        if not snap:
            return
        compact = _compact(snap)
        updates = [w for w in ready if at - w[1] <= AFTER_MAX_LAG]

        def write(conn):
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('usage_latest', ?)",
                (json.dumps(snap),),
            )
            befores = {}
            for call_id, _finished, _label in updates:
                conn.execute("UPDATE calls SET usage_after = ? WHERE id = ?", (json.dumps(compact), call_id))
                row = conn.execute("SELECT usage_before FROM calls WHERE id = ?", (call_id,)).fetchone()
                befores[call_id] = json.loads(row["usage_before"]) if row and row["usage_before"] else None
            return befores

        befores = _db_write(write) or {}
        for call_id, _finished, label in updates:
            parts = []
            for key, name in (("five_hour", "5-hour"), ("seven_day", "weekly")):
                d = _window_delta(befores.get(call_id), compact, key)
                if d and d["delta"] is not None:
                    parts.append(f"{name} {d['after']:g}% ({d['delta']:+g})")
                elif compact.get(key) is not None:
                    parts.append(f"{name} {compact[key]:g}%")
            if parts:
                log(f"Usage after {label}: " + " · ".join(parts), "INFO")

    def _request(self, token, at, plan):
        req = urllib.request.Request(USAGE_URL, headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        })
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("response is not a JSON object")
            return _normalize(data, at, plan), None
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                self.rejected_token = token
                return None, f"The usage endpoint rejected Claude Code's login token (HTTP {e.code}). The CLI renews it on its next call."
            if e.code == 429:
                try:
                    retry = float(e.headers.get("Retry-After") or 120)
                except ValueError:
                    retry = 120
                self.backoff_until = at + min(max(retry, 30), 900)
                return None, "The usage endpoint is rate-limiting the bridge; it will try again later."
            self.backoff_until = at + 60
            return None, f"The usage endpoint answered HTTP {e.code}."
        except (urllib.error.URLError, OSError, ValueError) as e:
            self.backoff_until = at + 30
            return None, f"Couldn't reach the usage endpoint: {getattr(e, 'reason', e)}"

    def status(self, refresh=False) -> dict:
        if refresh and time.time() - self.last_attempt >= MIN_FETCH_INTERVAL and time.time() >= self.backoff_until:
            self.fetch()
        with self.lock:
            return {
                "snapshot": self.latest,
                "error": self.error,
                "plan": self.plan or (self.latest or {}).get("plan"),
                "last_attempt": self.last_attempt or None,
                "cli": _cli_status(self.cli),
            }


def _cli_status(cli):
    """The last rate_limit_event from a CLI call, in the snapshot's window
    shape. The CLI reports utilization as a fraction of the window."""
    if not cli:
        return None
    info = cli["info"]
    windows = []
    for key, label in (("five_hour", "Current session (5-hour)"),
                       ("seven_day", "Weekly · all models"),
                       ("seven_day_overage_included", "Weekly · overage included")):
        w = (info.get("unifiedWindows") or {}).get(key)
        if isinstance(w, dict) and _num(w.get("utilization")) is not None:
            limiting = info.get("rateLimitType") == key
            windows.append({
                "key": key,
                "label": label,
                "percent": round(_num(w["utilization"]) * 100, 1),
                "resets_at": _num(w.get("resetsAt")),
                "severity": {"allowed_warning": "warning", "rejected": "critical"}.get(info.get("status"), "normal") if limiting else "normal",
                "active": limiting,
            })
    return {
        "at": cli["at"],
        "status": info.get("status"),
        "rate_limit_type": info.get("rateLimitType"),
        "resets_at": _num(info.get("resetsAt")),
        "windows": windows,
    }


_usage = _UsageTracker()


def usage_status(refresh=False) -> dict:
    """Latest usage reading for the Usage tab. refresh=True reads the
    endpoint first unless it was read in the last few seconds."""
    try:
        return _usage.status(refresh)
    except Exception as e:
        log(f"Usage status failed: {e}", "WARN")
        return {"snapshot": None, "error": str(e), "plan": None, "last_attempt": None, "cli": None}


# =============================================================================
# Queries for the dashboard
# =============================================================================

_SUMMARY_COLUMNS = (
    "id, started, finished, kind, label, char_key, model, effort, session_id, resumed, note, "
    "status, error, stop_reason, input_tokens, output_tokens, cache_read_tokens, "
    "cache_creation_tokens, cost_usd, rate_limit_json, usage_before, usage_after, "
    "system_prompt_chars, prompt_chars, response_chars, (prompt IS NOT NULL) AS has_text"
)


def _summary(row) -> dict:
    d = dict(row)
    before = json.loads(d.pop("usage_before")) if d.get("usage_before") else None
    after = json.loads(d.pop("usage_after")) if d.get("usage_after") else None
    d.pop("usage_before", None)
    d.pop("usage_after", None)
    rate_limit = json.loads(d.pop("rate_limit_json")) if d.get("rate_limit_json") else None
    d.pop("rate_limit_json", None)
    d["rate_limit_status"] = (rate_limit or {}).get("status")
    # Which window a warning or rejection is about, for the History list.
    d["rate_limit_window"] = {
        "type": rate_limit.get("rateLimitType"),
        "utilization": _num(rate_limit.get("utilization")),
        "resets_at": _num(rate_limit.get("resetsAt")),
    } if rate_limit and rate_limit.get("status") in ("allowed_warning", "rejected") else None
    d["duration"] = round(d["finished"] - d["started"], 1) if d.get("finished") else None
    d["tokens_total"] = sum(d.get(k) or 0 for k in (
        "input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens"))
    d["usage"] = {
        "five_hour": _window_delta(before, after, "five_hour"),
        "seven_day": _window_delta(before, after, "seven_day"),
        "pending": before is not None and after is None and d.get("finished") is not None
                   and time.time() - d["finished"] < AFTER_MAX_LAG,
    }
    d["has_text"] = bool(d.get("has_text"))
    if d.get("error") and len(d["error"]) > 400:
        d["error"] = d["error"][:400] + "…"
    return d


def list_calls(limit=50, before_id=None, kind=None, query=None) -> dict:
    """Newest calls first, without their text."""
    limit = max(1, min(int(limit or 50), 200))
    where, args = [], []
    if before_id:
        where.append("id < ?")
        args.append(int(before_id))
    if kind:
        where.append("kind = ?")
        args.append(kind)
    if query:
        like = f"%{query}%"
        where.append("(label LIKE ? OR prompt LIKE ? OR response LIKE ? OR char_key LIKE ?)")
        args += [like, like, like, like]
    sql = f"SELECT {_SUMMARY_COLUMNS} FROM calls"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit + 1)
    rows = _db_read(lambda conn: conn.execute(sql, args).fetchall(), [])
    return {"calls": [_summary(r) for r in rows[:limit]], "has_more": len(rows) > limit}


def get_call(call_id: int) -> Optional[dict]:
    """One call with its full text."""
    def read(conn):
        row = conn.execute(
            f"SELECT {_SUMMARY_COLUMNS}, argv, prompt, response, thinking, usage_json, "
            "rate_limit_json AS rate_limit_raw, system_prompt_hash, usage_before AS before_raw, "
            "usage_after AS after_raw FROM calls WHERE id = ?",
            (int(call_id),),
        ).fetchone()
        if not row:
            return None
        sp = None
        if row["system_prompt_hash"]:
            sp_row = conn.execute("SELECT text FROM system_prompts WHERE hash = ?", (row["system_prompt_hash"],)).fetchone()
            sp = sp_row["text"] if sp_row else None
            # The first call that used this system prompt, so the dashboard
            # can say a resumed turn re-sent the same one.
            first = conn.execute(
                "SELECT MIN(id) AS id FROM calls WHERE system_prompt_hash = ?", (row["system_prompt_hash"],)
            ).fetchone()
        else:
            first = None
        return row, sp, (first["id"] if first else None)

    found = _db_read(read)
    if not found:
        return None
    row, system_prompt, first_id = found
    d = _summary(row)
    for key in ("argv", "usage_json", "rate_limit_raw", "before_raw", "after_raw"):
        d[key] = json.loads(row[key]) if row[key] else None
    d["usage_details"] = d.pop("usage_json")
    d["rate_limit"] = d.pop("rate_limit_raw")
    d["usage_before"] = d.pop("before_raw")
    d["usage_after"] = d.pop("after_raw")
    d["prompt"] = row["prompt"]
    d["response"] = row["response"]
    d["thinking"] = row["thinking"]
    d["system_prompt"] = system_prompt
    d["system_prompt_first_id"] = first_id
    return d


def clear() -> int:
    """Delete every recorded call. Returns how many there were."""
    def write(conn):
        n = conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
        conn.execute("DELETE FROM calls")
        conn.execute("DELETE FROM system_prompts")
        return n
    return _db_write(write) or 0


def totals(since: float) -> dict:
    """Token totals of the calls started since `since`, by kind and by model,
    plus the summed usage deltas of tracked turns."""
    def read(conn):
        return conn.execute(
            "SELECT kind, model, input_tokens, output_tokens, cache_read_tokens, "
            "cache_creation_tokens, cost_usd, usage_before, usage_after FROM calls WHERE started >= ?",
            (since,),
        ).fetchall()

    rows = _db_read(read, [])
    token_keys = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")

    def empty():
        return {"calls": 0, **{k: 0 for k in token_keys}, "cost_usd": 0.0}

    total, by_kind, by_model = empty(), {}, {}
    deltas = {"five_hour": 0.0, "seven_day": 0.0, "turns_measured": 0}
    for r in rows:
        for bucket in (total, by_kind.setdefault(r["kind"], empty()),
                       by_model.setdefault(r["model"] or "unknown", empty())):
            bucket["calls"] += 1
            for k in token_keys:
                bucket[k] += r[k] or 0
            bucket["cost_usd"] += r["cost_usd"] or 0
        if r["usage_before"] and r["usage_after"]:
            before, after = json.loads(r["usage_before"]), json.loads(r["usage_after"])
            measured = False
            for key in ("five_hour", "seven_day"):
                d = _window_delta(before, after, key)
                if d and d["delta"] is not None:
                    deltas[key] += d["delta"]
                    measured = True
            deltas["turns_measured"] += int(measured)
    return {"since": since, "total": total, "by_kind": by_kind, "by_model": by_model, "usage_deltas": deltas}

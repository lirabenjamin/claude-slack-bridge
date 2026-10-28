"""
Live progress reporting for Slack-originated Claude runs.

- StreamParser: consumes `claude -p --output-format stream-json --verbose`
  lines, tracks tool steps, the latest assistant text, and the final result.
- StatusReporter: posts one "⏳ Working" message in the thread and edits it in
  place (throttled) as steps arrive; final edit is ✅ / ❌ / ⏹.
- Full step logs persist to tmp/progress/ so a 🔍 (`mag`) reaction on the
  status message can post the whole list later.

Every Slack call here is best-effort: failures are logged, never raised, so a
status problem can never break the run or its final reply.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("bridge.progress")

PROGRESS_DIR = Path(__file__).resolve().parent / "tmp" / "progress"
EDIT_INTERVAL_SECONDS = 3.0      # never edit more often than this
HEARTBEAT_SECONDS = 30.0         # refresh elapsed time even with no new steps
SHOW_LAST_STEPS = 5
SHORT_ARG_CHARS = 60
LONG_ARG_CHARS = 200
TEXT_PREVIEW_CHARS = 100
LOG_CHUNK_CHARS = 3500
LOG_RETENTION_SECONDS = 3 * 24 * 3600
FULL_LOG_EMOJI = "mag"
# Flip to True once the Slack app subscribes to the `reaction_added` bot event
# (reactions:read is already granted). While False, long runs get the full step
# log as a .txt snippet instead, and the status hint points there.
REACTION_EVENTS_ENABLED = True

# ---------------------------------------------------------------- redaction

_REDACTIONS = [
    (re.compile(r"([?&]t=)[^&\s'\"`>]+"), r"\1REDACTED"),
    (re.compile(r"xox[a-z]-[A-Za-z0-9-]+"), "xox?-REDACTED"),
    (re.compile(r"xapp-[A-Za-z0-9-]+"), "xapp-REDACTED"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"), "sk-REDACTED"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "gh_REDACTED"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), "github_pat_REDACTED"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AKIA-REDACTED"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"), "AIza-REDACTED"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"), r"\1REDACTED"),
    (re.compile(r"(://[^:/\s@]+:)[^@\s]+@"), r"\1REDACTED@"),
    (re.compile(r"(?i)((?:api[_-]?key|token|secret|password)\s*[=:]\s*)['\"]?[^\s'\"&]{6,}"), r"\1REDACTED"),
]


def redact(s: str) -> str:
    for pat, repl in _REDACTIONS:
        s = pat.sub(repl, s)
    return s


def _clip(s: str, n: int) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


# ------------------------------------------------------------ step formatting

def tool_label(name: str) -> str:
    if name.startswith("mcp__"):
        parts = name.split("__")
        server = parts[1] if len(parts) > 1 else "mcp"
        server = server.replace("claude_ai_", "").replace("plugin_", "")
        return f"{server}.{parts[-1]}" if len(parts) > 2 else server
    return name


def _rel(path: str, cwd: str) -> str:
    try:
        p = Path(path)
        if p.is_absolute() and cwd:
            return os.path.relpath(p, cwd) if str(p).startswith(str(Path(cwd))) else str(p)
    except Exception:
        pass
    return str(path)


def tool_arg(name: str, inp: Any, cwd: str) -> str:
    if not isinstance(inp, dict):
        return ""
    if name in ("Read", "Edit", "Write", "MultiEdit", "NotebookEdit"):
        p = inp.get("file_path") or inp.get("notebook_path") or ""
        return _rel(p, cwd)
    if name == "Grep":
        return str(inp.get("pattern", ""))
    if name == "Glob":
        return str(inp.get("pattern", ""))
    if name == "Bash":
        return str(inp.get("command", ""))
    if name in ("Agent", "Task"):
        return str(inp.get("description") or inp.get("subagent_type") or "")
    if name == "WebSearch":
        return str(inp.get("query", ""))
    if name == "WebFetch":
        return str(inp.get("url", ""))
    if name == "Skill":
        return str(inp.get("skill") or inp.get("name") or "")
    if name == "TodoWrite":
        return f"{len(inp.get('todos') or [])} items"
    for v in inp.values():  # MCP and anything else: first short string value
        if isinstance(v, str) and v.strip():
            return v
    return ""


def fmt_elapsed(seconds: float) -> str:
    s = int(max(0, seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {(s % 3600) // 60:02d}m"


# ------------------------------------------------------------------- parser

class StreamParser:
    """Thread-safe accumulator for stream-json events."""

    def __init__(self, cwd: str = "", started_at: float | None = None):
        self.cwd = cwd
        self.started_at = started_at or time.time()
        self.lock = threading.Lock()
        self.steps: list[dict] = []   # {"t", "tool", "short", "long", "sub"}
        self.last_text = ""
        self.result: dict | None = None
        self.session_id: str | None = None
        self.version = 0              # bumps on any visible change

    def feed_line(self, line: str) -> dict | None:
        line = line.strip()
        if not line:
            return None
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            return None
        if isinstance(ev, dict):
            self.feed_event(ev)
            return ev
        return None

    def feed_event(self, ev: dict) -> None:
        t = ev.get("type")
        with self.lock:
            if ev.get("session_id") and not self.session_id:
                self.session_id = ev["session_id"]
            if t == "result":
                self.result = ev
                if ev.get("session_id"):
                    self.session_id = ev["session_id"]
                return
            if t != "assistant":
                return
            msg = ev.get("message") or {}
            sub = bool(ev.get("parent_tool_use_id"))
            for block in msg.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    name = block.get("name") or "?"
                    arg = redact(tool_arg(name, block.get("input"), self.cwd))
                    self.steps.append({
                        "t": round(time.time() - self.started_at, 1),
                        "tool": tool_label(name),
                        "short": _clip(arg, SHORT_ARG_CHARS),
                        "long": _clip(arg, LONG_ARG_CHARS),
                        "sub": sub,
                    })
                    self.version += 1
                elif block.get("type") == "text" and not sub:
                    txt = " ".join((block.get("text") or "").split())
                    if txt:
                        self.last_text = redact(txt)
                        self.version += 1

    def snapshot(self) -> tuple[int, list[dict], str]:
        with self.lock:
            return self.version, list(self.steps), self.last_text


def _code_safe(s: str) -> str:
    return s.replace("```", "'''")


def render_status(state: str, elapsed: float, steps: list[dict], last_text: str) -> str:
    icon = {"working": "⏳ Working", "done": "✅ Done", "failed": "❌ Failed",
            "aborted": "⏹ Aborted"}[state]
    n = len(steps)
    lines = [f"{icon} · {fmt_elapsed(elapsed)} · {n} step{'' if n == 1 else 's'}"]
    tail = steps[-SHOW_LAST_STEPS:]
    if tail:
        body = "\n".join(
            f"{'↳ ' if s['sub'] else ''}{s['tool']}  {s['short']}".rstrip() for s in tail
        )
        lines.append("```" + _code_safe(body) + "```")
    if last_text and state == "working":
        lines.append(f"→ {_clip(last_text, TEXT_PREVIEW_CHARS)}")
    if state != "working" and n > SHOW_LAST_STEPS:
        lines.append(f"_react :{FULL_LOG_EMOJI}: for the full step log_" if REACTION_EVENTS_ENABLED
                     else "_full step log attached below_")
    return "\n".join(lines)


def render_full_log(steps: list[dict]) -> str:
    rows = [
        f"{i:>3}. +{fmt_elapsed(s['t']):>7}  {'↳ ' if s['sub'] else ''}{s['tool']}  {s['long']}".rstrip()
        for i, s in enumerate(steps, 1)
    ]
    return "\n".join(rows) if rows else "(no tool steps)"


def chunk_lines(text: str, size: int = LOG_CHUNK_CHARS) -> list[str]:
    chunks, cur = [], ""
    for line in text.splitlines():
        if cur and len(cur) + len(line) + 1 > size:
            chunks.append(cur)
            cur = ""
        cur = f"{cur}\n{line}" if cur else line[:size]
    if cur:
        chunks.append(cur)
    return chunks or [""]


# ---------------------------------------------------------- persistence

def _log_path(channel: str, status_ts: str) -> Path:
    return PROGRESS_DIR / f"{channel}-{status_ts}.json"


def save_log(channel: str, thread_ts: str, status_ts: str, steps: list[dict]) -> None:
    try:
        PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
        p = _log_path(channel, status_ts)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"channel": channel, "thread_ts": thread_ts,
                                   "status_ts": status_ts, "steps": steps}))
        tmp.replace(p)
    except Exception:
        log.exception("progress: saving step log failed")


def load_log(channel: str, status_ts: str) -> dict | None:
    p = _log_path(channel, status_ts)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        log.exception("progress: reading step log failed")
        return None


def prune_logs(max_age: float = LOG_RETENTION_SECONDS) -> None:
    try:
        cutoff = time.time() - max_age
        for p in PROGRESS_DIR.glob("*.json"):
            if p.stat().st_mtime < cutoff:
                p.unlink()
    except Exception:
        log.debug("progress: prune failed", exc_info=True)


# ---------------------------------------------------------- slack reporter

def _slack_error(e: Exception) -> tuple[str, float]:
    """Return (error_code, retry_after_seconds) for a SlackApiError-ish exception."""
    resp = getattr(e, "response", None)
    code, retry = "", 0.0
    try:
        code = (resp.get("error") if resp is not None else "") or ""
        hdrs = getattr(resp, "headers", None) or {}
        ra = hdrs.get("Retry-After") or hdrs.get("retry-after")
        retry = float(ra) if ra else 0.0
    except Exception:
        pass
    if code == "ratelimited" and retry <= 0:
        retry = 10.0
    return code, retry


class StatusReporter:
    def __init__(self, client, channel: str, thread_ts: str, parser: StreamParser,
                 clock=time.time, edit_interval: float = EDIT_INTERVAL_SECONDS,
                 heartbeat: float = HEARTBEAT_SECONDS):
        self.client = client
        self.channel = channel
        self.thread_ts = thread_ts
        self.parser = parser
        self.clock = clock
        self.edit_interval = edit_interval
        self.heartbeat = heartbeat
        self.started_at = parser.started_at
        self.status_ts: str | None = None
        self.last_edit_at = 0.0
        self.last_version = -1
        self.blocked_until = 0.0
        self.edits = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._edit_lock = threading.Lock()

    # -- lifecycle
    def start(self) -> None:
        try:
            resp = self.client.chat_postMessage(
                channel=self.channel, thread_ts=self.thread_ts,
                text=render_status("working", 0, [], ""),
            )
            self.status_ts = resp.get("ts")
            self.last_edit_at = self.clock()
            self.last_version = 0
        except Exception:
            log.exception("progress: posting status message failed")
            return
        self._thread = threading.Thread(target=self._loop, name="progress-status", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(0.5):
            try:
                self.maybe_edit()
            except Exception:
                log.exception("progress: status tick failed")

    def maybe_edit(self, force: bool = False) -> bool:
        if not self.status_ts:
            return False
        now = self.clock()
        version, steps, text = self.parser.snapshot()
        if not force:
            if now < self.blocked_until or now - self.last_edit_at < self.edit_interval:
                return False
            changed = version != self.last_version
            if not changed and now - self.last_edit_at < self.heartbeat:
                return False
        ok = self._edit(render_status("working", now - self.started_at, steps, text))
        self.last_edit_at = now
        if ok:
            self.last_version = version
            save_log(self.channel, self.thread_ts, self.status_ts, steps)
        return ok

    def _edit(self, text: str) -> bool:
        with self._edit_lock:
            try:
                self.client.chat_update(channel=self.channel, ts=self.status_ts, text=text)
                self.edits += 1
                return True
            except Exception as e:
                code, retry = _slack_error(e)
                if code == "ratelimited":
                    self.blocked_until = self.clock() + retry
                    log.warning("progress: ratelimited, skipping edits for %.0fs", retry)
                else:
                    log.warning("progress: chat_update failed: %s", code or e)
                return False

    def finish(self, state: str) -> None:
        """state in {'done','failed','aborted'}. Never raises."""
        try:
            self._stop.set()
            if self._thread:
                self._thread.join(timeout=5)
            if not self.status_ts:
                return
            _, steps, text = self.parser.snapshot()
            final = render_status(state, self.clock() - self.started_at, steps, text)
            save_log(self.channel, self.thread_ts, self.status_ts, steps)
            log.info("progress: status %s ts=%s state=%s steps=%d edits=%d",
                     self.channel, self.status_ts, state, len(steps), self.edits + 1)
            for _ in range(3):
                wait = self.blocked_until - self.clock()
                if wait > 0:
                    if wait > 30:
                        break
                    time.sleep(wait)
                if self._edit(final):
                    break
                if self.blocked_until <= self.clock():
                    break  # non-ratelimit failure: don't hammer
            prune_logs()
        except Exception:
            log.exception("progress: finish failed")

    @property
    def n_steps(self) -> int:
        return len(self.parser.snapshot()[1])

    def post_log_snippet(self) -> None:
        """Fallback when 🔍 reactions can't be received: one collapsed .txt snippet."""
        try:
            _, steps, _ = self.parser.snapshot()
            self.client.files_upload_v2(
                channel=self.channel, thread_ts=self.thread_ts,
                content=render_full_log(steps), filename="step-log.txt",
                title=f"Full step log ({len(steps)} steps)",
            )
        except Exception:
            log.exception("progress: step-log snippet upload failed")


# ----------------------------------------------------------- 🔍 handler

_RECENT_LOG_POSTS: dict[str, float] = {}
_RECENT_LOCK = threading.Lock()


def handle_full_log_reaction(client, event: dict, bot_user_id: str | None) -> None:
    """Post the full step list when someone reacts :mag: on a status message."""
    try:
        if event.get("reaction") != FULL_LOG_EMOJI:
            return
        if bot_user_id and event.get("user") == bot_user_id:
            return
        item = event.get("item") or {}
        if item.get("type") != "message":
            return
        channel, ts = item.get("channel"), item.get("ts")
        if not channel or not ts:
            return
        data = load_log(channel, ts)
        if not data:
            return
        key = f"{channel}:{ts}"
        with _RECENT_LOCK:
            if time.time() - _RECENT_LOG_POSTS.get(key, 0) < 10:
                return
            _RECENT_LOG_POSTS[key] = time.time()
        steps = data.get("steps") or []
        chunks = chunk_lines(render_full_log(steps))
        for i, chunk in enumerate(chunks):
            head = f"*Full step log* ({len(steps)} steps)" if i == 0 else f"_(cont. {i + 1}/{len(chunks)})_"
            client.chat_postMessage(
                channel=channel, thread_ts=data.get("thread_ts") or ts,
                text=f"{head}\n```{_code_safe(chunk)}```",
            )
        log.info("progress: posted full step log (%d steps) for %s", len(steps), key)
    except Exception:
        log.exception("progress: full-log reaction handler failed")

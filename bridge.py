"""
Slack ↔ Claude Code bridge.

Listens to a Slack workspace via Socket Mode. Each top-level message in a
mapped channel starts a new Claude Code conversation in that channel's
project directory; replies in the resulting thread continue the same
conversation. The bot reacts with :eyes: on receipt and posts Claude's
reply (chunked if long) into the thread.
"""

from __future__ import annotations

import collections
import concurrent.futures
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode.websocket_client import SocketModeHandler

import pip_api
import progress

ROOT = Path(__file__).resolve().parent
BIN_DIR = ROOT / "bin"
TMP_DIR = ROOT / "tmp"
CONFIG_PATH = ROOT / "config.yaml"
SESSIONS_PATH = ROOT / "sessions.json"
LOG_PATH = ROOT / "bridge.log"

CLAUDE_TIMEOUT_SECONDS = 60 * 10
MAX_TIMEOUT_SECONDS = 4 * 3600
TIMEOUT_DIRECTIVE_RE = re.compile(
    r"^\s*!timeout\s+(\d+)\s*([smh])?\b[ \t]*\n?", re.IGNORECASE
)
SLACK_CHUNK_CHARS = 3500
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".pdf"}
ABORT_KEYWORDS = {"stop", "abort", "cancel"}

TMP_DIR.mkdir(exist_ok=True)


def resolve_claude_bin() -> str:
    explicit = os.environ.get("CLAUDE_BIN")
    if explicit and Path(explicit).exists():
        return explicit
    candidates = [
        Path.home() / ".claude" / "local" / "claude",
        Path("/opt/homebrew/bin/claude"),
        Path("/usr/local/bin/claude"),
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    found = shutil.which(
        "claude",
        path=os.pathsep.join(
            [os.environ.get("PATH", ""), "/opt/homebrew/bin", "/usr/local/bin"]
        ),
    )
    if found:
        return found
    raise RuntimeError(
        "could not find the `claude` CLI. Set CLAUDE_BIN in .env to its absolute path."
    )


CLAUDE_BIN = resolve_claude_bin()

load_dotenv(ROOT / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("bridge")


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        log.error("config.yaml not found at %s — copy config.yaml.example", CONFIG_PATH)
        sys.exit(1)
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f) or {}


CONFIG = load_config()
DEFAULT_CWD = os.path.expanduser(CONFIG.get("default_dir") or str(Path.home()))
DM_CWD = os.path.expanduser(CONFIG.get("dm_dir") or DEFAULT_CWD)
CHANNEL_MAP: dict[str, str] = {
    name: os.path.expanduser(path) for name, path in (CONFIG.get("channels") or {}).items()
}
# Channels shared with other people: only respond when the bot is @-mentioned
# or the message starts with "claude:". Everything else is human-to-human talk.
MENTION_ONLY_CHANNELS: set[str] = set(CONFIG.get("mention_only_channels") or [])
# Optional allowlists. When set, the bridge ignores every other user / channel.
ALLOWED_USERS: set[str] = set(CONFIG.get("allowed_users") or [])
ALLOWED_CHANNELS: set[str] = set(CONFIG.get("allowed_channels") or [])
# skip_permissions: true runs claude with --dangerously-skip-permissions.
# Otherwise claude runs with --permission-mode <permission_mode>.
SKIP_PERMISSIONS: bool = bool(CONFIG.get("skip_permissions", False))
PERMISSION_MODE: str = CONFIG.get("permission_mode") or "acceptEdits"
PIP_HOST: str = CONFIG.get("pip_host") or "127.0.0.1"
PIP_PORT: int = int(CONFIG.get("pip_port") or 8765)
PIP_USERNAME: str = CONFIG.get("pip_username") or "pip"
CLAUDE_PREFIX_RE = re.compile(r"^\s*claude\s*:\s*", re.IGNORECASE)
BOT_USER_ID: str | None = None

SESSIONS_LOCK = threading.Lock()


def load_sessions() -> dict[str, dict]:
    if not SESSIONS_PATH.exists():
        return {}
    try:
        return json.loads(SESSIONS_PATH.read_text())
    except json.JSONDecodeError:
        log.exception("sessions.json corrupt — starting fresh")
        return {}


def save_sessions(s: dict[str, dict]) -> None:
    SESSIONS_PATH.write_text(json.dumps(s, indent=2))


SESSIONS: dict[str, dict] = load_sessions()


def get_or_create_session(thread_key: str) -> tuple[str, bool]:
    with SESSIONS_LOCK:
        entry = SESSIONS.get(thread_key)
        if entry:
            return entry["session_id"], False
        sid = str(uuid.uuid4())
        SESSIONS[thread_key] = {"session_id": sid, "created_at": time.time()}
        save_sessions(SESSIONS)
        return sid, True


def mark_session_started(thread_key: str) -> None:
    with SESSIONS_LOCK:
        if thread_key in SESSIONS:
            SESSIONS[thread_key]["started"] = True
            save_sessions(SESSIONS)


def drop_session(thread_key: str) -> None:
    with SESSIONS_LOCK:
        if thread_key in SESSIONS:
            del SESSIONS[thread_key]
            save_sessions(SESSIONS)


def session_started(thread_key: str) -> bool:
    with SESSIONS_LOCK:
        return bool(SESSIONS.get(thread_key, {}).get("started"))


KEY_LOCKS: dict[str, threading.Lock] = {}
KEY_LOCKS_GUARD = threading.Lock()
EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=8)

THREAD_INFLIGHT_COUNT: dict[str, int] = {}
THREAD_INFLIGHT_GUARD = threading.Lock()
THREAD_BUSY_EMOJI = "thinking_face"

# Dedupes a single Slack message ts across the live event handler and the
# startup recovery sweep. Whichever path claims it first runs; the other no-ops.
PROCESSING_TS: set[str] = set()
PROCESSING_TS_LOCK = threading.Lock()

RECOVERY_LOOKBACK_SECONDS = 24 * 3600


def claim_message_ts(ts: str) -> bool:
    with PROCESSING_TS_LOCK:
        if ts in PROCESSING_TS:
            return False
        PROCESSING_TS.add(ts)
        return True


def release_message_ts(ts: str) -> None:
    with PROCESSING_TS_LOCK:
        PROCESSING_TS.discard(ts)


def thread_lock(key: str) -> threading.Lock:
    with KEY_LOCKS_GUARD:
        return KEY_LOCKS.setdefault(key, threading.Lock())


def thread_inflight_increment(thread_key: str) -> int:
    with THREAD_INFLIGHT_GUARD:
        n = THREAD_INFLIGHT_COUNT.get(thread_key, 0) + 1
        THREAD_INFLIGHT_COUNT[thread_key] = n
        return n


def thread_inflight_decrement(thread_key: str) -> int:
    with THREAD_INFLIGHT_GUARD:
        n = max(0, THREAD_INFLIGHT_COUNT.get(thread_key, 0) - 1)
        if n == 0:
            THREAD_INFLIGHT_COUNT.pop(thread_key, None)
        else:
            THREAD_INFLIGHT_COUNT[thread_key] = n
        return n


def mark_thread_busy(client, channel: str, thread_ts: str) -> None:
    try:
        client.reactions_add(channel=channel, name=THREAD_BUSY_EMOJI, timestamp=thread_ts)
    except Exception:
        log.debug("thread busy reaction add failed (ok if duplicate)", exc_info=True)


def clear_thread_busy(client, channel: str, thread_ts: str) -> None:
    try:
        client.reactions_remove(channel=channel, name=THREAD_BUSY_EMOJI, timestamp=thread_ts)
    except Exception:
        log.debug("thread busy reaction remove failed", exc_info=True)


SLACK_SYSTEM_ADDENDUM = (
    "You are running inside a Slack-triggered Claude Code session. Your final "
    "text reply will be posted as a message in the originating Slack thread.\n"
    "\n"
    "HEADLESS ENVIRONMENT — there is no display, no browser, and no user at a "
    "terminal. Never run `open`, `xdg-open`, or any other command that would "
    "launch a GUI app; to share a URL, paste it as text in your reply. Never "
    "start a dev server (`npm run dev`, `vite`, `next dev`, `python -m http.server`, "
    "etc.) in the foreground — those processes never exit and will block this "
    "session until it times out. If you genuinely need a long-running command, "
    "pass `run_in_background=true` to the Bash tool, and prefer short, finite "
    "commands for everything else.\n"
    "\n"
    "The user can abort this run by posting `stop`, `abort`, or `cancel` as a "
    "thread reply in Slack — that will SIGKILL this process group.\n"
    "\n"
    "Two CLI helpers are on PATH for posting back mid-run:\n"
    "  • `slack-post \"text\"` — post an interim message to the thread "
    "(useful for status updates during long runs).\n"
    "  • `slack-image <path> [caption]` — upload an image, PDF, or other file "
    "to the thread, with an optional caption.\n"
    "Any image files (.png, .jpg, .jpeg, .gif, .webp, .svg, .pdf) you create "
    "in the current working directory will also be auto-uploaded after the run "
    "finishes — you don't need to call slack-image for those unless you want a "
    "specific caption. The Slack channel name is in $SLACK_CHANNEL_NAME and "
    "the thread permalink is in $SLACK_PERMALINK."
)


def _kill_pgroup(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass
    except PermissionError:
        log.debug("killpg %s sig=%s permission denied", pgid, sig)


# Maps thread_key -> pgid of the running claude subprocess, so a follow-up
# Slack message saying "stop" can SIGKILL the pgroup instead of queuing
# behind the in-flight run on `thread_lock`.
INFLIGHT_PROCS: dict[str, int] = {}
INFLIGHT_PROCS_GUARD = threading.Lock()
ABORTED_KEYS: set[str] = set()
ABORTED_KEYS_GUARD = threading.Lock()


def register_inflight_pgid(thread_key: str, pgid: int) -> None:
    with INFLIGHT_PROCS_GUARD:
        INFLIGHT_PROCS[thread_key] = pgid


def clear_inflight_pgid(thread_key: str) -> None:
    with INFLIGHT_PROCS_GUARD:
        INFLIGHT_PROCS.pop(thread_key, None)


def get_inflight_pgid(thread_key: str) -> int | None:
    with INFLIGHT_PROCS_GUARD:
        return INFLIGHT_PROCS.get(thread_key)


def mark_aborted(thread_key: str) -> None:
    with ABORTED_KEYS_GUARD:
        ABORTED_KEYS.add(thread_key)


def claim_aborted(thread_key: str) -> bool:
    """Atomically check-and-clear the aborted flag for thread_key."""
    with ABORTED_KEYS_GUARD:
        if thread_key in ABORTED_KEYS:
            ABORTED_KEYS.discard(thread_key)
            return True
        return False


def parse_timeout_directive(text: str) -> tuple[str, int | None]:
    """Strip a leading `!timeout <duration>` directive and return (cleaned, seconds).

    Accepts forms like `!timeout 3600`, `!timeout 90s`, `!timeout 30m`, `!timeout 1h`.
    Bare integers are treated as seconds. Capped at MAX_TIMEOUT_SECONDS. Tolerates
    a single leading Slack inline-code backtick (and a matching closing one).
    """
    stripped = text.lstrip()
    leading_tick = stripped.startswith("`") and not stripped.startswith("```")
    if leading_tick:
        stripped = stripped[1:]
    m = TIMEOUT_DIRECTIVE_RE.match(stripped)
    if not m:
        return text, None
    n = int(m.group(1))
    unit = (m.group(2) or "s").lower()
    seconds = n * {"s": 1, "m": 60, "h": 3600}[unit]
    seconds = max(1, min(seconds, MAX_TIMEOUT_SECONDS))
    rest = stripped[m.end():]
    if leading_tick:
        rest = rest.lstrip()
        if rest.startswith("`"):
            rest = rest[1:].lstrip()
    return rest, seconds


def _stream_reader(stream, on_line) -> None:
    try:
        for line in stream:
            try:
                on_line(line)
            except Exception:
                log.exception("stream line handler failed")
    except Exception:
        log.debug("stream reader ended with error", exc_info=True)


def run_claude(
    prompt: str,
    session_id: str,
    is_new: bool,
    cwd: str,
    thread_key: str,
    slack_env: dict[str, str] | None = None,
    timeout_seconds: int | None = None,
    parser: "progress.StreamParser | None" = None,
) -> dict[str, Any]:
    """Run claude with stream-json output.

    stdout is read line by line in a reader thread and fed to `parser` (callers
    may pass their own to observe live progress); stderr is drained in its own
    thread so neither pipe can deadlock. The return dict shape is unchanged.
    """
    permission_args = (
        ["--dangerously-skip-permissions"] if SKIP_PERMISSIONS
        else ["--permission-mode", PERMISSION_MODE]
    )
    cmd = [
        CLAUDE_BIN,
        "-p",
        prompt,
        *permission_args,
        "--append-system-prompt",
        SLACK_SYSTEM_ADDENDUM,
        "--output-format",
        "stream-json",
        "--verbose",
    ]
    if is_new:
        cmd += ["--session-id", session_id]
    else:
        cmd += ["--resume", session_id]
    effective_timeout = timeout_seconds if timeout_seconds is not None else CLAUDE_TIMEOUT_SECONDS
    log.info(
        "claude run: cwd=%s session=%s new=%s timeout=%ds prompt=%r",
        cwd, session_id, is_new, effective_timeout, prompt[:120],
    )
    env = os.environ.copy()
    if slack_env:
        env.update({k: v for k, v in slack_env.items() if v is not None})
    env["PATH"] = f"{BIN_DIR}{os.pathsep}{env.get('PATH', '')}"
    if parser is None:
        parser = progress.StreamParser(cwd=cwd)
    stdout_tail: collections.deque[str] = collections.deque(maxlen=40)
    stderr_buf: collections.deque[str] = collections.deque(maxlen=400)

    def on_stdout(line: str) -> None:
        stdout_tail.append(line)
        parser.feed_line(line)

    # start_new_session=True puts claude (and all its descendants) in a new
    # process group so we can reap orphaned grandchildren — e.g. a `vite`
    # backgrounded by a Bash tool call — that would otherwise hold our stdout
    # pipe open and stall the reader threads until the timeout.
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=env,
        start_new_session=True,
    )
    pgid = proc.pid
    register_inflight_pgid(thread_key, pgid)
    readers = [
        threading.Thread(target=_stream_reader, args=(proc.stdout, on_stdout),
                         name="claude-stdout", daemon=True),
        threading.Thread(target=_stream_reader, args=(proc.stderr, stderr_buf.append),
                         name="claude-stderr", daemon=True),
    ]
    for t in readers:
        t.start()

    def join_readers(timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        for t in readers:
            t.join(max(0.0, deadline - time.monotonic()))
        return not any(t.is_alive() for t in readers)

    try:
        try:
            proc.wait(timeout=effective_timeout)
        except subprocess.TimeoutExpired:
            _kill_pgroup(pgid, signal.SIGKILL)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            join_readers(10)
            if claim_aborted(thread_key):
                return {"ok": False, "aborted": True, "error": "aborted by user"}
            return {"ok": False, "error": f"claude timed out after {effective_timeout}s"}
        # Claude itself has exited. Sweep any backgrounded grandchildren so the
        # pipes close and the reader threads finish immediately.
        _kill_pgroup(pgid, signal.SIGTERM)
        if not join_readers(10):
            _kill_pgroup(pgid, signal.SIGKILL)
            join_readers(5)
        if claim_aborted(thread_key):
            return {"ok": False, "aborted": True, "error": "aborted by user"}
        stderr = "".join(stderr_buf)
        if proc.returncode != 0:
            res_txt = (parser.result or {}).get("result") or ""
            res_part = f"\nresult: {res_txt[:1000]}" if res_txt else ""
            return {
                "ok": False,
                "error": f"claude exited {proc.returncode}{res_part}\nstderr:\n{stderr[-2000:]}",
            }
        payload = parser.result
        if payload is None:
            return {
                "ok": False,
                "error": f"could not parse claude output (no result event):\n{''.join(stdout_tail)[-2000:]}",
            }
        if payload.get("is_error"):
            return {"ok": False, "error": f"claude reported error: {payload.get('result') or payload.get('subtype') or payload}"}
        return {
            "ok": True,
            "text": payload.get("result", "(empty response)"),
            "cost_usd": payload.get("total_cost_usd"),
            "duration_ms": payload.get("duration_ms"),
            "session_id": payload.get("session_id") or parser.session_id,
        }
    finally:
        clear_inflight_pgid(thread_key)


def chunk_text(text: str, size: int = SLACK_CHUNK_CHARS) -> list[str]:
    if len(text) <= size:
        return [text]
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= size:
            chunks.append(remaining)
            break
        split_at = remaining.rfind("\n", 0, size)
        if split_at < size // 2:
            split_at = size
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n")
    return chunks


def channel_name(client, channel_id: str, channel_type: str | None) -> str:
    if channel_type == "im":
        return ""
    try:
        info = client.conversations_info(channel=channel_id)
        return info["channel"].get("name", "") or ""
    except Exception:
        log.exception("conversations_info failed for %s", channel_id)
        return ""


def cwd_for_channel(name: str, channel_type: str | None) -> str:
    if channel_type == "im":
        return DM_CWD
    if name in CHANNEL_MAP:
        cwd = CHANNEL_MAP[name]
        if not Path(cwd).exists():
            log.warning("configured cwd does not exist: %s — falling back to default", cwd)
            return DEFAULT_CWD
        return cwd
    log.info("no mapping for channel %r — using default %s", name, DEFAULT_CWD)
    return DEFAULT_CWD


def slack_permalink(client, channel: str, message_ts: str) -> str:
    try:
        return client.chat_getPermalink(channel=channel, message_ts=message_ts)["permalink"]
    except Exception:
        log.debug("permalink lookup failed", exc_info=True)
        return ""


def post_chunks(client, channel: str, thread_ts: str, text: str, footer: str = "") -> None:
    chunks = chunk_text(text)
    for i, chunk in enumerate(chunks):
        body = chunk
        if i == len(chunks) - 1 and footer:
            body = f"{chunk}\n\n{footer}"
        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=body,
            blocks=[{"type": "markdown", "text": body}],
        )


def auto_upload_new_images(
    client,
    channel: str,
    thread_ts: str,
    cwd: str,
    started_at: float,
    already_posted: set[str],
) -> int:
    cwd_path = Path(cwd)
    if not cwd_path.exists():
        return 0
    uploaded = 0
    try:
        entries = list(cwd_path.iterdir())
    except OSError:
        log.exception("could not list cwd %s", cwd)
        return 0
    for entry in entries:
        if not entry.is_file():
            continue
        if entry.suffix.lower() not in IMAGE_EXTS:
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        if mtime < started_at:
            continue
        rp = str(entry.resolve())
        if rp in already_posted:
            continue
        try:
            client.files_upload_v2(
                channel=channel,
                thread_ts=thread_ts,
                file=str(entry),
                title=entry.name,
            )
            log.info("auto-uploaded %s to %s", entry.name, channel)
            uploaded += 1
        except Exception:
            log.exception("auto-upload failed for %s", entry.name)
    return uploaded


def addressed_text(text: str) -> str | None:
    """Return the text with the bot mention / "claude:" prefix removed, or None
    if the message is not addressed to the bot."""
    mention = f"<@{BOT_USER_ID}>" if BOT_USER_ID else None
    if mention and mention in text:
        return text.replace(mention, "").strip() or None
    if CLAUDE_PREFIX_RE.match(text):
        return CLAUDE_PREFIX_RE.sub("", text, count=1).strip() or None
    return None


def handle_message_async(client, event: dict[str, Any]) -> None:
    channel = event["channel"]
    user_ts = event["ts"]
    thread_ts = event.get("thread_ts") or user_ts
    text = (event.get("text") or "").strip()
    channel_type = event.get("channel_type")

    if not text or not is_allowed(event):
        return

    if MENTION_ONLY_CHANNELS and channel_type != "im":
        if channel_name(client, channel, channel_type) in MENTION_ONLY_CHANNELS:
            text = addressed_text(text)
            if not text:
                log.info("mention-only: ts=%s not addressed to bot, skipping", user_ts)
                return
            event = {**event, "text": text}

    if not claim_message_ts(user_ts):
        log.info("dedup: ts=%s already in flight, skipping", user_ts)
        return
    try:
        # Abort fast-path: a reply matching `stop`/`abort`/`cancel` kills the
        # in-flight pgroup instead of queuing behind it on `thread_lock`.
        is_reply = user_ts != thread_ts
        if is_reply and text.lower() in ABORT_KEYWORDS:
            handle_abort(client, channel, user_ts, thread_ts)
            return
        _handle_message_locked(client, event, channel, user_ts, thread_ts, text, channel_type)
    finally:
        release_message_ts(user_ts)


def handle_abort(client, channel: str, user_ts: str, thread_ts: str) -> None:
    thread_key = f"{channel}:{thread_ts}"
    pgid = get_inflight_pgid(thread_key)
    if pgid is None:
        log.info("abort: no in-flight run for thread_key=%s", thread_key)
        try:
            client.reactions_add(channel=channel, name="information_source", timestamp=user_ts)
        except Exception:
            log.debug("abort no-op reaction failed", exc_info=True)
        try:
            client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text="_no in-flight run to abort_",
            )
        except Exception:
            log.debug("abort no-op message failed", exc_info=True)
        return
    log.info("abort: SIGKILL pgid=%s for thread_key=%s", pgid, thread_key)
    mark_aborted(thread_key)
    _kill_pgroup(pgid, signal.SIGKILL)
    try:
        client.reactions_add(channel=channel, name="octagonal_sign", timestamp=user_ts)
    except Exception:
        log.debug("abort reaction add failed", exc_info=True)


def _handle_message_locked(
    client,
    event: dict[str, Any],
    channel: str,
    user_ts: str,
    thread_ts: str,
    text: str,
    channel_type: str | None,
) -> None:
    thread_key = f"{channel}:{thread_ts}"
    ch_name = channel_name(client, channel, channel_type)
    cwd = cwd_for_channel(ch_name, channel_type)
    permalink = slack_permalink(client, channel, thread_ts)
    posted_log = TMP_DIR / f"posted-{user_ts}.log"
    posted_log.touch()
    slack_env = {
        "SLACK_CHANNEL_NAME": ch_name,
        "SLACK_CHANNEL_ID": channel,
        "SLACK_THREAD_TS": thread_ts,
        "SLACK_PERMALINK": permalink,
        "SLACK_USER": event.get("user", ""),
        "SLACK_POSTED_FILES_LOG": str(posted_log),
    }

    try:
        client.reactions_add(channel=channel, name="eyes", timestamp=user_ts)
    except Exception as e:
        log.warning("reactions_add eyes failed: %s", e)

    is_reply = user_ts != thread_ts
    if is_reply:
        thread_inflight_increment(thread_key)
        mark_thread_busy(client, channel, thread_ts)

    with thread_lock(thread_key):
        session_id, just_created = get_or_create_session(thread_key)
        is_new = just_created or not session_started(thread_key)
        prompt_text, timeout_override = parse_timeout_directive(text)
        started_at = time.time()
        pip_api.mark_inflight(thread_key, prompt_text)
        parser = progress.StreamParser(cwd=cwd, started_at=started_at)
        reporter = progress.StatusReporter(client, channel, thread_ts, parser)
        reporter.start()
        result: dict[str, Any] = {"ok": False, "error": "bridge error before claude finished"}
        try:
            result = run_claude(
                prompt_text, session_id, is_new, cwd, thread_key,
                slack_env=slack_env, timeout_seconds=timeout_override,
                parser=parser,
            )
        finally:
            pip_api.clear_inflight(thread_key)
            reporter.finish(
                "aborted" if result.get("aborted") else "done" if result.get("ok") else "failed"
            )
        if not progress.REACTION_EVENTS_ENABLED and reporter.n_steps > progress.SHOW_LAST_STEPS:
            reporter.post_log_snippet()
        try:
            already_posted = set(posted_log.read_text().splitlines()) if posted_log.exists() else set()
        except OSError:
            already_posted = set()
        try:
            n_uploaded = auto_upload_new_images(
                client, channel, thread_ts, cwd, started_at, already_posted
            )
            if n_uploaded:
                log.info("auto-uploaded %d image(s) to thread %s", n_uploaded, thread_ts)
        except Exception:
            log.exception("auto-upload pass failed")
        try:
            posted_log.unlink()
        except OSError:
            pass
        if result["ok"]:
            mark_session_started(thread_key)
            reply_text = (result.get("text") or "").strip()
            log.info("claude ok: session=%s reply_len=%d", session_id, len(reply_text))
            footer_bits = []
            if result.get("duration_ms") is not None:
                footer_bits.append(f"_{result['duration_ms']/1000:.1f}s_")
            footer = " · ".join(footer_bits)
            if not reply_text:
                reply_text = "_(claude returned no reply text — task may have completed silently)_"
            try:
                post_chunks(client, channel, thread_ts, reply_text, footer)
                log.info("posted reply to %s thread=%s", channel, thread_ts)
            except Exception:
                log.exception("posting reply failed")
        elif result.get("aborted"):
            log.info("claude run aborted by user: thread=%s", thread_key)
        else:
            if is_new:
                drop_session(thread_key)
            err = result.get("error", "unknown error")
            log.warning("claude run failed: %s", err)
            try:
                client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_ts,
                    text=f":warning: {err[:3000]}",
                )
            except Exception:
                log.exception("posting error failed")
        try:
            client.reactions_remove(channel=channel, name="eyes", timestamp=user_ts)
            if result.get("aborted"):
                done_emoji = "octagonal_sign"
            elif result["ok"]:
                done_emoji = "white_check_mark"
            else:
                done_emoji = "x"
            client.reactions_add(channel=channel, name=done_emoji, timestamp=user_ts)
        except Exception:
            log.debug("reaction swap failed", exc_info=True)
        if is_reply:
            remaining = thread_inflight_decrement(thread_key)
            if remaining == 0:
                clear_thread_busy(client, channel, thread_ts)


PIP_PREFIX = "🐧 _from pip:_ "


def handle_pip_reply(client, bot_user_id: str | None, thread_key: str, channel: str, thread_ts: str, text: str) -> None:
    """Handle a reply submitted via the Pip web UI."""
    echo_ts: str | None = None
    try:
        resp = client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=PIP_PREFIX + text,
            blocks=[{"type": "markdown", "text": PIP_PREFIX + text}],
            username=PIP_USERNAME,
            icon_emoji=":penguin:",
        )
        echo_ts = resp.get("ts")
    except Exception:
        log.exception("failed to echo pip message to slack")
    if echo_ts:
        try:
            client.reactions_add(channel=channel, name="eyes", timestamp=echo_ts)
        except Exception as e:
            log.warning("reactions_add eyes failed on pip echo: %s", e)
    posted_log = TMP_DIR / f"posted-pip-{int(time.time()*1000)}.log"
    posted_log.touch()
    ch_name = channel_name(client, channel, None)
    cwd = cwd_for_channel(ch_name, None)
    permalink = slack_permalink(client, channel, thread_ts)
    slack_env = {
        "SLACK_CHANNEL_NAME": ch_name,
        "SLACK_CHANNEL_ID": channel,
        "SLACK_THREAD_TS": thread_ts,
        "SLACK_PERMALINK": permalink,
        "SLACK_USER": "pip",
        "SLACK_POSTED_FILES_LOG": str(posted_log),
    }
    thread_inflight_increment(thread_key)
    mark_thread_busy(client, channel, thread_ts)
    with thread_lock(thread_key):
        session_id, just_created = get_or_create_session(thread_key)
        is_new = just_created or not session_started(thread_key)
        prompt_text, timeout_override = parse_timeout_directive(text)
        started_at = time.time()
        pip_api.mark_inflight(thread_key, prompt_text)
        try:
            result = run_claude(
                prompt_text, session_id, is_new, cwd, thread_key,
                slack_env=slack_env, timeout_seconds=timeout_override,
            )
        finally:
            pip_api.clear_inflight(thread_key)
        try:
            already_posted = set(posted_log.read_text().splitlines()) if posted_log.exists() else set()
        except OSError:
            already_posted = set()
        try:
            auto_upload_new_images(client, channel, thread_ts, cwd, started_at, already_posted)
        except Exception:
            log.exception("auto-upload pass failed")
        try:
            posted_log.unlink()
        except OSError:
            pass
        if result["ok"]:
            mark_session_started(thread_key)
            reply_text = (result.get("text") or "").strip() or "_(no reply text)_"
            footer_bits = []
            if result.get("duration_ms") is not None:
                footer_bits.append(f"_{result['duration_ms']/1000:.1f}s_")
            footer = " · ".join(footer_bits)
            try:
                post_chunks(client, channel, thread_ts, reply_text, footer)
            except Exception:
                log.exception("posting pip reply failed")
        elif result.get("aborted"):
            log.info("pip claude run aborted by user: thread=%s", thread_key)
        else:
            if is_new:
                drop_session(thread_key)
            err = result.get("error", "unknown error")
            try:
                client.chat_postMessage(
                    channel=channel, thread_ts=thread_ts,
                    text=f":warning: {err[:3000]}",
                )
            except Exception:
                log.exception("posting pip error failed")
        if echo_ts:
            try:
                client.reactions_remove(channel=channel, name="eyes", timestamp=echo_ts)
                if result.get("aborted"):
                    done_emoji = "octagonal_sign"
                elif result["ok"]:
                    done_emoji = "white_check_mark"
                else:
                    done_emoji = "x"
                client.reactions_add(channel=channel, name=done_emoji, timestamp=echo_ts)
            except Exception:
                log.debug("pip echo reaction swap failed", exc_info=True)
        remaining = thread_inflight_decrement(thread_key)
        if remaining == 0:
            clear_thread_busy(client, channel, thread_ts)


def is_relevant_event(event: dict[str, Any], bot_user_id: str | None) -> bool:
    if event.get("subtype"):
        return False
    if event.get("bot_id"):
        return False
    if bot_user_id and event.get("user") == bot_user_id:
        return False
    if not (event.get("text") or "").strip():
        return False
    return is_allowed(event)


def is_allowed(event: dict[str, Any]) -> bool:
    if ALLOWED_USERS and event.get("user") not in ALLOWED_USERS:
        return False
    if ALLOWED_CHANNELS and event.get("channel") not in ALLOWED_CHANNELS:
        return False
    return True


def main() -> None:
    bot_token = os.environ.get("SLACK_BOT_TOKEN")
    app_token = os.environ.get("SLACK_APP_TOKEN")
    if not bot_token or not app_token:
        log.error("SLACK_BOT_TOKEN and SLACK_APP_TOKEN must be set in .env")
        sys.exit(1)

    app = App(token=bot_token)
    global BOT_USER_ID
    bot_user_id = app.client.auth_test()["user_id"]
    BOT_USER_ID = bot_user_id
    log.info("authenticated as bot user %s", bot_user_id)
    log.info("mention-only channels: %s", sorted(MENTION_ONLY_CHANNELS))
    log.info("channel mappings: %s", CHANNEL_MAP)
    log.info("allowed users: %s | allowed channels: %s",
             sorted(ALLOWED_USERS) or "any", sorted(ALLOWED_CHANNELS) or "any")
    log.info("permissions: %s", "skip (--dangerously-skip-permissions)" if SKIP_PERMISSIONS
             else f"--permission-mode {PERMISSION_MODE}")
    log.info("default cwd: %s | dm cwd: %s", DEFAULT_CWD, DM_CWD)

    @app.event("message")
    def on_message(event, client):
        if not is_relevant_event(event, bot_user_id):
            return
        EXECUTOR.submit(_safe_handle, client, event)

    @app.event("app_mention")
    def on_mention(event, client):
        if not is_relevant_event(event, bot_user_id):
            return
        EXECUTOR.submit(_safe_handle, client, event)

    @app.event("reaction_added")
    def on_reaction(event):
        # Isolated: only :mag: on a known status message does anything.
        if event.get("reaction") == progress.FULL_LOG_EMOJI:
            EXECUTOR.submit(progress.handle_full_log_reaction, app.client, event, bot_user_id)

    @app.event("reaction_removed")
    def on_reaction_removed(event):
        pass

    def enqueue_pip_reply(thread_key: str, channel: str, thread_ts: str, text: str) -> None:
        EXECUTOR.submit(
            lambda: handle_pip_reply(app.client, bot_user_id, thread_key, channel, thread_ts, text)
        )

    pip_app = pip_api.build_app(
        sessions_provider=lambda: dict(SESSIONS),
        slack_client_provider=lambda: app.client,
        bot_user_id_provider=lambda: bot_user_id,
        enqueue_pip_reply=enqueue_pip_reply,
    )
    pip_api.serve_in_thread(pip_app, host=PIP_HOST, port=PIP_PORT)
    log.info(
        "pip URL (bookmark this on your phone): http://localhost:%d/?t=%s",
        PIP_PORT, pip_api.PIP_TOKEN,
    )

    threading.Thread(
        target=lambda: recovery_sweep(app.client, bot_user_id),
        name="recovery-sweep",
        daemon=True,
    ).start()

    handler = SocketModeHandler(app, app_token)
    threading.Thread(
        target=lambda: connection_watchdog(make_socket_alive(handler.client)),
        name="conn-watchdog",
        daemon=True,
    ).start()
    log.info("starting Socket Mode handler — Ctrl+C to stop")
    handler.start()


WATCHDOG_POLL = 5
WATCHDOG_PONG_MAX_AGE = 45        # healthy socket gets a pong every ~10s (ping_interval)
WATCHDOG_MAX_UNHEALTHY = 90       # continuously unhealthy this long -> restart
WATCHDOG_DEFER_MAX = 900          # max extra wait for in-flight claude runs before forcing
WATCHDOG_TEST_FILE = Path(__file__).parent / "tmp" / "force_dead"   # test hook: touch to simulate a dead socket


def make_socket_alive(client, wall=time.time):
    born: dict[int, float] = {}

    def alive() -> bool:
        return socket_alive(client, born, wall)

    return alive


def socket_alive(client, session_born: dict, wall=time.time) -> bool:
    """Real liveness of the Socket Mode websocket: a live socket whose ping/pong is fresh.
    is_connected() alone is not enough: it only checks that a socket object exists, and
    a zombie connection after a DNS/network blip keeps one while receiving nothing."""
    if WATCHDOG_TEST_FILE.exists():
        return False
    ws = client.current_session
    if ws is None or ws.sock is None or not ws.sock.connected:
        return False
    if id(ws) not in session_born:
        session_born.clear()
        session_born[id(ws)] = wall()
    born = session_born[id(ws)]
    last = ws.last_pong_tm or born          # no pong yet: measure from when we first saw this session
    return wall() - last < WATCHDOG_PONG_MAX_AGE


def jobs_in_flight() -> int:
    with INFLIGHT_PROCS_GUARD:
        return len(INFLIGHT_PROCS)


def connection_watchdog(alive, in_flight=jobs_in_flight, poll=WATCHDOG_POLL,
                        max_unhealthy=WATCHDOG_MAX_UNHEALTHY, defer_max=WATCHDOG_DEFER_MAX,
                        exit_fn=None) -> None:
    """Exit when the socket has been continuously dead for `max_unhealthy` seconds; launchd
    KeepAlive restarts us and the recovery sweep re-runs unanswered messages. A quiet channel
    is fine (pings, not messages, prove liveness). While a claude run is in flight, wait up to
    `defer_max` more seconds so the user's job finishes first (replies use the Web API, unaffected).
    Uses time.monotonic, which pauses during machine sleep."""
    exit_fn = exit_fn or (lambda: os._exit(3))
    dead_since = None
    while True:
        time.sleep(poll)
        now = time.monotonic()
        if alive():
            if dead_since is not None:
                log.info("watchdog: socket healthy again after %ds", int(now - dead_since))
            dead_since = None
            continue
        dead_since = dead_since or now
        down = now - dead_since
        if down < max_unhealthy:
            continue
        if in_flight() and down < max_unhealthy + defer_max:
            log.warning("watchdog: socket dead %ds but %d claude run(s) in flight; deferring restart",
                        int(down), in_flight())
            time.sleep(25)
            continue
        log.error("watchdog: Socket Mode dead for %ds; exiting for launchd restart", int(down))
        exit_fn()
        return


def _safe_handle(client, event: dict[str, Any]) -> None:
    try:
        handle_message_async(client, event)
    except Exception:
        log.exception("unhandled error in event handler")


def _recovery_msg_eligible(msg: dict, bot_user_id: str | None) -> bool:
    if msg.get("bot_id") or msg.get("subtype") or msg.get("user") == bot_user_id:
        return False
    if not (msg.get("text") or "").strip():
        return False
    for r in msg.get("reactions") or []:
        if r.get("name") in ("white_check_mark", "x") and bot_user_id in (r.get("users") or []):
            return False
    return True


def _list_bot_conversations(client) -> list[dict]:
    out: list[dict] = []
    cursor: str | None = None
    while True:
        resp = client.users_conversations(
            types="public_channel,private_channel,im,mpim",
            limit=200,
            cursor=cursor or None,
        )
        out.extend(resp.get("channels") or [])
        cursor = (resp.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
    return out


def _recover_thread(client, channel_id: str, msgs: list[dict], channel_type: str | None) -> None:
    """Process a thread's unhandled messages serially in chronological order."""
    for msg in msgs:
        event = {
            "channel": channel_id,
            "user": msg.get("user", ""),
            "text": msg.get("text", ""),
            "ts": msg.get("ts"),
            "thread_ts": msg.get("thread_ts") or msg.get("ts"),
            "channel_type": channel_type,
        }
        try:
            handle_message_async(client, event)
        except Exception:
            log.exception("recovery: handler failed for ts=%s", msg.get("ts"))


def recovery_sweep(client, bot_user_id: str | None) -> None:
    """Scan for user messages we owe a reply on and re-enqueue them.

    Two sources of candidate threads:
    1. Known threads (from SESSIONS) within the lookback window — catches replies
       we never finished (e.g. bridge died mid-claude-run).
    2. Recent top-level messages in any channel/DM the bot is in — catches messages
       sent while the bridge was down (so they never made it into SESSIONS).

    A user message is eligible to recover if the bot has not added ✅ or ❌ to it.
    Live socket-mode delivery and recovery dedupe on message ts via claim_message_ts.
    """
    cutoff = time.time() - RECOVERY_LOOKBACK_SECONDS

    # (channel_id, thread_ts) -> channel_type
    candidates: dict[tuple[str, str], str | None] = {}

    for thread_key in list(SESSIONS.keys()):
        try:
            channel_id, thread_ts = thread_key.split(":", 1)
            ts_float = float(thread_ts)
        except ValueError:
            continue
        if ts_float < cutoff:
            continue
        ch_type = "im" if channel_id.startswith("D") else None
        candidates[(channel_id, thread_ts)] = ch_type

    try:
        bot_channels = _list_bot_conversations(client)
    except Exception:
        log.exception("recovery: list bot conversations failed")
        bot_channels = []

    for ch in bot_channels:
        ch_id = ch.get("id")
        if not ch_id:
            continue
        ch_type = "im" if ch.get("is_im") else None
        try:
            resp = client.conversations_history(channel=ch_id, oldest=str(cutoff), limit=200)
        except Exception:
            log.debug("recovery: history fetch failed for %s", ch_id, exc_info=True)
            continue
        for msg in resp.get("messages") or []:
            ts = msg.get("ts")
            if not ts:
                continue
            key = (ch_id, ts)
            if key in candidates:
                continue
            if _recovery_msg_eligible(msg, bot_user_id) or (msg.get("reply_count") or 0) > 0:
                candidates[key] = ch_type

    enqueued = 0
    for (channel_id, thread_ts), ch_type in candidates.items():
        try:
            resp = client.conversations_replies(channel=channel_id, ts=thread_ts, limit=200)
        except Exception:
            log.debug("recovery: replies fetch failed for %s:%s", channel_id, thread_ts, exc_info=True)
            continue
        eligible = [m for m in (resp.get("messages") or []) if _recovery_msg_eligible(m, bot_user_id)]
        if not eligible:
            continue
        log.info(
            "recovery: %s:%s — %d unhandled msg(s)", channel_id, thread_ts, len(eligible)
        )
        EXECUTOR.submit(_recover_thread, client, channel_id, eligible, ch_type)
        enqueued += len(eligible)

    log.info(
        "recovery sweep: %d msg(s) enqueued across %d candidate thread(s)",
        enqueued,
        len(candidates),
    )


if __name__ == "__main__":
    main()

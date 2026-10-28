"""
HTTP API surface for the Pip web app.

Lives in the same Python process as the Slack bridge so it shares in-memory
state (in-flight runs, sessions dict). Exposes:

  GET  /api/threads              — list of all known threads + status + sprite
  GET  /api/thread/{thread_key}  — full thread detail incl. recent messages
  POST /api/thread/{thread_key}/reply — send a message into the thread
  GET  /api/health
  GET  /                          — serves built React app from pip/dist

Polling-based; clients refresh every few seconds.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

log = logging.getLogger("pip_api")

ROOT = Path(__file__).resolve().parent
PIP_DIST = ROOT / "pip" / "dist"
SPRITES_PATH = ROOT / "sprites.json"
TOKEN_PATH = ROOT / ".pip_token"


def _resolve_token() -> str:
    env_tok = (os.environ.get("PIP_TOKEN") or "").strip()
    if env_tok:
        return env_tok
    if TOKEN_PATH.exists():
        existing = TOKEN_PATH.read_text().strip()
        if existing:
            return existing
    new = secrets.token_urlsafe(32)
    TOKEN_PATH.write_text(new)
    try:
        os.chmod(TOKEN_PATH, 0o600)
    except OSError:
        pass
    return new


PIP_TOKEN = _resolve_token()

SPRITE_KINDS = ["blob", "ghost", "cat", "mushroom", "bird", "slime"]
SPRITE_NAMES = [
    "Pip", "Bobo", "Mimi", "Yuzu", "Tofu", "Pumi", "Nori", "Coco",
    "Momo", "Kiwi", "Tuna", "Goji", "Bibi", "Lulu", "Nono", "Riri",
    "Suzu", "Taro", "Yoyo", "Zuzu", "Bubu", "Fifi", "Gigi", "Hopo",
    "Iko", "Juno", "Kuku", "Lala", "Miso", "Pepa", "Rara", "Sasa",
    "Tito", "Ula", "Veve", "Wawa", "Xixi", "Yaya", "Zazu", "Pico",
]

INFLIGHT: dict[str, dict] = {}
INFLIGHT_LOCK = threading.Lock()
CHANNEL_NAME_CACHE: dict[str, str] = {}
CHANNEL_NAME_TTL = 600  # seconds
_CHANNEL_NAME_CACHE_TS: dict[str, float] = {}

REPLIES_CACHE: dict[str, dict] = {}
REPLIES_TTL = 12  # seconds — Slack tier-3 limit on conversations.replies is tight

# Permalinks are stable for the life of a thread, so cache forever.
PERMALINK_CACHE: dict[str, str] = {}

AUDIT_LOG_PATH = ROOT / "pip_audit.log"

# Per-IP token-bucket rate limit for mutating endpoints. The token grant rate
# is generous for a single-user PWA but tight enough to make a bruteforced
# token costly to abuse. Auth failures are also rate-limited (separate bucket)
# so a leaked URL can't be probed forever.
MUTATION_RATE = 30          # tokens
MUTATION_PERIOD = 60.0      # seconds
AUTH_FAIL_LIMIT = 20        # 401s per minute per IP
AUTH_FAIL_PERIOD = 60.0
_RATE_LOCK = threading.Lock()
_RATE_BUCKETS: dict[tuple[str, str], list[float]] = {}


def _allow(bucket: str, key: str, limit: int, period: float) -> bool:
    now = time.time()
    with _RATE_LOCK:
        k = (bucket, key)
        hits = _RATE_BUCKETS.setdefault(k, [])
        cutoff = now - period
        # Evict in-place; keeps memory bounded for active IPs.
        hits[:] = [t for t in hits if t > cutoff]
        if len(hits) >= limit:
            return False
        hits.append(now)
        return True


def _client_ip(request) -> str:
    # cloudflared and other reverse proxies set X-Forwarded-For; trust only the
    # left-most entry (the original client). Falls back to the socket peer.
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return (request.client.host if request.client else "?")


def _audit(event: str, request, **fields) -> None:
    try:
        line = json.dumps({
            "ts": time.time(),
            "event": event,
            "ip": _client_ip(request),
            "ua": request.headers.get("user-agent", "")[:200],
            "path": request.url.path,
            **fields,
        }) + "\n"
        with AUDIT_LOG_PATH.open("a") as fh:
            fh.write(line)
    except Exception:
        log.exception("audit write failed")


def _stable_pick(seed: str, options: list[str]) -> str:
    h = hashlib.md5(seed.encode()).hexdigest()
    return options[int(h, 16) % len(options)]


def _stable_hue(seed: str) -> int:
    h = hashlib.md5(seed.encode()).hexdigest()
    return int(h, 16) % 360


def _load_sprites() -> dict[str, dict]:
    if not SPRITES_PATH.exists():
        return {}
    try:
        return json.loads(SPRITES_PATH.read_text())
    except json.JSONDecodeError:
        return {}


def _save_sprites(s: dict[str, dict]) -> None:
    SPRITES_PATH.write_text(json.dumps(s, indent=2))


_SPRITES = _load_sprites()
_SPRITES_LOCK = threading.Lock()


def assign_sprite(thread_key: str, channel_name: str) -> dict[str, Any]:
    with _SPRITES_LOCK:
        if thread_key in _SPRITES:
            return _SPRITES[thread_key]
        kind = _stable_pick(thread_key, SPRITE_KINDS)
        name = _stable_pick(thread_key + ":name", SPRITE_NAMES)
        hue = _stable_hue((channel_name or thread_key) + ":color")
        sprite = {
            "kind": kind,
            "name": name,
            "hue": hue,
            "channel": channel_name,
        }
        _SPRITES[thread_key] = sprite
        _save_sprites(_SPRITES)
        return sprite


def mark_inflight(thread_key: str, prompt: str) -> None:
    with INFLIGHT_LOCK:
        INFLIGHT[thread_key] = {"started_at": time.time(), "prompt": prompt[:300]}


def clear_inflight(thread_key: str) -> None:
    with INFLIGHT_LOCK:
        INFLIGHT.pop(thread_key, None)


def is_inflight(thread_key: str) -> bool:
    with INFLIGHT_LOCK:
        return thread_key in INFLIGHT


def cached_channel_name(client, channel_id: str) -> str:
    now = time.time()
    if channel_id in CHANNEL_NAME_CACHE and now - _CHANNEL_NAME_CACHE_TS.get(channel_id, 0) < CHANNEL_NAME_TTL:
        return CHANNEL_NAME_CACHE[channel_id]
    try:
        info = client.conversations_info(channel=channel_id)
        name = info["channel"].get("name") or ""
    except Exception:
        log.exception("conversations_info failed for %s", channel_id)
        name = ""
    CHANNEL_NAME_CACHE[channel_id] = name
    _CHANNEL_NAME_CACHE_TS[channel_id] = now
    return name


def cached_permalink(client, channel_id: str, thread_ts: str) -> str:
    key = f"{channel_id}:{thread_ts}"
    if key in PERMALINK_CACHE:
        return PERMALINK_CACHE[key]
    try:
        url = client.chat_getPermalink(channel=channel_id, message_ts=thread_ts)["permalink"]
    except Exception:
        log.debug("permalink lookup failed for %s", key, exc_info=True)
        url = ""
    PERMALINK_CACHE[key] = url
    return url


def fetch_replies(client, channel_id: str, thread_ts: str, limit: int = 50) -> list[dict]:
    cache_key = f"{channel_id}:{thread_ts}"
    now = time.time()
    cached = REPLIES_CACHE.get(cache_key)
    if cached and now - cached["ts"] < REPLIES_TTL:
        return cached["messages"]
    try:
        resp = client.conversations_replies(channel=channel_id, ts=thread_ts, limit=limit)
        messages = resp.get("messages") or []
    except Exception as e:
        # Common case: top-level message that never got a reply -> "thread_not_found".
        # Don't spam the log; just remember as empty.
        msg = str(e)
        if "thread_not_found" not in msg:
            log.exception("conversations_replies failed for %s/%s", channel_id, thread_ts)
        else:
            log.debug("no replies for %s/%s yet", channel_id, thread_ts)
        messages = []
    REPLIES_CACHE[cache_key] = {"ts": now, "messages": messages}
    return messages


MD_PATH_RE = re.compile(r"(?:^|[\s`'\"])((?:[~/\.]|[A-Za-z]:\\)?[\w./\\\- ]+\.md)\b")


def extract_md_paths(text: str) -> list[str]:
    return list(dict.fromkeys(MD_PATH_RE.findall(text or "")))


def _inline_to_md(el: dict) -> str:
    t = el.get("type")
    if t == "text":
        s = el.get("text") or ""
        st = el.get("style") or {}
        if st.get("code"):
            return f"`{s}`"
        if st.get("bold") and st.get("italic"):
            s = f"***{s}***"
        elif st.get("bold"):
            s = f"**{s}**"
        elif st.get("italic"):
            s = f"_{s}_"
        if st.get("strike"):
            s = f"~~{s}~~"
        return s
    if t == "link":
        url = el.get("url") or ""
        label = el.get("text") or url
        return f"[{label}]({url})"
    if t == "user":
        return f"@{el.get('user_id', '')}"
    if t == "channel":
        return f"#{el.get('channel_id', '')}"
    if t == "usergroup":
        return f"@{el.get('usergroup_id', '')}"
    if t == "broadcast":
        return f"@{el.get('range', '')}"
    if t == "emoji":
        u = el.get("unicode")
        if u:
            try:
                return "".join(chr(int(p, 16)) for p in u.split("-"))
            except (TypeError, ValueError):
                pass
        return f":{el.get('name', '')}:"
    return ""


def _inlines_to_md(elements) -> str:
    return "".join(_inline_to_md(e) for e in (elements or []))


def _rich_element_to_md(el: dict) -> str:
    t = el.get("type")
    if t == "rich_text_section":
        return _inlines_to_md(el.get("elements"))
    if t == "rich_text_list":
        ordered = el.get("style") == "ordered"
        indent = "  " * (el.get("indent") or 0)
        lines = []
        for i, sec in enumerate(el.get("elements") or []):
            prefix = f"{i + 1}." if ordered else "-"
            lines.append(f"{indent}{prefix} {_inlines_to_md(sec.get('elements'))}")
        return "\n".join(lines)
    if t == "rich_text_quote":
        body = _inlines_to_md(el.get("elements"))
        return "\n".join(f"> {ln}" if ln else ">" for ln in body.split("\n"))
    if t == "rich_text_preformatted":
        body = _inlines_to_md(el.get("elements"))
        lang = el.get("language") or ""
        return f"```{lang}\n{body}\n```"
    return ""


def _cell_to_md(cell: dict) -> str:
    parts = []
    for el in cell.get("elements") or []:
        if el.get("type") == "rich_text_section":
            parts.append(_inlines_to_md(el.get("elements")))
    return " ".join(p for p in parts if p).replace("|", "\\|").replace("\n", " ").strip()


def _table_to_md(block: dict) -> str:
    """Render a Slack `table` block as a GFM pipe table.
    Slack stores `rows` as a 2D list of rich_text cells."""
    rows = block.get("rows") or []
    if not rows or not isinstance(rows[0], list):
        return ""
    grid = [[_cell_to_md(c) for c in row] for row in rows]
    width = max(len(r) for r in grid)
    grid = [r + [""] * (width - len(r)) for r in grid]
    out = ["| " + " | ".join(r) + " |" for r in grid]
    out.insert(1, "| " + " | ".join(["---"] * width) + " |")
    return "\n".join(out)


def _header_to_md(block: dict) -> str:
    txt = ((block.get("text") or {}).get("text") or "").strip()
    if not txt:
        return ""
    level = max(1, min(6, int(block.get("level") or 2)))
    return f"{'#' * level} {txt}"


def slack_blocks_to_markdown(blocks) -> str:
    """Reconstruct CommonMark from Slack's stored blocks.

    Slack normalizes the `markdown` block we post into typed blocks
    (`header`, `rich_text`, `divider`, `table`) and collapses newlines in
    the `text` field. This walker emits proper markdown so react-markdown
    can render headings/lists/code/quotes/dividers/tables.
    """
    if not blocks:
        return ""
    out: list[str] = []
    for b in blocks:
        bt = b.get("type")
        if bt == "rich_text":
            for el in b.get("elements") or []:
                out.append(_rich_element_to_md(el))
        elif bt == "section":
            txt = (b.get("text") or {}).get("text") or ""
            if txt:
                out.append(txt)
        elif bt == "markdown":
            out.append(b.get("text") or "")
        elif bt == "header":
            out.append(_header_to_md(b))
        elif bt == "divider":
            out.append("---")
        elif bt == "table":
            out.append(_table_to_md(b))
    joined = "\n\n".join(s for s in out if s).strip()
    # Collapse runs of 3+ newlines that come from rich_text_section internal
    # `\n\n` plus our `\n\n` separator. CommonMark already treats blank lines
    # the same so this is just a visual tightening.
    return re.sub(r"\n{3,}", "\n\n", joined)


def thread_summary(messages: list[dict]) -> str:
    for m in messages:
        if m.get("subtype"):
            continue
        if m.get("bot_id"):
            continue
        return (m.get("text") or "").strip()[:200]
    return ""


def thread_status(thread_key: str, messages: list[dict], bot_user_id: str | None) -> str:
    if is_inflight(thread_key):
        return "working"
    if not messages:
        return "idle"
    last = messages[-1]
    last_user = last.get("user")
    last_bot = last.get("bot_id")
    # If the last message is from the bot, you've been replied-to; ball is in your court.
    if last_bot or last_user == bot_user_id:
        return "waiting_on_user"
    return "idle"


def build_app(
    sessions_provider,
    slack_client_provider,
    bot_user_id_provider,
    enqueue_pip_reply,
):
    """
    Args:
        sessions_provider: callable returning the SESSIONS dict snapshot
        slack_client_provider: callable returning a slack WebClient
        bot_user_id_provider: callable returning bot user id (str)
        enqueue_pip_reply: callable(thread_key, channel, thread_ts, text) -> None
    """
    app = FastAPI(title="Pip API", docs_url=None, redoc_url=None)

    MUTATION_METHODS = {"POST", "PATCH", "DELETE", "PUT"}

    @app.middleware("http")
    async def security_gate(request: Request, call_next):
        path = request.url.path
        method = request.method.upper()
        ip = _client_ip(request)

        if path.startswith("/api/"):
            # Throttle 401-spamming per IP before the constant-time compare so
            # a leaked token URL can't be bruteforced indefinitely.
            if not _allow("authfail", ip, AUTH_FAIL_LIMIT * 4, AUTH_FAIL_PERIOD):
                return JSONResponse({"error": "rate_limited"}, status_code=429)

            auth = request.headers.get("authorization", "")
            tok = ""
            via = "header"
            if auth.lower().startswith("bearer "):
                tok = auth[7:].strip()
            if not tok:
                tok = request.query_params.get("t", "")
                via = "query"
            if not secrets.compare_digest(tok, PIP_TOKEN):
                if not _allow("authfail-real", ip, AUTH_FAIL_LIMIT, AUTH_FAIL_PERIOD):
                    return JSONResponse({"error": "rate_limited"}, status_code=429)
                _audit("auth_fail", request, via=via)
                return JSONResponse({"error": "unauthorized"}, status_code=401)

            if method in MUTATION_METHODS:
                # Mutating endpoints (reply, sprite-update, forget) get a
                # tighter per-IP cap, and the token must come from the
                # Authorization header — never the query string. Query-string
                # tokens are fine for the GET that bootstraps the SPA, but
                # honoring them on mutations would expose us to image/video/
                # link-preview side-effects (e.g. someone posting a pip URL
                # in a Slack message that auto-unfurls).
                if via != "header":
                    return JSONResponse(
                        {"error": "header_token_required"}, status_code=401,
                    )
                if not _allow("mut", ip, MUTATION_RATE, MUTATION_PERIOD):
                    _audit("rate_limited", request, method=method)
                    return JSONResponse({"error": "rate_limited"}, status_code=429)
                _audit("mutation", request, method=method)

        response = await call_next(request)

        # Hardening headers on every response — cheap and helps even when
        # served behind cloudflared.
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        elif not path.startswith("/assets/"):
            # Only the index HTML / manifest / sw.js benefit from CSP. The
            # built JS lives under /assets and is loaded as a script-src self.
            response.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'self'; "
                "script-src 'self'; "
                "style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data: https:; "
                "font-src 'self' data:; "
                "connect-src 'self'; "
                "frame-ancestors 'none'; "
                "base-uri 'self'; "
                "form-action 'self'",
            )
        return response

    @app.get("/api/health")
    def health():
        return {"ok": True, "ts": time.time()}

    @app.get("/api/threads")
    def threads():
        sessions = sessions_provider()
        client = slack_client_provider()
        bot_id = bot_user_id_provider()
        out = []
        for thread_key, entry in sessions.items():
            try:
                channel_id, thread_ts = thread_key.split(":", 1)
            except ValueError:
                continue
            ch_name = cached_channel_name(client, channel_id)
            sprite = assign_sprite(thread_key, ch_name)
            messages = fetch_replies(client, channel_id, thread_ts, limit=50)
            status = thread_status(thread_key, messages, bot_id)
            summary = thread_summary(messages)
            last_msg = messages[-1] if messages else None
            # Hidden threads are filtered out until new Slack activity (a later
            # message ts) arrives — at which point they auto-unhide. Conversation
            # context (SESSIONS, sprite identity) is preserved either way.
            hidden_at = sprite.get("hidden_at")
            if hidden_at:
                try:
                    last_ts_f = float((last_msg or {}).get("ts") or 0)
                except (TypeError, ValueError):
                    last_ts_f = 0.0
                if last_ts_f <= float(hidden_at):
                    continue
            out.append({
                "thread_key": thread_key,
                "channel_id": channel_id,
                "channel_name": ch_name,
                "thread_ts": thread_ts,
                "session_id": entry.get("session_id"),
                "created_at": entry.get("created_at"),
                "status": status,
                "summary": summary,
                "message_count": len(messages),
                "last_ts": (last_msg or {}).get("ts"),
                "sprite": sprite,
            })
        def _sort_key(t):
            try:
                return float(t.get("last_ts") or t.get("created_at") or 0)
            except (TypeError, ValueError):
                return 0.0
        out.sort(key=_sort_key, reverse=True)
        return {"threads": out}

    @app.get("/api/thread/{thread_key}")
    def thread_detail(thread_key: str):
        sessions = sessions_provider()
        if thread_key not in sessions:
            raise HTTPException(404, "unknown thread")
        client = slack_client_provider()
        bot_id = bot_user_id_provider()
        try:
            channel_id, thread_ts = thread_key.split(":", 1)
        except ValueError:
            raise HTTPException(400, "invalid thread_key")
        ch_name = cached_channel_name(client, channel_id)
        sprite = assign_sprite(thread_key, ch_name)
        permalink = cached_permalink(client, channel_id, thread_ts)
        messages = fetch_replies(client, channel_id, thread_ts, limit=200)
        clean_messages = []
        all_text = []
        for m in messages:
            blocks = m.get("blocks")
            from_blocks = slack_blocks_to_markdown(blocks)
            text = from_blocks or (m.get("text") or "")
            all_text.append(text)
            clean_messages.append({
                "ts": m.get("ts"),
                "user": m.get("user") or m.get("bot_id"),
                "is_bot": bool(m.get("bot_id")) or m.get("user") == bot_id,
                "text": text,
                "files": [
                    {"name": f.get("name"), "url": f.get("permalink"), "mimetype": f.get("mimetype")}
                    for f in (m.get("files") or [])
                ],
            })
        md_paths = extract_md_paths("\n".join(all_text))
        status = thread_status(thread_key, messages, bot_id)
        return {
            "thread_key": thread_key,
            "channel_id": channel_id,
            "channel_name": ch_name,
            "thread_ts": thread_ts,
            "sprite": sprite,
            "status": status,
            "messages": clean_messages,
            "md_paths": md_paths,
            "permalink": permalink,
        }

    @app.patch("/api/thread/{thread_key}/sprite")
    async def update_sprite(thread_key: str, request: Request):
        body = await request.json()
        with _SPRITES_LOCK:
            if thread_key not in _SPRITES:
                raise HTTPException(404, "unknown sprite")
            sprite = _SPRITES[thread_key]
            if "name" in body:
                name = (body.get("name") or "").strip()
                if not name:
                    raise HTTPException(400, "empty name")
                if len(name) > 24:
                    raise HTTPException(400, "name too long")
                sprite["name"] = name
            if "parked_at" in body:
                pa = body.get("parked_at")
                if pa is None:
                    sprite.pop("parked_at", None)
                else:
                    try:
                        x = float(pa["x"])
                        y = float(pa["y"])
                    except (KeyError, TypeError, ValueError):
                        raise HTTPException(400, "invalid parked_at")
                    sprite["parked_at"] = {
                        "x": max(0.0, min(1.0, x)),
                        "y": max(0.0, min(1.0, y)),
                    }
            _save_sprites(_SPRITES)
            return {"ok": True, "sprite": sprite}

    @app.delete("/api/thread/{thread_key}")
    def forget(thread_key: str):
        # Despite the name + HTTP verb, this is a soft-hide: SESSIONS and the
        # sprite record are preserved so Claude's memory of the thread
        # survives. The sprite is stamped with `hidden_at`, and /api/threads
        # filters it out until new Slack activity arrives.
        sessions = sessions_provider()
        if thread_key not in sessions:
            raise HTTPException(404, "unknown thread")
        with _SPRITES_LOCK:
            sprite = _SPRITES.get(thread_key)
            if sprite is not None:
                sprite["hidden_at"] = time.time()
                _save_sprites(_SPRITES)
        return {"ok": True, "thread_key": thread_key, "hidden": True}

    @app.post("/api/thread/{thread_key}/reply")
    async def reply(thread_key: str, request: Request):
        body = await request.json()
        text = (body.get("text") or "").strip()
        if not text:
            raise HTTPException(400, "empty text")
        sessions = sessions_provider()
        if thread_key not in sessions:
            raise HTTPException(404, "unknown thread")
        try:
            channel_id, thread_ts = thread_key.split(":", 1)
        except ValueError:
            raise HTTPException(400, "invalid thread_key")
        enqueue_pip_reply(thread_key, channel_id, thread_ts, text)
        return {"ok": True, "thread_key": thread_key}

    if PIP_DIST.exists():
        app.mount("/assets", StaticFiles(directory=str(PIP_DIST / "assets")), name="assets")

        @app.get("/manifest.webmanifest")
        def manifest():
            p = PIP_DIST / "manifest.webmanifest"
            if p.exists():
                return FileResponse(p, media_type="application/manifest+json")
            raise HTTPException(404)

        @app.get("/sw.js")
        def sw():
            p = PIP_DIST / "sw.js"
            if p.exists():
                return FileResponse(p, media_type="application/javascript")
            raise HTTPException(404)

        @app.get("/{path:path}")
        def spa(path: str):
            target = (PIP_DIST / path).resolve()
            if path and target.is_relative_to(PIP_DIST.resolve()) and target.is_file():
                return FileResponse(target)
            return FileResponse(PIP_DIST / "index.html")
    else:
        @app.get("/")
        def placeholder():
            return JSONResponse({
                "ok": True,
                "msg": "Pip frontend not built yet. Run `cd pip && npm run build`.",
            })

    return app


def serve_in_thread(app, host: str = "127.0.0.1", port: int = 8765) -> threading.Thread:
    import uvicorn

    def run():
        config = uvicorn.Config(app, host=host, port=port, log_level="warning")
        server = uvicorn.Server(config)
        server.run()

    t = threading.Thread(target=run, name="pip-api", daemon=True)
    t.start()
    log.info("pip API listening on http://%s:%d", host, port)
    return t

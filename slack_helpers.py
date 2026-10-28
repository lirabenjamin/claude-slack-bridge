"""
Helpers Claude can call from inside a Slack-triggered run to post text
or upload files back to the originating thread.

Reads:
- SLACK_BOT_TOKEN  (from the repo's .env)
- SLACK_CHANNEL_ID (set by the bridge for each run)
- SLACK_THREAD_TS  (set by the bridge for each run)
- SLACK_POSTED_FILES_LOG (optional — bridge sets this; helper appends absolute
  paths of files it has already uploaded so the bridge can dedupe its
  end-of-run auto-upload pass)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")


def _client_or_die() -> WebClient:
    tok = os.environ.get("SLACK_BOT_TOKEN")
    if not tok:
        sys.exit("SLACK_BOT_TOKEN not set (is .env populated?)")
    return WebClient(token=tok)


def _channel_or_die() -> tuple[str, str | None]:
    ch = os.environ.get("SLACK_CHANNEL_ID")
    if not ch:
        sys.exit("SLACK_CHANNEL_ID not set — this helper only works inside a Slack-triggered run")
    return ch, os.environ.get("SLACK_THREAD_TS") or None


def _record_uploaded(path: str) -> None:
    log = os.environ.get("SLACK_POSTED_FILES_LOG")
    if not log:
        return
    try:
        with open(log, "a") as f:
            f.write(str(Path(path).resolve()) + "\n")
    except OSError:
        pass


def post_text(text: str) -> None:
    if not text.strip():
        sys.exit("nothing to post")
    client = _client_or_die()
    channel, thread_ts = _channel_or_die()
    try:
        resp = client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)
        print(f"posted ts={resp['ts']}")
    except SlackApiError as e:
        sys.exit(f"slack error: {e.response.get('error', e)}")


def upload_image(path: str, caption: str | None) -> None:
    p = Path(path).expanduser()
    if not p.exists():
        sys.exit(f"file not found: {p}")
    client = _client_or_die()
    channel, thread_ts = _channel_or_die()
    kwargs: dict = {"channel": channel, "file": str(p), "title": p.name}
    if thread_ts:
        kwargs["thread_ts"] = thread_ts
    if caption:
        kwargs["initial_comment"] = caption
    try:
        client.files_upload_v2(**kwargs)
    except SlackApiError as e:
        sys.exit(f"slack error: {e.response.get('error', e)}")
    _record_uploaded(str(p))
    print(f"uploaded {p.name}")

# claude-slack-bridge

Run Claude Code on one always-on machine and let your team drive it from Slack.

Each Slack channel is mapped to a repo on that machine. A top-level message in the
channel starts a new Claude Code session in that repo; replies in the thread continue
the same session. Everyone in the channel sees the prompts, live progress, and replies
as they happen.

Example: a research group builds a survey app. The repo lives on a lab Mac mini, mapped
to `#survey-app`. One person asks Claude to add a consent page, another replies in the
thread asking for a mobile fix, and a third posts a screenshot of a bug. Everyone
watches the same session move forward.

> Provided as-is, unmaintained, no support. Issues and PRs are not monitored.

## Features

- Socket Mode: no public webhook or open port needed.
- One Claude Code session per Slack thread, persisted across restarts (`sessions.json`).
- Live status message in the thread showing the tools Claude is using. React with :mag: to get the full step log.
- Reply `stop`, `abort`, or `cancel` in a thread to kill the running job.
- Start a message with `!timeout 30m` to raise the 10-minute default (max 4h).
- Images and PDFs that Claude creates in the top folder of the repo are uploaded to the thread. Claude can also call `slack-post` and `slack-image` mid-run.
- Recovery sweep on startup: messages sent while the bridge was down get answered.
- Watchdog restarts the process if the Slack socket silently dies (with launchd).
- Optional allowlist of Slack users and channels.
- Optional "mention-only" channels, for channels where humans also talk to each other.
- Optional Pip web UI: a phone-friendly dashboard of all threads, with a reply box.

## Requirements

- macOS (the launchd files and log rotation assume macOS; the bridge itself is plain Python and runs anywhere Claude Code runs).
- Python 3.10+
- [Claude Code](https://docs.claude.com/en/docs/claude-code) installed and logged in on the machine (`claude` on PATH, or set `CLAUDE_BIN`).
- A Slack workspace where you can install apps.
- Optional, for Pip: Node.js 20+ and [`cloudflared`](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/).

## Setup

### 1. Create the Slack app

Go to https://api.slack.com/apps, click **Create New App**, choose **From an app manifest**,
pick your workspace, and paste this (rename `claude` if you like):

```yaml
display_information:
  name: claude
features:
  bot_user:
    display_name: claude
    always_online: true
  app_home:
    messages_tab_enabled: true
    messages_tab_read_only_enabled: false
oauth_config:
  scopes:
    bot:
      - app_mentions:read
      - channels:history
      - channels:read
      - chat:write
      - chat:write.customize
      - files:write
      - groups:history
      - groups:read
      - im:history
      - im:read
      - mpim:history
      - mpim:read
      - reactions:read
      - reactions:write
settings:
  event_subscriptions:
    bot_events:
      - app_mention
      - message.channels
      - message.groups
      - message.im
      - message.mpim
      - reaction_added
  socket_mode_enabled: true
```

Then:

1. **Basic Information** > **App-Level Tokens** > **Generate Token** with the `connections:write` scope. This `xapp` token is `SLACK_APP_TOKEN`.
2. **Install App** > install to the workspace. The **Bot User OAuth Token** (`xoxb`) is `SLACK_BOT_TOKEN`.
3. In each channel you want to use, run `/invite @claude`. The bot only sees channels it is a member of.

### 2. Install

```bash
git clone <this-repo> claude-slack-bridge
cd claude-slack-bridge
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env                  # paste the two Slack tokens
cp config.yaml.example config.yaml    # map channels to repos
```

### 3. Configure

`config.yaml` maps channel names (without `#`) to repo paths, and holds the options:

```yaml
channels:
  survey-app: "~/code/survey-app"
  analysis:   "~/code/analysis-pipeline"
default_dir: ~/code/scratch     # unmapped channels
dm_dir: ~/code/scratch          # DMs to the bot
mention_only_channels: []
allowed_users: []               # Slack user IDs; empty = anyone in the workspace
allowed_channels: []            # Slack channel IDs; empty = any channel the bot is in
skip_permissions: false
permission_mode: acceptEdits
```

Permissions:

- `skip_permissions: false` (default) runs `claude --permission-mode <permission_mode>`. With `acceptEdits`, Claude can edit files but shell commands are denied unless the repo allows them in `.claude/settings.json` (`"permissions": {"allow": ["Bash(npm test:*)", ...]}`). Other valid modes: `auto`, `dontAsk`, `plan`, `manual`, `bypassPermissions`.
- `skip_permissions: true` runs `claude --dangerously-skip-permissions`. Claude can then run any command without asking. Read the Security section first.

Restart the bridge after editing `config.yaml`.

### 4. Run

```bash
.venv/bin/python bridge.py
```

Post a message in a mapped channel. The bot reacts with :eyes:, posts a status line in
the thread, then replies and swaps :eyes: for :white_check_mark: or :x:. Logs go to
`bridge.log`.

### 5. Keep it running (optional, macOS)

```bash
cp launchd/com.example.claude-slack-bridge.plist.example \
   ~/Library/LaunchAgents/com.example.claude-slack-bridge.plist
# edit the copy: replace REPO_DIR and HOME_DIR with absolute paths
launchctl load ~/Library/LaunchAgents/com.example.claude-slack-bridge.plist
```

To stop: `launchctl unload ~/Library/LaunchAgents/com.example.claude-slack-bridge.plist`.
`bin/run_bridge.sh` rotates the logs into `logs_archive/` when they pass 20 MB.
Keep the machine awake (System Settings > Energy, or `caffeinate`).

### 6. Pip web UI (optional)

Pip is a small React dashboard served by the bridge on `127.0.0.1:8765`. It lists every
thread, shows which ones are working or waiting on you, and lets you reply from a phone.

```bash
cd pip && npm ci && npm run build && cd ..
```

Restart the bridge, then open `http://localhost:8765/?t=<token>`. The token is in
`.pip_token` (generated on first run) or set it yourself with `PIP_TOKEN` in `.env`.

To reach Pip from outside the machine, run a Cloudflare quick tunnel:

```bash
bin/run_cloudflared.sh          # or load launchd/com.example.claude-slack-bridge.cloudflared.plist.example
bin/pip-url                     # prints the tunnel URL with the token appended
```

Quick-tunnel URLs change on every restart; run `bin/pip-url` again to get the new one.
Anyone with that URL and token can send prompts to Claude, so treat it like a password.

## Usage

- **New task:** post a top-level message in a mapped channel. That thread is now one Claude session.
- **Follow up:** reply in the thread. Messages in the same thread run one at a time, in order.
- **Fresh start:** post a new top-level message.
- **Stop a run:** reply `stop`.
- **Long job:** start the message with `!timeout 2h`.
- **Mention-only channels:** address the bot with `@claude` or start with `claude:`.

## Security

Read this before you invite anyone.

- **Whoever can message the bot can run code on the machine.** Claude Code can read and write files, run shell commands, and use any credentials the machine has: SSH keys, cloud CLIs, API keys in `.env` files, browser sessions. With `skip_permissions: true` nothing asks first.
- **Use a dedicated machine or user account.** Do not run this on a laptop with your personal email, password manager, or production credentials.
- **Use scoped keys.** Give the machine only the tokens it needs (for example a GitHub fine-grained token limited to the mapped repos, a database user limited to one database).
- **Set the allowlist.** Fill in `allowed_users` and `allowed_channels`. Without them, anyone in the workspace who can see a channel with the bot, or who DMs it, can command it. Be careful with Slack Connect and guest accounts.
- **Treat pasted content as untrusted.** Text, links, files, and web pages that end up in a prompt can carry prompt-injection instructions. Review what Claude did before you merge or deploy it.
- **Keep Pip private.** The Pip token gives full prompt access. Keep `pip_host` on `127.0.0.1` and only expose it through the tunnel.
- **Use a Claude account appropriate for team use.** Several people sharing one machine share its Claude Code login. Make sure that is allowed under Anthropic's terms for your plan (for example a Team or Enterprise plan, or an API key on a Console account).

## Files

| Path | Purpose |
|---|---|
| `bridge.py` | Slack listener; runs `claude -p` per message and posts results |
| `progress.py` | Live status message built from Claude's stream-json output |
| `pip_api.py` | HTTP API and static server for the Pip web UI |
| `slack_helpers.py`, `bin/slack-post`, `bin/slack-image` | Helpers Claude can call mid-run to post to its thread |
| `bin/run_bridge.sh`, `bin/run_cloudflared.sh` | launchd wrappers with log rotation |
| `bin/pip-url` | Prints the current tunnel URL with the Pip token |
| `launchd/*.plist.example` | launchd templates |
| `pip/` | Pip frontend source (React + Vite) |

Runtime files (all gitignored): `.env`, `config.yaml`, `sessions.json`, `sprites.json`,
`.pip_token`, `*.log`, `tmp/`, `logs_archive/`.

## License

MIT. See `LICENSE`.

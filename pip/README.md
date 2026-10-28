# Pip

Optional web dashboard for the bridge. It shows every Slack thread the bridge knows
about, whether Claude is working on it, and lets you reply from your phone.

```bash
npm ci
npm run build   # writes pip/dist, which the bridge serves on pip_port
```

Open `http://localhost:8765/?t=<token>` (the token is printed in `bridge.log` and stored
in `.pip_token`). Add `&demo=1` to see fake threads. See the main README for remote access.

# Telegram Codex Bridge

Local Telegram remote control for Codex without using the OpenAI API.

Flow:

```text
Telegram bot
  -> allowlist check
  -> Markdown raw log + JSONL queue
  -> codex exec --json / codex exec resume <thread_id>
  -> Telegram reply + optional Redis result queue
```

## Setup

1. Create a bot with BotFather and put the token in `.env`.
2. Copy `config.example.json` to `config.json` if you want to customize settings.
3. Run:

```bash
cd ~/telegram-codex-bridge
python3 telegram_codex_bridge.py run
```

You can also point the bridge at an existing env file with `TELEGRAM_CODEX_BRIDGE_ENV`.

## Commands

- `/help` shows command help.
- `/status` shows bridge status and current Codex thread.
- `/new` disconnects the current Telegram chat from its Codex thread.
- `/new <text>` starts a fresh Codex thread with the given text.
- `/mode chat` maps each Telegram chat to its own persistent Codex thread.
- `/mode queue` logs/enqueues only.
- `/mode resume-last` sends tasks to the latest Codex session.
- `/reply <text>` sends text to Codex.

Plain text from allowed users is accepted. Groups can require mention if configured.
The default mode is `chat`, which is the closest behavior to Claude Code's Telegram bridge.
Normal messages do not receive a queue-style acknowledgment. The bot shows Telegram's typing
indicator while Codex works, then sends the answer directly.

## Files

- Queue: `/tmp/claude_redis_queue.jsonl`
- State: `/tmp/telegram_codex_bridge_state.json`
- Raw logs: configured by `log_dir`, for example `~/wiki/raw/{me|telegram}/YYYY-MM-DD.md`
- Attachments: configured by `attachment_dir`
- Daily ingest candidates: `<wiki-root>/queries/daily_ingest_candidates/YYYY-MM-DD.md`

## Daily Wiki Ingest Candidates

Raw logs are saved immediately. A separate daily job can read yesterday's raw logs and create a
candidate report without editing `index.md`, `log.md`, `concepts`, or `entities`.

Manual run:

```bash
cd ~/telegram-codex-bridge
python3 daily_wiki_ingest.py --today
python3 daily_wiki_ingest.py --date 2026-05-26
```

Install daily 03:10 launchd job:

```bash
cd ~/telegram-codex-bridge
./install_daily_ingest_launchd.sh
```

## Safety

The bridge only accepts allowlisted Telegram user IDs. It does not store the bot token in config.
The Codex runner is local and uses the existing Codex login/session.

Never commit `.env` or `config.json`.

# Security

This project runs local Codex commands from Telegram messages. Treat the bot as a
remote-control surface for your machine.

Recommended defaults:

- Keep `.env` and `config.json` private.
- Use `allowed_user_ids`.
- Do not expose the bot to public groups unless `group_require_mention` is enabled.
- Rotate your Telegram bot token immediately if it is committed or shared.
- Review `codex_cwd`, `log_dir`, and `system_context_files` before running.

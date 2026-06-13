# Contributing to Worktide

Thanks for your interest! This is primarily a portfolio / showcase project, but
issues and pull requests are welcome.

## Ground rules

- **Never commit real data or secrets** — no employee data, Telegram tokens,
  production databases, server addresses, SSH keys, or production logs. Use
  synthetic/demo data (the screenshots in `docs/screenshots/` are demo data).
- Keep changes focused; one logical change per pull request.
- Match the existing code style and the surrounding comment density.
- For security issues, follow [SECURITY.md](SECURITY.md) instead of opening a
  public issue.

## Local setup

See the [Quickstart](README.md#quickstart) for server, admin, and agent setup.

## Running the tests

The backend ships a dry-run smoke suite that forbids real network sends:

```bash
cd server
python -c "from app.main import create_app; create_app()"   # init schema once
TELEGRAM_ENABLED=true TELEGRAM_BOT_TOKEN=dummy TELEGRAM_CHAT_ID=1 \
  python scripts/run_all_smokes.py
```

On Windows PowerShell:

```powershell
cd server
python -c "from app.main import create_app; create_app()"
$env:TELEGRAM_ENABLED="true"; $env:TELEGRAM_BOT_TOKEN="dummy"; $env:TELEGRAM_CHAT_ID="1"
python scripts/run_all_smokes.py
```

The admin console must build cleanly:

```bash
cd admin
npm ci
npm run build
```

CI (`.github/workflows/ci.yml`) runs both on every push and pull request — make
sure it is green before requesting review.

## Pull requests

- Describe what changed and why.
- Note any user-facing or deployment impact.
- Keep README capability claims accurate — don't describe planned work as done.

# AI Job Scout

AI Job Scout is a local-first app for finding and reviewing job opportunities. It collects listings from company career pages, keeps offer history in SQLite, and helps compare jobs with an approved profile built from a CV. The FastAPI dashboard runs on your computer.

The public repository contains application code, tests, and synthetic demo data. Personal CVs, profiles, databases, model files, and evaluation reports are kept out of version control.

## What it does

- Collects offers from configured company sources and tracks changes without removing saved offers when a source fails.
- Provides a local dashboard for offers, applications, source monitoring, profiles, notifications, and model experiments.
- Imports a PDF CV, including scanned PDFs through local OCR, and requires profile review before using its facts for scoring.
- Uses local models to help assess offers. The score covers opportunity, screening strength, work conditions, and development potential. Review model judgments before acting on them.
- Lets you maintain approved CV material and prepare a tailored English PDF for an offer. You review proposed changes and submit applications yourself.

Optional Notion and Telegram integrations support a separate workflow. SQLite remains the local source of truth for the dashboard.

## Run locally

Requires Python 3.11+ and [`uv`](https://docs.astral.sh/uv/). Local model evaluation also requires a configured model runtime; scanned CVs require Tesseract. See [SETUP.md](SETUP.md) for configuration.

```bash
uv sync --extra dev
cp .env.example .env
uv run python main.py init-db
uv run python main.py serve
```

Open `http://127.0.0.1:8765/`.

Useful commands:

```bash
uv run python main.py check-config
uv run python main.py demo-v2-scan --mode sample
uv run python main.py demo-v2-scan --mode full
```

The sample scan limits accepted offers per source; the full scan collects all matching offers. Both save results locally. To run the automated checks:

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
```

## Repository layout

- `src/job_scout/`: application, collectors, models, storage, and web dashboard
- `config/`: source settings and synthetic demo inputs
- `tests/`: automated tests
- `docs/golden-dataset/v1/`: public synthetic evaluation data
- `.env.example`: configuration template without credentials

Keep the app on a private local interface. Do not commit CVs, profile data, SQLite databases, or API keys.

# AI Job Scout

AI Job Scout is a local Python application that collects job listings, filters roles by their dominant duties, and compares relevant offers with a CV profile approved by the user. The web interface supports offer review, application status tracking, profile approval, source monitoring, and evaluation history.

## Architecture

1. **Collect:** source adapters fetch company career pages and Just Join IT listings. The collector cleans full descriptions, deduplicates offers by URL, and records source failures without expiring previously saved offers.
2. **Store:** SQLite keeps offers, content versions, scan runs, approved profile versions, assessments, and user feedback.
3. **Screen:** deterministic duty rules hide clear software and platform roles from the default view. Mixed roles remain available for review.
4. **Evaluate:** a local Qwen model compares offer fragments with approved CV facts. The application resolves evidence IDs to original quotes, checks cited profile facts, adds explicit CV proof gaps, and caps preliminary recommendations. Cache keys include the offer, profile, model, prompt, and rule versions.
5. **Review:** the user can inspect hidden roles, evaluation evidence, and application statuses in the FastAPI interface. Gemini evaluation is also available through an explicitly configured API key.

## Run locally

Requires Python 3.11+, [`uv`](https://docs.astral.sh/uv/), and `llama-server` with `Qwen3.5-9B-Q4_K_M.gguf` at `models/Qwen3.5-9B-Q4_K_M.gguf` for local evaluation.

```bash
uv sync
uv run python main.py init-db
uv run python main.py serve
```

Open `http://127.0.0.1:8765/`. To validate configured sources or collect a new sample:

```bash
uv run python main.py check-config
uv run python main.py demo-v2-scan --mode sample
```

Local CVs, profiles, databases, model files, and API keys are kept outside version control.

## For the curious

Local experiments compared Qwen, Gemma, Bielik, Granite, and other small models on reviewed Polish and English offers. Short classifier outputs reduced latency but did not reliably separate software roles from applied AI work. A Qwen classifier followed by a Gemma fit assessment did not improve that first decision. The current implementation uses a source-backed duty rule alongside one evidence-checked assessment and keeps ambiguous cases for human review.

# Local MVP setup

## 1. Rotate old credentials

Before enabling notifications, revoke the previous Telegram bot token and issue a new one.
Never copy current secret values into documentation or Git.

## 2. Install

```bash
uv sync --extra dev
cp .env.example .env
```

Fill the Notion and Telegram values in the local `.env`. The local LLM server and MLflow
addresses default to loopback interfaces.

## 3. Configure the existing Notion `Oferty` database

Share the database with the Notion integration and create these properties with the exact types:

- `Stanowisko` (title), `Firma`, `Źródło`, `Offer_key`, `Content_hash`, `Alert_key`,
  `Kluczowe_wymagania`, `Uzasadnienie` (rich text);
- `URL` (URL), `Data` (date), `Ocena_AI` (number);
- `Tryb_pracy`, `Umowa`, `Status_Scrapera`, `Pipeline_status`, `Alert_status`, `Decyzja_AI`
  (select).

`Pipeline_status` needs `Processing`, `Completed`, `Failed`. `Alert_status` needs `Pending`,
`Sent`, `Failed`, `Not eligible`. The CLI validates this before it starts a model.

## 4. Validate configuration and run the Notion-first demo

```bash
uv run python main.py check-config
uv run python main.py telegram-test
uv run python main.py notion-scan --mode quick
```

`notion-scan --mode quick` checks Xebia, EPAM and InPost and processes at most one accepted
offer per source. `uv run python main.py notion-scan --mode full` checks every enabled source
without a per-source offer cap. No web panel is required for this phase.

## 5. Run Demo v2 panel and monitoring

Demo v2 działa bez Notion i bez uruchamiania modeli. Używa syntetycznego profilu wyłącznie do
przyszłych testów Model Lab; bieżący scan zapisuje świeżo pobrane oferty i monitoring do SQLite.

```bash
uv run python main.py demo-v2-scan --mode sample  # maks. jedna pasująca oferta na źródło
uv run python main.py serve
```

Panel: `http://127.0.0.1:8765/` · monitoring: `http://127.0.0.1:8765/monitoring`.
`--mode full` zapisuje wszystkie pasujące oferty. Oferta jest oznaczana jako niedostępna tylko po
udanym pełnym odkryciu źródła; błąd źródła nigdy nie usuwa lokalnej historii. Po udanym
`telegram-test` można wysłać minimalne podsumowanie przebiegu przez `--notify`.

## 6. Profile and LLM Lab

Open `http://127.0.0.1:8765/profiles/new` to create a local profile. A PDF CV is required;
embedded text is extracted directly and scanned PDFs use local Tesseract OCR (`eng` and `pol`).
The source PDF and extracted text remain under `data/profile-documents/` and are never sent to
an external API.

`http://127.0.0.1:8765/lab` stores reproducible local model configurations and test plans.
Use **Model Lab / Demo** there to run the existing local evaluation flow. Scoring and Offer
Copilot require a profile in the `ready` state; samo zatwierdzenie odczytu CV jeszcze nie
wystarcza. Install the project dependencies with `uv sync --extra dev`; the local OCR binary
is installed separately with `brew install tesseract tesseract-lang`.

## 7. CV Workspace and manual applications

Open `http://127.0.0.1:8765/cv` after the profile is ready. This workspace separates
profile truth from the document you send to employers:

- upload and approve the HTML master CV;
- review the editable map of CV sections;
- add CV-only assets such as certifications, projects, skills, achievements, experience
  bullets, summary variants and ATS keywords;
- approve each asset before it can be used by the tailoring model;
- prepare a CV for a specific offer from the offer detail page.

CV assets are saved as local profile facts with `usable_for_cv=1`. They do not enter offer
scoring unless they are separately approved as scoring facts. This keeps the evaluation logic
separate from document wording.

Generated application packages contain an English PDF. The app does not upload to ATS and does
not mark an application as sent automatically. Open the offer form manually, submit the PDF
yourself and only then mark the package as applied in the app.

## 8. Privacy boundary

- Do not expose the application publicly.
- If the panel is exposed beyond loopback, use Tailscale Serve rather than a public endpoint.
- Do not configure an external OpenAI-compatible endpoint.
- Do not send CV, offer content, prompts, traces, or MLflow artifacts to hosted services.
- Telegram messages should contain the minimum useful alert data and a link to the private
  panel; Telegram is not the source of truth.

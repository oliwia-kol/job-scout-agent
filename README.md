# AI Job Scout

Prywatna aplikacja **local-first** do monitorowania ofert pracy, ich lokalnego
przetwarzania oraz wspieranej przez modele oceny dopasowania. Projekt pobiera oferty
bezpośrednio z oficjalnych stron firm, zapisuje dane w SQLite i udostępnia je w
lokalnym panelu FastAPI.

Repozytorium publiczne zawiera kod aplikacji, testy oraz wyłącznie syntetyczne dane
demonstracyjne. Lokalne profile, CV, historia rozmów, bazy SQLite i raporty z ocen
są celowo wykluczone z kontroli wersji.

## Co działa

- 27 skonfigurowanych źródeł: 18 aktywnych domyślnie i 9 celowo wyłączonych;
- pobieranie z adapterów ATS/HTML/przeglądarkowych, czyszczenie, filtry oraz trwały
  ledger ofert i zmian w SQLite;
- lokalny panel na `127.0.0.1:8765` z ofertami, monitoringiem, profilami, czatem
  Bielika, aplikacjami, CV Workspace, powiadomieniami i Model Lab;
- lokalne modele: Qwen jako Scout/ekstraktor, DeepSeek dla potoku Notion-first oraz
  Bielik dla wywiadu kariery i Copilota ofert;
- profil tworzony z lokalnego PDF CV, z OCR dla skanów i bramką jawnego zatwierdzenia
  przed użyciem profilu do scoringu;
- CV Workspace: master HTML, mapa edytowalnych bloków, pula materiałów CV
  (certyfikaty, projekty, skills, osiągnięcia, bullety, warianty profilu i keywordy)
  oraz historia wersji CV pod oferty;
- kontrolowane przygotowanie CV do konkretnej oferty: lokalny Qwen proponuje
  udokumentowane zmiany w dozwolonych blokach, korzysta z zatwierdzonej puli CV,
  użytkownik każdą zmianę akceptuje, poprawia albo odrzuca, a program tworzy maksymalnie
  dwustronicowy angielski PDF A4. Wysłanie pliku do ATS pozostaje ręczne.

Scoring używa czterech wymiarów: `35% opportunity`, `25% screening strength`,
`20% work conditions` i `20% development potential`. W przepływie Notion-first
DeepSeek wykonuje obecnie zarówno ocenę, jak i review — nie jest to niezależny judge.

## Szybki start

Wymagany jest Python `>=3.11` oraz `uv`.

```bash
uv sync --extra dev
cp .env.example .env
uv run python main.py init-db
uv run python main.py serve
```

Panel jest dostępny pod `http://127.0.0.1:8765/`. Główne ekrany produktu to
`/today`, `/`, `/chat`, `/applications`, `/cv`, `/profiles`, `/notifications`
i `/settings`. Pełna konfiguracja lokalnych modeli, Notion, Telegrama, OCR i
CV Workspace jest w [SETUP.md](SETUP.md).

## Najważniejsze komendy

```bash
# walidacja konfiguracji źródeł i lokalny monitoring bez modeli
uv run python main.py check-config
uv run python main.py demo-v2-scan --mode sample

# pełne odświeżenie lokalnego zbioru ofert
uv run python main.py demo-v2-scan --mode full

# Model Lab na zamrożonym zbiorze demonstracyjnym
uv run python main.py benchmark-extractor
uv run python main.py demo-flow

# operacyjny przepływ do Notion (wymaga NOTION_*)
uv run python main.py notion-scan --mode quick
```

`telegram-test` wysyła rzeczywistą wiadomość; używaj go dopiero po skonfigurowaniu
lokalnego `.env`. Telegram i Notion są opcjonalnymi integracjami zewnętrznymi — SQLite
pozostaje lokalnym źródłem prawdy dla panelu i monitoringu.

## Struktura

- `src/job_scout/` — aplikacja, CLI, adaptery źródeł, modele domenowe, SQLite i panel;
- `config/` — źródła oraz dane demonstracyjne;
- `tests/unit/` — testy automatyczne;
- `docs/golden-dataset/v1/` — publiczna, syntetyczna część benchmarku;
- `.env.example` — bezpieczny szablon konfiguracji lokalnej.

## Weryfikacja

```bash
UV_CACHE_DIR=/tmp/ai-job-scout-uv-cache .venv/bin/python -m pytest -q
UV_CACHE_DIR=/tmp/ai-job-scout-uv-cache .venv/bin/python -m ruff check .
```

Lokalne dane kandydata i klucz odpowiedzi holdoutu nie są częścią repozytorium.

# Golden Dataset v1

Golden v1 jest hybrydowym benchmarkiem reguł decyzji, nie zbiorem przypadkowych
ofert z internetu i nie automatycznym fine-tuningiem modelu.

## Artefakty

- `PROFILE_CONTEXT.json` — zamrożony, jawny kontekst Career Base i bieżącego CV;
- `calibration.synthetic.json` — 18 jawnie etykietowanych ofert syntetycznych;
- `validation.inputs.synthetic.json` — 18 ślepych ofert walidacyjnych;
- `prefilter.synthetic.json` — 8 testów wejścia/wykluczenia;
- `data/golden-private/v1/validation.answer-key.json` — prywatny klucz holdoutu,
  ignorowany przez Git i niedostępny dla promptu modelu.

Artefakty opisowe, które warto dodać przed pełną publikacją wyników:

- `DATASET_DESIGN.md` — docelowa próba, pokrycie i progi odbioru;
- `MODEL_EVALUATION_INSTRUCTION.md` — jedna instrukcja reasoning evaluatora;
- `CALIBRATION_REPORT.md` — wyniki baseline, porównanie modeli i historia korekt.

## Walidacja struktury i izolacji

```bash
PYTHONPATH=src .venv/bin/python -m job_scout.cli validate-golden-v1
```

Polecenie sprawdza liczebność, balans 6/6/6, identyfikatory, zakres ocen,
różnorodność tagów, brak etykiet w publicznych wejściach, zgodność klucza oraz
brak identyfikatorów walidacyjnych w instrukcji. Sprawdza także rozdzielenie
Career Base od bieżącego CV i wypisuje SHA-256 wszystkich zamrażanych artefaktów.

## Kalibracja instrukcji

```bash
PYTHONPATH=src .venv/bin/python -m job_scout.cli run-golden-v1-calibration
PYTHONPATH=src .venv/bin/python -m job_scout.cli score-golden-v1-calibration \
  data/benchmarks/golden-v1-calibration-results.json \
  --report data/benchmarks/golden-v1-calibration-report.json
```

Runner usuwa pole `expected` przed każdym wywołaniem modelu, używa temperatury
0, stałego seedu 42 i jawnego budżetu reasoning 384 tokeny, a wynik zapisuje po
każdym przypadku. Etykiety kalibracji można następnie analizować i wykorzystać
do poprawienia instrukcji.

Jeżeli pojedynczy przypadek nie przejdzie kontraktu technicznego, ponów tylko
brakujące wyniki, zachowując hashe, model i ukończone odpowiedzi:

```bash
PYTHONPATH=src .venv/bin/python -m job_scout.cli \
  run-golden-v1-calibration --resume
```

## Prawidłowy ślepy przebieg

1. Zamroź instrukcję i publiczne wejścia, zapisując ich SHA-256.
2. Uruchom model wyłącznie z profilem, instrukcją i
   `validation.inputs.synthetic.json`.
3. Zapisz wszystkie 18 odpowiedzi do pliku `results.json`. Nie wykonuj retry
   tylko dla błędnych merytorycznie przypadków.
4. Dopiero po zamknięciu procesu modelu uruchom scorer:

```bash
PYTHONPATH=src .venv/bin/python -m job_scout.cli score-golden-v1 \
  data/benchmarks/golden-v1-results.json \
  --report data/benchmarks/golden-v1-report.json
```

Scorer otwiera prywatny klucz dopiero po inferencji. Raportuje accuracy,
macro-F1, recall per klasa, false apply/reject, MAE każdej oceny, poprawność
cytatów i listę każdego rozbieżnego przypadku wraz z oczekiwanym uzasadnieniem.

## Granice wiarygodności

Syntetyczny holdout sprawdza, czy model odtwarza jawnie zaprojektowaną logikę i
przypadki brzegowe. Nie dowodzi trafności na zmieniającym się rynku ofert.
Po przejściu v1 należy zebrać 8–12 nowych realnych ofert jako shadow set bez
zmiany instrukcji. Dopiero oba wyniki pozwalają mówić o praktycznej walidacji.

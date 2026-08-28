"""Human-readable local review page for collected offers."""

from __future__ import annotations

from html import escape
from pathlib import Path

from .collector import CollectionResult


def write_review_report(result: CollectionResult, path: Path) -> None:
    cards = []
    for offer in result.offers:
        locations = ", ".join(offer.locations) or "Nie wykryto"
        supplemental = offer.supplemental_info

        def item_list(items) -> str:
            return (
                "".join(
                    f"<li><strong>{escape(item.category)}</strong> — {escape(item.evidence)}</li>"
                    for item in items
                )
                or "<li>Brak wykrytych informacji</li>"
            )

        cards.append(
            f"""
            <article class="card" data-company="{escape(offer.company.casefold())}">
              <div class="meta">{escape(offer.company)} · {escape(locations)}</div>
              <h2>{escape(offer.title)}</h2>
              <div class="tags">
                <span>{escape(offer.extraction_method)}</span>
                <span>pełny: {len(offer.description)} znaków</span>
                <span>do AI: {len(offer.analysis_text)} znaków</span>
                <span>usunięto: {offer.removed_characters}</span>
              </div>
              <p><a href="{escape(str(offer.url))}" target="_blank" rel="noreferrer">
                Otwórz ogłoszenie
              </a></p>
              <section class="supplemental">
                <h3>Poza scoringiem CV</h3>
                <h4>Warunki pracy i umowa</h4>
                <ul>{item_list(supplemental.work_conditions + supplemental.compensation)}</ul>
                <h4>Ciekawsze benefity</h4>
                <ul>{item_list(supplemental.interesting_benefits)}</ul>
                <details>
                  <summary>Standardowe benefity</summary>
                  <ul>{item_list(supplemental.standard_benefits)}</ul>
                </details>
                <h4>Podróże</h4>
                <ul>{item_list(supplemental.travel_requirements)}</ul>
              </section>
              <details>
                <summary>Pokaż tekst wysyłany do AI</summary>
                <div class="description">{escape(offer.analysis_text)}</div>
              </details>
              <details>
                <summary>Pokaż pełny opis audytowy</summary>
                <div class="description">{escape(offer.description)}</div>
              </details>
            </article>
            """
        )
    errors = "".join(
        f"<li><strong>{escape(error.company)}</strong>: {escape(error.error)}</li>"
        for error in result.errors
    )
    companies = sorted({offer.company for offer in result.offers}, key=str.casefold)
    options = "".join(
        f'<option value="{escape(company.casefold())}">{escape(company)}</option>'
        for company in companies
    )
    document = f"""<!doctype html>
<html lang="pl">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AI Job Scout — próbka ofert</title>
  <style>
    :root {{ color-scheme: light dark; font-family: Inter, system-ui, sans-serif; }}
    body {{ max-width: 1100px; margin: 0 auto; padding: 32px 20px; line-height: 1.55; }}
    header {{ margin-bottom: 28px; }}
    select {{ padding: 8px 12px; font: inherit; }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
      gap: 18px;
    }}
    .card {{ border: 1px solid #8886; border-radius: 14px; padding: 20px; background: #8881; }}
    .meta {{ color: #777; font-size: .9rem; }}
    h2 {{ font-size: 1.15rem; margin: 8px 0; }}
    .tags {{ display: flex; gap: 8px; flex-wrap: wrap; }}
    .tags span {{
      border: 1px solid #8886; border-radius: 999px; padding: 2px 8px; font-size: .8rem;
    }}
    summary {{ cursor: pointer; font-weight: 600; }}
    .description {{ white-space: pre-wrap; margin-top: 12px; font-size: .92rem; }}
    .supplemental {{ border-top: 1px solid #8886; margin: 14px 0; padding-top: 10px; }}
    .supplemental h3, .supplemental h4 {{ margin-bottom: 4px; }}
    .supplemental ul {{ margin-top: 4px; padding-left: 20px; font-size: .88rem; }}
    .errors {{ border-left: 4px solid #d97706; padding-left: 16px; }}
  </style>
</head>
<body>
  <header>
    <h1>Próbka ofert AI Job Scout</h1>
    <p>
      {len(result.offers)} oczyszczonych ofert z {len(companies)} firm.
      Dane są lokalnym wynikiem testowym.
    </p>
    <label>Firma: <select id="company"><option value="">Wszystkie</option>{options}</select></label>
  </header>
  <p>Odrzucone przez filtr tytułu/lokalizacji: {len(result.rejected)}</p>
  {f'<section class="errors"><h2>Problemy źródeł</h2><ul>{errors}</ul></section>' if errors else ""}
  <main class="grid">{"".join(cards)}</main>
  <script>
    const select = document.querySelector('#company');
    select.addEventListener('change', () => {{
      document.querySelectorAll('.card').forEach(card => {{
        card.hidden = select.value && card.dataset.company !== select.value;
      }});
    }});
  </script>
</body>
</html>"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document, encoding="utf-8")

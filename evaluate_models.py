import json
import time

from openai import OpenAI

# -------------------------------------------------------------------
# KONFIGURACJA LM STUDIO
# API_KEY w LM Studio jest ignorowany, ale biblioteka wymaga jakiegoś.
# BASE_URL to domyślny adres serwera lokalnego z zakładki "Local Server".
# -------------------------------------------------------------------
client = OpenAI(base_url="http://localhost:1234/v1", api_key="lm-studio")

# Testowa oferta pracy do wyciągania informacji (tzw. "Golden Data")
SAMPLE_JOB_TEXT = """
Senior Data Scientist (AI & Agents)
Company: AI Startup
Location: Warszawa (Hybrid)
Salary: 20000 - 30000 PLN B2B

We are looking for an experienced Data Scientist to build agentic workflows
using LangGraph and local LLMs.
Requirements:
- 3+ years in Machine Learning / Data Science
- Strong Python, SQL, and Pandas
- Experience with Agentic AI, LangChain, or CrewAI
- Familiarity with deploying models (Docker, FastAPI)
- Good English (B2)
"""

SYSTEM_PROMPT = """
You are an expert technical recruiter. Extract key information from the following job description.
Return ONLY a valid JSON object with these keys:
- "technologies": list of required technologies/tools (max 7 items)
- "languages": list of spoken languages required (max 3 items)
- "seniority": one word (e.g. Junior, Mid, Senior, Lead, Manager)
- "years_of_experience": integer or null
- "salary": salary range as string (e.g. "15000-22000 PLN") or "Nie podano"
- "contract_type": type of contract (e.g. "B2B", "UoP", "B2B/UoP") or "Nie podano"
- "location": city name or "Nie podano"
- "remote": one of "Remote", "Hybrid", "On-site", or "Nie podano"

Return ONLY the JSON. No extra text.
"""


def extract_json_from_response(content: str) -> dict:
    """Funkcja pomocnicza czyszcząca markdown (np. ```json) z odpowiedzi LLM."""
    content = content.strip()
    if content.startswith("```json"):
        content = content[7:]
    if content.endswith("```"):
        content = content[:-3]
    return json.loads(content.strip())


def evaluate_extraction():
    print("=" * 60)
    print("🚀 ROZPOCZYNAMY EWALUACJĘ MODELU Z LM STUDIO 🚀")
    print("=" * 60)

    # 1. Sprawdzenie połączenia z LM Studio i pobranie nazwy załadowanego modelu
    try:
        models = client.models.list()
        model_name = models.data[0].id
        print("✅ Połączono z LM Studio!")
        print(f"🤖 Aktualnie załadowany model: {model_name}")
    except Exception as e:
        print(f"❌ BŁĄD POŁĄCZENIA Z LM STUDIO: {e}")
        print("\n=> Upewnij się, że:")
        print("   1. Otworzyłaś LM Studio.")
        print("   2. Załadowałaś jakikolwiek model na czat.")
        print("   3. W zakładce 'Local Server' (<-->) kliknęłaś 'Start Server'.")
        return

    print("\nWysyłam testową ofertę pracy do modelu...")

    # 2. Wykonanie zapytania i pomiar czasu
    start_time = time.time()
    try:
        response = client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": SAMPLE_JOB_TEXT},
            ],
            temperature=0.0,  # Zawsze 0.0 do ekstrakcji JSON, by model nie fantazjował
        )
        end_time = time.time()

        raw_content = response.choices[0].message.content

        print(f"⏱️  Czas odpowiedzi: {end_time - start_time:.2f} s")
        print("\n--- Otrzymany surowy wynik z modelu ---")
        print(raw_content)
        print("---------------------------------------")

        # 3. Walidacja, czy wynik to faktycznie poprawny JSON
        try:
            parsed_json = extract_json_from_response(raw_content)
            print("\n✅ SUKCES: Model poprawnie wygenerował strukturę JSON!")
            print(f"Wykryte technologie: {parsed_json.get('technologies')}")
            print(f"Wykryty poziom (seniority): {parsed_json.get('seniority')}")
        except json.JSONDecodeError:
            print("\n❌ PORAŻKA (HALLUCYNACJA FORMATU):")
            print("Model wygenerował tekst, który nie da się zdekodować jako poprawny JSON.")
            print("To oznacza, że w prawdziwym systemie rzuciłby wyjątkiem i zatrzymał przepływ.")

    except Exception as e:
        print(f"\n❌ Błąd podczas generowania: {e}")


if __name__ == "__main__":
    evaluate_extraction()

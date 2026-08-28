import json

# pyrefly: ignore [missing-import]
from bs4 import BeautifulSoup
from openai import OpenAI


# ---------------------------------------------------------
# KROK 1: CZYSZCZENIE HTML (Złota Zasada Pre-processingu)
# ---------------------------------------------------------
def clean_html_to_text(html_path: str) -> str:
    """Wczytuje HTML z dysku i wyciąga sam czysty tekst z zachowaniem semantyki"""
    with open(html_path, encoding="utf-8") as f:
        html_content = f.read()

    # Inicjujemy parser BeautifulSoup
    soup = BeautifulSoup(html_content, "html.parser")

    # Usuwamy niepotrzebne znaczniki, które tylko "zanieczyszczają" pamięć modelu
    for tag in soup(["script", "style", "nav", "footer", "header", "meta"]):
        tag.decompose()

    # Wyciągamy sam czysty tekst (łączymy spacją)
    clean_text = soup.get_text(separator=" ", strip=True)
    return clean_text


# ---------------------------------------------------------
# KROK 2: EKSTRAKCJA PRZEZ LLM (Local LM Studio)
# ---------------------------------------------------------
def extract_json_from_offer(text: str) -> dict:
    """Uderza do lokalnego LM Studio, wymuszając zwrócenie formatu JSON"""
    print("⏳ Łączę się z lokalnym LM Studio...")

    # vLLM udostępnia serwer zachowujący się 1:1 jak OpenAI, pod adresem localhost:8000
    client = OpenAI(
        base_url="http://localhost:8000/v1",
        api_key="vllm",  # Ten klucz to tylko zaślepka
    )

    system_prompt = """
    Jesteś analitykiem HR. Twoim zadaniem jest przeczytanie oferty pracy
    i zwrócenie danych w formacie JSON.
    MUSISZ zwrócić TYLKO poprawny kod JSON. Żadnego tekstu powitalnego,
    żadnych bloków kodu markdown (```).

    Wymagany format JSON:
    {
        "stanowisko": "nazwa stanowiska",
        "firma": "nazwa firmy (jeśli brak to null)",
        "wymagane_technologie": ["technologia1", "technologia2"],
        "poziom": "Junior/Mid/Senior",
        "praca_zdalna": true/false
    }
    """

    response = client.chat.completions.create(
        # W LM Studio nazwa modelu nie ma znaczenia; bierze ten załadowany.
        model="local-model",
        messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": text}],
        temperature=0.1,  # Niska temperatura dla determinizmu
        # W LM Studio 2026 możemy wymusić output JSON:
        response_format={"type": "json_object"},
    )

    raw_json = response.choices[0].message.content
    return json.loads(raw_json)


# ---------------------------------------------------------
# URUCHOMIENIE TESTU
# ---------------------------------------------------------
if __name__ == "__main__":
    file_path = "mock_oferta.html"

    print("=" * 50)
    print("START TESTU: FAZA 1 (EKSTRAKTOR)")
    print("=" * 50)

    print(f"\n1. Czyszczę plik: {file_path}...")
    cleaned_text = clean_html_to_text(file_path)
    print(f"✅ Oczyszczono. Długość tekstu: {len(cleaned_text)} znaków.")
    print("Próbka tekstu:", cleaned_text[:100], "...")

    print("\n2. Wysyłam do modelu w celu ekstrakcji JSON...")
    try:
        extracted_data = extract_json_from_offer(cleaned_text)
        print("\n✅ SUKCES! Otrzymany JSON z modelu:")
        print(json.dumps(extracted_data, indent=4, ensure_ascii=False))
    except Exception as e:
        print("\n❌ BŁĄD! Upewnij się, że masz włączony serwer vLLM na porcie 8000.")
        print(f"Szczegóły błędu: {e}")

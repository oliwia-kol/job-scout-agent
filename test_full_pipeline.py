import asyncio

from src.job_scout.notion_integration import add_job_to_notion

from main import JobScoutState, app
from src.job_scout.telegram_integration import send_telegram_alert


async def main():
    print("=" * 50)
    print("START TESTU E2E: POBIERANIE -> LANGGRAPH -> INTEGRACJE")
    print("=" * 50)

    # Możesz tutaj przetestować dowolną firmę z configu (np. Netflix, InPost)
    company_name = "InPost"

    # Inicjalizacja stanu LangGraph - całe pobieranie odbywa się w grafie
    initial_state = JobScoutState(
        company_name=company_name,
        job_url=None,
        job_title=None,
        clean_text=None,
        extracted_data=None,
        is_valid=None,
        evaluation=None,
    )

    print(f"\n[Uruchamianie LangGraph dla {company_name}...]")
    # Używamy ainvoke, ponieważ pierwszy węzeł fetch_and_clean jest asynchroniczny
    final_state = await app.ainvoke(initial_state)

    print("\n🎉 Zakończono przetwarzanie przez LangGraph!")

    if final_state.get("is_valid") and final_state.get("evaluation"):
        print("\n[Wysyłanie do Notion i Telegram...]")
        add_job_to_notion(final_state)
        send_telegram_alert(final_state)
    else:
        print("\nOferta odrzucona przez pre-filtr, wystąpił błąd lub brak ocenianych danych.")


if __name__ == "__main__":
    asyncio.run(main())

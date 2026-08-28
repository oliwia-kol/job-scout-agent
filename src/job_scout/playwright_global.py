import asyncio

from playwright.async_api import async_playwright


async def global_playwright_scrape(url: str):
    jobs = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--disable-web-security"])
        page = await browser.new_page()
        try:
            await page.goto(url, wait_until="networkidle", timeout=15000)
            await asyncio.sleep(2)
        except Exception:
            pass  # Timeout is fine if DOM is mostly loaded

        # Scrape main page and ALL frames!
        for frame in page.frames:
            try:
                links = await frame.eval_on_selector_all(
                    "a", "elements => elements.map(e => ({href: e.href, text: e.innerText}))"
                )
                for link in links:
                    href = link.get("href", "").lower()
                    text = link.get("text", "").strip()
                    if not href or not text or len(text) < 5:
                        continue

                    valid = False
                    if "oferta" in href or "job" in href or "career" in href or "role" in href:
                        valid = True
                    if (
                        "pracuj.pl" in href
                        or "myworkdayjobs.com" in href
                        or "system.erecruiter.pl" in href
                    ):
                        valid = True
                    if "login" in href or "polityka" in href:
                        valid = False

                    if valid:
                        jobs.append(
                            {"title": text[:100].replace("\n", " "), "url": link.get("href")}
                        )
            except Exception:
                pass

        await browser.close()

    # Deduplicate
    unique_jobs = {j["url"]: j for j in jobs}.values()
    return list(unique_jobs)


async def test():
    urls = [
        "https://velobank.pl/kariera",
        "https://netflix.wd1.myworkdayjobs.com/Netflix_Careers",
        "https://careers.nordea.com/",
        "https://kariera.bik.pl",
        "https://revolut.com/careers",
    ]
    for u in urls:
        print(f"Testing {u}...")
        jobs = await global_playwright_scrape(u)
        print(f" -> Found {len(jobs)} jobs")


asyncio.run(test())

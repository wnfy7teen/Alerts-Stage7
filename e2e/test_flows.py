import re

from playwright.sync_api import expect

DID = "a" * 32
ADDRESS = "TXyzAddress1234567890abcdefghijk"


def collect_problems(page):
    problems = []
    page.on("console", lambda m: problems.append(m.text) if re.search(r"Content Security Policy|Refused to", m.text) else None)
    page.on("pageerror", lambda e: problems.append(str(e)))
    return problems


def test_donate_page_loads_under_csp(page, base_url):
    problems = collect_problems(page)
    page.goto(base_url + "/")
    expect(page.get_by_role("heading", level=1)).to_contain_text("Поддержать")
    expect(page.get_by_role("button", name=re.compile(r"Поддержать на 300"))).to_be_visible()
    page.get_by_role("button", name="500 ₽").click()
    expect(page.get_by_role("button", name=re.compile(r"Поддержать на 500"))).to_be_visible()
    assert problems == []


def test_crypto_payment_stays_on_site_and_shows_thanks(page, base_url):
    status = {"value": "pending"}
    seen = {}

    def create(route):
        seen["key"] = route.request.headers.get("idempotency-key")
        route.fulfill(json={"donation_id": DID, "status": "pending", "provider": "nowpayments", "amount": 300, "currency": "RUB",
                            "checkout": {"type": "crypto_address", "address": ADDRESS, "amount": 3.75, "currency": "USDTTRC20",
                                         "network": "trx", "qr": "data:image/svg+xml;base64,PHN2Zy8+"}})

    page.route("**/api/donations", create)
    page.route("**/api/payments/status/*", lambda route: route.fulfill(
        json={"id": DID, "status": status["value"], "amount": 300, "currency": "RUB", "provider": "nowpayments"}))
    problems = collect_problems(page)
    page.goto(base_url + "/")
    page.get_by_text("Криптовалюта", exact=True).click()
    page.get_by_role("button", name=re.compile(r"Поддержать на")).click()
    expect(page.get_by_text(ADDRESS)).to_be_visible()
    expect(page.get_by_alt_text("QR-код адреса для оплаты")).to_be_visible()
    status["value"] = "paid"
    expect(page.get_by_role("heading", name=re.compile("Спасибо"))).to_be_visible(timeout=10_000)
    assert page.url.startswith(base_url) and re.fullmatch(r"[A-Za-z0-9_-]{16,64}", seen["key"] or "")
    assert problems == []


def test_server_error_is_shown_and_form_stays_usable(page, base_url):
    page.route("**/api/donations", lambda route: route.fulfill(status=429, json={"detail": "Слишком много попыток, подождите несколько минут"}))
    page.goto(base_url + "/")
    button = page.get_by_role("button", name=re.compile(r"Поддержать на"))
    button.click()
    expect(page.get_by_role("alert")).to_contain_text("Слишком много попыток")
    expect(button).to_be_enabled()


def test_form_is_keyboard_operable(page, base_url):
    page.goto(base_url + "/")
    page.get_by_label("Сумма").focus()
    for _ in range(12):
        page.keyboard.press("Tab")
        if page.evaluate("document.activeElement.id") == "btn":
            break
    assert page.evaluate("document.activeElement.id") == "btn"


def test_admin_page_and_test_alert(browser, base_url, admin_api):
    ctx = browser.new_context(http_credentials={"username": "admin", "password": "e2e-admin-password-123"})
    page = ctx.new_page()
    problems = collect_problems(page)
    page.goto(base_url + "/admin")
    expect(page.get_by_role("heading", level=1)).to_have_text("Донат-алерты")
    expect(page.locator("#overlayLink")).to_contain_text("/overlay?key=")
    page.get_by_role("button", name="Отправить тестовый алерт").click()
    expect(page.locator("#testStatus")).to_contain_text("Оверлей не подключён")
    assert problems == []
    ctx.close()


def test_overlay_shows_alert(page, base_url, admin_api):
    problems = collect_problems(page)
    page.goto(f"{base_url}/overlay?key=e2e-overlay-key-0123456789")
    for _ in range(40):  # ждём, пока оверлей подключится по WebSocket
        sent = admin_api.post("/api/admin/test", data={"name": "Вася", "message": "Привет, стрим!", "amount": 500}).json()
        if sent["overlays_connected"]:
            break
        page.wait_for_timeout(250)
    assert sent["overlays_connected"] == 1
    expect(page.locator("#card")).to_be_visible(timeout=10_000)
    expect(page.locator("#title")).to_contain_text("Вася")
    expect(page.locator("#msg")).to_have_text("Привет, стрим!")
    assert problems == []


def test_page_is_readable_without_javascript(browser, base_url):
    """Так страницу видят роботы поисковиков и ИИ: JavaScript выключен, а текст и FAQ на месте."""
    ctx = browser.new_context(java_script_enabled=False)
    page = ctx.new_page()
    page.goto(base_url + "/")
    expect(page.get_by_role("heading", level=1)).to_contain_text("Поддержать")
    expect(page.get_by_text("Вопросы и ответы")).to_be_visible()
    expect(page.get_by_text("Карта или СБП", exact=True)).to_be_visible()
    assert page.locator('link[rel="canonical"]').get_attribute("href").endswith("/")
    assert page.locator('meta[property="og:image"]').get_attribute("content").endswith("/og-cover.png")
    ctx.close()

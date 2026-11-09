"""Автоматическая проверка доступности (axe-core, правила WCAG 2.0, 2.1 и 2.2 уровней A и AA). Ручная проверка скринридером остаётся за человеком."""
import re

import pytest
from axe_playwright_python.sync_playwright import Axe

TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"]
DID = "a" * 32


def violations(page):
    result = Axe().run(page, options={"runOnly": {"type": "tag", "values": TAGS}})
    return [f"{v['id']} ({v['impact']}): {v['help']} -> " + "; ".join(n["target"][0] if isinstance(n["target"][0], str) else str(n["target"][0]) for n in v["nodes"][:3])
            for v in result.response["violations"]]


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_donate_page_has_no_axe_violations(browser, base_url, scheme):
    ctx = browser.new_context(color_scheme=scheme)
    page = ctx.new_page()
    page.goto(base_url + "/")
    assert violations(page) == []
    ctx.close()


def test_payment_states_have_no_axe_violations(browser, base_url):
    ctx = browser.new_context()
    page = ctx.new_page()
    state = {"status": "pending"}
    page.route("**/api/donations", lambda route: route.fulfill(json={
        "donation_id": DID, "status": "pending", "provider": "nowpayments", "amount": 300, "currency": "RUB",
        "checkout": {"type": "crypto_address", "address": "TXyzAddress1234567890abcdefghijk", "amount": 3.75, "currency": "USDTTRC20",
                     "network": "trx", "qr": "data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciLz4="}}))
    page.route("**/api/payments/status/*", lambda route: route.fulfill(json={
        "id": DID, "status": state["status"], "amount": 300, "currency": "RUB", "provider": "nowpayments"}))
    page.goto(base_url + "/")
    page.get_by_text("Криптовалюта", exact=True).click()
    page.get_by_role("button", name=re.compile("Поддержать на")).click()
    page.get_by_text("Скопировать адрес").wait_for()
    assert violations(page) == []
    state["status"] = "paid"
    page.get_by_role("heading", name=re.compile("Спасибо")).wait_for(timeout=10_000)
    assert violations(page) == []
    ctx.close()


def test_error_state_has_no_axe_violations(browser, base_url):
    ctx = browser.new_context()
    page = ctx.new_page()
    page.route("**/api/donations", lambda route: route.fulfill(status=429, json={"detail": "Слишком много попыток, подождите несколько минут"}))
    page.goto(base_url + "/")
    page.get_by_role("button", name=re.compile("Поддержать на")).click()
    page.get_by_role("alert").filter(has_text="Слишком много").wait_for()
    assert violations(page) == []
    ctx.close()


def test_thanks_has_no_axe_violations(browser, base_url):
    ctx = browser.new_context()
    page = ctx.new_page()
    page.goto(base_url + "/thanks")
    assert violations(page) == []
    ctx.close()

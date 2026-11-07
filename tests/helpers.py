"""Общие помощники для тестов: имитация провайдеров и отправка доната."""
import json

import httpx

import payments as pay

KEY = "k" * 24


class Fake:
    """Подменяет HTTP к провайдерам и собирает алерты."""

    def __init__(self):
        self.yk_status, self.yk_amount, self.yk_currency = "pending", "300.00", "RUB"
        self.np_status, self.np_price, self.np_price_currency = "waiting", 300, "rub"
        self.cp_status, self.cp_amount, self.cp_fiat = "active", "300.00", "RUB"
        self.yk_created, self.np_created, self.alerts = [], [], []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url, method = str(request.url), request.method
        if url == "https://api.yookassa.ru/v3/payments" and method == "POST":
            self.yk_created.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "yk-1", "status": "pending",
                                             "confirmation": {"type": "embedded", "confirmation_token": "ct-1"}})
        if url == "https://api.yookassa.ru/v3/payments/yk-1":
            did = self.yk_created[0]["metadata"]["donation_id"]
            return httpx.Response(200, json={"id": "yk-1", "status": self.yk_status, "paid": self.yk_status == "succeeded",
                                             "amount": {"value": self.yk_amount, "currency": self.yk_currency},
                                             "metadata": {"donation_id": did}})
        if url == "https://api.nowpayments.io/v1/payment" and method == "POST":
            self.np_created.append(json.loads(request.content))
            return httpx.Response(201, json={"payment_id": 777, "payment_status": "waiting", "network": "trx",
                                             "pay_address": "TXyzAddress1234567890abcdefghijk", "pay_amount": 3.75,
                                             "pay_currency": "usdttrc20"})
        if url == "https://api.nowpayments.io/v1/payment/777":
            return httpx.Response(200, json={"payment_id": 777, "payment_status": self.np_status,
                                             "order_id": self.np_created[0]["order_id"], "price_amount": self.np_price,
                                             "price_currency": self.np_price_currency})
        if url == "https://testnet-pay.crypt.bot/api/createInvoice":
            return httpx.Response(200, json={"ok": True, "result": {"invoice_id": 555,
                                             "bot_invoice_url": "https://t.me/CryptoTestnetBot?start=IVx"}})
        if url.startswith("https://testnet-pay.crypt.bot/api/getInvoices"):
            return httpx.Response(200, json={"ok": True, "result": {"items": [
                {"invoice_id": 555, "status": self.cp_status, "amount": self.cp_amount, "fiat": self.cp_fiat}]}})
        return httpx.Response(404, json={"error": "unexpected " + url})


def donate(client, method="yookassa", amount=300, key=KEY, **kw):
    return client.post("/api/donations", headers={"Idempotency-Key": key},
                       json={"method": method, "amount": amount, "name": kw.get("name", "Вася"),
                             "message": kw.get("message", "Привет")})


def route_through(monkeypatch, fake, override):
    """Перехватывает часть HTTP-запросов к провайдерам, остальное отдаёт обычному моку."""
    def handler(request):
        res = override(request)
        return res if res is not None else fake.handler(request)
    monkeypatch.setattr(pay, "HTTP_TRANSPORT", httpx.MockTransport(handler))

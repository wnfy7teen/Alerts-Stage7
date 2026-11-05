# Этап 1: оплата внутри сайта

| Файл | Что сделано |
|---|---|
| `payments.py` (новый) | Таблица `donations` по ТЗ (+ `idem_key`, `checkout`), миграция старой схемы, уникальный индекс `(provider, provider_txn_id)`, провайдеры ЮKassa / NOWPayments / Crypto Pay, webhook-и, опрос статусов, валидация суммы, санитайзинг, лимитер |
| `server.py` | Убраны `/api/donate` и старый опрос. Добавлены `POST /api/donations`, `GET /api/payments/status/{id}`, `POST /api/webhooks/{yookassa,nowpayments,cryptopay}`, `GET /api/health`. История и повтор алерта работают с новой таблицей. Лимит сообщения 500. Пути `DB_PATH`, `CONFIG_PATH`, `MEDIA_DIR` из окружения |
| `static/donate.html` | Новая страница: пресеты 100/300/500/1000 и своя сумма, выбор способа, форма ЮKassa, адрес и QR, состояния idle → processing → success / error / canceled, галочка, возврат по `?donation=`, защита от двойной отправки |
| `tests/` (новые) | 19 тестов: сумма, санитайзинг, идемпотентность, лимит, подписи, повторные webhook-и, подделка, несовпадение суммы, опрос, миграция |
| `.env.example`, `requirements*.txt`, `README.md` | Новые переменные, `segno`, раздел «Как работает оплата» |
| `static/overlay.html`, `static/admin.html` | Без изменений |

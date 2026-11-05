# Донат-алерты для Twitch

Зритель открывает страницу доната, платит прямо на сайте, и на стриме появляется алерт с гифкой, звуком и озвучкой.
Способы оплаты: карта и СБП (ЮKassa), криптовалюта на сайте (NOWPayments), Crypto Bot (откроется Telegram).

## Установка
Нужен Python 3.12 (подойдёт 3.10 и новее).
```
pip install -r requirements.lock
```
`requirements.lock` содержит точные проверенные версии. `requirements.txt` с диапазонами версий нужен для обновления зависимостей.

## Переменные окружения
Скопируйте `.env.example` в `.env` и заполните. Файл `.env` нельзя публиковать и добавлять в репозиторий.

| Переменная | Нужна | Что это |
|---|---|---|
| `ADMIN_PASSWORD` | да | Пароль админки (логин `admin`), не короче 12 символов |
| `OVERLAY_KEY` | да | Секрет в ссылке оверлея для OBS, не короче 16 символов |
| `PUBLIC_URL` | в проде | Адрес сайта с https, без слеша в конце. Нужен для возврата после оплаты и webhook-ов |
| `STREAMER_NAME` | нет | Имя в заголовке страницы доната |
| `PORT`, `HOST` | нет | Порт (8000) и адрес запуска (по умолчанию 127.0.0.1; в Docker 0.0.0.0) |
| `YOOKASSA_SHOP_ID`, `YOOKASSA_SECRET_KEY` | для карт и СБП | Ключи ЮKassa (сначала тестовые) |
| `YOOKASSA_WEBHOOK_IPS` | нет | Список IP ЮKassa для проверки webhook-ов (из документации ЮKassa) |
| `NOWPAYMENTS_API_KEY`, `NOWPAYMENTS_IPN_SECRET` | для крипты | Ключи NOWPayments |
| `NOWPAYMENTS_PAY_CURRENCY`, `NOWPAYMENTS_SANDBOX` | нет | Монета (`usdttrc20` по умолчанию) и режим песочницы |
| `CRYPTO_PAY_TOKEN`, `TESTNET` | для Crypto Bot | Токен Crypto Pay и тестовая сеть |
| `TRUST_PROXY_HEADERS` | за прокси | 1, если сервер стоит за Caddy, nginx или Cloudflare (в Docker включено) |
| `FORCE_HTTPS` | в проде | 1 — перенаправлять http на https (в Docker включено) |
| `ADMIN_ALLOWED_CIDRS` | нет | Пускать в админку только с этих адресов (через запятую) |
| `TURNSTILE_SITE_KEY`, `TURNSTILE_SECRET_KEY` | нет | Cloudflare Turnstile. Пусто, значит выключено |
| `CSP_REPORT_ONLY` | нет | 1 — политика CSP только сообщает о нарушениях (для отладки) |
| `SENTRY_DSN`, `SENTRY_ENV` | нет | Мониторинг ошибок Sentry без персональных данных |
| `DB_PATH`, `CONFIG_PATH`, `MEDIA_DIR` | нет | Где хранить базу, настройки и загруженные файлы (в Docker это `/data`) |
| `DOMAIN`, `ACME_EMAIL` | Docker | Домен сайта и почта для сертификата |
| `BACKUP_KEEP` | нет | Сколько копий базы хранить (14) |

## Запуск локально
Windows: запустите `start.bat`. Другие системы: `python server.py`. Сервер откроется на `http://localhost:8000`.
- Админка: `http://localhost:8000/admin` (логин `admin`, пароль из `.env`). Там же ссылка для OBS.
- OBS: Источники, «+», Браузер, ссылка из админки, ширина 1000, высота 600.
- Для проб без реальных денег: тестовый магазин ЮKassa, `NOWPAYMENTS_SANDBOX=1`, `TESTNET=1` (@CryptoTestnetBot).
- Чтобы страницу доната видели зрители, нужен публичный адрес (см. «Деплой»). Для проб подойдёт `cloudflared tunnel --url http://localhost:8000`, но этот адрес меняется при каждом запуске.

## Как работает оплата
Способы оплаты включаются сами, как только в `.env` заполнены ключи:

| Способ | Что видит зритель | Ключи |
|---|---|---|
| Карта или СБП (ЮKassa) | Платёжная форма внутри страницы доната | `YOOKASSA_SHOP_ID`, `YOOKASSA_SECRET_KEY` |
| Криптовалюта (NOWPayments) | Адрес, сумма и QR прямо на сайте | `NOWPAYMENTS_API_KEY`, `NOWPAYMENTS_IPN_SECRET` |
| Crypto Bot | Кнопка «Открыть Crypto Bot» (оплата идёт в Telegram) | `CRYPTO_PAY_TOKEN` |

Порядок: форма, `POST /api/donations` (заголовок `Idempotency-Key`), провайдер, оплата, webhook или опрос, алерт на стриме. Страница сама проверяет статус через `GET /api/payments/status/{id}` и показывает «Спасибо» без перехода на другие сайты.

Надёжность:
- Webhook только будит проверку. «Оплачено» решается запросом в API провайдера, а не телом входящего запроса, поэтому подделка не засчитается.
- Подписи: NOWPayments (HMAC-SHA512) и Crypto Pay (HMAC-SHA256). У ЮKassa подписи нет: защита из проверки статуса через API и необязательного списка IP.
- Дубль не засчитается: атомарное обновление статуса и уникальный индекс по `(provider, provider_txn_id)`.
- Если webhook не дошёл, сервер сам опрашивает провайдеров каждые 5 секунд. Сумма платежа сверяется с суммой доната.
- Лимит: 5 попыток создать платёж за 10 минут на один IP. Сообщение до 500 символов, HTML-теги вырезаются.

Адреса webhook-ов (нужен `PUBLIC_URL` с https):
- ЮKassa: Интеграция, HTTP-уведомления, `https://ваш-домен/api/webhooks/yookassa`, события `payment.succeeded` и `payment.canceled`.
- NOWPayments: адрес передаётся при создании платежа автоматически, нужен только IPN secret в `.env`.
- Crypto Pay: `https://ваш-домен/api/webhooks/cryptopay`.

## Безопасность
Подробный аудит: `SECURITY-AUDIT.md`. Главное для эксплуатации:
- Сервер не запустится со слабыми секретами.
- Админка: 10 неверных паролей за 15 минут блокируют IP, изменяющие запросы защищены от CSRF. По желанию закройте её списком адресов (`ADMIN_ALLOWED_CIDRS`).
- CSP включена на всех страницах. Платёжная форма ЮKassa и Turnstile подгружают свои скрипты и фреймы: при первой тестовой оплате откройте консоль браузера. Если форма не появилась, запустите с `CSP_REPORT_ONLY=1` и посмотрите в логе строки `CSP:`.
- Cloudflare Turnstile по желанию. Минус: у части зрителей проверка может не загрузиться, и они не смогут отправить форму.
- Приложение не использует cookie и сессии. Админка работает на Basic Auth только по HTTPS (с HSTS).
- Озвучка отправляет текст сообщений на серверы Microsoft (edge-tts). Если это неприемлемо, отключите озвучку в админке.

## Поиск, ИИ и красивая ссылка в Telegram
Главная страница `/` и есть страница доната (старый адрес `/donate` перенаправляет на неё). Текст, способы оплаты и вопросы-ответы отрисовываются на сервере, поэтому их видят роботы без JavaScript.
- Что формируется автоматически из настроек сайта: `title` (до 60 символов), `description` (до 160), канонический адрес, Open Graph и Twitter Card, разметка JSON-LD (Person с DonateAction, WebSite, WebPage, FAQPage), `/robots.txt` (разрешены GPTBot, OAI-SearchBot, ChatGPT-User, ClaudeBot, anthropic-ai, PerplexityBot, Google-Extended, Bingbot, Applebot-Extended; закрыты админка, оверлей и API), `/sitemap.xml`, `/llms.txt`.
- Факты в тексте и FAQ (минимальная сумма, способы оплаты, озвучка) берутся из настроек админки и меняются вместе с ними.
- Имя, ссылки и текст: `STREAMER_NAME`, `SAME_AS` (ссылки на Twitch, Telegram и т.д.), `ABOUT_TEXT`. Обязательно задайте `PUBLIC_URL`: от него строятся все адреса в разметке.
- Обложка для ссылок (1200×630) и иконка лежат в `static/`. Пересоздать под другое имя или оттенок розового: `python scripts/make_brand_assets.py --name ИМЯ --pink "#F8C8DC"`.

После выкладки проверьте:
1. `curl -A "GPTBot" -I https://ваш-домен/` отвечает 200 и `text/html`. Если домен за Cloudflare, проверьте в его настройках блокировку ИИ-ботов (раздел Security, Bots или AI Crawl Control): она может блокировать роботов вопреки `robots.txt`.
2. Отправьте ссылку в Telegram. Telegram запоминает превью, поэтому после любых правок обложки или текста отправьте ссылку боту @WebpageBot, он сбросит кэш.
3. Отладчик репостов Facebook (Sharing Debugger) и проверка карточки Twitter/X покажут, как ссылка выглядит в других сервисах.
4. Добавьте сайт в Google Search Console и Яндекс Вебмастер и укажите `https://ваш-домен/sitemap.xml`.

## Деплой на VPS за 10 минут (Docker + Caddy)
Нужны: VPS с Ubuntu 22.04 или новее, домен.
1. Создайте A-запись домена на IP сервера.
2. Установите Docker: `curl -fsSL https://get.docker.com | sh`.
3. Скопируйте проект на сервер (`git clone` вашего репозитория) и перейдите в папку.
4. `cp .env.example .env` и заполните: `DOMAIN`, `ACME_EMAIL`, `PUBLIC_URL` (`https://ваш-домен`), `ADMIN_PASSWORD`, `OVERLAY_KEY`, ключи провайдеров.
5. Файрвол: `ufw allow OpenSSH && ufw allow 80/tcp && ufw allow 443/tcp && ufw allow 443/udp && ufw enable`. Порт приложения наружу не публикуется, он доступен только Caddy.
6. Запуск: `docker compose up -d --build`. Логи: `docker compose logs -f`. Сертификат Caddy получит сам при первом обращении к домену.
7. Откройте `https://ваш-домен/admin`, настройте и скопируйте ссылку для OBS.
8. Впишите адреса webhook-ов в кабинетах провайдеров (см. «Как работает оплата»).
9. Проведите по одной тестовой оплате каждым способом.

Обновление: `git pull && docker compose up -d --build`. Либо автоматически: workflow `.github/workflows/deploy.yml` выкладывает `main` по SSH после успешной проверки. В настройках репозитория нужны секреты `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_SSH_KEY`, `DEPLOY_PATH` и окружение `production`.

Если домен за Cloudflare (оранжевое облако): режим SSL «Full (strict)», а порты 80 и 443 на сервере откройте только для адресов Cloudflare (список на cloudflare.com/ips). Иначе заголовок с IP клиента можно подделать в обход Cloudflare.

## Мониторинг и резервные копии
- Ошибки: создайте проект в Sentry (Python), вставьте DSN в `SENTRY_DSN`. Имена, сообщения, заголовки и параметры адреса в Sentry не отправляются.
- Доступность: в UptimeRobot добавьте HTTP(s)-монитор на `https://ваш-домен/api/health` (интервал 5 минут, ключевое слово `ok`).
- Копии: сервис `backup` в `docker-compose.yml` раз в сутки делает копию базы и настроек в том `backups` (14 последних, с проверкой целостности). Вынесите копии за пределы сервера, например по cron: `docker compose cp backup:/backups ./backups-copy`, а затем rsync на другой диск или сервер. В копиях есть имена и сообщения зрителей, храните их закрыто. Без Docker: `python scripts/backup.py` по расписанию.
- Восстановление: `docker compose stop app`, затем положите нужный файл на место базы: `docker compose run --rm --no-deps -v "$PWD/restore:/restore:ro" app sh -c "cp /restore/donations-ГГГГММДД-ЧЧММСС.db /data/donations.db && rm -f /data/donations.db-wal /data/donations.db-shm"`, затем `docker compose start app`.

## Тесты и проверки
```
pip install -r requirements-dev.txt
python -m pytest                        # 60 тестов: оплата, webhook-и, безопасность, SEO
ruff check . && mypy                    # линтер с правилами безопасности и проверка типов
pip-audit -r requirements.lock          # известные уязвимости зависимостей
pip install -r requirements-e2e.txt && python -m playwright install chromium && python -m pytest e2e   # тесты в браузере
```
Все проверки запускаются автоматически в GitHub Actions (`.github/workflows/ci.yml`).

## Озвучка
Голос синтезируется библиотекой edge-tts (серверам Microsoft нужен доступ из вашей сети). Если озвучка не создалась, оверлей пробует голос браузера, но в OBS он часто молчит. TTS читает сообщения зрителей как есть, фильтра мата нет. Учитывайте правила Twitch: то, что прозвучало в эфире, считается вашим контентом. Ссылки из текста озвучки вырезаются.

## Файлы
`server.py` (маршруты и админка), `payments.py` (оплата), `security.py` (защита), `seo.py` (поиск и ссылки), `static/` (страницы), `scripts/backup.py` (копии), `tests/` и `e2e/` (тесты), `Dockerfile`, `docker-compose.yml`, `Caddyfile` (деплой). Настройки хранятся в `config.json`, история в `donations.db`, ваши гифки и звуки в `media/`.

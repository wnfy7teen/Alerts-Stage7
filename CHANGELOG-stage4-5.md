# Этапы 4 и 5: поиск, ИИ-поиск и ссылка в Telegram

| Файл | Что сделано |
|---|---|
| `seo.py` (новый) | Мета-теги, Open Graph, Twitter Card, JSON-LD (Person с DonateAction, WebSite, WebPage, FAQPage), FAQ из настроек сайта, `robots.txt`, `sitemap.xml`, `llms.txt`, серверная отрисовка страницы |
| `server.py` | Главная `/` теперь страница доната (старый `/donate` перенаправляет на неё кодом 301). Маршруты `/robots.txt`, `/sitemap.xml`, `/llms.txt`, `/og-cover.png`, `/favicon.svg`, `/favicon.ico` |
| `static/donate.html` | Стал шаблоном: заголовок, вступительный абзац, способы оплаты, пресеты сумм и FAQ приходят уже в HTML. Настройки лежат в странице, отдельного запроса нет (меньше сдвигов макета). Семантика: header, main, footer, один h1, h2 и h3 по порядку |
| `static/og-cover.png`, `favicon.svg`, `favicon.ico` (новые) | Обложка 1200×630 (97 КБ) в цветах #0593C6, #FCFFFF и нежно-розовом, иконка сайта |
| `scripts/make_brand_assets.py` (новый) | Пересоздаёт обложку и иконку под другое имя и оттенок розового. Считает контраст и выдаёт тёмно-голубой для текста (#007ca8, 4,7:1 на белом) |
| `security.py` | `X-Robots-Tag: noindex` для админки, оверлея, API, `/media` и `/thanks` |
| `payments.py` | Адрес возврата после оплаты теперь `/?donation=` |
| `static/admin.html`, `overlay.html`, `thanks.html` | `noindex` в самой странице |
| `tests/test_seo.py` (новый) | 17 тестов: HTML полный без JS, title и description в лимитах, один h1, OG и Twitter Card, JSON-LD совпадает с видимым FAQ, защита от подстановки кода через имя, robots для всех ИИ-ботов, sitemap, llms.txt, noindex, размер обложки, боты получают HTML |
| `e2e/test_flows.py` | Новый тест: страница читается при выключенном JavaScript |
| `.env.example`, `README.md`, `requirements-dev.txt` | `SAME_AS`, `ABOUT_TEXT`, раздел «Поиск, ИИ и красивая ссылка в Telegram», Pillow для скрипта обложки |

Сознательно не делалось:
- BreadcrumbList и SearchAction: на сайте из одной страницы без поиска эта разметка была бы ложной.
- Внутренняя перелинковка и hreflang: страница одна и на одном языке.
- Lighthouse: запустить его без браузера я не мог. Проверка будет в этапе 6 вместе с редизайном.

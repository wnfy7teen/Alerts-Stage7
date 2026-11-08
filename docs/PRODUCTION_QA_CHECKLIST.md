# Production QA checklist

## Payment provider matrix (sandbox/test mode only)
Run against provider test credentials or an isolated test account. Never use a real card or mainnet funds for routine QA.

| Scenario | Expected result |
|---|---|
| YooKassa successful payment | API confirms succeeded+paid, exact donation metadata/amount/currency; one paid transition and one outbox row |
| YooKassa canceled payment | Status becomes canceled; no alert |
| YooKassa duplicate webhook | HTTP success; no second credit or alert |
| YooKassa forged/unknown webhook | No credit; authenticated provider API lookup remains authoritative |
| YooKassa partial refund | One refund record; partially_refunded status; original alert remains one-time |
| YooKassa full refund | refunded status; no second alert |
| NOWPayments finished | HMAC valid and API confirms finished, matching order_id/price/currency; one credit |
| NOWPayments invalid HMAC | 403; no state change |
| NOWPayments partially_paid | Remains pending; no alert until provider reports finished |
| NOWPayments failed/expired | Matching final status; no alert |
| NOWPayments refunded | Refund status recorded; no duplicate credit |
| Crypto Pay paid | Valid HMAC plus getInvoices confirms invoice, fiat and amount; one credit |
| Crypto Pay invalid HMAC | 403; no state change |
| Crypto Pay expired | Expired; no alert |
| Any provider amount/currency mismatch | No credit; investigate provider record |
| Double-click/retry with same Idempotency-Key | Same checkout response; no duplicate invoice |
| Same key with different payload | 409; no second invoice |
| Provider API temporarily unavailable | Payment stays pending; poller retries; no false credit |
| OBS offline during payment | Payment is still paid; durable outbox retries when OBS reconnects |
| Replayed webhook after restart | One paid transition and one outbox entry |

After every scenario inspect the admin transaction journal, payment status, refunds table and alert_outbox. Reset only isolated test data between cases.

## Refund and cancellation operational rules
- A refund is considered successful only after the provider API confirms it or the provider's verified refund status is reconciled.
- Do not change a payment to refunded based on a donor screenshot.
- Partial refunds must not reduce the original payment amount in the audit trail; store refund rows separately.
- Confirm a canceled payment does not produce an OBS alert.
- Repeated refund notifications must not create duplicate refund rows.
- Compare provider refund ID, original payment ID, currency and amount.
- Do not attempt automatic refunds from this application unless an explicit, authenticated refund workflow is implemented and tested.

## Mobile and browser QA
Test on real devices where available:
- iPhone Safari, including private browsing.
- Android Chrome.
- Desktop Chrome, Firefox and Edge.
- Portrait and landscape; widths around 320, 360, 390, 430 and 768 CSS px.
- Slow 4G / throttled network and temporary offline transition.
- Keyboard-only navigation and visible focus states.
- VoiceOver/TalkBack labels for amount, payment methods, inputs, errors and status.
- Reduced-motion preference.
- Long donor names, emoji, mixed scripts and maximum-length messages.
- Minimum/maximum amount and invalid numeric input.
- Double tap on payment action; button must disable while request is in flight.
- Provider iframe/widget and return flow; verify pending is not presented as paid.
- Crypto address/QR wrapping and copy/paste accuracy.
- Error states: rate limit, Turnstile failure, provider timeout, malformed response, expired invoice.
- Success page wording must not claim payment success until server status is confirmed.
- Copy donation URL from browser address bar and open in a second browser/device.

Record browser, OS, viewport, network profile, result and screenshot for each defect.

## Performance and Core Web Vitals
Measure the canonical homepage on mobile and desktop using Lighthouse and PageSpeed Insights:
- LCP target <= 2.5 s.
- INP target <= 200 ms.
- CLS target <= 0.1.
- TTFB target <= 0.8 s as an operational goal, not a Core Web Vital.
- Repeat runs and use field data (CrUX) when available; lab scores alone do not establish field performance.
- Record mobile and desktop separately.
- Inspect render-blocking Google Fonts, image dimensions, cache headers, unused CSS/JS and third-party scripts.
- Do not remove Turnstile or payment-provider scripts solely to improve a synthetic score.
- Use responsive image formats and explicit width/height/aspect ratio where imagery is added.
- Re-test payment widget after any optimization.

## SEO and trust
- Canonical homepage returns 200 and self-references https://vanesso4ka.site/.
- www redirects to the canonical apex host in one hop.
- robots.txt returns 200 and advertises sitemap.xml.
- sitemap.xml is valid XML and contains only public, canonical, indexable URLs.
- /admin, /overlay, /api, /thanks and private media remain noindex/disallowed as intended.
- Verify title, description, robots, canonical, Open Graph and JSON-LD with rendered HTML.
- Verify favicon and OG image over HTTPS.
- Check Google Search Console, Bing Webmaster Tools and Yandex Webmaster separately; a submitted URL or successful live test is not proof of indexing.
- Check sitemap processing status and index coverage/errors; retain screenshots/dated notes.
- Publish accurate contact details, payment instructions, refund/cancellation rules and privacy policy before linking them in the footer or adding legal claims.
- Privacy policy must accurately describe data stored (donor name, message, payment identifiers, IP/security logs, media) and retention; do not claim card data is stored by this app.
- Ensure the operator's legal status and jurisdiction-specific consumer/payment requirements are reviewed before publishing refund terms.

## Release gate
- Backup completed and archive passed integrity/checksum verification.
- Restore dry-run passed; isolated restore passed on a non-production target.
- Ruff, unit tests and browser tests pass.
- Payment matrix relevant to changed code passes in provider sandbox.
- Deploy job succeeds and public homepage, health endpoint and SEO endpoints pass.
- No production database/schema migration without a pre-change backup and rollback plan.

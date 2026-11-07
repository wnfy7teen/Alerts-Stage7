import os
import pathlib
import sys
import tempfile

_tmp = pathlib.Path(tempfile.mkdtemp(prefix="donation-tests-"))
os.environ.update({
    "ADMIN_PASSWORD": "pw-test-1234567", "OVERLAY_KEY": "overlay-key-test-0123456789",
    "DB_PATH": str(_tmp / "test.db"), "CONFIG_PATH": str(_tmp / "config.json"), "MEDIA_DIR": str(_tmp / "media"),
    "PUBLIC_URL": "https://example.test",
    "YOOKASSA_SHOP_ID": "shop1", "YOOKASSA_SECRET_KEY": "yk-secret",
    "NOWPAYMENTS_API_KEY": "np-key", "NOWPAYMENTS_IPN_SECRET": "np-ipn-secret",
    "CRYPTO_PAY_TOKEN": "cp-token", "TESTNET": "1", "TRUST_PROXY_HEADERS": "0",
})
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


import httpx  # noqa: E402
import pytest  # noqa: E402
from contextlib import closing  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from helpers import Fake  # noqa: E402


@pytest.fixture()
def fake(monkeypatch):
    import payments as pay
    import server

    f = Fake()
    monkeypatch.setattr(pay, "HTTP_TRANSPORT", httpx.MockTransport(f.handler))
    # The fake broadcaster represents an available overlay in payment tests.
    monkeypatch.setattr(server, "clients", {object()})

    async def capture(alert):
        f.alerts.append(alert)
        return True

    monkeypatch.setattr(server, "broadcast", capture)
    monkeypatch.setattr(server, "donate_limiter", pay.RateLimiter(5, 600))
    return f


@pytest.fixture()
def client(fake):
    import payments as pay
    import server

    with TestClient(server.app) as c:
        server.load_cfg().tts_enabled = False  # без обращения к серверам озвучки
        with closing(pay.db()) as db:
            db.execute("DELETE FROM alert_outbox")
            db.execute("DELETE FROM refunds")
            db.execute("DELETE FROM donations")
            db.commit()
        yield c

"""E2E-тесты в настоящем браузере (Playwright). Сервер запускается отдельным процессом на свободном порту.
Платёжные провайдеры не вызываются: ответы API оплаты подменяются на уровне браузера (page.route)."""
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
ADMIN_PASSWORD = "e2e-admin-password-123"
OVERLAY_KEY = "e2e-overlay-key-0123456789"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def base_url():
    port, tmp = _free_port(), pathlib.Path(tempfile.mkdtemp(prefix="donation-e2e-"))
    env = {
        **os.environ,
        "ADMIN_PASSWORD": ADMIN_PASSWORD, "OVERLAY_KEY": OVERLAY_KEY, "PORT": str(port), "HOST": "127.0.0.1",
        "DB_PATH": str(tmp / "e2e.db"), "CONFIG_PATH": str(tmp / "config.json"), "MEDIA_DIR": str(tmp / "media"),
        "PUBLIC_URL": f"http://127.0.0.1:{port}",
        # фиктивные ключи нужны только затем, чтобы способы оплаты появились на странице
        "YOOKASSA_SHOP_ID": "e2e", "YOOKASSA_SECRET_KEY": "e2e", "NOWPAYMENTS_API_KEY": "e2e", "CRYPTO_PAY_TOKEN": "e2e",
    }
    proc = subprocess.Popen([sys.executable, "server.py"], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{port}"
    for _ in range(60):
        try:
            urllib.request.urlopen(url + "/api/health", timeout=1)
            break
        except OSError:
            if proc.poll() is not None:
                raise RuntimeError("Сервер не запустился:\n" + (proc.stdout.read().decode() if proc.stdout else ""))
            time.sleep(0.5)
    else:
        proc.kill()
        raise RuntimeError("Сервер не ответил на /api/health за 30 секунд")
    yield url
    proc.terminate()
    proc.wait(timeout=10)


@pytest.fixture(scope="session")
def admin_api(playwright, base_url):
    """HTTP-клиент с паролем админки и заголовком защиты от CSRF."""
    ctx = playwright.request.new_context(
        base_url=base_url, http_credentials={"username": "admin", "password": ADMIN_PASSWORD},
        extra_http_headers={"X-Requested-With": "donation-admin"},
    )
    cfg = ctx.get("/api/admin/config").json()
    cfg["tts_enabled"] = False  # в тестах без озвучки: не ходим на серверы синтеза
    assert ctx.put("/api/admin/config", data=cfg).ok
    yield ctx
    ctx.dispose()

#!/usr/bin/env python3
"""Submit the canonical public homepage to IndexNow after a meaningful public update."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
KEY_FILE = ROOT / "static" / "5ea344e2aa92cfda21c202a64fd75638.txt"
HOST = "vanesso4ka.site"
URL = f"https://{HOST}/"
ENDPOINT = "https://api.indexnow.org/indexnow"


def main() -> int:
    key = KEY_FILE.read_text(encoding="utf-8").strip()
    if key != KEY_FILE.stem:
        raise SystemExit("IndexNow key file name/content mismatch")

    payload = json.dumps({
        "host": HOST,
        "key": key,
        "keyLocation": f"https://{HOST}/{key}.txt",
        "urlList": [URL],
    }).encode("utf-8")
    try:
        response = httpx.post(
            ENDPOINT,
            json={
                "host": HOST,
                "key": key,
                "keyLocation": f"https://{HOST}/{key}.txt",
                "urlList": [URL],
            },
            timeout=15.0,
        )
        if response.status_code not in (200, 202):
            print(
                f"IndexNow rejected submission: HTTP {response.status_code}",
                file=sys.stderr,
            )
            return 1
        print(f"IndexNow accepted submission: HTTP {response.status_code}")
        return 0
    except httpx.HTTPError as error:
        print(f"IndexNow request failed: {type(error).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

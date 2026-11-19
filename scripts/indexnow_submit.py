#!/usr/bin/env python3
"""Submit the canonical public homepage to IndexNow after a meaningful public update."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

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
    request = Request(
        ENDPOINT,
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=15) as response:
            status = response.status
            print(f"IndexNow accepted submission: HTTP {status}")
            return 0 if status in (200, 202) else 1
    except HTTPError as error:
        print(f"IndexNow rejected submission: HTTP {error.code}", file=sys.stderr)
        return 1
    except (TimeoutError, URLError, OSError) as error:
        print(f"IndexNow request failed: {type(error).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

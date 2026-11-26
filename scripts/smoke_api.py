"""Non-generating API smoke test: health, models, credits, and auth.

  DREAMINA_API_KEY=... uv run scripts/smoke_api.py [--base-url http://127.0.0.1:8000]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dreamina.envstore import read_env_file  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    key = os.environ.get("DREAMINA_API_KEY") or read_env_file(root / ".env").get("DREAMINA_API_KEY")
    if not key:
        print("DREAMINA_API_KEY is not set", file=sys.stderr)
        return 2
    failures = 0
    with httpx.Client(base_url=args.base_url, timeout=30) as client:
        def check(label: str, response: httpx.Response, expected: int) -> httpx.Response:
            nonlocal failures
            ok = response.status_code == expected
            failures += not ok
            print(f"{'ok  ' if ok else 'FAIL'} {label}: HTTP {response.status_code}")
            return response

        check("health", client.get("/health"), 200)
        check("models without key is rejected", client.get("/v1/models"), 401)
        headers = {"Authorization": f"Bearer {key}"}
        models = check("models", client.get("/v1/models", headers=headers), 200).json()
        for model in models.get("models", []):
            print(f"     {model['name']}: {model['pricing_credits_per_second']} ({models.get('pricing_source')})")
        credits = check("credits", client.get("/v1/credits", headers=headers), 200).json()
        print(f"     total available: {credits.get('total_available')}")
        for account in credits.get("accounts", []):
            print(f"     {account['id']}: {account['state']} {account['credits']}")
        check("generations list", client.get("/v1/generations?limit=1", headers=headers), 200)
        check(
            "generation without Idempotency-Key is rejected",
            client.post("/v1/generations", headers=headers, data={"model": "seedance-2.5-flash"}),
            400,
        )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

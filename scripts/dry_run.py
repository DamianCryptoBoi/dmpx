"""Drive the full generation UI flow for one account WITHOUT submitting.

Uploads references, selects mode/model/settings, enters the prompt with
mention chips, and checks that submit is enabled. Spends no credits. A
screenshot is written to <data dir>/dry-run/prepared.png.

  uv run scripts/dry_run.py --account default --model seedance-2.5-flash --prompt "@image1 waves" --image a.png --audio b.mp3
  uv run scripts/dry_run.py --account default --model seedance-2.0-mini --resolution 720p --first-frame a.png --last-frame b.png
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dreamina.browser import BrowserError, BrowserManager  # noqa: E402
from dreamina.catalog import validate_generation  # noqa: E402
from dreamina.config import Settings  # noqa: E402
from dreamina.db import Database  # noqa: E402
from dreamina.envstore import EnvStore  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--account", required=True)
    parser.add_argument("--model", default="seedance-2.5-flash")
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--duration", type=int, default=4)
    parser.add_argument("--resolution", default="480p")
    parser.add_argument("--aspect-ratio", default="16:9")
    parser.add_argument("--image", action="append", default=[])
    parser.add_argument("--audio", action="append", default=[])
    parser.add_argument("--first-frame")
    parser.add_argument("--last-frame")
    args = parser.parse_args()

    spec = validate_generation(
        model_name=args.model,
        prompt=args.prompt,
        duration_seconds=args.duration,
        resolution=args.resolution,
        aspect_ratio=args.aspect_ratio,
        images=[str(Path(p).resolve()) for p in args.image],
        audio=[str(Path(p).resolve()) for p in args.audio],
        first_frame=str(Path(args.first_frame).resolve()) if args.first_frame else None,
        last_frame=str(Path(args.last_frame).resolve()) if args.last_frame else None,
    )
    settings = Settings.from_env()
    settings.ensure_dirs()
    db = Database(settings.db_path)
    account = db.get_account(args.account)
    if account is None:
        print(f"unknown account {args.account}", file=sys.stderr)
        return 2
    browsers = BrowserManager(settings, EnvStore(settings.env_path))
    try:
        outcome = await browsers.generate(account["id"], account["profile_id"], spec, "dry-run", dry_run=True)
        print(f"prepared {spec.model.name} ({spec.mode}); submit enabled; not submitted")
        print("\n".join(outcome.warnings))
        return 0
    except BrowserError as exc:
        print(f"dry run failed: {exc.code}: {exc.message}", file=sys.stderr)
        if exc.screenshot:
            print(f"screenshot: {exc.screenshot}", file=sys.stderr)
        return 1
    finally:
        await browsers.close_all()
        db.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

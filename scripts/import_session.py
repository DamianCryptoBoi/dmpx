"""Attach an existing, already logged-in ShardX profile to an account.

Copies the profile folder into the service's profiles directory and saves the
exported cookies, so the account starts `ready` without a fresh login.

  uv run scripts/import_session.py --account default --profile-dir .shardx/profiles/<id> --cookies .auth/dreamina-cookies.json

Stop any browser that is using the profile before copying it.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dreamina.accounts import AccountService  # noqa: E402
from dreamina.config import Settings  # noqa: E402
from dreamina.db import Database  # noqa: E402
from dreamina.envstore import EnvStore  # noqa: E402
from dreamina.http_client import DEFAULT_UA, load_cookie_file, save_cookie_file  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--account", required=True)
    parser.add_argument("--profile-dir", required=True, type=Path)
    parser.add_argument("--cookies", required=True, type=Path)
    args = parser.parse_args()

    settings = Settings.from_env()
    settings.ensure_dirs()
    env = EnvStore(settings.env_path)
    db = Database(settings.db_path)
    accounts = AccountService(settings, db, env, browsers=None)  # type: ignore[arg-type]
    accounts.sync_from_env()
    account = accounts.get(args.account)

    source = args.profile_dir.resolve()
    if not (source / "profile.json").exists():
        print(f"{source} is not a ShardX profile folder", file=sys.stderr)
        return 2
    profile_id = source.name
    target = settings.profiles_dir / profile_id
    if source != target.resolve():
        if target.exists():
            print(f"{target} already exists; refusing to overwrite", file=sys.stderr)
            return 2
        shutil.copytree(source, target, symlinks=True)

    cookies, user_agent = load_cookie_file(args.cookies)
    cookie_path = settings.data_dir / "accounts" / account["id"] / "cookies.json"
    save_cookie_file(cookie_path, cookies, user_agent or DEFAULT_UA)
    db.update_account(account["id"], profile_id=profile_id, cookies_path=str(cookie_path), user_agent=user_agent)
    account = await accounts.refresh_credits(account["id"], renew=False)
    print(f"{account['id']}: {account['session_state']}, {account['credits']} credits, profile {profile_id}")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

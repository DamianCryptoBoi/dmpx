"""Create admin/API secrets.

  uv run scripts/setup_admin.py              prompt for an admin password, print ADMIN_PASSWORD_HASH
  uv run scripts/setup_admin.py --write      same, and store it (plus any missing API key and
                              session secret) in the env file
  uv run scripts/setup_admin.py --check-env  validate configuration without changing anything
"""

from __future__ import annotations

import argparse
import getpass
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dreamina.config import Settings  # noqa: E402
from dreamina.envstore import EnvStore  # noqa: E402
from dreamina.security import hash_password  # noqa: E402


def check_env() -> int:
    settings = Settings.from_env()
    env = EnvStore(settings.env_path)
    print(f"env file:     {settings.env_path}")
    print(f"data dir:     {settings.data_dir}")
    print(f"accounts:     {', '.join(env.account_ids()) or 'none'}")
    for account_id in env.account_ids():
        email, password = env.account_credentials(account_id)
        if not password:
            print(f"  {account_id}: password missing")
    mode = settings.env_path.stat().st_mode & 0o777 if settings.env_path.exists() else None
    if mode is not None and mode & 0o077:
        print(f"warning: {settings.env_path} is mode {oct(mode)}; run chmod 600")
    for warning in settings.warnings:
        print(f"warning: {warning}")
    return 1 if settings.warnings else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--write", action="store_true", help="store the hash and missing secrets in the env file")
    parser.add_argument("--check-env", action="store_true", help="validate configuration only")
    args = parser.parse_args()
    if args.check_env:
        return check_env()

    password = getpass.getpass("Admin password: ")
    if len(password) < 12:
        print("Use at least 12 characters.", file=sys.stderr)
        return 2
    if getpass.getpass("Repeat password: ") != password:
        print("Passwords do not match.", file=sys.stderr)
        return 2
    encoded = hash_password(password)
    if not args.write:
        print(f"ADMIN_PASSWORD_HASH={encoded}")
        return 0

    settings = Settings.from_env()
    env = EnvStore(settings.env_path)
    values = {"ADMIN_PASSWORD_HASH": encoded}
    if not env.get("DREAMINA_SESSION_SECRET"):
        values["DREAMINA_SESSION_SECRET"] = secrets.token_urlsafe(48)
    if not env.get("DREAMINA_API_KEY"):
        values["DREAMINA_API_KEY"] = secrets.token_urlsafe(32)
        print(f"New API key for your Worker: {values['DREAMINA_API_KEY']}")
    env.set_many(values)
    print(f"Updated {', '.join(values)} in {settings.env_path}. Restart the service to apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

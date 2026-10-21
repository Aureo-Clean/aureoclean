#!/usr/bin/env python3
"""Create the first AureoClean administrator, or link an existing account.

Run this on the Raspberry Pi itself (it uses the service-account key, so only
someone with access to the Pi can run it):

    .venv/bin/python tools/create_admin.py                  # new admin account
    .venv/bin/python tools/create_admin.py --link-existing  # existing Firebase account

--link-existing is for an email that already has a Firebase Authentication
account (for example one created during earlier testing). It only adds the
AureoClean admin profile; the existing account and its password are not
changed or deleted.
"""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.config import load_settings  # noqa: E402
from backend.factory import build_service  # noqa: E402
from backend.providers.base import ProviderError  # noqa: E402
from backend.service import ServiceError  # noqa: E402


def ask(prompt: str, given: str | None) -> str:
    if given:
        return given
    value = ""
    while not value:
        value = input(prompt).strip()
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--link-existing", action="store_true", help="link an existing sign-in account instead of creating one")
    parser.add_argument("--email")
    parser.add_argument("--username")
    parser.add_argument("--full-name")
    args = parser.parse_args()

    settings = load_settings()
    print(f"AureoClean admin setup ({settings.mode} mode", end="")
    print(f", Firebase project {settings.firebase_project_id})" if settings.mode == "firebase" else f", data in {settings.data_dir})")

    try:
        service = build_service(settings)
    except (ProviderError, OSError, ValueError) as exc:
        print(f"Could not connect: {exc}")
        return 1

    email = ask("Email: ", args.email)
    username = ask("Username (3-24 letters, numbers, dots, underscores): ", args.username)
    full_name = ask("Full name: ", args.full_name)

    password = None
    if not args.link_existing:
        while True:
            password = getpass.getpass("Password (8+ chars, letter and number): ")
            if password == getpass.getpass("Confirm password: "):
                break
            print("Passwords do not match, try again.")

    try:
        profile = service.bootstrap_admin(full_name, username, email, password, link_existing=args.link_existing)
    except ServiceError as exc:
        print(f"Not done: {exc.message}")
        return 1
    except ProviderError as exc:
        print(f"Provider error: {exc}")
        return 1

    how = "linked (existing password unchanged)" if args.link_existing else "created"
    print(f"Admin '{profile['username']}' {how}. You can now sign in to the AureoClean app.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

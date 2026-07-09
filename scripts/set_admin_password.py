"""
Set or rotate the single admin login password.

Usage:
    python scripts/set_admin_password.py

Prompts for a new password (input hidden), hashes it with bcrypt, and
writes ADMIN_PASSWORD_HASH into .env in the project root. Restart the
backend afterwards for the new password to take effect.
"""

import getpass
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bcrypt

ENV_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")


def main():
    password = getpass.getpass("New admin password: ")
    if len(password) < 12:
        print("Password must be at least 12 characters.", file=sys.stderr)
        sys.exit(1)
    confirm = getpass.getpass("Confirm: ")
    if password != confirm:
        print("Passwords did not match.", file=sys.stderr)
        sys.exit(1)

    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

    if not os.path.exists(ENV_PATH):
        print(f".env not found at {ENV_PATH}", file=sys.stderr)
        sys.exit(1)

    with open(ENV_PATH, "r") as f:
        lines = f.readlines()

    pattern = re.compile(r"^ADMIN_PASSWORD_HASH=")
    replaced = False
    for i, line in enumerate(lines):
        if pattern.match(line):
            lines[i] = f"ADMIN_PASSWORD_HASH={password_hash}\n"
            replaced = True
            break

    if not replaced:
        lines.append(f"ADMIN_PASSWORD_HASH={password_hash}\n")

    with open(ENV_PATH, "w") as f:
        f.writelines(lines)

    print("Updated ADMIN_PASSWORD_HASH in .env. Restart the backend to apply it.")


if __name__ == "__main__":
    main()

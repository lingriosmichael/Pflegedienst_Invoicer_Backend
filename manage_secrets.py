#!/usr/bin/env python
"""
Entry point for secrets management CLI.
Run: python manage_secrets.py set
"""
from app.cli.secrets import set_openai_key_interactive, delete_openai_key_from_keyring
import sys

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "set":
        set_openai_key_interactive()
    elif len(sys.argv) > 1 and sys.argv[1] == "delete":
        delete_openai_key_from_keyring()
    else:
        print("Usage: python manage_secrets.py <set|delete>")
        print("  set    - Interactively set your OpenAI API key")
        print("  delete - Delete key from OS keyring")

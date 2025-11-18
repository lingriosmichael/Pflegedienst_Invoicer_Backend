"""
Interactive CLI tool to manage secrets (OpenAI API key) securely.
Stores keys in OS keyring (Keychain on macOS, Credential Manager on Windows, etc.)
Falls back to .env if keyring is unavailable.
"""
import logging
import getpass

logger = logging.getLogger(__name__)

try:
    import keyring
    KEYRING_AVAILABLE = True
except ImportError:
    KEYRING_AVAILABLE = False
    logger.warning("keyring module not available. Will use .env file for secrets. Install with: pip install keyring")

SERVICE_NAME = "pflegedienst-invoicer"
KEY_NAME = "openai_api_key"


def set_openai_key_interactive():
    """
    Prompt user for OpenAI API key and store it securely.
    """
    print("\n" + "=" * 60)
    print("🔐 OpenAI API Key Setup")
    print("=" * 60)
    
    if not KEYRING_AVAILABLE:
        print("\n⚠️  keyring module not installed. Installing it is recommended for secure key storage.")
        print("    Install with: pip install keyring")
        use_keyring = input("\nContinue without keyring (store in .env)? (y/n): ").strip().lower()
        if use_keyring != 'y':
            print("Exiting.")
            return False
    
    print("\n📝 Paste your OpenAI API key below.")
    print("   You can get it from: https://platform.openai.com/api-keys")
    print("   (The key will not be echoed to the terminal for security)")
    
    key = getpass.getpass("OpenAI API Key (sk-...): ").strip()
    
    if not key:
        print("❌ Key cannot be empty.")
        return False
    
    if not key.startswith("sk-"):
        confirm = input("⚠️  Key does not start with 'sk-'. Continue? (y/n): ").strip().lower()
        if confirm != 'y':
            print("Aborted.")
            return False
    
    # Try keyring first, fallback to .env
    stored_in_keyring = False
    if KEYRING_AVAILABLE:
        try:
            keyring.set_password(SERVICE_NAME, KEY_NAME, key)
            stored_in_keyring = True
            logger.info(f"✅ Key stored in OS keyring ({SERVICE_NAME}).")
            print("✅ Key stored in OS keyring.")
        except Exception as e:
            logger.warning(f"Could not store in keyring: {e}. Will use .env file.")
            print(f"⚠️  Could not store in keyring: {e}")
    
    if not stored_in_keyring:
        # Store in .env file
        try:
            with open(".env", "r") as f:
                env_content = f.read()
        except FileNotFoundError:
            env_content = ""
        
        # Remove old key if present
        lines = [line for line in env_content.split("\n") if not line.startswith("OPENAI_API_KEY=")]
        lines.append(f"OPENAI_API_KEY={key}")
        
        with open(".env", "w") as f:
            f.write("\n".join(lines))
        
        logger.info("✅ Key stored in .env file.")
        print("✅ Key stored in .env file.")
    
    print("\n✅ Setup complete! Restart the app or set OPENAI_API_KEY in your environment.")
    return True


def get_openai_key_from_keyring():
    """
    Retrieve OpenAI key from keyring (if available).
    Returns None if not found or keyring unavailable.
    """
    if not KEYRING_AVAILABLE:
        return None
    
    try:
        return keyring.get_password(SERVICE_NAME, KEY_NAME)
    except Exception as e:
        logger.debug(f"Could not retrieve from keyring: {e}")
        return None


def delete_openai_key_from_keyring():
    """
    Delete OpenAI key from keyring.
    """
    if not KEYRING_AVAILABLE:
        print("❌ keyring module not available.")
        return False
    
    try:
        keyring.delete_password(SERVICE_NAME, KEY_NAME)
        print("✅ Key deleted from OS keyring.")
        return True
    except Exception as e:
        logger.error(f"Could not delete from keyring: {e}")
        print(f"❌ Error: {e}")
        return False


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "set":
            set_openai_key_interactive()
        elif cmd == "delete":
            delete_openai_key_from_keyring()
        elif cmd == "show":
            if KEYRING_AVAILABLE:
                key = get_openai_key_from_keyring()
                if key:
                    print(f"Key found in keyring: {key[:10]}...{key[-5:]}")
                else:
                    print("No key found in keyring.")
            else:
                print("keyring not available")
        else:
            print(f"Unknown command: {cmd}")
            print("Usage: python -m app.cli.secrets <set|delete|show>")
    else:
        print("Usage: python -m app.cli.secrets <set|delete|show>")

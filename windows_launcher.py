"""
Windows Launcher for Pflegedienst Invoicer.
This script starts the FastAPI backend and opens a browser window.
Handles initial setup (API key prompt) and logging.
"""
import os
import sys
import subprocess
import time
import logging
import webbrowser
from pathlib import Path

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('invoicer_launcher.log'),
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger(__name__)


def get_app_data_dir():
    """
    Get the application data directory on Windows.
    Typically: C:\Users\<username>\AppData\Local\Pflegedienst Invoicer
    """
    if 'APPDATA' in os.environ:
        app_data = Path(os.environ['APPDATA'])
    elif 'LOCALAPPDATA' in os.environ:
        app_data = Path(os.environ['LOCALAPPDATA'])
    else:
        app_data = Path.home() / 'AppData' / 'Local'
    
    app_dir = app_data / 'Pflegedienst Invoicer'
    app_dir.mkdir(parents=True, exist_ok=True)
    return app_dir


def setup_env_file():
    """
    Create .env file if it doesn't exist and prompt for API key.
    """
    app_dir = get_app_data_dir()
    env_file = app_dir / '.env'
    
    if not env_file.exists():
        logger.info("First run detected. Setting up configuration...")
        print("\n" + "="*60)
        print("🔐 Welcome to Pflegedienst Invoicer!")
        print("="*60)
        print("\nThis is your first run. You need to set up your OpenAI API key.")
        print("\n📝 Steps:")
        print("  1. Go to https://platform.openai.com/api-keys")
        print("  2. Create a new API key (or use existing one)")
        print("  3. Copy the key (starts with 'sk-')")
        print("  4. Paste it below (it won't be echoed to screen)")
        print("\n" + "="*60 + "\n")
        
        # Use getpass for secure input on Windows
        import getpass
        api_key = getpass.getpass("Paste your OpenAI API Key: ").strip()
        
        if not api_key:
            print("❌ API key cannot be empty.")
            input("Press Enter to exit...")
            sys.exit(1)
        
        if not api_key.startswith('sk-'):
            print("\n⚠️  Warning: Key should start with 'sk-'")
            confirm = input("Continue anyway? (y/n): ").strip().lower()
            if confirm != 'y':
                sys.exit(1)
        
        # Write .env file
        try:
            with open(env_file, 'w') as f:
                f.write(f"OPENAI_API_KEY={api_key}\n")
                f.write("DB_PATH=data/invoices.db\n")
                f.write("LOG_LEVEL=INFO\n")
            
            os.chmod(env_file, 0o600)  # Restrict permissions
            logger.info(f"✅ Configuration saved to {env_file}")
            print(f"\n✅ API key saved securely!")
            print(f"   Location: {env_file}")
        except Exception as e:
            logger.error(f"Failed to save .env file: {e}")
            print(f"\n❌ Error saving configuration: {e}")
            input("Press Enter to exit...")
            sys.exit(1)
    else:
        logger.info(f"Configuration found at {env_file}")


def start_backend():
    """
    Start the FastAPI backend server.
    Returns the process handle.
    """
    logger.info("Starting FastAPI backend...")
    
    try:
        # Start uvicorn in a subprocess
        proc = subprocess.Popen(
            [sys.executable, '-m', 'uvicorn', 'backend:app', '--host', '127.0.0.1', '--port', '8000'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        logger.info(f"Backend started with PID {proc.pid}")
        return proc
    except Exception as e:
        logger.error(f"Failed to start backend: {e}")
        print(f"\n❌ Error starting backend: {e}")
        return None


def wait_for_server(timeout=30):
    """
    Wait for the server to be ready.
    """
    import socket
    
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect(('127.0.0.1', 8000))
            sock.close()
            logger.info("✅ Server is ready!")
            return True
        except (socket.error, ConnectionRefusedError):
            time.sleep(0.5)
    
    logger.error(f"Server did not start within {timeout} seconds")
    return False


def open_browser():
    """
    Open the default browser to the app URL.
    """
    url = 'http://127.0.0.1:8000'
    logger.info(f"Opening browser to {url}")
    
    try:
        webbrowser.open(url)
    except Exception as e:
        logger.warning(f"Could not open browser: {e}")
        print(f"\n⚠️  Could not open browser automatically.")
        print(f"   Open manually: {url}")


def main():
    """
    Main launcher logic.
    """
    print("\n" + "="*60)
    print("🚀 Pflegedienst Invoicer")
    print("="*60 + "\n")
    
    # Set up environment (prompt for API key if needed)
    setup_env_file()
    
    # Start the backend
    backend_proc = start_backend()
    if not backend_proc:
        print("\n❌ Failed to start the backend. Check the logs.")
        input("Press Enter to exit...")
        sys.exit(1)
    
    # Wait for server to be ready
    print("\n⏳ Waiting for server to start...")
    if not wait_for_server():
        print("\n❌ Server failed to start. Check the logs for details.")
        input("Press Enter to exit...")
        backend_proc.terminate()
        sys.exit(1)
    
    print("\n✅ Server started successfully!")
    print("   API running at: http://127.0.0.1:8000")
    
    # Open browser
    time.sleep(1)
    open_browser()
    
    print("\n" + "="*60)
    print("📊 Application is running!")
    print("="*60)
    print("\nTo access the app:")
    print("  - Web API: http://127.0.0.1:8000")
    print("  - Health check: http://127.0.0.1:8000/")
    print("\nTo stop the application:")
    print("  - Close this window or press Ctrl+C")
    print("\nLogs saved to: invoicer_launcher.log")
    print("="*60 + "\n")
    
    # Keep the process alive
    try:
        backend_proc.wait()
    except KeyboardInterrupt:
        logger.info("User requested shutdown...")
        print("\n\n🛑 Shutting down...")
        backend_proc.terminate()
        backend_proc.wait(timeout=5)
        logger.info("Backend stopped.")
        print("✅ Shutdown complete. Goodbye!")


if __name__ == '__main__':
    main()

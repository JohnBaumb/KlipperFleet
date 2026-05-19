"""
One-command screenshot generation for KlipperFleet.

Usage:
    python screenshots/run.py           # Generate all screenshots
    python screenshots/run.py --install  # Install dependencies first
"""
import subprocess
import sys
from pathlib import Path

SCREENSHOTS_DIR = Path(__file__).parent
REQUIREMENTS = SCREENSHOTS_DIR / "requirements.txt"


def install_deps():
    print("Installing dependencies...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", str(REQUIREMENTS)])
    print("Installing Playwright browsers...")
    subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium"])


def main():
    if "--install" in sys.argv:
        install_deps()

    # Verify playwright is available
    try:
        import playwright  # noqa: F401
    except ImportError:
        print("Playwright not installed. Run with --install first:")
        print(f"  python {Path(__file__).name} --install")
        sys.exit(1)

    # Verify httpx is available (used by wait_for_server)
    try:
        import httpx  # noqa: F401
    except ImportError:
        print("httpx not installed. Installing...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "httpx"])

    print("Generating screenshots...")
    subprocess.check_call([sys.executable, str(SCREENSHOTS_DIR / "take_screenshots.py")])


if __name__ == "__main__":
    main()

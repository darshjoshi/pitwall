"""
F1 TV Authentication Setup.

Two modes:
  1. Automatic (via FastF1's browser flow)
  2. Manual (paste your subscription token directly)

Run: python3 auth_setup.py
"""

import json
import sys
from datetime import datetime
from pathlib import Path

TOKEN_FILE = Path.home() / "Library" / "Application Support" / "fastf1" / "f1auth.json"
# Also save a copy in our project for standalone use
LOCAL_TOKEN_FILE = Path(__file__).parent / ".f1token"


def check_existing_token():
    """Check if a valid token already exists."""
    for path in [TOKEN_FILE, LOCAL_TOKEN_FILE]:
        if path.exists():
            token = path.read_text().strip()
            if token:
                try:
                    import jwt
                    decoded = jwt.decode(token, options={"verify_signature": False})
                    exp = datetime.fromtimestamp(decoded.get("exp", 0))
                    now = datetime.now()

                    print(f"Found token at: {path}")
                    print(f"  Subscription: {decoded.get('SubscriptionStatus', '?')}")
                    print(f"  Product:      {decoded.get('SubscribedProduct', '?')}")
                    print(f"  Expires:      {exp}")

                    if exp > now:
                        remaining = exp - now
                        print(f"  Status:       VALID ({remaining.days}d {remaining.seconds // 3600}h remaining)")
                        return token
                    else:
                        print(f"  Status:       EXPIRED ({(now - exp).days} days ago)")
                except Exception as e:
                    print(f"  Token parse error: {e}")
    return None


def save_token(token: str):
    """Save token to both FastF1's location and our local project."""
    # Save to FastF1's expected location
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(token)
    print(f"  Saved to: {TOKEN_FILE}")

    # Save a local copy for our standalone client
    LOCAL_TOKEN_FILE.write_text(token)
    print(f"  Saved to: {LOCAL_TOKEN_FILE}")
    print(f"  (Add .f1token to .gitignore!)")


def method_fastf1():
    """Use FastF1's built-in browser auth flow."""
    try:
        from fastf1.internals.f1auth import get_auth_token
        print("\nStarting FastF1 auth flow...")
        print("A URL will appear — open it in your browser.\n")
        token = get_auth_token()
        if token:
            save_token(token)
            return token
    except ImportError:
        print("FastF1 not installed. Install with: pip3 install fastf1")
    except Exception as e:
        print(f"Auth flow error: {e}")
    return None


def method_manual():
    """Paste the subscription token directly."""
    print("\n--- Manual Token Entry ---")
    print("To get your token:")
    print("  1. Open https://f1tv.formula1.com in Chrome")
    print("  2. Log in with your F1 TV Premium account")
    print("  3. Open DevTools (F12) → Application → Cookies → f1tv.formula1.com")
    print("  4. Find the 'loginSession' cookie")
    print("  5. URL-decode its value (paste into https://urldecoder.org)")
    print("  6. The decoded JSON has: data.subscriptionToken")
    print("  7. Copy that token value (starts with 'eyJ...')\n")

    token = input("Paste your subscriptionToken here: ").strip()

    if not token:
        print("No token provided.")
        return None

    if not token.startswith("eyJ"):
        print("Warning: Token doesn't look like a JWT (should start with 'eyJ').")
        confirm = input("Save anyway? (y/n): ").strip().lower()
        if confirm != "y":
            return None

    # Validate
    try:
        import jwt
        decoded = jwt.decode(token, options={"verify_signature": False})
        exp = datetime.fromtimestamp(decoded.get("exp", 0))
        print(f"\nToken decoded successfully:")
        print(f"  Subscription: {decoded.get('SubscriptionStatus', '?')}")
        print(f"  Product:      {decoded.get('SubscribedProduct', '?')}")
        print(f"  Expires:      {exp}")

        if exp < datetime.now():
            print("  WARNING: This token is expired!")
    except Exception as e:
        print(f"  Could not decode token: {e}")

    save_token(token)
    return token


def main():
    print("=" * 50)
    print("F1 TV Authentication Setup")
    print("=" * 50)

    # Check existing
    existing = check_existing_token()
    if existing:
        print("\nYou already have a valid token!")
        choice = input("Re-authenticate anyway? (y/n): ").strip().lower()
        if choice != "y":
            return

    print("\nChoose authentication method:")
    print("  1. Automatic (FastF1 browser flow — recommended)")
    print("  2. Manual (paste token from browser DevTools)")
    print()

    choice = input("Enter 1 or 2: ").strip()

    if choice == "1":
        token = method_fastf1()
    elif choice == "2":
        token = method_manual()
    else:
        print("Invalid choice.")
        return

    if token:
        print("\n" + "=" * 50)
        print("Authentication complete!")
        print("=" * 50)
        print("\nYou can now use the full client with auth:")
        print('  client = F1LiveClient(no_auth=False, auth_token="...")')
        print("\nOr load the token automatically:")
        print("  from auth_setup import load_token")
        print("  client = F1LiveClient(no_auth=False, auth_token=load_token())")


def load_token() -> str:
    """Load saved token from disk. Use this in your scripts."""
    for path in [LOCAL_TOKEN_FILE, TOKEN_FILE]:
        if path.exists():
            token = path.read_text().strip()
            if token:
                return token
    raise FileNotFoundError(
        "No F1 TV token found. Run: python3 auth_setup.py"
    )


if __name__ == "__main__":
    main()

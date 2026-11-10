"""Daily Zerodha login. Run once each trading day before fetching data.

    python scripts/kite_login.py

It prints a login link. Open it, log in on Zerodha's own page, and you are
redirected to your app's redirect URL with `request_token=...` in the
address bar. Paste that whole address back here. The access token is saved
to .kite_session.json (kept out of git) and expires early next morning.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from urllib.parse import parse_qs, urlparse

import _bootstrap


def main() -> None:
    try:
        from kiteconnect import KiteConnect
    except ImportError:
        sys.exit("kiteconnect is not installed. Run: pip install -r requirements.txt")

    _bootstrap.load_env()
    api_key = _bootstrap.require_env("KITE_API_KEY")
    api_secret = _bootstrap.require_env("KITE_API_SECRET")

    kite = KiteConnect(api_key=api_key)
    print("\n1. Open this link and log in:\n")
    print("   " + kite.login_url())
    pasted = input("\n2. Paste the address you were redirected to (or just the request_token):\n> ").strip()

    token = parse_qs(urlparse(pasted).query).get("request_token", [pasted])[0]
    session = kite.generate_session(token, api_secret=api_secret)
    _bootstrap.SESSION_FILE.write_text(json.dumps({
        "access_token": session["access_token"],
        "user_id": session.get("user_id"),
        "created": datetime.now().isoformat(timespec="seconds"),
    }))
    print(f"\nLogged in as {session.get('user_id')}. Session saved.")


if __name__ == "__main__":
    main()

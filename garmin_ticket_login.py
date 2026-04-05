#!/usr/bin/env python3
"""
Exchange a Garmin SSO Service Ticket (ST-...) for native-oauth2 session + Garth oauth2_token.

Manual step: log in via browser, copy the redirect URL or ticket string, then run this script immediately.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import webbrowser
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

from pirate_garmin.auth import (
    DI_CLIENT_IDS,
    GARTH_CLIENT_ID,
    GARTH_LOGIN_URL,
    AuthManager,
    NativeOAuth2Session,
    _it_client_id_candidates,
)

DEFAULT_SSO_URL = (
    "https://sso.garmin.com/mobile/sso/en_US/sign-in"
    "?clientId=GCM_ANDROID_DARK&service=https://mobile.integration.garmin.com/gcm/android"
)


def _utc_now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


ST_PATTERN = re.compile(r"ST-[A-Za-z0-9_-]+-sso")


def parse_service_ticket(raw: str) -> str:
    """Accept full redirect URL, or a bare ST-...-sso string."""
    s = raw.strip().strip('"').strip("'")
    if not s:
        raise ValueError("Empty ticket input")

    if s.startswith("http://") or s.startswith("https://"):
        parsed = urlparse(s)
        for key, store in (
            (parse_qs(parsed.query), "query"),
            (parse_qs(parsed.fragment), "fragment"),
        ):
            if "ticket" in key and key["ticket"]:
                return key["ticket"][0]
        m = ST_PATTERN.search(s)
        if m:
            return m.group(0)
        raise ValueError("No ticket= parameter found in URL")

    m = ST_PATTERN.search(s)
    if m:
        return m.group(0)
    if s.startswith("ST-"):
        return s
    raise ValueError("Could not parse Service Ticket; paste a URL with ticket= or ST-...-sso")


def migrate_pirate_token_to_garth(
    native_oauth2_path: str | Path,
    garth_dir: str | Path | None = None,
) -> None:
    """Copy DI OAuth2 token from pirate-garmin JSON into ~/.garth/oauth2_token.json."""
    path = Path(native_oauth2_path).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"Missing native OAuth2 file: {path}")

    with path.open() as f:
        pirate_data = json.load(f)

    oauth2_token = pirate_data["di"]["token"]
    gdir = Path(garth_dir or os.path.expanduser("~/.garth"))
    gdir.mkdir(parents=True, exist_ok=True)
    out = gdir / "oauth2_token.json"
    with out.open("w") as f:
        json.dump(oauth2_token, f, indent=4)
    print(f"✅ 长效通行证已植入 {out}")


def write_garth_compat(garth_dir: str | Path | None = None) -> None:
    """Dummy oauth1 + domain_profile for older garminconnect/garth checks."""
    gdir = Path(garth_dir or os.path.expanduser("~/.garth"))
    gdir.mkdir(parents=True, exist_ok=True)
    with (gdir / "oauth1_token.json").open("w") as f:
        json.dump({"oauth_token": "dummy", "oauth_token_secret": "dummy"}, f, indent=4)
    with (gdir / "domain_profile.json").open("w") as f:
        json.dump({}, f)
    print(f"✅ Wrote compatibility stubs under {gdir}")


def create_session_from_service_ticket(
    service_ticket: str,
    app_dir: str | Path | None,
) -> Path:
    auth = AuthManager(credentials=None, app_dir=Path(app_dir) if app_dir else None)
    with httpx.Client(follow_redirects=True, timeout=auth.timeout) as client:
        di_slot = auth.exchange_service_ticket_for_di_token(
            client, service_ticket, DI_CLIENT_IDS
        )
    it_slot = auth.exchange_di_token_for_it_token(
        di_slot.token.access_token,
        _it_client_id_candidates(di_slot.client_id),
    )
    session = NativeOAuth2Session(
        created_at=_utc_now_iso(),
        login_client_id=GARTH_CLIENT_ID,
        service_url=GARTH_LOGIN_URL,
        di=di_slot,
        it=it_slot,
    )
    auth.save_native_session(session)
    out = auth.native_oauth2_path
    print(f"✅ Saved pirate-garmin session to {out}")
    return Path(out)


def main() -> int:
    p = argparse.ArgumentParser(
        description="Exchange Garmin Service Ticket for pirate-garmin + Garth tokens."
    )
    p.add_argument("--ticket", help="Service ticket ST-...-sso")
    p.add_argument("--url", help="Full redirect URL containing ticket=")
    p.add_argument(
        "--app-dir",
        help="pirate-garmin data dir (default: ~/.local/share/pirate-garmin or PIRATE_GARMIN_APP_DIR)",
    )
    p.add_argument(
        "--garth-dir",
        default=os.path.expanduser("~/.garth"),
        help="Garth token directory (default: ~/.garth)",
    )
    p.add_argument(
        "--compat",
        action="store_true",
        help="Write dummy oauth1_token.json and domain_profile.json for older garminconnect",
    )
    p.add_argument(
        "--run-sync",
        action="store_true",
        help="Run garmin_sync.py from the repo root after success",
    )
    p.add_argument(
        "--open-browser",
        action="store_true",
        help=f"Open the mobile SSO page in a browser ({DEFAULT_SSO_URL[:48]}...)",
    )
    args = p.parse_args()

    if args.open_browser:
        webbrowser.open(DEFAULT_SSO_URL)

    raw = None
    if args.ticket:
        raw = args.ticket
    elif args.url:
        raw = args.url
    else:
        print(
            "请在浏览器登录后，从地址栏复制「重定向后的完整 URL」（含 ticket=…）。",
            "也可只粘贴 ST-…-sso；票据约 1 分钟内有效，请尽快。",
            "",
            sep="\n",
            flush=True,
        )
        raw = (
            sys.stdin.readline()
            if not sys.stdin.isatty()
            else input("粘贴重定向完整 URL: ")
        )
    raw = (raw or "").strip()

    try:
        st = parse_service_ticket(raw)
    except ValueError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1

    print(f"Using ticket: {st[:20]}...", flush=True)

    try:
        native_path = create_session_from_service_ticket(st, args.app_dir)
    except Exception as e:
        print(f"❌ Token exchange failed: {e}", file=sys.stderr)
        return 1

    try:
        migrate_pirate_token_to_garth(native_path, args.garth_dir)
    except Exception as e:
        print(f"❌ Migrate to Garth failed: {e}", file=sys.stderr)
        return 1

    if args.compat:
        write_garth_compat(args.garth_dir)

    if args.run_sync:
        root = Path(__file__).resolve().parent
        sync_script = root / "garmin_sync.py"
        if not sync_script.is_file():
            print(f"❌ garmin_sync.py not found at {sync_script}", file=sys.stderr)
            return 1
        print("⬇️ Running garmin_sync.py ...", flush=True)
        r = subprocess.run([sys.executable, str(sync_script)], cwd=root)
        return int(r.returncode)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

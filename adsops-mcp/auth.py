"""OAuth desktop flow -> refresh token -> google-ads.yaml.

    python auth.py                       # uses GOOGLE_ADS_CLIENT_ID/SECRET from .env (or --client-secrets file)
    python auth.py --client-secrets client_secret.json
    python auth.py --no-browser          # print the URL instead of opening a browser
    python auth.py --manual              # headless server: open URL on any device, paste the redirect URL back

Needs an OAuth client of type "Desktop app" in Google Cloud (APIs & Services -> Credentials),
and the Google Ads API enabled in that project. Keep the OAuth consent screen in "In production"
status, otherwise refresh tokens expire after 7 days.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
SCOPES = ["https://www.googleapis.com/auth/adwords"]


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--client-secrets", help="Path to the downloaded OAuth client JSON (Desktop app).")
    p.add_argument("--yaml", default=os.getenv("GOOGLE_ADS_YAML", str(ROOT / "google-ads.yaml")))
    p.add_argument("--port", type=int, default=0, help="Local redirect port (0 = random free port).")
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--manual", action="store_true",
                   help="No local listener: open the URL anywhere, then paste the URL your browser was redirected to.")
    args = p.parse_args()

    from google_auth_oauthlib.flow import InstalledAppFlow

    yaml_path = Path(args.yaml)
    existing = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) if yaml_path.exists() else {}
    existing = existing or {}

    if args.client_secrets:
        flow = InstalledAppFlow.from_client_secrets_file(args.client_secrets, scopes=SCOPES)
        client_id = flow.client_config["client_id"]
        client_secret = flow.client_config["client_secret"]
    else:
        client_id = os.getenv("GOOGLE_ADS_CLIENT_ID") or existing.get("client_id")
        client_secret = os.getenv("GOOGLE_ADS_CLIENT_SECRET") or existing.get("client_secret")
        if not client_id or not client_secret:
            sys.exit("Set GOOGLE_ADS_CLIENT_ID / GOOGLE_ADS_CLIENT_SECRET in .env or pass --client-secrets.")
        flow = InstalledAppFlow.from_client_config(
            {"installed": {"client_id": client_id, "client_secret": client_secret,
                           "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                           "token_uri": "https://oauth2.googleapis.com/token",
                           "redirect_uris": ["http://localhost"]}},
            scopes=SCOPES,
        )

    bad = [n for n, v in (("client_id", client_id), ("client_secret", client_secret)) if not v or "..." in v or "INSERT" in v]
    if bad:
        sys.exit(f"{', '.join(bad)} still contain placeholder values — put the real values in .env.")

    if args.manual:
        os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")  # loopback redirect is http by design
        os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
        flow.redirect_uri = "http://localhost:1/"
        url, _ = flow.authorization_url(access_type="offline", prompt="consent")
        print("\n1) Open this URL in any browser and approve access:\n\n" + url + "\n")
        print("2) The browser then shows an error page on http://localhost:1/... — that is expected.")
        print("   Copy the FULL address from the address bar and paste it here.\n")
        redirect = input("Redirect URL: ").strip()
        if "code=" not in redirect:
            sys.exit("That URL has no ?code= parameter — copy the whole address bar after approving.")
        flow.fetch_token(authorization_response=redirect)
        creds = flow.credentials
    else:
        creds = flow.run_local_server(port=args.port, open_browser=not args.no_browser,
                                      access_type="offline", prompt="consent",
                                      authorization_prompt_message="Open this URL to authorize adsops-mcp:\n{url}\n")
    if not creds.refresh_token:
        sys.exit("No refresh token returned. Revoke the app at https://myaccount.google.com/permissions and retry.")

    cfg = {
        "developer_token": os.getenv("GOOGLE_ADS_DEVELOPER_TOKEN") or existing.get("developer_token") or "INSERT_DEVELOPER_TOKEN",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": creds.refresh_token,
        "login_customer_id": str(os.getenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID") or existing.get("login_customer_id") or "INSERT_MCC_ID").replace("-", ""),
        "use_proto_plus": True,
    }
    yaml_path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    try:
        os.chmod(yaml_path, 0o600)
    except OSError:
        pass
    print(f"Wrote refresh token to {yaml_path}")
    if "INSERT" in cfg["developer_token"] or "INSERT" in cfg["login_customer_id"]:
        print("Now fill developer_token and login_customer_id (your MCC ID, digits only) in that file.")


if __name__ == "__main__":
    main()

"""One-time OAuth authorization for meshy-youtube-mcp.

Run this ONCE **on a machine that has a web browser** (e.g. your laptop):

    python -m meshy_youtube.authorize

It reads your OAuth client (``client_secret.json``), opens Google's consent
screen, and writes a reusable token file. After that, the MCP server uploads
unattended using the stored refresh token — you never authorize again unless
the token is revoked or deleted.

Headless server? The OAuth callback is a localhost redirect, so it must
complete on the same host that opened the browser. Authorize on your laptop,
then copy the resulting ``token.json`` to the server's
``YOUTUBE_TOKEN_FILE`` path. (Or SSH local-forward the callback port.)

Set custom paths with YOUTUBE_CLIENT_SECRET_FILE / YOUTUBE_TOKEN_FILE; otherwise
both default to ~/.config/meshy-youtube-mcp/.
"""
from __future__ import annotations

import os
import sys

from meshy_youtube import youtube


def main() -> None:
    cs = youtube.client_secret_file()
    if not os.path.isfile(cs):
        print(
            f"client_secret.json not found at {cs}.\n\n"
            "Set one up (one-time):\n"
            "  1. https://console.cloud.google.com/ → create/select a project\n"
            "  2. Enable 'YouTube Data API v3'\n"
            "  3. APIs & Services → Credentials → Create OAuth client ID →\n"
            "     Application type: Desktop app → download the JSON\n"
            f"  4. Save it as {cs}\n"
            "     (or point YOUTUBE_CLIENT_SECRET_FILE at it)\n",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("google-auth-oauthlib not installed; pip install -r requirements.txt",
              file=sys.stderr)
        sys.exit(1)

    flow = InstalledAppFlow.from_client_secrets_file(cs, youtube.SCOPES)
    # Spins up a localhost callback server and opens the browser. The redirect
    # must land back on THIS host (see the headless note in the module docstring).
    creds = flow.run_local_server(port=0)

    tf = youtube.token_file()
    youtube.write_token(tf, creds.to_json())  # atomic 0600 write
    print(f"Authorized. Token saved to {tf}. The MCP server can now upload.")


if __name__ == "__main__":
    main()

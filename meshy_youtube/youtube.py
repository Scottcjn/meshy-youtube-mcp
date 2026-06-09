"""YouTube upload via the Data API v3 (OAuth2 / resumable upload).

Unlike BoTTube's simple API key, YouTube needs OAuth2: a one-time browser
consent (run ``python -m meshy_youtube.authorize``) mints a token file, after
which uploads run unattended via the stored refresh token.

Google client libraries are imported lazily inside the functions, so this
module (and its input validation) can be imported and unit-tested without them.
"""
from __future__ import annotations

import os
import tempfile
from typing import Optional

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
DEFAULT_CONFIG_DIR = os.path.expanduser("~/.config/meshy-youtube-mcp")

# Per-request HTTP timeout for the upload, so a stalled Google connection can't
# hang the MCP worker indefinitely.
_HTTP_TIMEOUT = 600
# Resumable upload chunk size — real chunking (not -1/single-shot) so large
# uploads stream in bounded memory and can resume after a dropped connection.
_UPLOAD_CHUNK = 10 * 1024 * 1024  # 10 MB

VALID_PRIVACY = ("public", "unlisted", "private")

# A light guardrail mirroring the BoTTube tool: refuse obvious non-videos.
_VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v", ".flv"}

# Common YouTube category ids (US). 22 = People & Blogs is the safe default.
COMMON_CATEGORIES = {
    "1": "Film & Animation", "20": "Gaming", "22": "People & Blogs",
    "23": "Comedy", "24": "Entertainment", "27": "Education",
    "28": "Science & Technology",
}


class YouTubeError(RuntimeError):
    """Missing/invalid credentials, bad input, or an API failure."""


def client_secret_file() -> str:
    return os.environ.get(
        "YOUTUBE_CLIENT_SECRET_FILE",
        os.path.join(DEFAULT_CONFIG_DIR, "client_secret.json"),
    )


def token_file() -> str:
    return os.environ.get(
        "YOUTUBE_TOKEN_FILE", os.path.join(DEFAULT_CONFIG_DIR, "token.json"))


def has_token() -> bool:
    return os.path.isfile(token_file())


def write_token(path: str, data: str) -> None:
    """Atomically write the OAuth token with 0600 perms — the file is never
    even briefly world-readable, and an interrupted write can't corrupt an
    existing token (temp file + atomic replace)."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".token-", suffix=".json")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def _load_credentials():
    """Load and (if needed) refresh stored OAuth credentials. Never launches an
    interactive flow — that is `authorize`'s job, done once up front."""
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
    except ImportError as exc:  # pragma: no cover - depends on optional deps
        raise YouTubeError(
            "google client libraries not installed; "
            "pip install -r requirements.txt") from exc

    tf = token_file()
    if not os.path.isfile(tf):
        raise YouTubeError(
            f"no YouTube token at {tf}. Run `python -m meshy_youtube.authorize` "
            f"once to authorize (needs client_secret.json)."
        )
    creds = Credentials.from_authorized_user_file(tf, SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            write_token(tf, creds.to_json())  # atomic 0600 write
        else:
            raise YouTubeError(
                f"stored credentials at {tf} are invalid; re-run authorize.")
    return creds


def _build_service():
    from googleapiclient.discovery import build
    creds = _load_credentials()
    # Give the upload an explicit per-request timeout so a hung Google socket
    # can't wedge the worker. Fall back to the default transport if the helper
    # package isn't present.
    try:
        import google_auth_httplib2
        import httplib2
        authed = google_auth_httplib2.AuthorizedHttp(
            creds, http=httplib2.Http(timeout=_HTTP_TIMEOUT))
        return build("youtube", "v3", http=authed, cache_discovery=False)
    except ImportError:  # pragma: no cover - depends on optional dep
        return build("youtube", "v3", credentials=creds, cache_discovery=False)


def upload(video_path: str, title: str, description: str = "",
           tags: Optional[list] = None, privacy: str = "unlisted",
           category_id: str = "22", made_for_kids: bool = False) -> dict:
    """Upload ``video_path`` to YouTube. ``tags`` is a list of strings.

    privacy: public | unlisted | private (default unlisted — shareable by link,
    not surfaced publicly until you choose to).
    made_for_kids: YouTube's COPPA audience declaration. Default False; set True
    only if the content is genuinely directed at children. Returns video_id +
    watch_url.
    """
    video_path = os.path.abspath(video_path)
    if not os.path.isfile(video_path):
        raise YouTubeError(f"video file not found: {video_path}")
    ext = os.path.splitext(video_path)[1].lower()
    if ext not in _VIDEO_EXTS:
        raise YouTubeError(
            f"refusing to upload non-video file (extension {ext or 'none'!r}); "
            f"allowed: {', '.join(sorted(_VIDEO_EXTS))}")
    if not title or not title.strip():
        raise YouTubeError("a non-empty title is required")
    if privacy not in VALID_PRIVACY:
        raise YouTubeError(f"privacy must be one of {VALID_PRIVACY}, got {privacy!r}")
    # YouTube hard limit: titles <=100 chars, description <=5000.
    if len(title) > 100:
        raise YouTubeError(f"title exceeds YouTube's 100-char limit ({len(title)})")
    if len(description) > 5000:
        raise YouTubeError("description exceeds YouTube's 5000-char limit")

    from googleapiclient.http import MediaFileUpload
    from googleapiclient.errors import HttpError

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": list(tags or []),
            "categoryId": str(category_id),
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": bool(made_for_kids),
        },
    }
    media = MediaFileUpload(video_path, chunksize=_UPLOAD_CHUNK, resumable=True,
                            mimetype="video/*")
    service = _build_service()
    try:
        request = service.videos().insert(
            part="snippet,status", body=body, media_body=media)
        response = None
        while response is None:
            _, response = request.next_chunk()
    except HttpError as exc:
        raise YouTubeError(f"YouTube upload failed: {exc}") from exc

    video_id = response.get("id")
    if not video_id:
        raise YouTubeError(f"upload returned no video id: {str(response)[:300]}")
    return {
        "video_id": video_id,
        "watch_url": f"https://youtu.be/{video_id}",
        "studio_url": f"https://studio.youtube.com/video/{video_id}/edit",
        "privacy": privacy,
        "title": title,
    }

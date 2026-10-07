"""Google Drive access via a service account (share the clips folder with its email)."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path

from .config import CACHE_DIR

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
VIDEO_TYPES = ("video/",)


def _service():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    raw = os.environ.get("GDRIVE_SERVICE_ACCOUNT_JSON")
    if not raw:
        raise RuntimeError("GDRIVE_SERVICE_ACCOUNT_JSON is not set")
    creds = service_account.Credentials.from_service_account_info(json.loads(raw), scopes=SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def list_videos(folder_id: str) -> list[dict]:
    svc = _service()
    files, token = [], None
    while True:
        resp = svc.files().list(
            q=f"'{folder_id}' in parents and trashed = false and mimeType contains 'video/'",
            fields="nextPageToken, files(id, name, size, mimeType, modifiedTime, videoMediaMetadata)",
            pageSize=200, pageToken=token, supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute()
        files += resp.get("files", [])
        token = resp.get("nextPageToken")
        if not token:
            return files


def download(file_id: str, name: str) -> Path:
    from googleapiclient.http import MediaIoBaseDownload

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    dest = CACHE_DIR / f"{file_id}{Path(name).suffix.lower() or '.mp4'}"
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    req = _service().files().get_media(fileId=file_id, supportsAllDrives=True)
    buf = io.FileIO(dest, "wb")
    dl = MediaIoBaseDownload(buf, req, chunksize=32 * 1024 * 1024)
    done = False
    while not done:
        _, done = dl.next_chunk()
    buf.close()
    return dest

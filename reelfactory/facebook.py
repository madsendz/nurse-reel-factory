"""Cross-post the same reel to the Facebook Page (Facebook Reels Publishing API).

Reels published through the Instagram API never get shared to Facebook on their own
(the app's "share to Facebook" switch only applies to reels posted from the app), so the
bot uploads the same mp4 + caption to the Page right after the Instagram post.
Needs secrets FB_PAGE_ID and FB_PAGE_TOKEN (a Page access token with pages_manage_posts).
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import requests

GRAPH = "https://graph.facebook.com"
RUPLOAD = "https://rupload.facebook.com/video-upload"


def enabled(cfg: dict) -> bool:
    fb = cfg.get("facebook") or {}
    if not fb.get("crosspost"):
        return False
    if not (os.environ.get("FB_PAGE_ID") and os.environ.get("FB_PAGE_TOKEN")):
        print("[facebook] crosspost is on but FB_PAGE_ID / FB_PAGE_TOKEN secrets are missing — skipping")
        return False
    return True


def _check(r: requests.Response) -> dict:
    try:
        body = r.json()
    except ValueError:
        body = {"raw": r.text}
    if r.status_code >= 400 or "error" in body:
        raise RuntimeError(f"{r.status_code}: {body.get('error', body)}")
    return body


def publish_reel(video: Path, caption: str, cfg: dict) -> dict:
    """Upload the reel to the Page and publish it. Returns {fb_video_id, fb_permalink}."""
    page, token = os.environ["FB_PAGE_ID"].strip(), os.environ["FB_PAGE_TOKEN"].strip()
    v = (cfg.get("facebook") or {}).get("api_version") or (cfg.get("instagram") or {}).get("api_version", "v25.0")
    start = _check(requests.post(f"{GRAPH}/{v}/{page}/video_reels", timeout=60,
                                 data={"upload_phase": "start", "access_token": token}))
    vid = start["video_id"]
    size = video.stat().st_size
    with open(video, "rb") as f:
        _check(requests.post(f"{RUPLOAD}/{v}/{vid}", data=f, timeout=600, headers={
            "Authorization": f"OAuth {token}", "offset": "0", "file_size": str(size)}))
    _check(requests.post(f"{GRAPH}/{v}/{page}/video_reels", timeout=60,
                         data={"upload_phase": "finish", "video_id": vid, "video_state": "PUBLISHED",
                               "description": caption, "access_token": token}))
    info: dict = {}
    for _ in range(40):  # wait for Meta to process it (up to ~10 min)
        time.sleep(15)
        info = _check(requests.get(f"{GRAPH}/{v}/{vid}", timeout=60,
                                   params={"fields": "status,permalink_url", "access_token": token}))
        st = info.get("status") or {}
        phase = (st.get("publishing_phase") or {}).get("status")
        if st.get("video_status") == "ready" or phase == "complete":
            break
        if st.get("video_status") == "error" or phase == "error" or \
                (st.get("processing_phase") or {}).get("status") == "error":
            raise RuntimeError(f"Facebook reel {vid} failed: {st}")
    link = info.get("permalink_url") or f"/reel/{vid}"
    if link.startswith("/"):
        link = "https://www.facebook.com" + link
    return {"fb_video_id": vid, "fb_permalink": link}

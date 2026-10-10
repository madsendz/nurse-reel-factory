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
    return bool((cfg.get("facebook") or {}).get("crosspost"))


def _version(cfg: dict) -> str:
    return (cfg.get("facebook") or {}).get("api_version") or (cfg.get("instagram") or {}).get("api_version", "v25.0")


def resolve_page(cfg: dict) -> tuple[str, str]:
    """(page_id, page_token). Uses FB_PAGE_ID / FB_PAGE_TOKEN secrets when set; otherwise finds the
    Page linked to the Instagram account through IG_ACCESS_TOKEN (the Instagram token already has
    access to that Page, so no extra secrets are needed)."""
    pid, ptok = (os.environ.get("FB_PAGE_ID") or "").strip(), (os.environ.get("FB_PAGE_TOKEN") or "").strip()
    if pid and ptok:
        return pid, ptok
    v, ig_tok = _version(cfg), os.environ.get("IG_ACCESS_TOKEN")
    ig_id = os.environ.get("IG_USER_ID") or (cfg.get("instagram") or {}).get("ig_user_id")
    if not ig_tok:
        raise RuntimeError("no FB_PAGE_TOKEN and no IG_ACCESS_TOKEN")
    pid = pid or str((cfg.get("facebook") or {}).get("page_id") or "").strip()
    if pid:  # known Page: ask for its token directly (me/accounts can come back empty even when the token has access)
        page = _check(requests.get(f"{GRAPH}/{v}/{pid}", timeout=60, params={
            "fields": "id,name,access_token", "access_token": ig_tok}))
        if page.get("access_token"):
            print(f"[facebook] using Page {page.get('name')} ({pid})")
            return pid, page["access_token"]
    pages = _check(requests.get(f"{GRAPH}/{v}/me/accounts", timeout=60, params={
        "fields": "id,name,access_token,instagram_business_account", "limit": 100, "access_token": ig_tok})).get("data", [])
    if not pages:
        raise RuntimeError("the Instagram token can't see any Facebook Pages (needs pages_show_list + pages_manage_posts)")
    page = next((p for p in pages if pid and p["id"] == pid), None) or \
        next((p for p in pages if str((p.get("instagram_business_account") or {}).get("id")) == str(ig_id)), None) or pages[0]
    print(f"[facebook] using Page {page.get('name')} ({page['id']})")
    return page["id"], page["access_token"]


def check(cfg: dict) -> str:
    """Connection check for the logs: which Page the bot would post to."""
    pid, tok = resolve_page(cfg)
    me = _check(requests.get(f"{GRAPH}/{_version(cfg)}/{pid}", timeout=60, params={"fields": "id,name", "access_token": tok}))
    return f"{me.get('name')} ({pid})"


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
    page, token = resolve_page(cfg)
    v = _version(cfg)
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

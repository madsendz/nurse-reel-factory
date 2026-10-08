"""Instagram Graph API (Facebook Login for Business): publish reels + read insights.

Uses the resumable upload so the video goes straight from GitHub to Meta;
no public video hosting needed.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import requests

GRAPH = "https://graph.facebook.com"
RUPLOAD = "https://rupload.facebook.com/ig-api-upload"
REEL_METRICS = ["views", "reach", "shares", "saved", "likes", "comments", "total_interactions",
                "ig_reels_avg_watch_time", "reels_skip_rate"]


class IGError(RuntimeError):
    pass


def pick_audio(ig: "Instagram", posts: list[dict], cfg: dict, rng) -> dict | None:
    """Pick a trending sound that wasn't used in the last few reels."""
    a = cfg.get("sound") or {}
    if not a.get("enabled"):
        return None
    first = a.get("type", "music")
    tries = [(first, a.get("search") or None)]
    other = "original_sound" if first == "music" else "music"
    tries += [(first, "trending"), (other, None), (other, "trending")]
    items = []
    for kind, q in tries:
        try:
            items = ig.trending_audio(kind, q)
        except Exception as e:
            print(f"[sound] ig_audio {kind} q={q!r} failed: {e}")
            continue
        keys = sorted(items[0].keys()) if items else []
        print(f"[sound] ig_audio {kind} q={q!r}: {len(items)} results {keys}")
        if items:
            break
    recent = {p.get("audio_id") for p in posts[-a.get("avoid_last", 6):]}
    pool = []
    for it in items:
        aid = str(it.get("audio_id") or it.get("id") or "")
        if aid and aid not in recent:
            pool.append({"audio_id": aid, "title": it.get("title"), "artist": it.get("display_artist")
                         or it.get("ig_username"), "audio_volume": a.get("audio_volume", 100),
                         "video_volume": a.get("video_volume", 0)})
    pool = pool[: a.get("top_n", 10)]
    return rng.choice(pool) if pool else None


class Instagram:
    def __init__(self, cfg: dict):
        ig = cfg["instagram"]
        self.version = ig.get("api_version", "v25.0")
        self.user_id = ig.get("ig_user_id")
        self.token = os.environ.get("IG_ACCESS_TOKEN")
        self.share_to_feed = ig.get("share_to_feed", True)
        if not self.token or not self.user_id:
            raise IGError("IG_ACCESS_TOKEN and IG_USER_ID must be set")

    # ---- helpers ----
    def _url(self, path: str) -> str:
        return f"{GRAPH}/{self.version}/{path.lstrip('/')}"

    def _check(self, r: requests.Response) -> dict:
        try:
            body = r.json()
        except ValueError:
            body = {"raw": r.text}
        if r.status_code >= 400 or "error" in body:
            raise IGError(f"{r.status_code}: {body.get('error', body)}")
        return body

    def get(self, path: str, **params) -> dict:
        params["access_token"] = self.token
        return self._check(requests.get(self._url(path), params=params, timeout=60))

    def post(self, path: str, **data) -> dict:
        data["access_token"] = self.token
        return self._check(requests.post(self._url(path), data=data, timeout=120))

    # ---- publishing ----
    def publishing_quota(self) -> dict:
        return self.get(f"{self.user_id}/content_publishing_limit", fields="quota_usage,config")

    def trending_audio(self, audio_type: str = "music", query: str | None = None) -> list[dict]:
        """Instagram Audio API: trending sounds when no query is given."""
        params = {"audio_type": audio_type, "user_id": self.user_id}
        if query:
            params["search_query"] = query
        body = self.get("ig_audio", **params)
        data = body.get("audio") or body.get("data") or []
        if not data:
            print(f"[sound] ig_audio raw reply: {json.dumps(body)[:400]}")
        return data

    def publish_reel(self, video: Path, caption: str, thumb_offset_ms: int | None = None,
                     trial_strategy: str | None = None, audio: dict | None = None,
                     video_url: str | None = None) -> dict:
        params = {"media_type": "REELS", "caption": caption,
                  "share_to_feed": "true" if self.share_to_feed else "false"}
        if video_url:  # standard upload (needed for trial reels: Meta ignores trial_params on resumable uploads)
            params["video_url"] = video_url
        else:
            params["upload_type"] = "resumable"
        if audio:  # music from Instagram's library (e.g. a trending sound)
            # only the sound id: custom volume values seemed to leave the reel silent
            params["audio_configuration"] = json.dumps({"audio_id": str(audio["audio_id"])})
        if trial_strategy:  # trial reel: shown to non-followers first
            params["trial_params"] = json.dumps({"graduation_strategy": trial_strategy})
        if thumb_offset_ms is not None:
            params["thumb_offset"] = str(thumb_offset_ms)
        container = self.post(f"{self.user_id}/media", **params)
        cid = container["id"]

        if not video_url:
            size = video.stat().st_size
            with open(video, "rb") as f:
                r = requests.post(f"{RUPLOAD}/{self.version}/{cid}", data=f, timeout=600, headers={
                    "Authorization": f"OAuth {self.token}", "offset": "0", "file_size": str(size)})
            self._check(r)

        # Wait for Meta to process the video (up to ~10 min)
        status = None
        for _ in range(40):
            time.sleep(15)
            status = self.get(cid, fields="status_code,status").get("status_code")
            if status == "FINISHED":
                break
            if status in ("ERROR", "EXPIRED"):
                info = self.get(cid, fields="status_code,status")
                raise IGError(f"Container {cid} failed: {info}")
        if status != "FINISHED":
            raise IGError(f"Container {cid} still processing (last status {status})")

        media = self.post(f"{self.user_id}/media_publish", creation_id=cid)
        mid = media["id"]
        info = self.get(mid, fields="id,permalink,timestamp")
        return {"ig_media_id": mid, "permalink": info.get("permalink"), "timestamp": info.get("timestamp")}

    # ---- insights ----
    def reel_insights(self, media_id: str) -> dict:
        out = {}
        # ask for all metrics at once, then fall back one-by-one if any are unsupported
        try:
            body = self.get(f"{media_id}/insights", metric=",".join(REEL_METRICS))
            rows = body.get("data", [])
        except IGError:
            rows = []
            for m in REEL_METRICS:
                try:
                    rows += self.get(f"{media_id}/insights", metric=m).get("data", [])
                except IGError:
                    pass
        for row in rows:
            vals = row.get("values") or [{}]
            out[row["name"]] = vals[0].get("value", row.get("total_value", {}).get("value"))
        return out

    def recent_media(self, limit: int = 50) -> list[dict]:
        body = self.get(f"{self.user_id}/media", fields="id,caption,timestamp,permalink,media_product_type",
                        limit=limit)
        return body.get("data", [])

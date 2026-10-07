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

    def publish_reel(self, video: Path, caption: str, thumb_offset_ms: int | None = None,
                     trial_strategy: str | None = None) -> dict:
        params = {"media_type": "REELS", "upload_type": "resumable", "caption": caption,
                  "share_to_feed": "true" if self.share_to_feed else "false"}
        if trial_strategy:  # trial reel: shown to non-followers first
            params["trial_params"] = json.dumps({"graduation_strategy": trial_strategy})
        if thumb_offset_ms is not None:
            params["thumb_offset"] = str(thumb_offset_ms)
        container = self.post(f"{self.user_id}/media", **params)
        cid = container["id"]

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

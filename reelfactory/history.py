"""Look back at @lexixoofficial's past reels: views, shares, saves, and estimated
link clicks, plus a few frames from each top reel so the on-screen text can be read.

Run:  python -m reelfactory.history --days 180
Out:  out/history/history.csv, history.json, frames/*.jpg
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from PIL import Image

from .clicks import attribute, fetch_hourly
from .config import OUT_DIR, load_config
from .instagram import Instagram
from .scoring import parse_ts

FIELDS = "id,caption,timestamp,permalink,media_product_type,media_type,media_url,thumbnail_url"


def all_reels(ig: Instagram, since: datetime, cap: int) -> list[dict]:
    out, body = [], ig.get(f"{ig.user_id}/media", fields=FIELDS, limit=50)
    while True:
        for m in body.get("data", []):
            if parse_ts(m["timestamp"]) < since or len(out) >= cap:
                return out
            if m.get("media_product_type") == "REELS" or m.get("media_type") == "VIDEO":
                out.append(m)
        nxt = (body.get("paging") or {}).get("next")
        if not nxt:
            return out
        body = requests.get(nxt, timeout=60).json()


def clicks_hourly(cfg, since):
    until = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    for days in (90, 60, 30, 14):
        start = max(since, until - timedelta(days=days))
        try:
            h = fetch_hourly(cfg, start, until)
            print(f"[history] clicks from {start:%Y-%m-%d} ({days}d window)")
            return h, start
        except Exception as e:
            print(f"[history] clicks {days}d failed: {str(e)[:200]}")
    return {}, until


def frames(url: str, out: Path, times=(0.5, 2, 3.5, 5, 6.5, 8, 10, 13)) -> bool:
    with tempfile.TemporaryDirectory() as td:
        v = Path(td) / "v.mp4"
        try:
            r = requests.get(url, timeout=120)
            r.raise_for_status()
            v.write_bytes(r.content)
        except Exception as e:
            print(f"[history] download failed: {e}")
            return False
        ims = []
        for k, t in enumerate(times):
            p = Path(td) / f"{k}.jpg"
            subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-ss", str(t), "-i", str(v), "-frames:v", "1",
                            "-vf", "scale=360:-2", str(p)])
            if p.exists():
                ims.append(Image.open(p).convert("RGB"))
        if not ims:
            return False
        w, h = ims[0].size
        sheet = Image.new("RGB", (w * len(ims), h), "white")
        for k, im in enumerate(ims):
            sheet.paste(im.resize((w, h)), (k * w, 0))
        sheet.save(out, quality=80)
        return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--max", type=int, default=600)
    ap.add_argument("--frames", type=int, default=80, help="grab frames for the top N reels")
    a = ap.parse_args()
    cfg = load_config()
    ig = Instagram(cfg)
    since = datetime.now(timezone.utc) - timedelta(days=a.days)
    reels = all_reels(ig, since, a.max)
    print(f"[history] {len(reels)} reels since {since:%Y-%m-%d}")

    posts = []
    for m in reels:
        try:
            met = ig.reel_insights(m["id"])
        except Exception as e:
            print(f"[history] insights failed for {m['id']}: {e}")
            met = {}
        posts.append({"id": m["id"], "ig_media_id": m["id"], "posted_at": parse_ts(m["timestamp"]).isoformat(),
                      "metrics": met, "permalink": m.get("permalink"), "caption": (m.get("caption") or ""),
                      "media_url": m.get("media_url")})

    hourly, click_start = clicks_hourly(cfg, since)
    attribute(posts, hourly, cfg)
    for p in posts:
        if parse_ts(p["posted_at"]) < click_start - timedelta(hours=1):
            p["clicks_attr"] = None  # no click data that far back

    def g(p, k):
        return float((p["metrics"] or {}).get(k) or 0)

    for p in posts:
        v = max(g(p, "views"), 1)
        p["share_rate"] = round(1000 * g(p, "shares") / v, 2)
        p["save_rate"] = round(1000 * g(p, "saved") / v, 2)
        p["clicks_per_1k"] = round(1000 * p["clicks_attr"] / v, 3) if p["clicks_attr"] is not None else None
    posts.sort(key=lambda p: g(p, "views"), reverse=True)
    for i, p in enumerate(posts):
        p["rank_views"] = i + 1

    out = OUT_DIR / "history"
    (out / "frames").mkdir(parents=True, exist_ok=True)
    cols = ["rank_views", "posted_at", "views", "reach", "shares", "saved", "likes", "comments",
            "ig_reels_avg_watch_time", "reels_skip_rate", "clicks_attr", "clicks_per_1k", "share_rate",
            "save_rate", "permalink", "caption"]
    with open(out / "history.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for p in posts:
            row = {**p["metrics"], **p}
            w.writerow([str(row.get(c, "")).replace("\n", " ")[:600] for c in cols])
    (out / "history.json").write_text(json.dumps([{k: v for k, v in p.items() if k != "media_url"}
                                                  for p in posts], indent=1, ensure_ascii=False), encoding="utf-8")

    # frames: top N by views, top 25 by clicks, bottom 15 for contrast
    pick = {p["id"]: p for p in posts[: a.frames]}
    by_clicks = sorted([p for p in posts if p["clicks_attr"]], key=lambda p: p["clicks_attr"], reverse=True)
    pick.update({p["id"]: p for p in by_clicks[:25]})
    pick.update({p["id"]: p for p in posts[-15:]})
    n = 0
    for p in pick.values():
        if p.get("media_url"):
            name = f"r{p['rank_views']:03d}_{p['id']}.jpg"
            n += frames(p["media_url"], out / "frames" / name)
    print(f"[history] saved frames for {n} reels")
    total = sum(p["clicks_attr"] or 0 for p in posts)
    md = ["# Past reels", "", f"{len(posts)} reels, {total:.0f} attributed link clicks", "",
          "| # | date | views | shares | saves | clicks | caption |", "|---|---|---|---|---|---|---|"]
    for p in posts[:40]:
        cap = p["caption"].split("\n")[0][:70].replace("|", "/")
        md.append(f"| {p['rank_views']} | {p['posted_at'][:10]} | {g(p,'views'):.0f} | {g(p,'shares'):.0f} | "
                  f"{g(p,'saved'):.0f} | {p['clicks_attr'] if p['clicks_attr'] is not None else '-'} | {cap} |")
    import os
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write("\n".join(md) + "\n")


if __name__ == "__main__":
    main()

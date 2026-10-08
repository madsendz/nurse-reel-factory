"""Royalty-free music baked into the reel (Instagram's API won't attach library sounds to
this account). Tracks come from Openverse (Jamendo, ccMixter, Freesound...) under licenses
that allow commercial use: CC0 (no credit needed) or CC BY (credit goes in the caption).
Each reel searches one of the popular reel genres in config.yaml and picks among the
best-ranked results, skipping tracks used recently.
"""
from __future__ import annotations

import os
import random
import subprocess
from pathlib import Path

import requests

from .config import ROOT

API = "https://api.openverse.org/v1/audio/"
CACHE = ROOT / ".cache" / "music"
HEADERS = {"User-Agent": "nurse-reel-factory/1.0 (reel music picker)"}
DEFAULT_GENRES = ["lofi hip hop", "phonk", "chill rnb", "deep house", "trap beat", "afrobeat", "chillhop"]


def _settings(cfg) -> dict:
    return cfg.get("sound") or {}


def baked(cfg) -> bool:
    s = _settings(cfg)
    return bool(s.get("enabled")) and s.get("mode", "baked") == "baked"


def recent_ids(items: list[dict], n: int = 15) -> set[str]:
    out = set()
    for x in items[-n:]:
        mid = x.get("music_id") or (x.get("music") or {}).get("id")
        if mid:
            out.add(str(mid))
    return out


def pick_track(cfg, avoid: set[str] | None = None, rng=None) -> dict | None:
    """Jamendo's most-played-this-week tracks when a JAMENDO_CLIENT_ID is set; else Openverse."""
    rng = rng or random.Random()
    if os.environ.get("JAMENDO_CLIENT_ID"):
        t = _pick_jamendo(cfg, avoid or set(), rng)
        if t:
            return t
    return _pick_openverse(cfg, avoid, rng)


JAMENDO = "https://api.jamendo.com/v3.0/tracks/"
DEFAULT_TAGS = ["lounge", "hiphop", "chillout", "rnb", "house", "electronic", "pop", "funk"]


def _pick_jamendo(cfg, avoid: set[str], rng) -> dict | None:
    s = _settings(cfg)
    tags = list(s.get("jamendo_tags") or DEFAULT_TAGS)
    rng.shuffle(tags)
    for tag in tags[:4]:
        try:
            r = requests.get(JAMENDO, headers=HEADERS, timeout=30, params={
                "client_id": os.environ["JAMENDO_CLIENT_ID"], "format": "json", "limit": 30,
                "order": "popularity_week", "tags": tag, "vocalinstrumental": s.get("vocal", "instrumental"),
                "ccnc": "false", "ccnd": "false", "ccsa": "false",  # = CC BY only: ok for business use
                "audioformat": "mp32", "include": "licenses"})
            body = r.json()
            res = body.get("results") or []
            if (body.get("headers") or {}).get("status") != "success":
                print(f"[music] jamendo: {str((body.get('headers') or {}).get('error_message'))[:120]}")
        except Exception as ex:
            print(f"[music] jamendo '{tag}' failed: {ex}")
            continue
        ok = [t for t in res if t.get("audio") and f"jamendo-{t.get('id')}" not in avoid
              and (t.get("duration") or 0) >= 40]
        print(f"[music] jamendo '{tag}' (popular this week): {len(res)} results, {len(ok)} usable")
        if not ok:
            continue
        t = rng.choice(ok[: s.get("top_n", 8)])
        lic = (t.get("license_ccurl") or "").rstrip("/").split("/")
        ver = lic[-1] if len(lic) >= 2 else ""
        print(f"[music] {tag}: {t.get('name')} by {t.get('artist_name')} (CC BY {ver}, jamendo)")
        return {"id": f"jamendo-{t['id']}", "title": t.get("name") or "untitled", "creator": t.get("artist_name") or "unknown",
                "license": "by", "license_version": ver, "url": t.get("audiodownload") or t["audio"],
                "source": "jamendo", "duration_ms": int(t.get("duration") or 0) * 1000,
                "page": t.get("shareurl"), "genre": tag}
    return None


def _pick_openverse(cfg, avoid: set[str] | None = None, rng=None) -> dict | None:
    s = _settings(cfg)
    rng = rng or random.Random()
    genres = list(s.get("genres") or DEFAULT_GENRES)
    rng.shuffle(genres)
    avoid = avoid or set()
    for genre in genres[:4]:
        res = []
        for params in ({"q": genre, "license": s.get("licenses", "cc0,by"), "category": "music", "page_size": 20},
                       {"q": genre, "license": s.get("licenses", "cc0,by"), "page_size": 20}):
            try:
                r = requests.get(API, headers=HEADERS, timeout=30, params=params)
                body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
                print(f"[music] openverse {r.status_code} count={body.get('result_count')} {str(body.get('detail') or '')[:120]}")
                res = body.get("results", []) or []
            except Exception as ex:
                print(f"[music] search '{genre}' failed: {ex}")
            if res:
                break
        ok = [t for t in res if t.get("url") and str(t.get("id")) not in avoid
              and (t.get("duration") or 0) >= 45000 and t.get("source") in ("jamendo", "ccmixter")]
        print(f"[music] '{genre}': {len(res)} results, {len(ok)} usable "
              f"{[(t.get('filetype'), t.get('duration'), t.get('source')) for t in res[:3]]}")
        if not ok:
            continue
        t = rng.choice(ok[: s.get("top_n", 8)])  # results come ranked by relevance + popularity
        print(f"[music] {genre}: {t.get('title')} by {t.get('creator')} ({t.get('license')}, {t.get('source')})")
        return {"id": str(t["id"]), "title": t.get("title") or "untitled", "creator": t.get("creator") or "unknown",
                "license": (t.get("license") or "").lower(), "license_version": t.get("license_version") or "",
                "url": t["url"], "source": t.get("source"), "duration_ms": t.get("duration") or 0,
                "page": t.get("foreign_landing_url"), "genre": genre}
    print("[music] no track found — keeping the clip's own audio")
    return None


def _download(track: dict) -> Path | None:
    CACHE.mkdir(parents=True, exist_ok=True)
    p = CACHE / f"{track['id']}.audio"
    if p.exists() and p.stat().st_size > 10000:
        return p
    try:
        r = requests.get(track["url"], headers=HEADERS, timeout=120)
        r.raise_for_status()
        p.write_bytes(r.content)
        return p
    except Exception as ex:
        print(f"[music] download failed: {ex}")
        return None


def add_music(video: Path, track: dict, seconds: float, cfg) -> bool:
    """Replace the clip's background noise with the track (faded in/out). Returns True on success."""
    src = _download(track)
    if not src:
        return False
    s = _settings(cfg)
    dur = (track.get("duration_ms") or 0) / 1000
    start = min(float(s.get("music_start", 25)), max(0.0, dur * 0.25)) if dur > seconds + 5 else 0
    vol = float(s.get("music_volume", 0.9))
    fade_out = max(0.0, seconds - 0.8)
    tmp = video.with_suffix(".music.mp4")
    cmd = ["ffmpeg", "-loglevel", "error", "-y", "-i", str(video), "-ss", f"{start:.2f}", "-i", str(src),
           "-filter_complex", f"[1:a]volume={vol},afade=t=in:st=0:d=0.3,afade=t=out:st={fade_out:.2f}:d=0.8[m]",
           "-map", "0:v", "-map", "[m]", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-ar", "44100",
           "-t", f"{seconds:.2f}", str(tmp)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not tmp.exists():
        print(f"[music] mixing failed: {r.stderr[-400:]}")
        return False
    tmp.replace(video)
    return True


def credit(track: dict | None) -> str:
    """Caption credit (required for CC BY, not for CC0)."""
    if not track or track.get("license") in ("cc0", "pdm"):
        return ""
    lic = f"CC {track.get('license', '').upper()} {track.get('license_version', '')}".strip()
    return f"🎵 {track['title']} by {track['creator']} ({lic})"

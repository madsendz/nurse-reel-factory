"""Small 9:16 copies of every clip for the dashboard's Clips tab.

They're stored on the relay (Cloudflare KV) for good, so Nick can scrub through a clip and mark
when the boxes pop up and where the reel should end. The copy is cropped exactly like the finished
reel, so a tap on the preview lands on the same spot in the reel.
"""
from __future__ import annotations

import hashlib
import subprocess
import tempfile
from pathlib import Path

import requests

from .config import load_clips

SIZE = (540, 960)  # half of the reel's 1080x1920


def _auth() -> dict:
    from .approve import _token
    return {"Authorization": "Bearer " + hashlib.sha256(_token().encode()).hexdigest()}


def push_previews(cfg, force: bool = False, only: str | None = None) -> int:
    """Upload a preview for every clip the relay doesn't have yet (or all, with force). Returns the count."""
    relay = (cfg.get("approvals") or {}).get("relay_url")
    if not relay:
        print("[previews] no relay_url in config.yaml")
        return 0
    relay = relay.rstrip("/")
    have = set()
    if not force:
        r = requests.get(relay + "/clips", headers=_auth(), timeout=60)
        r.raise_for_status()
        have = set(r.json())
    from .cli import _clip_file
    w, h = SIZE
    done = 0
    for c in load_clips():
        if (only and c["id"] != only) or (c["id"] in have):
            continue
        try:
            src = _clip_file(c)
            with tempfile.TemporaryDirectory() as td:
                out = Path(td) / f"{c['id']}.mp4"
                subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
                                "-vf", f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},fps=30",
                                "-c:v", "libx264", "-preset", "veryfast", "-crf", "30", "-pix_fmt", "yuv420p",
                                "-c:a", "aac", "-b:a", "64k", "-ac", "1", "-movflags", "+faststart", str(out)],
                               check=True)
                data = out.read_bytes()
            r = requests.post(relay + "/clip/" + c["id"], data=data, timeout=180,
                              headers={**_auth(), "content-type": "video/mp4"})
            r.raise_for_status()
            done += 1
            print(f"[previews] {c['id']}: {len(data) // 1024} KB uploaded")
            if not c.get("local_path"):
                Path(src).unlink(missing_ok=True)  # keep the runner's disk free
        except Exception as ex:
            print(f"[previews] {c['id']} failed: {ex}")
    return done

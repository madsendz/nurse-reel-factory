"""Command line entry point.

  python -m reelfactory sync-clips          add new Drive clips to clips/clips.yaml
  python -m reelfactory auto-tag            find where Lexi points in new clips + make contact sheets
  python -m reelfactory refresh             pull IG insights + link clicks, rescore, write report
  python -m reelfactory post [--force]      post the next reel if a time slot is due
  python -m reelfactory preview --n 3       render N upcoming reels to out/ without posting
  python -m reelfactory report              print the performance report
  python -m reelfactory ideas               (optional) have Claude draft new topics for review
"""
from __future__ import annotations

import argparse
import os
import random
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import picker as pk
from .config import (OUT_DIR, ROOT, load_bank, load_clips, load_config, load_posts, save_bank, save_clips,
                     save_posts)
from .scoring import feature_estimates, leaderboard, parse_ts, score_posts

SLOT_GRACE_MIN = 120  # GitHub schedules can run late; a slot stays "due" this long


def _summary(md: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(md + "\n")


def _clip_file(clip: dict) -> Path:
    if clip.get("local_path"):
        return ROOT / clip["local_path"]
    from .drive import download
    return download(clip["drive_id"], clip.get("name", clip["id"]))


# ───────────────────────── commands ─────────────────────────
def cmd_sync_clips(cfg, args):
    from .drive import list_videos
    folder = cfg["drive"]["folder_id"]
    if not folder:
        sys.exit("Set drive.folder_id in config.yaml (or DRIVE_FOLDER_ID secret)")
    clips = load_clips()
    known = {c.get("drive_id") for c in clips}
    added = 0
    for f in list_videos(folder):
        if f["id"] in known:
            continue
        n = len(clips) + 1
        clips.append({"id": f"clip{n:02d}", "drive_id": f["id"], "name": f["name"], "status": "untagged",
                      "point": "", "zone": {"x": 0.08, "y": 0.14, "w": 0.84, "h": 0.40}, "start": 0, "end": None})
        added += 1
    save_clips(clips)
    print(f"Added {added} new clip(s); {len(clips)} total. New clips are 'untagged' until their zone is set.")


def cmd_auto_tag(cfg, args):
    from .autotag import contact_sheet, extract_frames, tag_clip
    clips = load_clips()
    todo = [c for c in clips if args.all or c.get("status") == "untagged"]
    sheet_dir = OUT_DIR / "sheets"
    sheet_dir.mkdir(parents=True, exist_ok=True)
    entries = []
    for c in todo:
        try:
            path = _clip_file(c)
            res = tag_clip(path, c)
            c["zone"], c["auto"] = res["zone"], res["auto"]
            if c.get("status") == "untagged":
                c["status"] = "active"
            frame = extract_frames(path, sheet_dir, n=1)[0].rename(sheet_dir / f"{c['id']}.jpg")
            entries.append((c["id"], frame, c["zone"]))
            print(f"{c['id']}: {res['auto']['method']} -> {c['zone']}")
            if not c.get("local_path") and not args.keep:
                path.unlink(missing_ok=True)  # don't fill the runner's disk
        except Exception as e:
            print(f"{c['id']}: FAILED {e}")
        save_clips(clips)
    for k in range(0, len(entries), 16):
        contact_sheet(entries[k:k + 16], sheet_dir / f"sheet_{k // 16 + 1:02d}.jpg")
    for _, f, _ in entries:
        f.unlink(missing_ok=True)
    print(f"Tagged {len(entries)} clip(s). Contact sheets in {sheet_dir}")


def cmd_refresh(cfg, args):
    from .clicks import attribute, update_click_log
    posts = load_posts()
    now = datetime.now(timezone.utc)
    try:
        from .instagram import Instagram
        ig = Instagram(cfg)
        try:  # connection check: shows up in the run log
            me = ig.get(ig.user_id, fields="username,followers_count,media_count")
            quota = ig.publishing_quota()
            print(f"[instagram] connected as @{me.get('username')} ({me.get('followers_count')} followers); "
                  f"publishing quota: {quota.get('data', [{}])[0]}")
        except Exception as e:
            print(f"[instagram] CONNECTION CHECK FAILED: {e}")
        for p in posts:
            if not p.get("ig_media_id"):
                continue
            age_d = (now - parse_ts(p["posted_at"])).days
            if age_d > 45 and p.get("metrics"):
                continue  # old reels are settled
            try:
                p["metrics"] = ig.reel_insights(p["ig_media_id"])
                p["metrics_updated_at"] = now.isoformat()
            except Exception as e:  # deleted reel etc.
                print(f"[insights] {p['ig_media_id']}: {e}")
    except Exception as e:
        print(f"[insights] skipped: {e}")
    hourly = update_click_log(cfg)
    attribute(posts, hourly, cfg)
    save_posts(posts)
    md = leaderboard(score_posts(posts, cfg), feature_estimates(score_posts(posts, cfg)))
    (ROOT / "reports").mkdir(exist_ok=True)
    (ROOT / "reports" / "latest.md").write_text(md, encoding="utf-8")
    _summary(md)
    print(md)


def _slots(cfg):
    for t in cfg["post_times"]:
        if isinstance(t, str):
            yield t, "regular"
        else:
            yield t["time"], t.get("type", "regular")


def _due_slot(cfg, posts, now_utc):
    """Return (slot_key, post_type) for the most recent due slot not yet posted, else None."""
    tz = ZoneInfo(cfg.get("timezone", "America/New_York"))
    now = now_utc.astimezone(tz)
    done = {p.get("slot") for p in posts}
    due = []
    for t, kind in _slots(cfg):
        hh, mm = map(int, t.split(":"))
        slot_dt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        key = slot_dt.strftime("%Y-%m-%d %H:%M")
        if slot_dt <= now < slot_dt + timedelta(minutes=SLOT_GRACE_MIN) and key not in done:
            due.append((slot_dt, key, kind))
    if not due:
        return None
    _, key, kind = max(due)
    return key, kind


def _make(cfg, choice, out_dir: Path, tag: str):
    from .render import render_reel
    item, clip = choice["item"], choice["clip"]
    out = out_dir / f"{tag}_{item['id']}_{clip['id']}.mp4"
    meta = render_reel(item, clip, _clip_file(clip), out, cfg)
    caption = pk.build_caption(item, choice["cta"], cfg)
    return out, caption, meta


def cmd_post(cfg, args):
    if cfg.get("paused") and not args.force:
        print("Paused in config.yaml — not posting.")
        return
    posts = load_posts()
    now = datetime.now(timezone.utc)
    due = _due_slot(cfg, posts, now)
    if not due:
        if not args.force:
            print("No posting slot due right now.")
            return
        due = ("manual " + now.strftime("%Y-%m-%d %H:%M"), "trial" if args.trial else "regular")
    slot, post_type = due
    if args.trial:
        post_type = "trial"
    choice = pk.pick_next(load_bank(), load_clips(), posts, cfg, now, post_type=post_type)
    out, caption, meta = _make(cfg, choice, OUT_DIR, "post")
    print(f"[{post_type}] Picked {choice['item']['id']} on {choice['clip']['id']} ({choice['mode']}); rendered {meta['seconds']}s")
    record = {"id": uuid.uuid4().hex[:10], "slot": slot, "item_id": choice["item"]["id"],
              "post_type": post_type, "category": choice["item"]["category"], "style": choice["item"].get("style"),
              "clip_id": choice["clip"]["id"], "cta": str(choice["cta"]), "mode": choice["mode"],
              "seconds": meta["seconds"], "caption": caption}
    if args.dry_run:
        print("Dry run — not publishing.\n\n" + caption)
        return
    from .instagram import Instagram
    strategy = cfg.get("trial", {}).get("graduation_strategy", "MANUAL") if post_type == "trial" else None
    res = Instagram(cfg).publish_reel(out, caption, thumb_offset_ms=int(meta["stages"][-1] * 1000) + 500,
                                      trial_strategy=strategy)
    record.update(res)
    record["posted_at"] = (res.get("timestamp") or now.isoformat()).replace("+0000", "+00:00")
    posts.append(record)
    save_posts(posts)
    msg = f"Posted {post_type} reel **{record['item_id']}** on {record['clip_id']} ({record['mode']}) → {record.get('permalink')}"
    print(msg)
    _summary(msg)


def cmd_preview(cfg, args):
    posts = load_posts()
    bank, clips = load_bank(), load_clips()
    if args.clip:
        clips = [c for c in clips if c["id"] == args.clip]
        for c in clips:
            c["status"] = "active"
    if args.item:
        bank = [i for i in bank if i["id"] == args.item]
        for i in bank:
            i["status"] = "active"
    out_dir = OUT_DIR / "preview"
    rng = random.Random(args.seed)
    used, lines = set(), ["# Upcoming reels (preview)", ""]
    fake_posts = list(posts)
    for k in range(args.n):
        choice = pk.pick_next(bank, clips, fake_posts, cfg, rng=rng, exclude_items=used)
        used.add(choice["item"]["id"])
        out, caption, meta = _make(cfg, choice, out_dir, f"{k + 1:02d}")
        fake_posts.append({"item_id": choice["item"]["id"], "category": choice["item"]["category"],
                           "clip_id": choice["clip"]["id"], "posted_at": datetime.now(timezone.utc).isoformat()})
        lines += [f"## {k + 1}. {choice['item']['id']} · {choice['clip']['id']} · {choice['mode']} · {meta['seconds']}s",
                  "", "```", caption, "```", ""]
        print(f"rendered {out.name}")
    (out_dir / "preview.md").write_text("\n".join(lines), encoding="utf-8")
    _summary("\n".join(lines))


def cmd_report(cfg, args):
    posts = load_posts()
    s = score_posts(posts, cfg)
    print(leaderboard(s, feature_estimates(s)))


def cmd_ideas(cfg, args):
    from .ideas import generate
    if not cfg["ideas"].get("enabled") and not args.force:
        print("ideas.enabled is false in config.yaml")
        return
    bank = load_bank()
    s = score_posts(load_posts(), cfg)
    new = generate(bank, feature_estimates(s), cfg)
    save_bank(bank + new)
    md = "## New topic ideas (status: review)\n\n" + "\n".join(f"- **{i['id']}**: {i['hook']}" for i in new)
    _summary(md)
    print(md)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="reelfactory")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sync-clips")
    p = sub.add_parser("auto-tag")
    p.add_argument("--all", action="store_true", help="re-tag every clip, not just untagged ones")
    p.add_argument("--keep", action="store_true", help="keep downloaded clips")
    sub.add_parser("refresh")
    p = sub.add_parser("post")
    p.add_argument("--force", action="store_true", help="post now even if no slot is due / paused")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--trial", action="store_true", help="post as a trial reel")
    p = sub.add_parser("preview")
    p.add_argument("--n", type=int, default=3)
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--clip", help="only use this clip id")
    p.add_argument("--item", help="only use this topic id")
    sub.add_parser("report")
    p = sub.add_parser("ideas")
    p.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)
    cfg = load_config()
    {"sync-clips": cmd_sync_clips, "auto-tag": cmd_auto_tag, "refresh": cmd_refresh, "post": cmd_post, "preview": cmd_preview,
     "report": cmd_report, "ideas": cmd_ideas}[args.cmd](cfg, args)

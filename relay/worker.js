// Lexi reels relay + dashboard (Cloudflare Worker).
// - Telegram webhook: answers ✅/❌ taps instantly and keeps every update for the GitHub jobs (/updates).
// - Dashboard (/): every reel at its time slot; approve / skip / notes / redo / move / post now.
//   Dashboard clicks become "actions" (/actions) the jobs apply exactly like Telegram taps,
//   and are mirrored into the Telegram chat, so both stay in sync.
// - Alarm clock (cron every 5 min): starts the GitHub post job at each approved slot, and the
//   replacement job right after a note or redo, instead of relying on GitHub's own timers.
// Secrets: BOT_TOKEN, DASH_PASSWORD, GH_TOKEN. Var: GH_REPO. KV binding: KV.
import DASHBOARD from "./dashboard.html";

const APPROVE = ["approve", "approved", "yes", "post", "post it", "ok", "good", "✅", "👍"];
const REDO = { swap_music: "new music", new_clip: "same topic, new clip", new_topic: "same clip, new topic", never_topic: "a new topic (that one is retired for good)", text_edit: "your text edit" };
const STATUS = {
  pending: "⏳ waiting for you", approved: "✅ approved — will post", skipped: "❌ skipped", posted: "📤 posted",
  expired: "⌛ not approved in time", missed: "⏭ not posted (another reel took this time slot)",
};

export default {
  async fetch(req, env, ctx) {
    const url = new URL(req.url);
    const c = await ctxFor(env);
    if (!c.token) return new Response("BOT_TOKEN secret missing", { status: 500 });
    const p = url.pathname;

    if (p === "/tg" && req.method === "POST") return telegramWebhook(req, env, c, ctx);
    if (p === "/updates") return bearer(req, c) ? listPrefix(env, "u:", url.searchParams.get("offset"), true) : no();
    if (p === "/actions") return bearer(req, c) ? listActions(env, url.searchParams.get("after") || "") : no();
    if (p === "/state" && req.method === "POST") {
      if (!bearer(req, c)) return no();
      await env.KV.put("state", await req.text());
      return new Response("ok");
    }
    // trial reels must be posted from a public video URL (Meta ignores trial_params on resumable uploads)
    if (p === "/media" && req.method === "POST") {
      if (!bearer(req, c)) return no();
      const key = [...crypto.getRandomValues(new Uint8Array(16))].map((x) => x.toString(16).padStart(2, "0")).join("");
      await env.KV.put("m:" + key, await req.arrayBuffer(), { expirationTtl: 86400 });
      return Response.json({ url: url.origin + "/m/" + key + ".mp4" });
    }
    if (p.startsWith("/m/")) {
      const key = p.slice(3).replace(/\.mp4$/, "");
      const v = await env.KV.get("m:" + key, "arrayBuffer");
      if (!v) return new Response("not found", { status: 404 });
      const h = { "content-type": "video/mp4", "content-length": String(v.byteLength), "cache-control": "no-store" };
      return new Response(req.method === "HEAD" ? null : v, { headers: h });
    }
    // clip previews for the dashboard's Clips tab (small 9:16 copies, uploaded by the jobs, kept for good)
    if (p === "/clips" && req.method === "GET") return bearer(req, c) ? Response.json(await listIds(env, "c:")) : no();
    if (p.startsWith("/clip/") && req.method === "POST") {
      if (!bearer(req, c)) return no();
      await env.KV.put("c:" + p.slice(6).replace(/[^A-Za-z0-9_-]/g, ""), await req.arrayBuffer());
      return new Response("ok");
    }
    if (p === "/setup") {
      const r = await c.tg("setWebhook", { url: url.origin + "/tg", secret_token: c.secret, allowed_updates: ["message", "callback_query"] });
      return Response.json({ webhook_set: r.ok, info: r.description });
    }

    // ---- dashboard ----
    if (p === "/api/login" && req.method === "POST") {
      const { password } = await req.json().catch(() => ({}));
      if (!env.DASH_PASSWORD || password !== env.DASH_PASSWORD) return Response.json({ ok: false }, { status: 401 });
      return Response.json({ ok: true }, { headers: { "Set-Cookie": `dash=${await c.cookie()}; Path=/; Max-Age=31536000; HttpOnly; Secure; SameSite=Lax` } });
    }
    if (p === "/" || p === "/app") return new Response(DASHBOARD, { headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" } });
    if (p.startsWith("/api/")) {
      if (!(await loggedIn(req, c))) return Response.json({ error: "login" }, { status: 401 });
      if (p === "/api/state") return Response.json(await dashState(env), { headers: { "cache-control": "no-store" } });
      if (p.startsWith("/api/clip/")) return clipVideo(p.split("/").pop().replace(/\.mp4$/, ""), env, req);
      if (p === "/api/action" && req.method === "POST") return dashboardAction(await req.json(), env, c);
      if (p.startsWith("/api/video/")) return video(p.split("/").pop(), env, c, req);
    }
    return new Response("Not found", { status: 404 });
  },

  async scheduled(event, env, ctx) {
    const c = await ctxFor(env);
    const st = await liveState(env);
    const now = Date.now();
    // 1) post job at each approved slot (and for "post now")
    for (const e of st.entries || []) {
      const t = Date.parse(e.slot_iso);
      const due = e.status === "approved" && ((t <= now && now < t + 60 * 60000) || e.post_now);
      if (!due) continue;
      const k = "d:" + e.slot_key + (e.post_now ? ":now" : "");
      if (await env.KV.get(k)) continue;
      if (await dispatch(env, "post.yml")) await env.KV.put(k, "1", { expirationTtl: 2 * 86400 });
      break; // one per run
    }
    // 2) replacement job after notes / redo / moves
    const need = await env.KV.get("needjob");
    if (need) {
      const last = Number((await env.KV.get("lastjob")) || 0);
      if (now - last > 6 * 60000 && (await dispatch(env, "replies.yml"))) {
        await env.KV.put("lastjob", String(now), { expirationTtl: 86400 });
        await env.KV.delete("needjob");
      }
    }
  },
};

// ---------- helpers ----------
async function ctxFor(env) {
  const raw = env.BOT_TOKEN || "";
  const token = (raw.match(/\d{6,}:[A-Za-z0-9_-]{30,}/) || [raw.trim()])[0];
  const secret = token ? await sha256(token) : "";
  const tg = (m, body) =>
    fetch(`https://api.telegram.org/bot${token}/${m}`, {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body),
    }).then((r) => r.json()).catch(() => ({ ok: false }));
  return { token, secret, tg, cookie: async () => sha256("dash|" + (env.DASH_PASSWORD || "") + "|" + secret) };
}
const no = () => new Response("no", { status: 403 });
const bearer = (req, c) => req.headers.get("Authorization") === "Bearer " + c.secret;
async function loggedIn(req, c) {
  const m = (req.headers.get("Cookie") || "").match(/(?:^|;\s*)dash=([a-f0-9]+)/);
  return !!m && m[1] === (await c.cookie());
}
async function sha256(s) {
  const b = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return [...new Uint8Array(b)].map((x) => x.toString(16).padStart(2, "0")).join("");
}
const setStatus = (cap, s) => (cap || "").replace(/Status: [^\n]*/, "Status: " + s);
const fmtSlot = (iso, tz) =>
  new Date(iso).toLocaleString("en-US", { timeZone: tz || "America/New_York", weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });

async function dispatch(env, workflow, inputs) {
  if (!env.GH_TOKEN || !env.GH_REPO) return false;
  const r = await fetch(`https://api.github.com/repos/${env.GH_REPO}/actions/workflows/${workflow}/dispatches`, {
    method: "POST",
    headers: { Authorization: `Bearer ${env.GH_TOKEN}`, Accept: "application/vnd.github+json", "User-Agent": "lexi-reels-relay" },
    body: JSON.stringify({ ref: "main", inputs: inputs || {} }),
  });
  return r.status === 204;
}

async function listPrefix(env, prefix, offset, numeric) {
  const off = parseInt(offset || "0", 10);
  const out = [];
  let cursor;
  do {
    const l = await env.KV.list({ prefix, cursor });
    for (const k of l.keys) {
      const id = parseInt(k.name.slice(prefix.length), 10);
      if (numeric && id < off) await env.KV.delete(k.name);
      else {
        const v = await env.KV.get(k.name);
        if (v) out.push(JSON.parse(v));
      }
    }
    cursor = l.list_complete ? null : l.cursor;
  } while (cursor);
  out.sort((a, b) => a.update_id - b.update_id);
  return Response.json(out);
}

async function listActions(env, after) {
  const out = [];
  let cursor;
  do {
    const l = await env.KV.list({ prefix: "a:", cursor });
    for (const k of l.keys) {
      if (after && k.name <= after) await env.KV.delete(k.name);
      else {
        const v = await env.KV.get(k.name);
        if (v) out.push({ ...JSON.parse(v), key: k.name });
      }
    }
    cursor = l.list_complete ? null : l.cursor;
  } while (cursor);
  out.sort((a, b) => (a.key < b.key ? -1 : 1));
  return Response.json(out);
}

// state from the jobs + changes made since (dashboard or Telegram), so the page is always current
async function liveState(env) {
  const st = JSON.parse((await env.KV.get("state")) || '{"entries":[]}');
  const ov = JSON.parse((await env.KV.get("overlay")) || "{}");
  const gen = Date.parse(st.generated_at || 0) || 0;
  for (const e of st.entries || []) {
    const o = ov[e.id];
    if (o && o.at > gen) Object.assign(e, o.patch, { pending_sync: true });
  }
  return st;
}
async function listIds(env, prefix) {
  const out = [];
  let cursor;
  do {
    const l = await env.KV.list({ prefix, cursor });
    for (const k of l.keys) out.push(k.name.slice(prefix.length));
    cursor = l.list_complete ? null : l.cursor;
  } while (cursor);
  return out;
}
// state for the page: entries + clips, both with the changes made on the dashboard since the last push
async function dashState(env) {
  const st = await liveState(env);
  const gen = Date.parse(st.generated_at || 0) || 0;
  const ov = JSON.parse((await env.KV.get("clipov")) || "{}");
  for (const cl of st.clips || []) {
    const o = ov[cl.id];
    if (o && o.at > gen) Object.assign(cl, o.patch, { pending_sync: true });
  }
  st.clip_previews = await listIds(env, "c:");
  return st;
}
async function overlay(env, id, patch) {
  const ov = JSON.parse((await env.KV.get("overlay")) || "{}");
  const cut = Date.now() - 3 * 86400000;
  for (const k of Object.keys(ov)) if (ov[k].at < cut) delete ov[k];
  ov[id] = { at: Date.now(), patch: { ...((ov[id] || {}).patch || {}), ...patch } };
  await env.KV.put("overlay", JSON.stringify(ov));
}

function captionFor(e, statusText, tz) {
  const kind = e.post_type === "trial" ? "TRIAL reel" : "Regular reel";
  const mu = e.music ? `\n🎵 ${e.music.title} — ${e.music.creator}` : "";
  return `${e.slot_label || fmtSlot(e.slot_iso, tz)} · ${kind}\n${e.item_id} on ${e.clip_id}${mu}\nStatus: ${statusText}\n\n` +
    "Tap ✅ to post it or ❌ to skip. Reply to this video with any notes (text placement, timing).";
}
const buttons = (id) => ({ inline_keyboard: [[{ text: "✅ Approve", callback_data: `a:${id}` }, { text: "❌ Skip", callback_data: `s:${id}` }]] });

// ---------- Telegram ----------
async function telegramWebhook(req, env, c, ctx) {
  if (req.headers.get("X-Telegram-Bot-Api-Secret-Token") !== c.secret) return no();
  const u = await req.json();
  await env.KV.put("u:" + String(u.update_id).padStart(12, "0"), JSON.stringify(u), { expirationTtl: 7 * 86400 });
  const cq = u.callback_query;
  if (cq) {
    const [k, id] = (cq.data || "").split(":");
    const ok = k === "a";
    await c.tg("answerCallbackQuery", { callback_query_id: cq.id, text: ok ? "Approved ✅ it will post at its time" : "Skipped ❌" });
    const m = cq.message;
    if (m && m.caption) {
      const cap = setStatus(m.caption, ok ? STATUS.approved : STATUS.skipped);
      if (cap !== m.caption) await c.tg("editMessageCaption", { chat_id: m.chat.id, message_id: m.message_id, caption: cap, reply_markup: m.reply_markup });
    }
    if (id) await overlay(env, id, { status: ok ? "approved" : "skipped" });
  }
  const msg = u.message;
  const r = msg && msg.reply_to_message;
  if (r && r.caption && /Status:/.test(r.caption) && msg.text) {
    const t = msg.text.toLowerCase().trim().replace(/^[ .!]+|[ .!]+$/g, "");
    const st = JSON.parse((await env.KV.get("state")) || '{"entries":[]}');
    const e = (st.entries || []).find((x) => x.msg_id === r.message_id);
    if (APPROVE.includes(t)) {
      await c.tg("editMessageCaption", { chat_id: msg.chat.id, message_id: r.message_id, caption: setStatus(r.caption, STATUS.approved), reply_markup: r.reply_markup });
      await c.tg("sendMessage", { chat_id: msg.chat.id, reply_to_message_id: msg.message_id, text: "Approved ✅ it will post at its time." });
      if (e) await overlay(env, e.id, { status: "approved" });
    } else if (/📤 posted/.test(r.caption)) {
      await c.tg("sendMessage", { chat_id: msg.chat.id, reply_to_message_id: msg.message_id, text: "Got it 👍 notes saved for the next fixes." });
    } else {
      await c.tg("editMessageCaption", { chat_id: msg.chat.id, message_id: r.message_id, caption: setStatus(r.caption, "❌ skipped (your notes) — new version coming"), reply_markup: r.reply_markup });
      await c.tg("sendMessage", { chat_id: msg.chat.id, reply_to_message_id: msg.message_id, text: "Got it 👍 notes saved. This one is skipped and a new version with the same topic + clip is coming (about 10 min). If it's about where or when the boxes pop up, set that clip on the dashboard's Clips tab first — the new version uses it." });
      if (e) await overlay(env, e.id, { status: "skipped", needs_replacement: true, feedback: [...(e.feedback || []), msg.text] });
      await env.KV.put("needjob", "1", { expirationTtl: 86400 });
    }
  }
  return new Response("ok");
}

// ---------- dashboard actions ----------
async function topicAction(a, env) {
  const st = JSON.parse((await env.KV.get("state")) || '{"entries":[]}');
  const t = st.topics || { review: [], counts: { winner: {}, new: {} } };
  const it = (t.review || []).find((x) => x.id === a.item_id);
  if (!it) return Response.json({ ok: false, error: "topic not found" }, { status: 404 });
  const key = "a:" + new Date().toISOString() + ":" + Math.random().toString(36).slice(2, 6);
  await env.KV.put(key, JSON.stringify({ kind: a.kind, item_id: a.item_id, at: new Date().toISOString() }), { expirationTtl: 14 * 86400 });
  t.review = t.review.filter((x) => x.id !== a.item_id);
  if (a.kind === "topic_keep") {
    t.counts.new.active = (t.counts.new.active || 0) + 1;
    t.counts.new.free = (t.counts.new.free || 0) + 1;
  }
  st.topics = t;
  await env.KV.put("state", JSON.stringify(st));
  return Response.json({ ok: true });
}

// Clips tab: where/when the boxes pop, title height, trim — saved to clips.yaml by the next job
async function clipAction(a, env, c) {
  const st = JSON.parse((await env.KV.get("state")) || '{"entries":[]}');
  const cl = (st.clips || []).find((x) => x.id === a.clip_id);
  if (!cl) return Response.json({ ok: false, error: "clip not found" }, { status: 404 });
  const num = (v, lo, hi) => (v === null || v === undefined || v === "" || isNaN(Number(v)) ? null : Math.min(hi, Math.max(lo, Number(v))));
  const patch = {};
  if ("events" in a) {
    patch.events_manual = a.events === null ? null : (Array.isArray(a.events) ? a.events : []).slice(0, 6).map((e) => ({
      t: +(num(e.t, 0, 600) ?? 0).toFixed(2), dir: ["up", "center", "down"].includes(e.dir) ? e.dir : "center",
      x: +(num(e.x, 0, 1) ?? 0.5).toFixed(3), y: +(num(e.y, 0, 1) ?? 0.6).toFixed(3),
    })).sort((p, q) => p.t - q.t);
  }
  if ("title_y" in a) patch.title_y = num(a.title_y, 0.1, 0.9);
  if ("start" in a) patch.start = num(a.start, 0, 600) || 0;
  if ("end" in a) patch.end = num(a.end, 0.5, 600);
  const key = "a:" + new Date().toISOString() + ":" + Math.random().toString(36).slice(2, 6);
  await env.KV.put(key, JSON.stringify({ kind: "clip_edit", clip_id: a.clip_id, patch, at: new Date().toISOString() }), { expirationTtl: 14 * 86400 });
  const ov = JSON.parse((await env.KV.get("clipov")) || "{}");
  const cut = Date.now() - 3 * 86400000;
  for (const k of Object.keys(ov)) if (ov[k].at < cut) delete ov[k];
  ov[a.clip_id] = { at: Date.now(), patch: { ...((ov[a.clip_id] || {}).patch || {}), ...patch } };
  await env.KV.put("clipov", JSON.stringify(ov));
  if (st.chat_id) await c.tg("sendMessage", { chat_id: st.chat_id, text: `✏️ ${a.clip_id}: settings saved from the dashboard — used from the next render on.` });
  return Response.json({ ok: true });
}

async function clipVideo(id, env, req) {
  const v = await env.KV.get("c:" + id, "arrayBuffer");
  if (!v) return new Response("no preview", { status: 404 });
  const total = v.byteLength, range = req.headers.get("Range");
  const h = new Headers({ "content-type": "video/mp4", "accept-ranges": "bytes", "cache-control": "private, max-age=86400" });
  if (range) { // iPhone Safari needs 206 answers
    const m = range.match(/bytes=(\d*)-(\d*)/);
    const s = m && m[1] ? +m[1] : 0, e = Math.min(m && m[2] ? +m[2] : total - 1, total - 1);
    h.set("content-range", `bytes ${s}-${e}/${total}`);
    h.set("content-length", String(e - s + 1));
    return new Response(v.slice(s, e + 1), { status: 206, headers: h });
  }
  h.set("content-length", String(total));
  return new Response(v, { headers: h });
}

async function dashboardAction(a, env, c) {
  if (a && (a.kind === "topic_keep" || a.kind === "topic_toss") && a.item_id) return topicAction(a, env);
  if (a && a.kind === "clip_edit" && a.clip_id) return clipAction(a, env, c);
  if (a && a.kind === "new_ideas") { // ✨ Topics tab: have Claude write new topics into the review list
    const job = (await dispatch(env, "ideas.yml")) ? "started" : "unavailable";
    return Response.json({ ok: job === "started", job, error: job === "started" ? undefined : "the relay can't start GitHub jobs (GH_DISPATCH_TOKEN not set)" }, { status: job === "started" ? 200 : 503 });
  }
  const kinds = ["approve", "skip", "note", "swap_music", "new_clip", "new_topic", "never_topic", "move", "post_now", "swap_type", "text_edit"];
  if (!a || !kinds.includes(a.kind) || !a.entry_id) return Response.json({ ok: false, error: "bad action" }, { status: 400 });
  const st = await liveState(env);
  const e = (st.entries || []).find((x) => x.id === a.entry_id);
  if (!e) return Response.json({ ok: false, error: "reel not found" }, { status: 404 });
  if (e.status === "posted") return Response.json({ ok: false, error: "already posted" }, { status: 400 });
  if (a.kind === "note" && !(a.text || "").trim()) return Response.json({ ok: false, error: "empty note" }, { status: 400 });
  let hook = null, beats = null;
  if (a.kind === "text_edit") { // title + fact lines edited on the card; saved for the topic by the next job
    hook = String(a.hook || "").trim().slice(0, 200);
    beats = (Array.isArray(a.beats) ? a.beats : []).map((b) => String(b || "").trim().slice(0, 160)).filter(Boolean).slice(0, 8);
    if (!hook) return Response.json({ ok: false, error: "the title can't be empty" }, { status: 400 });
    if (hook === (e.title || "") && JSON.stringify(beats) === JSON.stringify(e.beats || [])) return Response.json({ ok: false, error: "nothing changed" }, { status: 400 });
  }
  if (a.kind === "post_now" && e.status !== "approved") return Response.json({ ok: false, error: "approve it first" }, { status: 400 });
  if (a.kind === "move" && !(a.slot_iso && Date.parse(a.slot_iso) > Date.now())) return Response.json({ ok: false, error: "pick a future time" }, { status: 400 });

  const key = "a:" + new Date().toISOString() + ":" + Math.random().toString(36).slice(2, 6);
  const rec = { entry_id: a.entry_id, kind: a.kind, text: (a.text || "").slice(0, 2000), slot_iso: a.slot_iso || null, at: new Date().toISOString() };
  if (hook !== null) Object.assign(rec, { hook, beats });
  await env.KV.put(key, JSON.stringify(rec), { expirationTtl: 14 * 86400 });

  const chat = st.chat_id, tz = st.timezone;
  let patch = {}, statusText = null, say = null;
  if (a.kind === "approve") { patch = { status: "approved" }; statusText = STATUS.approved; }
  if (a.kind === "skip") { patch = { status: "skipped" }; statusText = STATUS.skipped; }
  if (a.kind === "note") {
    patch = { status: "skipped", needs_replacement: true, feedback: [...(e.feedback || []), rec.text] };
    statusText = "❌ skipped (your notes) — new version coming";
    say = `📝 Notes from the dashboard:\n${rec.text}\n\nSkipped — a new version with the same topic + clip is coming (about 10 min).`;
  }
  if (a.kind === "text_edit") {
    patch = { status: "skipped", needs_replacement: true, redo: "text_edit", title: hook, beats, text_edited: true };
    statusText = "❌ skipped (your text edit) — new version coming";
    say = `✏️ Text edited on the dashboard:\n${hook}\n${beats.map((b) => "• " + b).join("\n")}\n\nRe-drawing the same reel with the new words (about 10 min). This wording is saved for the topic from now on.`;
  } else if (REDO[a.kind]) {
    patch = { status: "skipped", needs_replacement: true, redo: a.kind };
    if (a.kind === "never_topic") {
      const t = st.topics || {}, kind = (e.item_id || "").startsWith("pr-") ? "winner" : "new", cnt = ((t.counts || {})[kind]) || {};
      if (cnt.active) cnt.active -= 1;
      if (cnt.free) cnt.free -= 1;
      await env.KV.put("state", JSON.stringify(st));
    }
    statusText = `❌ skipped (${REDO[a.kind]}) — new version coming`;
    say = `🔁 From the dashboard: making a new version with ${REDO[a.kind]} (about 10 min).`;
  }
  if (a.kind === "move") {
    const label = fmtSlot(a.slot_iso, tz);
    patch = { status: "pending", slot_iso: new Date(a.slot_iso).toISOString(), slot_label: label, post_now: false };
    statusText = STATUS.pending;
    say = `🕑 Moved from the dashboard to ${label}. Approve it again when you're ready.`;
  }
  if (a.kind === "post_now") { patch = { post_now: true }; say = "🚀 Posting this one now (from the dashboard)."; }
  if (a.kind === "swap_type") { // trial <-> regular; same video, re-captioned at post time
    const nt = e.post_type === "trial" ? "regular" : "trial";
    patch = { post_type: nt, status: "pending", post_now: false };
    statusText = STATUS.pending;
    say = `🔄 Switched to a ${nt === "trial" ? "TRIAL" : "regular"} reel from the dashboard. Approve it again when you're ready.`;
  }
  await overlay(env, e.id, patch);

  if (chat && e.msg_id) {
    if (statusText) await c.tg("editMessageCaption", { chat_id: chat, message_id: e.msg_id, caption: captionFor({ ...e, ...patch }, statusText, tz), reply_markup: buttons(e.id) });
    if (say) await c.tg("sendMessage", { chat_id: chat, reply_to_message_id: e.msg_id, text: say });
  }
  let job = null;
  if (a.kind === "note" || REDO[a.kind] || a.kind === "move") {
    job = (await dispatch(env, "replies.yml")) ? "started" : "queued";
    if (job === "started") await env.KV.put("lastjob", String(Date.now()), { expirationTtl: 86400 });
    else await env.KV.put("needjob", "1", { expirationTtl: 86400 });
  }
  if (a.kind === "post_now") {
    job = (await dispatch(env, "post.yml")) ? "started" : "queued";
    if (job === "started") await env.KV.put("d:" + e.slot_key + ":now", "1", { expirationTtl: 2 * 86400 });
  }
  return Response.json({ ok: true, job });
}

async function video(id, env, c, req) {
  const st = await liveState(env);
  const e = (st.entries || []).find((x) => x.id === id);
  if (!e || !e.file_id) return new Response("no video", { status: 404 });
  let path = await env.KV.get("fp:" + e.file_id);
  if (!path) {
    const f = await c.tg("getFile", { file_id: e.file_id });
    if (!f.ok) return new Response("video unavailable", { status: 404 });
    path = f.result.file_path;
    await env.KV.put("fp:" + e.file_id, path, { expirationTtl: 3000 }); // Telegram file links last ~1 h
  }
  const range = req.headers.get("Range");
  const r = await fetch(`https://api.telegram.org/file/bot${c.token}/${path}`, { headers: range ? { Range: range } : {} });
  const h = new Headers({ "content-type": "video/mp4", "cache-control": "private, max-age=3600", "accept-ranges": "bytes" });
  for (const k of ["content-length", "content-range"]) if (r.headers.get(k)) h.set(k, r.headers.get(k));
  return new Response(r.body, { status: r.status, headers: h }); // 206 for range requests (needed by iPhone Safari)
}

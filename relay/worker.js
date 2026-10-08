// Telegram relay for lexi_reels_bot (Cloudflare Worker).
// Answers ✅/❌ taps instantly, and keeps every update in KV so the GitHub jobs
// can read them later from /updates (Telegram allows a webhook OR polling, not both).
// Needs: KV binding "KV", secret "BOT_TOKEN". Visit /setup once after deploying.
const APPROVE = ["approve", "approved", "yes", "post", "post it", "ok", "good", "✅", "👍"];

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    const token = ((env.BOT_TOKEN || "").match(/\d{6,}:[A-Za-z0-9_-]{30,}/) || [(env.BOT_TOKEN || "").trim()])[0];
    if (!token) return new Response("BOT_TOKEN secret missing", { status: 500 });
    const secret = await sha256(token);
    const tg = (m, body) =>
      fetch(`https://api.telegram.org/bot${token}/${m}`, {
        method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body),
      }).then((r) => r.json());
    const status = (cap, s) => (cap || "").replace(/Status: [^\n]*/, "Status: " + s);

    if (url.pathname === "/tg" && req.method === "POST") {
      if (req.headers.get("X-Telegram-Bot-Api-Secret-Token") !== secret) return new Response("no", { status: 403 });
      const u = await req.json();
      await env.KV.put("u:" + String(u.update_id).padStart(12, "0"), JSON.stringify(u), { expirationTtl: 7 * 86400 });
      const cq = u.callback_query;
      if (cq) {
        const ok = (cq.data || "").startsWith("a:");
        await tg("answerCallbackQuery", { callback_query_id: cq.id, text: ok ? "Approved ✅ it will post at its time" : "Skipped ❌" });
        const m = cq.message;
        if (m && m.caption) {
          const cap = status(m.caption, ok ? "✅ approved — will post" : "❌ skipped");
          if (cap !== m.caption)
            await tg("editMessageCaption", { chat_id: m.chat.id, message_id: m.message_id, caption: cap, reply_markup: m.reply_markup });
        }
      }
      const msg = u.message;
      const r = msg && msg.reply_to_message;
      if (r && r.caption && /Status:/.test(r.caption) && msg.text) {
        const t = msg.text.toLowerCase().trim().replace(/^[ .!]+|[ .!]+$/g, "");
        if (APPROVE.includes(t)) {
          await tg("editMessageCaption", { chat_id: msg.chat.id, message_id: r.message_id, caption: status(r.caption, "✅ approved — will post"), reply_markup: r.reply_markup });
          await tg("sendMessage", { chat_id: msg.chat.id, reply_to_message_id: msg.message_id, text: "Approved ✅ it will post at its time." });
        } else if (/📤 posted/.test(r.caption)) {
          await tg("sendMessage", { chat_id: msg.chat.id, reply_to_message_id: msg.message_id, text: "Got it 👍 notes saved for the next fixes." });
        } else {
          await tg("editMessageCaption", { chat_id: msg.chat.id, message_id: r.message_id, caption: status(r.caption, "❌ skipped (your notes) — replacement coming"), reply_markup: r.reply_markup });
          await tg("sendMessage", { chat_id: msg.chat.id, reply_to_message_id: msg.message_id, text: "Got it 👍 notes saved. This one is skipped and a replacement is coming (within ~30 min)." });
        }
      }
      return new Response("ok");
    }

    if (url.pathname === "/updates") {
      if (req.headers.get("Authorization") !== "Bearer " + secret) return new Response("no", { status: 403 });
      const off = parseInt(url.searchParams.get("offset") || "0", 10);
      const out = [];
      let cursor;
      do {
        const l = await env.KV.list({ prefix: "u:", cursor });
        for (const k of l.keys) {
          const id = parseInt(k.name.slice(2), 10);
          if (id < off) await env.KV.delete(k.name);
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

    if (url.pathname === "/setup") {
      const r = await tg("setWebhook", { url: url.origin + "/tg", secret_token: secret, allowed_updates: ["message", "callback_query"] });
      return Response.json({ webhook_set: r.ok, info: r.description });
    }
    return new Response("Lexi reels relay is running.");
  },
};

async function sha256(s) {
  const b = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return [...new Uint8Array(b)].map((x) => x.toString(16).padStart(2, "0")).join("");
}

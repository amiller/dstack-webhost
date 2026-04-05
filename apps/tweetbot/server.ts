// tweetbot.ts — Hermes Notebook → Twitter bridge on the tee-daemon
// Polls hermes.teleport.computer, tweets visible entries, synthesizes aiOnly with free LLM

const NOTEBOOK_API = "https://hermes.teleport.computer/api/entries";
const POLL_INTERVAL_MS = 20 * 60 * 1000;
const TWEET_MAX_LEN = 280;
const OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions";
const FREE_MODEL = "google/gemini-2.0-flash-exp:free";

// ── State ──
let credsReady = false;
let X_API_KEY=***
let X_API_SECRET=***
let X_ACCESS_TOKEN=***
let X_ACCESS_TOKEN_SECRET=***
let OPENROUTER_API_KEY=***
let savedCursor: string | null = null;
const TWEET_COOLDOWN_MS = 60 * 60 * 1000; // 1 hour between tweets
let lastTweetTime = 0;

interface Tweetable {
  type: "direct" | "synth";
  entry: { pseudonym: string; content?: string; keywords?: string[]; topicHints?: string[] };
  text: string;           // pre-formatted for direct, placeholder for synth
  timestamp: number;
}

const tweetQueue: Tweetable[] = [];

interface TweetRecord {
  id: string;
  text: string;
  source: "notebook" | "synthesized" | "manual";
  pseudonym: string;
  timestamp: number;
}

interface PollRecord {
  timestamp: number;
  totalNew: number;
  tweeted: number;
  queued: number;
  skipped: number;
  error?: string;
}

const recentTweets: TweetRecord[] = [];
const recentPolls: PollRecord[] = [];
const recentEntries: Array<{
  id: string;
  pseudonym: string;
  content: string;
  timestamp: number;
  aiOnly: boolean;
  topicHints?: string[];
  keywords: string[];
  tweeted: boolean;
  queued: boolean;
}> = [];

// ── OAuth 1.0a signing ──
function percentEncode(s: string): string {
  return encodeURIComponent(s).replace(/[!'()*]/g, (c) =>
    "%" + c.charCodeAt(0).toString(16).toUpperCase()
  );
}

async function hmacSha1(key: string, data: string): Promise<string> {
  const enc = new TextEncoder();
  const cryptoKey = await crypto.subtle.importKey(
    "raw", enc.encode(key),
    { name: "HMAC", hash: "SHA-1" }, false, ["sign"]
  );
  const sig = await crypto.subtle.sign("HMAC", cryptoKey, enc.encode(data));
  return btoa(String.fromCharCode(...new Uint8Array(sig)));
}

async function signRequest(method: string, url: string): Promise<string> {
  const oauth: Record<string, string> = {
    oauth_consumer_key: X_API_KEY,
    oauth_nonce: crypto.randomUUID().replace(/-/g, ""),
    oauth_signature_method: "HMAC-SHA1",
    oauth_timestamp: Math.floor(Date.now() / 1000).toString(),
    oauth_token: X_ACCESS_TOKEN,
    oauth_version: "1.0",
  };
  const paramStr = Object.keys(oauth)
    .sort()
    .map((k) => `${percentEncode(k)}=${percentEncode(oauth[k])}`)
    .join("&");
  const baseString = `${method.toUpperCase()}&${percentEncode(url)}&${percentEncode(paramStr)}`;
  const signingKey = `${percentEncode(X_API_SECRET)}&${percentEncode(X_ACCESS_TOKEN_SECRET)}`;
  const signature = await hmacSha1(signingKey, baseString);
  const headerParams = { ...oauth, oauth_signature: signature };
  const headerStr = Object.keys(headerParams)
    .sort()
    .map((k) => `${percentEncode(k)}="${percentEncode(headerParams[k])}"`)
    .join(", ");
  return `OAuth ${headerStr}`;
}

async function postTweet(text: string): Promise<{ id: string } | null> {
  const url = "https://api.x.com/2/tweets";
  const authHeader = await signRequest("POST", url);
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Authorization": authHeader, "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });
  const body = await resp.json();
  if (!resp.ok) {
    console.error(`Tweet failed [${resp.status}]:`, JSON.stringify(body));
    return null;
  }
  console.log(`Tweeted: ${body.data?.id} — "${text.slice(0, 60)}..."`);
  return body.data;
}

// ── aiOnly synthesis via free Gemini ──
async function synthesizeTweet(keywords: string[], topicHints?: string[]): Promise<string | null> {
  if (!OPENROUTER_API_KEY) {
    console.error("No OPENROUTER_API_KEY, can't synthesize");
    return null;
  }
  const hints = topicHints?.length ? `\nTopics: ${topicHints.join(", ")}` : "";
  const prompt = `You are @teleport_router, a Hermes Notebook bot. Write ONE tweet (max 270 chars, no hashtags, no quotes, just the text) summarizing what was worked on based on these keywords:${hints}\n\nKeywords: ${keywords.slice(0, 30).join(", ")}`;

  try {
    const resp = await fetch(OPENROUTER_URL, {
      method: "POST",
      headers: {
        "Authorization": `Bearer ${OPENROUTER_API_KEY}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        model: FREE_MODEL,
        messages: [{ role: "user", content: prompt }],
        max_tokens: 80,
        temperature: 0.8,
      }),
    });
    const body = await resp.json();
    if (!resp.ok) {
      console.error("OpenRouter error:", JSON.stringify(body).slice(0, 200));
      return null;
    }
    let text = body.choices?.[0]?.message?.content?.trim() || "";
    // Strip quotes if the model wraps them
    if (text.startsWith('"') && text.endsWith('"')) text = text.slice(1, -1);
    if (text.length > TWEET_MAX_LEN) text = text.slice(0, TWEET_MAX_LEN - 3) + "...";
    return text;
  } catch (e) {
    console.error("Synthesis failed:", e);
    return null;
  }
}

// ── Notebook polling ──
interface Entry {
  id: string;
  pseudonym: string;
  content: string;
  timestamp: number;
  keywords: string[];
  handle?: string;
  isReflection?: boolean;
  humanVisible?: boolean;
  aiOnly?: boolean;
  topicHints?: string[];
  inReplyTo?: string;
}

function truncateForDisplay(content: string, max = 180): string {
  const clean = content.replace(/\n/g, " ").replace(/\s+/g, " ").trim();
  return clean.length > max ? clean.slice(0, max) + "..." : clean;
}

function formatTweetFromContent(entry: Entry): string {
  const content = entry.content.replace(/\n/g, " ").replace(/\s+/g, " ").trim();
  const prefix = `📓 ${entry.pseudonym}: `;
  const maxContent = TWEET_MAX_LEN - prefix.length - 3;
  return `${prefix}${content.length > maxContent ? content.slice(0, maxContent) + "..." : content}`;
}

async function pollAndTweet(): Promise<PollRecord> {
  const now = Date.now();
  const record: PollRecord = { timestamp: now, totalNew: 0, tweeted: 0, skipped: 0 };

  if (!credsReady) { record.error = "creds not ready"; return record; }

  let url: string;
  if (savedCursor) {
    url = `${NOTEBOOK_API}?cursor=${encodeURIComponent(savedCursor)}&limit=5`;
  } else {
    url = `${NOTEBOOK_API}?limit=1`;
  }

  console.log(`[${new Date().toISOString()}] Polling...`);

  let entries: Entry[];
  let nextCursor: string;
  try {
    const resp = await fetch(url);
    if (!resp.ok) { record.error = `notebook [${resp.status}]`; return record; }
    const data = await resp.json();
    entries = data.entries || [];
    nextCursor = data.nextCursor;
  } catch (e) {
    record.error = `fetch: ${e}`;
    return record;
  }

  if (!savedCursor) {
    if (nextCursor) savedCursor = nextCursor;
    console.log("Initial cursor saved.");
    record.totalNew = entries.length;
    record.skipped = entries.length;
    return record;
  }

  record.totalNew = entries.length;

  if (entries.length === 0) {
    if (nextCursor) savedCursor = nextCursor;
    return record;
  }

  // Phase 1: categorize and queue all tweetable entries
  for (const entry of entries) {
    const rec = {
      id: entry.id,
      pseudonym: entry.pseudonym,
      content: entry.content || "",
      timestamp: entry.timestamp,
      aiOnly: !!entry.aiOnly || entry.humanVisible === false,
      topicHints: entry.topicHints,
      keywords: entry.keywords || [],
      tweeted: false,
      queued: false,
    };
    recentEntries.unshift(rec);

    if (entry.content && entry.humanVisible !== false && !entry.aiOnly) {
      tweetQueue.push({ type: "direct", entry: { pseudonym: entry.pseudonym, content: entry.content }, text: formatTweetFromContent(entry), timestamp: entry.timestamp });
      rec.queued = true;
      record.queued++;
    } else if (entry.keywords?.length) {
      tweetQueue.push({ type: "synth", entry: { pseudonym: entry.pseudonym, keywords: entry.keywords, topicHints: entry.topicHints }, text: "", timestamp: entry.timestamp });
      rec.queued = true;
      record.queued++;
    } else {
      record.skipped++;
    }
  }

  // Phase 2: tweet if cooldown allows and queue has items
  const cooldownLeft = TWEET_COOLDOWN_MS - (now - lastTweetTime);
  if (tweetQueue.length > 0 && cooldownLeft <= 0) {
    const pick = tweetQueue.shift()!;
    let tweetText = pick.text;

    if (pick.type === "synth") {
      tweetText = await synthesizeTweet(pick.entry.keywords!, pick.entry.topicHints) || "";
    }

    if (tweetText) {
      const result = await postTweet(tweetText);
      if (result) {
        lastTweetTime = now;
        record.tweeted++;
        const src = pick.type === "synth" ? "synthesized" : "notebook";
        recentTweets.unshift({ id: result.id, text: tweetText, source: src, pseudonym: pick.entry.pseudonym, timestamp: now });
        // Mark in recentEntries
        const match = recentEntries.find(e => e.pseudonym === pick.entry.pseudonym && Math.abs(e.timestamp - pick.timestamp) < 5000);
        if (match) match.tweeted = true;
      } else {
        // Put it back if tweet failed
        tweetQueue.unshift(pick);
        record.skipped++;
      }
    } else {
      record.skipped++;
    }
  }

  // Drain stale queue items (older than 4 hours, never tweet them)
  while (tweetQueue.length > 0 && (now - tweetQueue[0].timestamp) > 4 * 60 * 60 * 1000) {
    tweetQueue.shift();
  }
  // Cap queue
  if (tweetQueue.length > 20) tweetQueue.splice(0, tweetQueue.length - 20);

  // Keep bounded
  if (recentTweets.length > 50) recentTweets.length = 50;
  if (recentEntries.length > 100) recentEntries.length = 100;

  if (nextCursor) savedCursor = nextCursor;
  return record;
}

// ── Dashboard HTML ──
function dashboardHTML(): string {
  const lastPoll = recentPolls[0];
  const cooldownLeft = lastTweetTime ? Math.max(0, TWEET_COOLDOWN_MS - (Date.now() - lastTweetTime)) : 0;
  const nextTweet = cooldownLeft > 0 && lastTweetTime ? new Date(Date.now() + cooldownLeft).toLocaleTimeString() : "now";
  const lastPollTime = lastPoll ? new Date(lastPoll.timestamp).toLocaleTimeString() : "never";
  const totalTweeted = recentTweets.length;

  const entriesHTML = recentEntries.slice(0, 12).map(e => {
    const display = e.aiOnly
      ? (e.topicHints?.join(", ") || e.keywords.slice(0, 8).join(", "))
      : truncateForDisplay(e.content);
    const badge = e.aiOnly
      ? e.tweeted
        ? `<span class="badge done">tweeted</span>`
        : e.queued
          ? `<span class="badge ai">queued</span>`
          : `<span class="badge skip">aiOnly</span>`
      : e.tweeted
        ? `<span class="badge done">tweeted</span>`
        : e.queued
          ? `<span class="badge ai">queued</span>`
          : `<span class="badge skip">skipped</span>`;
    return `<div class="entry">
      <div class="entry-header">
        <span class="pseudonym">${e.pseudonym}</span>
        ${badge}
      </div>
      <div class="entry-content">${escapeHtml(display)}</div>
      <div class="entry-meta">${timeAgo(e.timestamp)}${e.aiOnly && e.topicHints?.length ? ` · ${e.topicHints.map(t => `<span class="hint">${t}</span>`).join(" ")}` : ""}</div>
    </div>`;
  }).join("");

  const tweetsHTML = recentTweets.slice(0, 8).map(t => {
    const src = t.source === "synthesized"
      ? `<span class="synth">synthesized</span>`
      : t.source === "manual"
        ? `<span class="direct">manual</span>`
        : `<span class="direct">notebook</span>`;
    return `<div class="tweet">
      <div class="tweet-meta">${src} · @teleport_router · ${timeAgo(t.timestamp)}</div>
      <div class="tweet-text">${escapeHtml(t.text)}</div>
    </div>`;
  }).join("");

  const pollsHTML = recentPolls.slice(0, 5).map(p =>
    `<div class="poll-row ${p.error ? 'error' : ''}">
      <span>${new Date(p.timestamp).toLocaleTimeString()}</span>
      <span>${p.totalNew} new</span>
      <span>${p.tweeted} tweeted</span>
      <span>${p.queued ?? 0} queued</span>
      ${p.error ? `<span class="poll-error">${p.error}</span>` : ""}
    </div>`
  ).join("");

  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>teleport_router — Hermes Notebook Bot</title>
  <meta http-equiv="refresh" content="60">
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body { font-family: 'IBM Plex Mono', 'SF Mono', ui-monospace, monospace; background: #fafaf9; color: #1c1917; line-height: 1.6; }
    .container { max-width: 960px; margin: 0 auto; padding: 32px 24px; }

    header { margin-bottom: 28px; }
    header h1 { font-size: 1.15rem; font-weight: 600; color: #111; }
    header h1 a { color: #111; text-decoration: none; }
    header h1 a:hover { color: #6366f1; }
    header .arrow { color: #d6d3d1; font-weight: 400; }
    header .sub { color: #78716c; font-weight: 400; font-size: 0.95rem; }
    header p { font-size: 0.78rem; color: #a8a29e; margin-top: 6px; }

    .stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-bottom: 28px; }
    .stat { background: #fff; border: 1px solid #e7e5e4; border-radius: 8px; padding: 14px 16px; }
    .stat-label { font-size: 0.65rem; color: #a8a29e; text-transform: uppercase; letter-spacing: 0.06em; font-weight: 500; }
    .stat-value { font-size: 1.1rem; color: #1c1917; font-weight: 600; margin-top: 2px; }

    .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
    @media (max-width: 700px) { .grid { grid-template-columns: 1fr; } .stats { grid-template-columns: repeat(2, 1fr); } }

    .panel { background: #fff; border: 1px solid #e7e5e4; border-radius: 10px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.04); }
    .panel-header { padding: 12px 16px; border-bottom: 1px solid #f5f5f4; font-size: 0.7rem; color: #78716c; text-transform: uppercase; letter-spacing: 0.05em; font-weight: 500; display: flex; justify-content: space-between; align-items: center; }
    .panel-body { padding: 0; max-height: 600px; overflow-y: auto; }

    .entry { padding: 12px 16px; border-bottom: 1px solid #fafaf9; transition: background 0.15s; }
    .entry:hover { background: #fafaf9; }
    .entry:last-child { border-bottom: none; }
    .entry-header { display: flex; align-items: center; gap: 8px; margin-bottom: 4px; }
    .pseudonym { font-size: 0.78rem; color: #57534e; font-weight: 500; }
    .entry-content { font-size: 0.82rem; color: #292524; line-height: 1.55; }
    .entry-meta { font-size: 0.65rem; color: #d6d3d1; margin-top: 4px; }
    .hint { background: #f0eeff; color: #7c6faa; padding: 1px 6px; border-radius: 4px; font-size: 0.6rem; margin-right: 2px; }

    .badge { font-size: 0.58rem; padding: 2px 7px; border-radius: 4px; font-weight: 600; letter-spacing: 0.02em; }
    .badge.done { background: #ecfdf5; color: #059669; }
    .badge.ai { background: #fffbeb; color: #d97706; }
    .badge.skip { background: #f5f5f4; color: #a8a29e; }

    .tweet { padding: 12px 16px; border-bottom: 1px solid #fafaf9; transition: background 0.15s; }
    .tweet:hover { background: #fafaf9; }
    .tweet:last-child { border-bottom: none; }
    .tweet-meta { font-size: 0.65rem; color: #d6d3d1; margin-bottom: 4px; }
    .tweet-meta .synth { color: #d97706; }
    .tweet-meta .direct { color: #059669; }
    .tweet-text { font-size: 0.82rem; color: #1c1917; line-height: 1.55; }

    .polls-panel { margin-top: 16px; }
    .poll-row { display: flex; gap: 16px; padding: 8px 16px; font-size: 0.7rem; color: #a8a29e; border-bottom: 1px solid #fafaf9; }
    .poll-row:last-child { border-bottom: none; }
    .poll-row span { min-width: 60px; }
    .poll-row.error { color: #dc2626; }
    .poll-error { margin-left: auto; color: #dc2626; }

    .empty { padding: 32px; text-align: center; color: #d6d3d1; font-size: 0.8rem; }
    a { color: #6366f1; }
  </style>
</head>
<body>
  <div class="container">
    <header>
      <h1><a href="https://x.com/teleport_router" target="_blank">@teleport_router</a> <span class="arrow">←</span> <span class="sub">Hermes Notebook → Twitter</span></h1>
      <p>Polls hermes.teleport.computer every 20 min · tweets visible entries · synthesizes aiOnly with free Gemini</p>
    </header>

    <div class="stats">
      <div class="stat">
        <div class="stat-label">Status</div>
        <div class="stat-value" style="color: ${credsReady ? '#059669' : '#dc2626'}">${credsReady ? "● running" : "● no creds"}</div>
      </div>
      <div class="stat">
        <div class="stat-label">Last poll</div>
        <div class="stat-value">${lastPollTime}</div>
      </div>
      <div class="stat">
        <div class="stat-label">Next tweet</div>
        <div class="stat-value">${nextTweet}${tweetQueue.length > 0 ? ` <span style="font-size:0.7rem;color:#a8a29e">(${tweetQueue.length} queued)</span>` : ""}</div>
      </div>
      <div class="stat">
        <div class="stat-label">Tweeted</div>
        <div class="stat-value">${totalTweeted}</div>
      </div>
    </div>

    <div class="grid">
      <div class="panel">
        <div class="panel-header">
          <span>Recent Relevant</span>
          <span style="font-size:0.65rem;color:#d6d3d1">${recentEntries.length} tracked</span>
        </div>
        <div class="panel-body">
          ${entriesHTML || '<div class="empty">No entries yet. First poll pending...</div>'}
        </div>
      </div>
      <div class="panel">
        <div class="panel-header">
          <span>Tweets Sent</span>
          <a href="https://x.com/teleport_router" target="_blank" style="text-decoration:none;font-size:0.65rem;color:#a8a29e">view on X →</a>
        </div>
        <div class="panel-body">
          ${tweetsHTML || '<div class="empty">No tweets yet.</div>'}
        </div>
      </div>
    </div>

    <div class="panel polls-panel">
      <div class="panel-header"><span>Poll History</span></div>
      <div class="panel-body">
        ${pollsHTML || '<div class="empty">No polls recorded.</div>'}
      </div>
    </div>
  </div>
</body>
</html>`;
}

function escapeHtml(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function timeAgo(ts: number): string {
  const diff = Date.now() - ts;
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

// ── Background poll loop ──
console.log("tweetbot: loaded, waiting for creds via first request...");

setInterval(async () => {
  if (!credsReady) return;
  try {
    const record = await pollAndTweet();
    recentPolls.unshift(record);
    if (recentPolls.length > 50) recentPolls.length = 50;
  } catch (e) {
    console.error("poll error:", e);
  }
}, POLL_INTERVAL_MS);

// ── HTTP handler ──
export default async function handler(
  req: Request,
  ctx: { env: Record<string, string> }
): Promise<Response> {
  if (!credsReady && ctx.env?.X_API_KEY) {
    X_API_KEY=ctx.en...KEY;
    X_API_SECRET=ctx.en...RET;
    X_ACCESS_TOKEN=ctx.en...KEN;
    X_ACCESS_TOKEN_SECRET=ctx.en...RET;
    OPENROUTER_API_KEY=ctx.en..._KEY || "";
    credsReady = true;
    console.log("tweetbot: creds injected, poll loop active");
    setTimeout(async () => {
      try {
        const record = await pollAndTweet();
        recentPolls.unshift(record);
      } catch (e) { console.error("init poll:", e); }
    }, 5000);
  }

  const url = new URL(req.url);

  // Dashboard
  if (url.pathname === "/" || url.pathname === "") {
    return new Response(dashboardHTML(), {
      headers: { "Content-Type": "text/html; charset=utf-8" },
    });
  }

  // Force poll (triggered by background loop and manual)
  if (url.pathname === "/poll") {
    const record = await pollAndTweet();
    recentPolls.unshift(record);
    if (recentPolls.length > 50) recentPolls.length = 50;
    return new Response(JSON.stringify(record), {
      headers: { "Content-Type": "application/json" },
    });
  }

  // Manual tweet
  if (url.pathname === "/manual-tweet" && req.method === "POST") {
    if (!credsReady) return new Response("creds not ready", { status: 503 });
    const body = await req.json() as { text?: string };
    if (!body.text) return new Response("missing text", { status: 400 });
    const result = await postTweet(body.text);
    if (result) {
      recentTweets.unshift({
        id: result.id,
        text: body.text!,
        source: "manual",
        pseudonym: "manual",
        timestamp: Date.now(),
      });
    }
    return new Response(JSON.stringify(result), {
      headers: { "Content-Type": "application/json" },
    });
  }

  return new Response("not found", { status: 404 });
}

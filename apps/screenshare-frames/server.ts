// screenshare-frames — browser screenshares, downsamples to 1 fps / small JPEG,
// POSTs each frame here. Server stores the last N frames and a tiny per-frame
// stat (mean luminance) so you can see processing happen.

const MAX_FRAMES = 200;

export default async function handler(
  req: Request,
  ctx: { env: Record<string, string>; dataDir: string },
) {
  const url = new URL(req.url);
  const dir = ctx.dataDir;
  await Deno.mkdir(dir, { recursive: true });

  if (req.method === "GET" && url.pathname === "/") {
    return new Response(PAGE, { headers: { "content-type": "text/html" } });
  }

  if (req.method === "POST" && url.pathname === "/frame") {
    const buf = new Uint8Array(await req.arrayBuffer());
    const lumaHeader = req.headers.get("x-luma");
    const ts = Date.now();
    const name = `${ts}.jpg`;
    await Deno.writeFile(`${dir}/${name}`, buf);
    await Deno.writeTextFile(
      `${dir}/${ts}.json`,
      JSON.stringify({ ts, bytes: buf.length, luma: Number(lumaHeader) }),
    );
    await prune(dir);
    return Response.json({ ok: true, name, bytes: buf.length });
  }

  if (req.method === "GET" && url.pathname === "/frames") {
    const entries = [];
    for await (const e of Deno.readDir(dir)) {
      if (e.name.endsWith(".json")) {
        entries.push(JSON.parse(await Deno.readTextFile(`${dir}/${e.name}`)));
      }
    }
    entries.sort((a, b) => b.ts - a.ts);
    return Response.json(entries);
  }

  if (req.method === "GET" && url.pathname.startsWith("/frame/")) {
    const name = url.pathname.slice("/frame/".length);
    if (!/^\d+\.jpg$/.test(name)) return new Response("bad name", { status: 400 });
    const body = await Deno.readFile(`${dir}/${name}`);
    return new Response(body, { headers: { "content-type": "image/jpeg" } });
  }

  return new Response("not found", { status: 404 });
}

async function prune(dir: string) {
  const jpgs: string[] = [];
  for await (const e of Deno.readDir(dir)) {
    if (e.name.endsWith(".jpg")) jpgs.push(e.name);
  }
  if (jpgs.length <= MAX_FRAMES) return;
  jpgs.sort();
  for (const old of jpgs.slice(0, jpgs.length - MAX_FRAMES)) {
    await Deno.remove(`${dir}/${old}`).catch(() => {});
    const stem = old.replace(/\.jpg$/, "");
    await Deno.remove(`${dir}/${stem}.json`).catch(() => {});
  }
}

const PAGE = `<!doctype html>
<meta charset="utf-8">
<title>screenshare-frames</title>
<style>
  body { font: 14px system-ui; max-width: 900px; margin: 2em auto; padding: 0 1em; }
  button { font-size: 1em; padding: .5em 1em; }
  #thumbs { display: grid; grid-template-columns: repeat(4, 1fr); gap: 6px; margin-top: 1em; }
  #thumbs img { width: 100%; display: block; border: 1px solid #ccc; }
  #thumbs figcaption { font-size: 11px; color: #666; }
  #status { margin: 1em 0; color: #444; }
  video { display: none; }
</style>
<h1>screenshare → TEE</h1>
<p>Click start, pick a window/screen. Browser samples 1 frame / 2 s, downsamples
to 320 px wide JPEG, POSTs to the TEE. Server stores the last ${MAX_FRAMES}.</p>
<button id="start">start</button>
<button id="stop" disabled>stop</button>
<label> interval (s) <input id="interval" type="number" value="2" min="0.2" step="0.2" style="width:5em"></label>
<label> width (px) <input id="width" type="number" value="320" min="64" step="32" style="width:5em"></label>
<label> quality <input id="quality" type="number" value="0.6" min="0.1" max="1" step="0.05" style="width:5em"></label>
<div id="status">idle</div>
<video id="v" autoplay muted playsinline></video>
<canvas id="c" style="display:none"></canvas>
<div id="thumbs"></div>
<script>
const v = document.getElementById('v');
const c = document.getElementById('c');
const status = document.getElementById('status');
const thumbs = document.getElementById('thumbs');
let stream = null, timer = null;

document.getElementById('start').onclick = async () => {
  stream = await navigator.mediaDevices.getDisplayMedia({ video: true, audio: false });
  v.srcObject = stream;
  await v.play();
  document.getElementById('start').disabled = true;
  document.getElementById('stop').disabled = false;
  const ms = Math.max(200, Number(document.getElementById('interval').value) * 1000);
  timer = setInterval(capture, ms);
  status.textContent = 'capturing every ' + ms + ' ms';
  stream.getVideoTracks()[0].onended = stop;
  refresh();
};

document.getElementById('stop').onclick = stop;

function stop() {
  if (timer) clearInterval(timer); timer = null;
  if (stream) stream.getTracks().forEach(t => t.stop()); stream = null;
  document.getElementById('start').disabled = false;
  document.getElementById('stop').disabled = true;
  status.textContent = 'stopped';
}

async function capture() {
  if (!v.videoWidth) return;
  const w = Math.max(64, Number(document.getElementById('width').value) | 0);
  const h = Math.round(v.videoHeight * w / v.videoWidth);
  c.width = w; c.height = h;
  const ctx = c.getContext('2d');
  ctx.drawImage(v, 0, 0, w, h);
  const img = ctx.getImageData(0, 0, w, h).data;
  let sum = 0;
  for (let i = 0; i < img.length; i += 4) {
    sum += 0.299 * img[i] + 0.587 * img[i+1] + 0.114 * img[i+2];
  }
  const luma = sum / (w * h);
  const q = Math.min(1, Math.max(0.1, Number(document.getElementById('quality').value)));
  const blob = await new Promise(r => c.toBlob(r, 'image/jpeg', q));
  const r = await fetch('frame', { method: 'POST', headers: { 'content-type': 'image/jpeg', 'x-luma': luma.toFixed(2) }, body: blob });
  const j = await r.json();
  status.textContent = 'sent ' + j.name + ' (' + j.bytes + ' B, luma ' + luma.toFixed(1) + ')';
  refresh();
}

async function refresh() {
  const list = await fetch('frames').then(r => r.json());
  thumbs.innerHTML = '';
  for (const f of list.slice(0, 24)) {
    const fig = document.createElement('figure');
    fig.style.margin = '0';
    const img = document.createElement('img');
    img.src = 'frame/' + f.ts + '.jpg';
    const cap = document.createElement('figcaption');
    cap.textContent = new Date(f.ts).toLocaleTimeString() + ' · ' + f.bytes + 'B · luma ' + f.luma;
    fig.append(img, cap);
    thumbs.append(fig);
  }
}

refresh();
</script>
`;

if (import.meta.main) {
  const dataDir = "./.data";
  await Deno.mkdir(dataDir, { recursive: true });
  Deno.serve({ port: 3000 }, (req) => handler(req, { env: {}, dataDir }));
}

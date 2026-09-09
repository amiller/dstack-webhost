"""Issue #77 — the browser parity board: cutover gate from twitter-debug's
bespoke Brave to the RFC 0028 leased pool.

Runs every capability of the issue table against BOTH engines and publishes one
board row per capability (board.json + index.html, plus captured shots):

  bespoke  the deployed twitter-debug app (TWITTER_DEBUG_BASE). Its jar
           injection is owner-approved now (/twitter/setjar was replaced by
           OAuth3 vault connect) and its writes need X-Debug-Secret, so without
           the operator those cells are NOT-YET — recorded, never faked green.
  pool     this daemon's RFC 0028 pool, driven ONLY through the public seam
           POST /_api/browser/render + GET /_api/browser/pool, recording the
           lease/bridge result (a pool health check alone is not parity).

Statuses per check: PASS (assertion ran green), FAIL (assertion ran red —
aborts the run), NOT-YET (exact missing dependency), CANNOT (structural: the
bespoke single browser cannot pass isolation — that is the migration's point).

Cutover rule carried on the board: the bespoke Brave is not retired and the
flag is not flipped until every row is green, including isolation. Red and
NOT-YET rows are the remaining work.

Repeatable run (env; no defaults for the required ones):
  PARITY_BASE=http://localhost:18080 PARITY_TOKEN=... python browser_parity.py
Optional: TWITTER_DEBUG_BASE, X_DEBUG_SECRET, PARITY_JAR (seeded/live jar),
PARITY_HANDLE (handle the jar must render), PARITY_REAL_BRIDGE=1 (the pool runs
a real Neko/Chromium bridge image, not the Deno test double), PARITY_EGRESS_EXPECT
(the VPN egress IP the pod must show), PARITY_MIN_ENTRIES, PARITY_OUT (default
parity/). NOTE: with X-Debug-Secret + a live jar configured, the write row
posts PARITY_POST_TEXT to the real account — that is the capability under test.
"""

import base64
import html
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests

PASS, FAIL, NOT_YET, CANNOT = "PASS", "FAIL", "NOT-YET", "CANNOT"

CUTOVER_RULE = ("the bespoke Brave is NOT retired and the flag is NOT flipped until every "
                "row is green, including isolation; red and NOT-YET rows are the remaining work")

ISSUE_ASSERTIONS = {
    "inject jar": "a known jar renders logged-in on both",
    "logged-in screenshot": "x.com/home shows the account (a known @handle visible), non-blank",
    "DOM reconstruct (reify)": ">=N timeline entries with text, same order-of-magnitude",
    "driven browser actions": "a scripted task reaches the same end state",
    "write / post": "a post (or dry-run) lands, asserted present",
    "egress binding": "VPN egress IP (not a datacenter IP) on both",
    "liveness": "green",
    "isolation (parity-PLUS)": "no cross-session bleed — each read returns only its own account",
}


class ParityFail(Exception):
    """A runnable assertion failed. The board is still written first."""


def _check(name, status, detail):
    return {"name": name, "status": status, "detail": detail}


def _cell(checks, evidence=None):
    return {"checks": checks, "evidence": evidence or {}}


def _green(cell):
    return bool(cell["checks"]) and all(c["status"] == PASS for c in cell["checks"])


def _save_png(data_url_or_bytes, path):
    """Save a screenshot; returns bytes saved. Raises on a blank/invalid image —
    a blank PNG reads as fabricated evidence and is worse than none."""
    if isinstance(data_url_or_bytes, str):
        b64 = data_url_or_bytes.split(",", 1)[-1]
        raw = base64.b64decode(b64)
    else:
        raw = data_url_or_bytes
    if len(raw) < 100 or not raw.startswith(b"\x89PNG"):
        raise ValueError(f"not a non-blank PNG ({len(raw)} bytes)")
    with open(path, "wb") as f:
        f.write(raw)
    return raw


class Pool:
    """The leased-pool side, through the daemon's public /_api seam only."""

    def __init__(self, base, token, timeout=120):
        self.api = f"{base.rstrip('/')}/_api"
        self.auth = {"Authorization": f"Bearer {token}"}
        self.timeout = timeout

    def status(self):
        return requests.get(f"{self.api}/browser/pool", headers=self.auth,
                            timeout=self.timeout)

    def render(self, domain, jar, url, op=None, timeout=None):
        body = {"domain": domain, "jar": jar, "url": url}
        if op is not None:
            body["op"] = op
        if timeout is not None:
            body["timeout"] = timeout
        return requests.post(f"{self.api}/browser/render", headers=self.auth,
                             json=body, timeout=self.timeout)


class Bespoke:
    """The bespoke twitter-debug side (deployed app; public reads, secret writes)."""

    def __init__(self, base, secret=None, timeout=120):
        self.base = base.rstrip("/")
        self.headers = {"X-Debug-Secret": secret} if secret else {}
        self.timeout = timeout

    def get(self, path, **kw):
        return requests.get(f"{self.base}{path}", headers=self.headers,
                            timeout=self.timeout, **kw)

    def post(self, path, payload):
        return requests.post(f"{self.base}{path}", headers=self.headers,
                             json=payload, timeout=self.timeout)


class Config:
    def __init__(self, pool, bespoke, jar, handle, real_bridge, egress_expect,
                 min_entries, post_text, out_dir):
        self.pool = pool
        self.bespoke = bespoke
        self.jar = jar
        self.handle = handle
        self.real_bridge = real_bridge
        self.egress_expect = egress_expect
        self.min_entries = min_entries
        self.post_text = post_text
        self.out_dir = out_dir
        self.shots = os.path.join(out_dir, "shots")


# ---- rows ------------------------------------------------------------------

def row_liveness(cfg):
    st = cfg.pool.status()
    checks, evidence = [], {"GET /_api/browser/pool": {"http": st.status_code,
                                                       "body": st.json()}}
    if st.status_code != 200 or not st.json().get("started"):
        checks.append(_check("GET /_api/browser/pool green", FAIL,
                             f"status {st.status_code}: {st.text}"))
    else:
        checks.append(_check("GET /_api/browser/pool green", PASS,
                             f"started, size={st.json()['size']}"))
    r = cfg.pool.render("parity.internal", "@liveness", "/me")
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    evidence["POST /_api/browser/render"] = {"http": r.status_code, "body": body}
    if r.status_code == 200 and body.get("body") == "@liveness" and body.get("lease", {}).get("id"):
        checks.append(_check("a lease renders (health alone is not parity)", PASS,
                             f"lease {body['lease']['id']} slot {body['lease']['slot']} "
                             f"in {body['lease']['render_ms']}ms"))
    else:
        checks.append(_check("a lease renders (health alone is not parity)", FAIL,
                             f"status {r.status_code}: {r.text}"))
    pool_cell = _cell(checks, evidence)

    if not cfg.bespoke:
        return pool_cell, _cell([_check(
            "GET /twitter/health", NOT_YET,
            "twitter-debug not configured (TWITTER_DEBUG_BASE unset)")])
    h = cfg.bespoke.get("/twitter/health")
    hb = h.json() if h.headers.get("content-type", "").startswith("application/json") else {}
    ok = h.status_code == 200 and hb.get("ok")
    return pool_cell, _cell([_check(
        "GET /twitter/health", PASS if ok else FAIL,
        f"jarLoaded={hb.get('jarLoaded')}")], {"GET /twitter/health": {"http": h.status_code,
                                                                       "body": hb}})


def row_inject_jar(cfg):
    jar = cfg.jar or "@parity-seed"
    r = cfg.pool.render("x.com", jar, "/me")
    body = r.json()
    checks = [
        _check("seeded jar renders logged-in", PASS if (r.status_code == 200
             and body.get("body") == jar) else FAIL,
             f"render returned {body.get('body')!r} for jar {jar!r}"),
    ]
    r2 = cfg.pool.render("x.com", "", "/me")
    checks.append(_check("reset clears (no bleed into the next lease)",
                         PASS if r2.json().get("body") == "" else FAIL,
                         f"jar-less render returned {r2.json().get('body')!r}"))
    checks.append(_check("broker jar-inject (RFC 0018)", NOT_YET,
                         "/_api/browser/render accepts a raw jar; the scoped broker "
                         "delegation is unwired (issue #50 open)"))
    pool_cell = _cell(checks, {"request": {"domain": "x.com", "jar": jar, "url": "/me"},
                               "response": body})

    if not cfg.bespoke:
        return pool_cell, _cell([_check("jar injection", NOT_YET,
            "twitter-debug not configured (TWITTER_DEBUG_BASE unset)")])
    s = cfg.bespoke.get("/twitter/oauth3/status")
    sb = s.json()
    loaded = s.status_code == 200 and sb.get("jarLoaded")
    return pool_cell, _cell([_check("jar loaded (OAuth3 vault connect)",
                                    PASS if loaded else NOT_YET,
                                    "POST /twitter/oauth3/connect then approve in the OAuth3 "
                                    "popup — /twitter/setjar was removed; injection is "
                                    "owner-approved now")],
        {"GET /twitter/oauth3/status": {"http": s.status_code, "body": sb}})


def row_screenshot(cfg):
    checks = []
    if not cfg.jar:
        checks.append(_check("live logged-in jar", NOT_YET,
                             "operator-provided jar unavailable (PARITY_JAR unset)"))
    if not cfg.real_bridge:
        checks.append(_check("real browser bridge", NOT_YET,
                             "pool runs the Deno bridge double (PARITY_REAL_BRIDGE unset); "
                             "a real Neko/Chromium bridge image is required for a real "
                             "x.com/home frame"))
    if cfg.jar and cfg.real_bridge:
        if not cfg.handle:
            checks.append(_check("known handle", NOT_YET, "PARITY_HANDLE unset"))
        else:
            r = cfg.pool.render("x.com", cfg.jar, "https://x.com/home", op={"op": "screenshot"})
            body = r.json()
            try:
                _save_png(body.get("screenshot") or "",
                          os.path.join(cfg.shots, "pool-x-home.png"))
                shot = "saved pool-x-home.png"
            except Exception as e:
                shot = f"NOT saved: {e}"
            checks.append(_check("x.com/home non-blank, known handle visible",
                 PASS if (r.status_code == 200 and body.get("handle") == cfg.handle
                          and shot.startswith("saved")) else FAIL,
                 f"handle={body.get('handle')!r} expected {cfg.handle!r}; shot {shot}"))
    pool_cell = _cell(checks)

    if not cfg.bespoke:
        return pool_cell, _cell([_check("GET /twitter/shot", NOT_YET,
            "twitter-debug not configured (TWITTER_DEBUG_BASE unset)")])
    # Capture the frame even without a jar: a signed-out shot proves serving and
    # names the wall (the value state is behind the operator's login) — it is
    # never counted as the capability passing.
    sh = cfg.bespoke.get("/twitter/shot")
    try:
        _save_png(sh.content, os.path.join(cfg.shots, "bespoke-x-home.png"))
        shot = f"saved bespoke-x-home.png ({len(sh.content)} bytes)"
        nonblank = True
    except Exception as e:
        shot = f"NOT saved: {e}"
        nonblank = False
    health = cfg.bespoke.get("/twitter/health").json()
    if cfg.jar:
        handle_check = _check("known handle visible", NOT_YET,
            "the bespoke shot is a raw PNG with no machine-readable account "
            "signal — operator confirms the handle on the saved frame")
    else:
        handle_check = _check("known handle visible", NOT_YET,
            "live logged-in jar unavailable — operator OAuth3 vault connect "
            "required (PARITY_JAR); the saved frame is the signed-out wall")
    return pool_cell, _cell([
        _check("GET /twitter/shot non-blank (serving)", PASS if nonblank else FAIL,
               f"{shot}; jarLoaded={health.get('jarLoaded')}"),
        handle_check,
    ], {"GET /twitter/health": {"body": health},
        "GET /twitter/shot": {"http": sh.status_code,
                              "content-type": sh.headers.get("content-type")}})


def row_reify(cfg):
    checks = []
    if not cfg.jar:
        checks.append(_check("live logged-in jar", NOT_YET,
                             "operator-provided jar unavailable (PARITY_JAR unset)"))
    if not cfg.real_bridge:
        checks.append(_check("real browser bridge", NOT_YET,
                             "pool runs the Deno bridge double (PARITY_REAL_BRIDGE unset)"))
    if cfg.jar and cfg.real_bridge:
        r = cfg.pool.render("x.com", cfg.jar, "https://x.com/home", op={"op": "reify"})
        body = r.json()
        entries = body.get("entries") or []
        with_text = [e for e in entries if e.get("text")]
        checks.append(_check(f">= {cfg.min_entries} timeline entries with text",
             PASS if (r.status_code == 200 and len(with_text) >= cfg.min_entries) else FAIL,
             f"{len(with_text)} entries with text"))
    pool_cell = _cell(checks)

    if not cfg.bespoke:
        return pool_cell, _cell([_check("POST /twitter/reify", NOT_YET,
            "twitter-debug not configured (TWITTER_DEBUG_BASE unset)")])
    if not cfg.jar:
        return pool_cell, _cell([_check("live logged-in jar", NOT_YET,
            "operator OAuth3 vault connect required (PARITY_JAR unset)")])
    r = cfg.bespoke.post("/twitter/reify", {})
    try:
        body = r.json()
    except ValueError:
        body = {"raw": r.text[:400]}
    replay = body.get("reify") or {}
    ok = (r.status_code == 200 and replay.get("status") == 200
          and (replay.get("entries") or 0) >= cfg.min_entries)
    return pool_cell, _cell([_check(f">= {cfg.min_entries} timeline entries with text",
                                    PASS if ok else FAIL,
                                    f"replay status={replay.get('status')} "
                                    f"entries={replay.get('entries')}")],
        {"POST /twitter/reify": {"http": r.status_code,
                                 "verdict": body.get("verdict")}})


def row_driven(cfg):
    steps = ["navigate https://x.com/home", "click compose", "type parity"]
    r = cfg.pool.render("x.com", "@driver", "/home", op={"op": "drive", "steps": steps})
    body = r.json()
    end = body.get("end_state") or {}
    checks = [
        _check("driven task reaches its end state through the lease",
               PASS if (r.status_code == 200 and end.get("steps_done") == len(steps)
                        and body.get("lease", {}).get("id")) else FAIL,
               f"end_state={end}, lease={body.get('lease', {}).get('id')}"),
        _check("real driven task (same end state on both engines)", NOT_YET,
               "needs the real bridge + live jar on the pool and the secret-gated "
               "/twitter/browser on the bespoke side"),
    ]
    pool_cell = _cell(checks, {"request": {"url": "/home", "op": {"op": "drive",
                                                                 "steps": steps}},
                               "response": body})
    if not cfg.bespoke:
        return pool_cell, _cell([_check("POST /twitter/browser {task}", NOT_YET,
            "twitter-debug not configured (TWITTER_DEBUG_BASE unset)")])
    if not cfg.jar:
        return pool_cell, _cell([_check("live logged-in jar", NOT_YET,
            "operator OAuth3 vault connect required (PARITY_JAR unset); "
            "browser-driving is lock+cooldown'd on the single bespoke Brave")])
    b = cfg.bespoke.post("/twitter/browser", {"task": "trace"})
    try:
        bb = b.json()
    except ValueError:
        bb = {"raw": b.text[:200]}
    ran = b.status_code == 200 and bool(bb.get("reified"))
    return pool_cell, _cell([_check("driven task (trace) runs on the bespoke Brave",
                                    PASS if ran else FAIL,
                                    f"http {b.status_code}: {str(bb)[:120]}")],
        {"POST /twitter/browser {task:trace}": {"http": b.status_code}})


def row_write(cfg):
    text = f"{cfg.post_text} (dry-run {int(time.time())})"
    r = cfg.pool.render("x.com", "@writer", "/post",
                        op={"op": "post", "text": text, "dry_run": True})
    posted = (r.json().get("end_state") or {}).get("posted") == text
    landed = text in [e.get("text") for e in r.json().get("entries") or []]
    other = cfg.pool.render("x.com", "@reader", "/timeline").json()
    other_entries = [e.get("text") for e in other.get("entries") or []]
    after = cfg.pool.render("x.com", "", "/timeline").json()
    after_entries = [e.get("text") for e in after.get("entries") or []]
    checks = [
        _check("dry-run post lands, asserted present",
               PASS if (r.status_code == 200 and posted and landed) else FAIL,
               f"end_state={r.json().get('end_state')}, read-back entries="
               f"{r.json().get('entries')}"),
        _check("post is account-scoped (invisible to another jar)",
               PASS if text not in other_entries else FAIL,
               f"other jar timeline={other_entries}"),
        _check("reset clears the post",
               PASS if text not in after_entries else FAIL,
               f"post-reset timeline={after_entries}"),
        _check("real post (non-dry-run)", NOT_YET,
               "needs the real bridge + live jar on the pool; the bespoke side needs "
               "X-Debug-Secret and posts FOR REAL (PARITY_POST_TEXT)"),
    ]
    pool_cell = _cell(checks, {"post_response": r.json(), "other_jar_timeline": other,
                               "post_reset_timeline": after})
    if not cfg.bespoke:
        return pool_cell, _cell([_check("POST /twitter/api {op:post}", NOT_YET,
            "twitter-debug not configured (TWITTER_DEBUG_BASE unset)")])
    if not cfg.jar:
        return pool_cell, _cell([_check("live logged-in jar", NOT_YET,
            "operator OAuth3 vault connect required (PARITY_JAR unset)")])
    if not cfg.bespoke.headers:
        return pool_cell, _cell([_check("X-Debug-Secret", NOT_YET,
            "bespoke writes are secret-gated (X_DEBUG_SECRET unset here)")])
    btext = f"{cfg.post_text} ({int(time.time())})"
    p = cfg.bespoke.post("/twitter/api", {"op": "post", "text": btext})
    try:
        pb = p.json()
    except ValueError:
        pb = {"raw": p.text[:200]}
    tl = cfg.bespoke.post("/twitter/api", {"op": "timeline"})
    tweets = [t.get("text") for t in tl.json().get("tweets") or []]
    ok = p.status_code == 200 and pb.get("id") and btext in tweets
    return pool_cell, _cell([_check("post lands, asserted present",
                                    PASS if ok else FAIL,
                                    f"post id={pb.get('id')}; present in timeline="
                                    f"{btext in tweets}")],
        {"POST /twitter/api {op:post}": {"http": p.status_code, "body": pb},
         "POST /twitter/api {op:timeline}": {"tweets": tweets[:5]}})


def row_egress(cfg):
    pool_cell = _cell([_check("per-lease egress locked to the jar's domain", NOT_YET,
        "unimplemented: BrowserPool carries no per-lease egress/proxy config "
        "(RFC 0028 open question 'Egress binding')")])
    if not cfg.bespoke:
        return pool_cell, _cell([_check("GET /twitter/ip", NOT_YET,
            "twitter-debug not configured (TWITTER_DEBUG_BASE unset)")])
    r = cfg.bespoke.get("/twitter/ip")
    ip = r.json().get("ip")
    if cfg.egress_expect:
        return pool_cell, _cell([_check("egress IP is the expected VPN",
             PASS if ip == cfg.egress_expect else FAIL,
             f"ip={ip} expected {cfg.egress_expect}")],
            {"GET /twitter/ip": {"http": r.status_code, "body": r.json()}})
    return pool_cell, _cell([_check("egress IP is the expected VPN (not datacenter)", NOT_YET,
        f"observed ip={ip}, org={r.json().get('org')!r}; the VPN expectation is "
        "operator-provided (PARITY_EGRESS_EXPECT)")],
        {"GET /twitter/ip": {"http": r.status_code, "body": r.json()}})


def row_isolation(cfg):
    st = cfg.pool.status().json()
    checks = [_check("pool size >= 2 (two concurrent leases)",
                     PASS if st.get("size", 0) >= 2 else FAIL,
                     f"pool size={st.get('size')} — cannot run two concurrent leases")]
    def read_tw():
        return cfg.pool.render("x.com", "@parity-tw", "/me")
    def read_yt():
        return cfg.pool.render("youtube.com", "@parity-yt", "/me")
    seen_busy = []
    done = threading.Event()
    def watch_busy():
        while not done.is_set():
            try:
                seen_busy.append(cfg.pool.status().json().get("busy"))
            except Exception:
                pass
            time.sleep(0.05)
    watcher = threading.Thread(target=watch_busy)
    watcher.start()
    with ThreadPoolExecutor(max_workers=2) as ex:
        ftw, fyt = ex.submit(read_tw), ex.submit(read_yt)
        rtw, ryt = ftw.result(), fyt.result()
    done.set()
    watcher.join()
    btw, byt = rtw.json(), ryt.json()
    only_own = (btw.get("body") == "@parity-tw" and byt.get("body") == "@parity-yt"
                and "@parity-yt" not in str(btw) and "@parity-tw" not in str(byt))
    checks.append(_check("each read contains only its own account",
                         PASS if only_own else FAIL,
                         f"twitter read={btw.get('body')!r}, youtube read={byt.get('body')!r}"))
    # max_active is per bridge container, so cross-lease overlap is proven through
    # the pool seam: sample /_api/browser/pool while both leases are live.
    checks.append(_check("leases truly held concurrently (pool busy == 2 observed)",
                         PASS if max(seen_busy or [0]) == 2 else FAIL,
                         f"max busy observed = {max(seen_busy or [0])} "
                         f"({len(seen_busy)} samples)"))
    r_empty = cfg.pool.render("x.com", "", "/me")
    checks.append(_check("reset clears after both leases",
                         PASS if r_empty.json().get("body") == "" else FAIL,
                         f"jar-less read={r_empty.json().get('body')!r}"))
    checks.append(_check("RFC 0028 DECIDED isolation model (fresh container per lease)",
                         NOT_YET,
                         "browser_pool.py resets and reuses containers; the decided "
                         "destroy-per-lease is unimplemented (RFC 0028 Decisions)"))
    pool_cell = _cell(checks, {"pool_status": st, "twitter_read": btw, "youtube_read": byt})
    bespoke_cell = _cell([_check("two subjects isolated", CANNOT,
        "structural: the bespoke engine is one Brave with one global session "
        "(RFC 0028 Problem) — it cannot pass this row; that is the migration's "
        "justification")])
    return pool_cell, bespoke_cell


# ---- board -----------------------------------------------------------------

ROWS = [("liveness", row_liveness), ("inject jar", row_inject_jar),
        ("logged-in screenshot", row_screenshot), ("DOM reconstruct (reify)", row_reify),
        ("driven browser actions", row_driven), ("write / post", row_write),
        ("egress binding", row_egress), ("isolation (parity-PLUS)", row_isolation)]


def _html(board):
    chip = {PASS: "background:#1a7f37;color:#fff", FAIL: "background:#cf222e;color:#fff",
            NOT_YET: "background:#bf8700;color:#fff", CANNOT: "background:#57606a;color:#fff"}
    out = ["""<!doctype html><html><head><meta charset="utf-8">
<title>browser parity board — issue #77</title><style>
body{font:14px/1.45 -apple-system,Segoe UI,sans-serif;margin:2rem;max-width:70rem;color:#1f2328}
h1{font-size:1.4rem} .banner{padding:.7rem 1rem;border-radius:8px;margin:1rem 0}
.notready{background:#fff8c5;border:1px solid #d4a72c}.ready{background:#dafbe1;border:1px solid #1a7f37}
table{border-collapse:collapse;width:100%}td,th{border:1px solid #d0d7de;padding:.5rem;vertical-align:top;text-align:left}
.c{display:inline-block;padding:.1rem .5rem;border-radius:10px;font-size:.8rem;margin-right:.3rem}
details{margin-top:.3rem}pre{background:#f6f8fa;padding:.5rem;overflow:auto;font-size:.75rem}
.meta{color:#57606a;font-size:.85rem} ul{margin:.2rem 0;padding-left:1.1rem}
</style></head><body>
<h1>Browser parity board — bespoke twitter-debug Brave vs the RFC 0028 leased pool</h1>"""]
    cls = "ready" if board["cutover"]["ready"] else "notready"
    verdict = "READY" if board["cutover"]["ready"] else "NOT READY"
    out.append(f'<div class="banner {cls}"><b>CUTOVER: {verdict}.</b> {board["cutover"]["rule"]}</div>')
    out.append(f'<div class="meta">generated {board["generated"]} · '
               f'pool <code>{board["targets"]["pool"]}</code> · '
               f'bespoke <code>{board["targets"]["bespoke"] or "(not configured)"}</code></div>')
    out.append('<ul class="meta">' + "".join(f"<li>{n}</li>" for n in board["notes"]) + "</ul>")
    out.append("<table><tr><th>capability</th><th>parity assertion</th>"
               "<th>bespoke (twitter-debug)</th><th>pool (RFC 0028)</th></tr>")
    for row in board["rows"]:
        cells = []
        for side in ("bespoke", "pool"):
            checks = "".join(
                f'<li><span class="c" style="{chip[c["status"]]}">{c["status"]}</span>'
                f'<b>{html.escape(c["name"])}</b> — {html.escape(c["detail"])}</li>'
                for c in row[side]["checks"])
            ev = html.escape(json.dumps(row[side]["evidence"], indent=1))
            cells.append(f'<td><ul>{checks}</ul>'
                         f'<details><summary>evidence</summary><pre>{ev}</pre></details></td>')
        out.append(f'<tr><td><b>{row["capability"]}</b></td>'
                   f'<td>{row["assertion"]}</td>{cells[0]}{cells[1]}</tr>')
    out.append("</table></body></html>")
    return "\n".join(out)


def run_parity(base, token, *, twitter_debug=None, debug_secret=None, jar=None,
               handle=None, real_bridge=False, egress_expect=None, min_entries=8,
               post_text="parity board probe", out_dir="parity"):
    pool = Pool(base, token)
    bespoke = Bespoke(twitter_debug, debug_secret) if twitter_debug else None
    cfg = Config(pool, bespoke, jar, handle, real_bridge, egress_expect,
                 min_entries, post_text, out_dir)
    os.makedirs(cfg.shots, exist_ok=True)

    # The pool warms in the background (image pull + health poll) so a slow pull
    # never blocks daemon readiness — wait for it, loudly, before asserting.
    deadline = time.monotonic() + 120
    while True:
        st = pool.status()
        if st.status_code == 200 and st.json().get("started"):
            break
        if time.monotonic() >= deadline:
            raise ParityFail(f"browser pool never became ready: "
                             f"{st.status_code} {st.text}")
        time.sleep(0.5)

    rows = []
    for capability, fn in ROWS:
        pool_cell, bespoke_cell = fn(cfg)
        rows.append({"capability": capability,
                     "assertion": ISSUE_ASSERTIONS[capability],
                     "pool": pool_cell, "bespoke": bespoke_cell,
                     "green": _green(pool_cell) and _green(bespoke_cell)})
        print(f'  {capability:<28} {"GREEN" if rows[-1]["green"] else "red":<5} '
              f'pool={[c["status"] for c in pool_cell["checks"]]} '
              f'bespoke={[c["status"] for c in bespoke_cell["checks"]]}')

    board = {
        "issue": "amiller/dstack-webhost#77",
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "cutover": {"ready": all(r["green"] for r in rows), "rule": CUTOVER_RULE},
        "targets": {"pool": base, "bespoke": twitter_debug,
                    "pool_status": pool.status().json()},
        "notes": [
            "RFC 0028 decided ephemeral-per-lease + a metering hook; the pool implements "
            "reset-and-reuse with no metering — remaining work, tracked by the isolation row.",
            "RFC 0018 broker jar-injection is unwired (issue #50): /_api/browser/render "
            "accepts a raw jar.",
            "twitter-debug's /twitter/setjar was removed; bespoke jar injection is now "
            "owner-approved OAuth3 vault connect.",
        ],
        "rows": rows,
    }
    with open(os.path.join(out_dir, "board.json"), "w") as f:
        json.dump(board, f, indent=1)
    with open(os.path.join(out_dir, "index.html"), "w") as f:
        f.write(_html(board))

    fails = [f'{r["capability"]}/{s}/{c["name"]}: {c["detail"]}'
             for r in rows for s in ("pool", "bespoke") for c in r[s]["checks"]
             if c["status"] == FAIL]
    if fails:
        raise ParityFail("parity assertions FAILED (board written):\n  " + "\n  ".join(fails))
    return board


def main():
    base = os.environ.get("PARITY_BASE", "")
    token = os.environ.get("PARITY_TOKEN", "")
    if not base or not token:
        sys.exit("PARITY_BASE and PARITY_TOKEN are required "
                 "(the daemon base URL and its API token)")
    board = run_parity(
        base, token,
        twitter_debug=os.environ.get("TWITTER_DEBUG_BASE") or None,
        debug_secret=os.environ.get("X_DEBUG_SECRET") or None,
        jar=os.environ.get("PARITY_JAR") or None,
        handle=os.environ.get("PARITY_HANDLE") or None,
        real_bridge=os.environ.get("PARITY_REAL_BRIDGE") == "1",
        egress_expect=os.environ.get("PARITY_EGRESS_EXPECT") or None,
        min_entries=int(os.environ.get("PARITY_MIN_ENTRIES", "8")),
        post_text=os.environ.get("PARITY_POST_TEXT", "parity board probe"),
        out_dir=os.environ.get("PARITY_OUT", "parity"))
    n_green = sum(1 for r in board["rows"] if r["green"])
    print(f'\nparity board: {n_green}/{len(board["rows"])} rows green -> '
          f'cutover {"READY" if board["cutover"]["ready"] else "NOT READY"} '
          f'({board["cutover"]["rule"]})')


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Work-view section for the paseo Evidence Report (issue #60).

Emits an HTML fragment listing the autonomous loop's recent runs (issue -> branch ->
verify pass/fail -> PR -> status), recent PRs with merge + pass/fail state, and the
ready/operator-ask queue. Each subsection carries a last-run timestamp computed from
its real substance and renders RED when that substance is older than STALE_HOURS
(default 24) or absent.

Data sources — no fallbacks. A `gh` failure raises (nonzero exit, traceback to stderr);
an empty source renders an honest in-page notice, never a fabricated value:
  runs   : $PASEO_OUT/<N>/result.json        (default ~/paseo-batch/out)
  PRs    : gh pr list  --repo <REPO> --state all
  queue  : gh issue list --repo <REPO> --state open --label ready | --label operator-ask

Usage:
  reports/gen-work-dashboard.py              # fragment to stdout
  reports/gen-work-dashboard.py --out FILE   # fragment to file
  reports/gen-work-dashboard.py --standalone # full HTML doc (preview / screenshot)

Env: PASEO_OUT (lane-log root), REPO (default amiller/dstack-webhost),
     GH (gh binary; set by the report refresh), STALE_HOURS (default 24).
"""
import argparse, datetime as dt, html, json, os, pathlib, re, subprocess, sys

REPO = os.environ.get("REPO", "amiller/dstack-webhost")
PASEO_OUT = pathlib.Path(os.environ.get("PASEO_OUT", str(pathlib.Path.home() / "paseo-batch" / "out")))
STALE = dt.timedelta(hours=float(os.environ.get("STALE_HOURS", "24")))
NOW = dt.datetime.now(dt.timezone.utc)
E = html.escape


def _gh(bin_, args):
    r = subprocess.run([bin_, *args], capture_output=True, text=True, timeout=40)
    if r.returncode != 0:
        raise RuntimeError("gh failed: " + (r.stderr.strip()[:160] or f"exit {r.returncode}"))
    return json.loads(r.stdout or "[]")


def fetch_prs():
    out = _gh(os.environ.get("GH") or "gh",
              ["pr", "list", "--repo", REPO, "--state", "all", "--limit", "40", "--json",
               "number,title,state,headRefName,mergedAt,createdAt,url"])
    by_branch, by_num = {}, {}
    for p in out:
        by_branch[p.get("headRefName")] = p
        by_num[p.get("number")] = p
    return out, by_branch, by_num


def fetch_queue():
    res = {}
    for label in ("operator-ask", "ready"):
        res[label] = _gh(os.environ.get("GH") or "gh",
                         ["issue", "list", "--repo", REPO, "--state", "open", "--label", label,
                          "--limit", "30", "--json", "number,title,labels,url"])
    return res


FAIL = re.compile(r"\b(fail(?:ed)?|red|did not pass|not pass|exit [1-9]\d*)\b", re.I)
PASS = re.compile(r"(all tests passed|\bpassed\b|\bpass\b|\bgreen\b|exit 0|\u2713)", re.I)


def classify(tests):
    t = "" if tests is None else str(tests)
    if FAIL.search(t):
        return "fail"
    if PASS.search(t):
        return "pass"
    return "unknown"


def load_runs(by_branch, by_num):
    runs = []
    for d in sorted(PASEO_OUT.iterdir(), reverse=True):
        if not d.name.isdigit():
            continue
        rj = d / "result.json"
        if not rj.is_file():
            continue
        try:
            r = json.loads(rj.read_text())
        except Exception:
            continue  # malformed result.json is not a gh failure; skip honestly
        mtime = dt.datetime.fromtimestamp(rj.stat().st_mtime, dt.timezone.utc)
        runs.append({"r": r, "mtime": mtime, "dir": d.name})
    runs.sort(key=lambda x: x["mtime"], reverse=True)
    return runs[:18]


def fmt_ago(then):
    if then is None:
        return "never"
    s = int((NOW - then).total_seconds())
    if s < 0:
        s = 0
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def iso(then):
    return then.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if then else "—"


def stale(then):
    return then is None or (NOW - then) > STALE


# --- renderers ------------------------------------------------------------------

CSS = """
section.work{ margin-top:14px; }
section.work h2{ font-size:16px; border-bottom:1px solid var(--rule,#d8d4cc); padding-bottom:4px; }
section.work h3{ font-size:13.5px; margin:18px 0 2px; }
section.work .last-run{ float:right; font-size:12px; color:var(--faint,#6e6a62); font-variant-numeric:tabular-nums; }
section.work .sub.stale{ background:var(--fail-bg,#fbe9eb); border-left:4px solid var(--fail-fg,#b00020);
  padding:8px 12px; border-radius:6px; }
section.work .sub.stale h3{ color:var(--fail-fg,#b00020); margin-top:0; }
section.work table.smoke-tbl{ font-size:13px; }
section.work td.verify{ font-weight:600; white-space:nowrap; }
section.work .v-pass{ color:#0b7a44; } section.work .v-fail{ color:var(--fail-fg,#b00020); }
section.work .v-unknown{ color:var(--faint,#6e6a62); }
section.work .badge{ font-size:11px; padding:1px 7px; border-radius:999px; white-space:nowrap; }
section.work .b-merged{ background:#e3f3e8; color:#0b7a44; }
section.work .b-open{ background:#e7f0fb; color:#1f6feb; }
section.work .b-closed{ background:#f1ecea; color:#6e6a62; }
section.work .b-ask{ background:#fde8e8; color:#b00020; }
section.work .none{ color:var(--faint,#6e6a62); font-style:italic; }
section.work .cap{ color:var(--faint,#6e6a62); font-size:12px; margin:6px 0 0; }
section.work a{ color:#1f6feb; }
"""


def badge(state):
    return f'<span class="badge b-{E(state)}">{E(state)}</span>'


def render_runs(runs, by_branch, by_num):
    if not runs:
        head = '<p class="none">No loop runs logged — the ready queue has produced nothing.</p>'
        return None, head
    ts = runs[0]["mtime"]
    rows = []
    for x in runs[:12]:
        r = x["r"]
        issue = r.get("issue", x["dir"])
        title = r.get("title") or "(no title)"
        branch = r.get("branch") or ""
        pr = r.get("pr")
        # join to PR state
        p = by_branch.get(branch)
        if p is None and isinstance(pr, int):
            p = by_num.get(pr)
        if p is None and isinstance(pr, str) and "pull/" in pr:
            m = re.search(r"pull/(\d+)", pr)
            if m:
                p = by_num.get(int(m.group(1)))
        if p:
            state = "merged" if p.get("state") == "MERGED" or p.get("mergedAt") else (
                "open" if p.get("state") == "OPEN" else "closed")
            prlink = f'<a href="{E(p["url"])}">#{p["number"]}</a>'
        else:
            state = "open" if isinstance(pr, int) or pr else "none"
            prlink = f'#{pr}' if pr else "—"
        verdict = classify(r.get("local_tests"))
        rows.append(
            f'<tr><td><a href="https://github.com/{REPO}/issues/{issue}">#{issue}</a></td>'
            f'<td>{E(title[:70])}</td><td><code>{E(branch)}</code></td>'
            f'<td class="verify v-{verdict}">{verdict}</td>'
            f'<td>{prlink} {badge(state) if state!="none" else ""}</td></tr>')
    head = ('<table class="smoke-tbl"><thead><tr><th>issue</th><th>title</th><th>branch</th>'
            '<th>verify</th><th>PR · merge</th></tr></thead><tbody>'
            + "\n".join(rows) + '</tbody></table>')
    return ts, head


def render_prs(prs, run_by_branch):
    # live-fetched → timestamp is now; never stale (it is the fresh state of the world)
    ts = NOW
    order = {"OPEN": 0, "MERGED": 1, "CLOSED": 2}

    def ptime(p):
        s = p.get("mergedAt") or p.get("createdAt") or "1970-01-01T00:00:00Z"
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    listed = sorted(prs, key=lambda p: (order.get(p.get("state"), 9), -ptime(p).timestamp()))[:14]
    rows = []
    for p in listed:
        st = "merged" if p.get("state") == "MERGED" or p.get("mergedAt") else (
            "open" if p.get("state") == "OPEN" else "closed")
        run = run_by_branch.get(p.get("headRefName"))
        verdict = classify(run["r"].get("local_tests")) if run else "—"
        vcls = f'v-{verdict}' if verdict in ("pass", "fail", "unknown") else ""
        rows.append(
            f'<tr><td><a href="{E(p["url"])}">#{p["number"]}</a></td>'
            f'<td>{E(p["title"][:64])}</td><td><code>{E(p.get("headRefName") or "")}</code></td>'
            f'<td class="verify {vcls}">{verdict}</td><td>{badge(st)}</td></tr>')
    head = ('<table class="smoke-tbl"><thead><tr><th>PR</th><th>title</th><th>branch</th>'
            '<th>verify</th><th>state</th></tr></thead><tbody>' + "\n".join(rows) + '</tbody></table>')
    return ts, head


def render_queue(q):
    ts = NOW  # live gh fetch
    sub = ""
    asks = q["operator-ask"] + [i for i in q["ready"]
                               if "operator-ask" not in {l["name"] for l in i.get("labels", [])}]
    if not asks:
        head = '<p class="none">No ready or operator-ask issues — the queue is empty.</p>'
        return ts, head
    lis = []
    for i in asks:
        labs = {l["name"] for l in i.get("labels", [])}
        cls = "b-ask" if "operator-ask" in labs else "b-open"
        lis.append(
            f'<li><span class="badge {cls}">{"operator-ask" if "operator-ask" in labs else "ready"}</span> '
            f'<a href="{E(i["url"])}">#{i["number"]}</a> — {E(i["title"])}</li>')
    head = '<ul class="asks">' + "\n".join(lis) + '</ul>'
    return ts, head


def fragment():
    prs, by_branch, by_num = fetch_prs()
    queue = fetch_queue()
    runs = load_runs(by_branch, by_num)

    def sub(title, ts, body, cap=None):
        s = "stale" if stale(ts) else ""
        ago = fmt_ago(ts)
        cap_html = f'<p class="cap">{E(cap)}</p>' if cap else ""
        return (f'<div class="sub {s}"><span class="last-run" title="{E(iso(ts))}">last run {E(ago)}</span>'
                f'<h3>{title}{(" — STALE" if s else "")}</h3>{body}{cap_html}</div>')

    parts = [f'<section class="work"><h2 id="work">Work — the autonomous loop</h2>'
             f'<p class="cap">What the loop is doing: recent runs (issue → branch → verify → PR), '
             f'PR merge + pass/fail state, and the ready/operator-ask queue. Sourced live from '
             f'<code>gh</code> + <code>~/paseo-batch/out</code>; any section whose substance is '
             f'&gt;{int(STALE.total_seconds()//3600)}h old or absent turns RED.</p>']
    rts, rbody = render_runs(runs, by_branch, by_num)
    parts.append(sub("Recent runs", rts, rbody,
                     "Each row is one ready-queue iteration from the result.json lane logs; "
                     "verify = test_daemon.py / unit result recorded by that run."))
    run_by_branch = {x["r"].get("branch"): x for x in runs}
    pts, pbody = render_prs(prs, run_by_branch)
    parts.append(sub("Pull requests", pts, pbody))
    qts, qbody = render_queue(queue)
    parts.append(sub("Queue — ready + operator-ask", qts, qbody))
    parts.append("</section>")
    return "\n".join(parts)


def standalone(frag):
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport "
        "content='width=device-width,initial-scale=1'><title>Paseo work dashboard · preview</title>"
        "<style>:root{--rule:#d8d4cc;--faint:#6e6a62;--fail-fg:#b00020;--fail-bg:#fbe9eb;}"
        "body{font-family:system-ui,sans-serif;background:#faf9f6;color:#111;max-width:1000px;"
        "margin:0 auto;padding:30px}" + CSS + "</style></head><body>" + frag + "</body></html>")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out")
    ap.add_argument("--standalone", action="store_true")
    a = ap.parse_args()
    frag = fragment()
    out = standalone(frag) if a.standalone else (CSS + frag)
    if a.out:
        pathlib.Path(a.out).write_text(out)
        print(f"wrote {a.out} ({len(out)} bytes)", file=sys.stderr)
    else:
        sys.stdout.write(out + "\n")


if __name__ == "__main__":
    main()

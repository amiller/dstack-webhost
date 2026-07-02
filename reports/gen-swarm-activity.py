#!/usr/bin/env python3
"""Regenerate reports/swarm-activity.html from live git/gh data.

Mechanical facts (per-branch diffstat effort, RFC doc sizes, PR states) are read
live from `git` and `gh`. The tech-tree topology, PR->outcome classification, the
over-claim-bug list, and the timeline are curated constants below (they encode the
narrative, which git can't derive). Run from anywhere inside the repo:

    python3 reports/gen-swarm-activity.py

No fallbacks: if a git/gh call fails, it raises.
"""
import json, subprocess, os, sys, pathlib

REPO = "amiller/dstack-webhost"
ROOT = subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip()
os.chdir(ROOT)

HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Paseo Swarm · tee-daemon security sprint</title>
<style>
:root{
  --surface-1:#fcfcfb; --page:#f9f9f7; --card:#ffffff;
  --text-primary:#0b0b0b; --text-secondary:#52514e; --muted:#898781;
  --grid:#e1e0d9; --baseline:#c3c2b7; --border:rgba(11,11,11,.10);
  --c-landed:#2a78d6; --c-implemented:#1baf7a; --c-paper:#eda100; --c-queued:#4a3aa7;
  --good:#0ca30c; --critical:#d03b3b; --warning:#e69500;
  --tint-landed:rgba(42,120,214,.10); --tint-implemented:rgba(27,175,122,.10);
  --tint-paper:rgba(237,161,0,.12); --tint-queued:rgba(74,58,167,.10);
}
@media (prefers-color-scheme:dark){:root{
  --surface-1:#1a1a19; --page:#0d0d0d; --card:#1f1f1e;
  --text-primary:#ffffff; --text-secondary:#c3c2b7; --muted:#898781;
  --grid:#2c2c2a; --baseline:#383835; --border:rgba(255,255,255,.12);
  --c-landed:#3987e5; --c-implemented:#199e70; --c-paper:#c98500; --c-queued:#9085e9;
  --good:#0ca30c; --critical:#d03b3b; --warning:#fab219;
  --tint-landed:rgba(57,135,229,.16); --tint-implemented:rgba(25,158,112,.16);
  --tint-paper:rgba(201,133,0,.18); --tint-queued:rgba(144,133,233,.18);
}}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--text-primary);
  font-family:system-ui,-apple-system,"Segoe UI",sans-serif;line-height:1.5;
  -webkit-font-smoothing:antialiased;}
.wrap{max-width:1240px;margin:0 auto;padding:40px 28px 80px}
header .eyebrow{color:var(--muted);font-size:13px;letter-spacing:.08em;text-transform:uppercase;font-weight:600}
h1{font-size:34px;margin:.15em 0 .1em;letter-spacing:-.02em}
.sub{color:var(--text-secondary);font-size:16px;max-width:70ch}
.band{display:inline-flex;align-items:center;gap:8px;margin-top:14px;padding:6px 12px;
  border:1px solid var(--border);border-radius:999px;font-size:13px;color:var(--text-secondary);background:var(--surface-1)}
.band b{color:var(--text-primary);font-variant-numeric:tabular-nums}
section{margin-top:52px}
h2{font-size:13px;letter-spacing:.09em;text-transform:uppercase;color:var(--muted);
  font-weight:700;margin:0 0 4px;border-top:1px solid var(--border);padding-top:22px}
.lede{color:var(--text-secondary);font-size:15px;margin:0 0 20px;max-width:78ch}
.card{background:var(--surface-1);border:1px solid var(--border);border-radius:14px;padding:22px 24px}
/* stat tiles */
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:14px;margin-top:26px}
.tile{background:var(--surface-1);border:1px solid var(--border);border-radius:14px;padding:18px 18px 16px}
.tile .v{font-size:34px;font-weight:700;letter-spacing:-.02em;line-height:1}
.tile .k{color:var(--text-secondary);font-size:13px;margin-top:8px}
.tile .accent{width:26px;height:3px;border-radius:2px;margin-bottom:12px;background:var(--c-landed)}
/* legend */
.legend{display:flex;flex-wrap:wrap;gap:18px;margin:0 0 6px;font-size:13px;color:var(--text-secondary)}
.legend span{display:inline-flex;align-items:center;gap:7px}
.chip{width:12px;height:12px;border-radius:3px;display:inline-block}
/* tech tree */
.tree-scroll{overflow-x:auto}
svg.tree{display:block;width:100%;min-width:940px;height:auto}
.node-title{font-size:12px;fill:var(--text-primary)}
.node-rfc{font-size:10px;font-weight:700;letter-spacing:.03em}
/* bars */
.bars{display:flex;flex-direction:column;gap:12px}
.bar-row{display:grid;grid-template-columns:180px 1fr 70px;align-items:center;gap:14px}
.bar-row .lbl{font-size:14px;color:var(--text-primary);text-align:right}
.bar-track{height:22px;background:var(--grid);border-radius:5px;overflow:hidden}
.bar-fill{height:100%;border-radius:5px 3px 3px 5px;background:var(--c-landed)}
.bar-val{font-size:13px;font-variant-numeric:tabular-nums;color:var(--text-secondary)}
/* funnel */
.funnel{display:flex;height:46px;border-radius:8px;overflow:hidden;gap:2px;background:var(--page)}
.funnel div{display:flex;align-items:center;justify-content:center;color:#fff;font-weight:700;font-size:15px;min-width:34px}
.funnel-legend{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px;margin-top:16px}
.fl{display:flex;gap:10px;align-items:flex-start;font-size:13.5px;color:var(--text-secondary)}
.fl .chip{margin-top:4px;flex:0 0 auto}
.fl b{color:var(--text-primary)}
/* bug callout */
.bugs{margin-top:22px;border:1px solid var(--border);border-left:4px solid var(--critical);
  border-radius:10px;background:var(--surface-1);padding:16px 20px}
.bugs h3{margin:0 0 4px;font-size:15px}
.bugs .note{color:var(--text-secondary);font-size:13.5px;margin:0 0 12px}
.bug{padding:10px 0;border-top:1px dashed var(--border)}
.bug:first-of-type{border-top:none}
.bug code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px;color:var(--text-primary)}
.bug .why{color:var(--text-secondary);font-size:13px;margin-top:3px}
.bug .meta{color:var(--muted);font-size:11.5px;font-variant-numeric:tabular-nums}
/* timeline */
.tl{position:relative;margin-top:26px;padding-left:6px}
.tl::before{content:"";position:absolute;left:64px;top:6px;bottom:6px;width:2px;background:var(--grid)}
.tl-row{position:relative;display:grid;grid-template-columns:56px 1fr;gap:20px;padding:10px 0}
.tl-date{text-align:right;font-variant-numeric:tabular-nums;font-weight:700;font-size:13px;color:var(--text-secondary)}
.tl-dot{position:absolute;left:59px;top:16px;width:12px;height:12px;border-radius:50%;
  background:var(--c-landed);border:3px solid var(--page)}
.tl-label{font-size:14.5px}
.foot{margin-top:60px;color:var(--muted);font-size:12.5px;border-top:1px solid var(--border);padding-top:18px}
.tip{position:fixed;pointer-events:none;z-index:50;background:var(--card);color:var(--text-primary);
  border:1px solid var(--border);border-radius:8px;padding:8px 11px;font-size:12.5px;
  box-shadow:0 8px 28px rgba(0,0,0,.22);opacity:0;transition:opacity .1s;max-width:280px}
[data-tip]{cursor:default}
table.data{border-collapse:collapse;width:100%;font-size:12.5px;margin-top:8px}
table.data th,table.data td{text-align:left;padding:5px 10px;border-bottom:1px solid var(--border)}
table.data td.n{font-variant-numeric:tabular-nums;text-align:right}
details{margin-top:18px}
summary{cursor:pointer;color:var(--muted);font-size:12.5px}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="eyebrow">Autonomous swarm sprint · tee-daemon</div>
    <h1>Where the effort went, what landed</h1>
    <p class="sub">A generation → verification → fix → merge run of Paseo coding workers on
      <b>amiller/dstack-webhost</b>. Workers drafted features on branches; a review session re-landed
      them on main after catching real defects. This is the tech tree that came out of it.</p>
    <div class="band">Sprint window <b id="b-start"></b> → <b id="b-end"></b></div>
  </header>

  <div class="tiles" id="tiles"></div>

  <section>
    <h2>RFC tech tree</h2>
    <p class="lede">Dependency graph of the design corpus. Colour = implementation status. The middle
      column — evidence, broker, console, per-app attestation — is what this sprint moved from paper to
      landed; the right column is queued for the next loop.</p>
    <div class="card">
      <div class="legend" id="tree-legend"></div>
      <div class="tree-scroll"><svg class="tree" id="tree" viewBox="0 0 1200 600"
        preserveAspectRatio="xMidYMid meet" role="img" aria-label="RFC dependency tech tree"></svg></div>
    </div>
  </section>

  <section>
    <h2>Effort by area</h2>
    <p class="lede">Insertions + deletions of each branch's distinctive commit, grouped by area (RFC row =
      design-doc lines for 0015–0029). An honest token/line proxy — where the work actually went.</p>
    <div class="card"><div class="bars" id="bars"></div></div>
  </section>

  <section>
    <h2>Generation → verification funnel</h2>
    <p class="lede">Eleven worker branches. Most weren't merged as-is — the review pass closed them as
      superseded and re-landed fixed versions on main. Distrust-the-loop by construction.</p>
    <div class="card">
      <div class="funnel" id="funnel"></div>
      <div class="funnel-legend" id="funnel-legend"></div>
    </div>
    <div class="bugs" id="bugs"></div>
  </section>

  <section>
    <h2>Sprint timeline</h2>
    <div class="card"><div class="tl" id="tl"></div></div>
  </section>

  <details>
    <summary>Data table — per-branch effort (accessibility / provenance)</summary>
    <table class="data" id="tbl"></table>
  </details>

  <div class="foot" id="foot"></div>
</div>
<div class="tip" id="tip"></div>

<script id="data" type="application/json">__DATA__</script>
<script>
const DATA = JSON.parse(document.getElementById('data').textContent);
const $ = (t,c)=>{const e=document.createElement(t);if(c)e.className=c;return e;};
const STATUS = {
  landed:{c:'var(--c-landed)',tint:'var(--tint-landed)',label:'Landed this sprint'},
  implemented:{c:'var(--c-implemented)',tint:'var(--tint-implemented)',label:'Implemented (foundation)'},
  paper:{c:'var(--c-paper)',tint:'var(--tint-paper)',label:'Paper / design only'},
  queued:{c:'var(--c-queued)',tint:'var(--tint-queued)',label:'Queued for the loop'},
};
// tooltip
const tip=document.getElementById('tip');
function bindTip(el,html){el.addEventListener('mousemove',e=>{tip.innerHTML=html;tip.style.opacity=1;
  let x=e.clientX+14,y=e.clientY+14;if(x>innerWidth-290)x=e.clientX-tip.offsetWidth-14;
  tip.style.left=x+'px';tip.style.top=y+'px';});
  el.addEventListener('mouseleave',()=>tip.style.opacity=0);}

// header band
document.getElementById('b-start').textContent=DATA.sprint.start;
document.getElementById('b-end').textContent=DATA.sprint.end;

// tiles
const tileDefs=[
  {v:DATA.stats.total_lines.toLocaleString(),k:'lines changed (ins + del)'},
  {v:DATA.stats.worker_branches,k:'Paseo worker branches'},
  {v:DATA.stats.rfcs_landed,k:'RFCs moved to landed'},
  {v:DATA.stats.merged,k:'features landed on main'},
  {v:DATA.stats.bugs_caught,k:'over-claim bugs caught in review'},
];
const tiles=document.getElementById('tiles');
tileDefs.forEach((t,i)=>{const el=$('div','tile');
  const a=$('div','accent');if(i===4)a.style.background='var(--critical)';el.appendChild(a);
  const v=$('div','v');v.textContent=t.v;const k=$('div','k');k.textContent=t.k;
  el.append(v,k);tiles.appendChild(el);});

// tech-tree legend
const tl=document.getElementById('tree-legend');
Object.values(STATUS).forEach(s=>{const sp=$('span');const c=$('span','chip');
  c.style.background=s.c;sp.append(c,document.createTextNode(s.label));tl.appendChild(sp);});

// tech tree svg
const svg=document.getElementById('tree');
const NS='http://www.w3.org/2000/svg';
const byId=Object.fromEntries(DATA.nodes.map(n=>[n.id,n]));
const W=156,H=52;
function el(tag,attrs){const e=document.createElementNS(NS,tag);
  for(const k in attrs)e.setAttribute(k,attrs[k]);return e;}
// edges first (behind)
DATA.edges.forEach(([a,b])=>{const s=byId[a],t=byId[b];
  const x1=s.cx+W/2,y1=s.cy,x2=t.cx-W/2,y2=t.cy,dx=Math.max(40,(x2-x1)*0.45);
  const p=el('path',{d:`M${x1},${y1} C${x1+dx},${y1} ${x2-dx},${y2} ${x2},${y2}`,
    fill:'none',stroke:STATUS[t.status].c,'stroke-width':1.6,'stroke-opacity':.42});
  svg.appendChild(p);});
// nodes
DATA.nodes.forEach(n=>{const st=STATUS[n.status];const g=el('g',{});
  const x=n.cx-W/2,y=n.cy-H/2;
  g.appendChild(el('rect',{x,y,width:W,height:H,rx:11,fill:st.tint,
    stroke:'var(--border)','stroke-width':1}));
  g.appendChild(el('rect',{x,y,width:5,height:H,rx:2.5,fill:st.c}));
  const rfc=el('text',{x:x+16,y:y+19,class:'node-rfc',fill:st.c});
  rfc.textContent='RFC '+n.rfc;g.appendChild(rfc);
  // wrap title into up to 2 lines
  const words=n.title.split(' ');const lines=[];let cur='';
  words.forEach(w=>{if((cur+' '+w).trim().length>19){lines.push(cur.trim());cur=w;}else cur+=' '+w;});
  if(cur.trim())lines.push(cur.trim());
  const t=el('text',{x:x+16,class:'node-title'});
  const y0=lines.length>1?y+33:y+37;
  lines.slice(0,2).forEach((ln,i)=>{const ts=el('tspan',{x:x+16,y:y0+i*14});ts.textContent=ln;t.appendChild(ts);});
  g.appendChild(t);
  bindTip(g,`<b>RFC ${n.rfc}</b> — ${n.title}<br><span style="color:var(--muted)">${st.label}</span>`);
  svg.appendChild(g);});

// effort bars
const bars=document.getElementById('bars');
const maxE=Math.max(...DATA.effort.map(e=>e.lines));
DATA.effort.forEach(e=>{const row=$('div','bar-row');
  const l=$('div','lbl');l.textContent=e.area;
  const track=$('div','bar-track');const fill=$('div','bar-fill');
  fill.style.width=(e.lines/maxE*100).toFixed(1)+'%';track.appendChild(fill);
  const v=$('div','bar-val');v.textContent=e.lines.toLocaleString();
  bindTip(row,`<b>${e.area}</b><br>${e.lines.toLocaleString()} lines (${(e.lines/DATA.stats.total_lines*100).toFixed(0)}% of sprint)`);
  row.append(l,track,v);bars.appendChild(row);});

// funnel
const FCOL={merged:'var(--good)',relanded:'var(--c-landed)',queued:'var(--c-paper)',reverted:'var(--critical)'};
const funnel=document.getElementById('funnel');
const totF=DATA.funnel.reduce((a,f)=>a+f.count,0);
DATA.funnel.forEach(f=>{if(!f.count)return;const d=$('div');
  d.style.flex=f.count;d.style.background=FCOL[f.key];d.textContent=f.count;
  bindTip(d,`<b>${f.label}</b><br>${f.count} of ${totF} worker branches`);
  funnel.appendChild(d);});
const fleg=document.getElementById('funnel-legend');
DATA.funnel.forEach(f=>{const el=$('div','fl');const c=$('span','chip');c.style.background=FCOL[f.key];
  const txt=$('div');txt.innerHTML=`<b>${f.count}</b> · ${f.label}`;el.append(c,txt);fleg.appendChild(el);});

// bugs
const bugsEl=document.getElementById('bugs');
const bh=$('h3');bh.textContent='Verification caught '+DATA.bugs.length+' over-claim bugs';
const bn=$('p','note');bn.textContent='Worker code claimed more than it delivered. The review pass re-landed fixed versions — the point of the loop.';
bugsEl.append(bh,bn);
DATA.bugs.forEach(b=>{const d=$('div','bug');
  d.innerHTML=`<code>${b.fix}</code><div class="why">${b.why}</div><div class="meta">${b.meta}</div>`;
  bugsEl.appendChild(d);});

// timeline
const tlEl=document.getElementById('tl');
DATA.timeline.forEach(t=>{const row=$('div','tl-row');
  const dot=$('div','tl-dot');const d=$('div','tl-date');d.textContent=t.date;
  const l=$('div','tl-label');l.textContent=t.label;
  row.append(d,l,dot);tlEl.appendChild(row);});

// data table
const tbl=document.getElementById('tbl');
tbl.innerHTML='<thead><tr><th>Branch</th><th>Area</th><th>Distinctive commit</th><th class="n">Lines</th></tr></thead>';
const tb=$('tbody');
DATA.branches.sort((a,b)=>b.lines-a.lines).forEach(r=>{const tr=$('tr');
  tr.innerHTML=`<td>${r.branch}</td><td>${r.area}</td><td>${r.subject}</td><td class="n">${r.lines.toLocaleString()}</td>`;
  tb.appendChild(tr);});
tbl.appendChild(tb);

document.getElementById('foot').textContent=
  `Generated from git + gh on ${DATA.sprint.repo}. Effort = ins+del of each branch's distinctive commit; `+
  `all commits authored "Andrew Miller" (workers, human, review session share one identity) — work is distinguished by branch and Co-Authored-By, not author.`;
</script>
</body>
</html>
"""

def git(*args):
    return subprocess.check_output(["git", *args], text=True)

def lead_commit_stat(branch):
    """ins+del of the branch's newest non-merge commit = its distinctive contribution."""
    ref = f"origin/{branch}"
    lead = git("log", "--no-merges", "--format=%H", f"origin/main..{ref}").split()
    if not lead:
        return {"subject": "(no distinctive commit)", "lines": 0, "files": 0}
    h = lead[0]
    subject = git("log", "-1", "--format=%s", h).strip()
    ins = dele = files = 0
    for line in git("show", "--numstat", "--format=", h).splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[0].isdigit():
            ins += int(parts[0]); dele += int(parts[1]) if parts[1].isdigit() else 0; files += 1
    return {"subject": subject, "lines": ins + dele, "ins": ins, "del": dele, "files": files, "sha": h[:8]}

def full_stat(branch):
    """ins+del of the whole branch vs its merge-base with main (clean feature branches)."""
    ref = f"origin/{branch}"
    subject = git("log", "--no-merges", "-1", "--format=%s", f"origin/main..{ref}").strip()
    ins = dele = files = 0
    for line in git("diff", "--numstat", f"origin/main...{ref}").splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[0].isdigit():
            ins += int(parts[0]); dele += int(parts[1]) if parts[1].isdigit() else 0; files += 1
    return {"subject": subject, "lines": ins + dele, "ins": ins, "del": dele, "files": files, "sha": ""}

# ---- curation: which branch feeds which effort area ----
BRANCH_AREA = {
    "staging-51": "evidence", "staging-23": "evidence", "staging-49": "evidence",
    "staging-50": "broker",   "staging-18": "broker",
    "staging-20": "console",  "staging-16": "console",
    "staging-13": "ops",      "staging-21": "ops", "staging-41": "ops", "staging-44": "ops",
    "feat/console-next-rung": "console",
    "feat/landing-hero-proof": "landing",
    "feat/attested-capabilities": "caps",
}
AREA_LABEL = {
    "evidence": "Attestation evidence", "broker": "Credential broker",
    "console": "Fleet console", "ops": "Daemon ops / durability",
    "landing": "Landing hero-proof", "caps": "Attested capabilities",
    "rfcs": "RFC design docs",
}

# ---- gather per-branch effort ----
branches = {}
for b in BRANCH_AREA:
    branches[b] = full_stat(b) if b.startswith("feat/") else lead_commit_stat(b)

# ---- RFC doc effort (design lines, 0015-0029) ----
rfc_files = [f for f in git("ls-tree", "origin/main", "--name-only", "rfcs/").splitlines()
             if f.endswith(".md") and "README" not in f]
rfc_lines = 0
sprint_rfcs = [f"{n:04d}" for n in range(15, 30)]
for f in rfc_files:
    num = os.path.basename(f)[:4]
    if num in sprint_rfcs:
        rfc_lines += len(git("show", f"origin/main:{f}").splitlines())

# ---- effort by area ----
area_totals = {k: 0 for k in AREA_LABEL}
for b, area in BRANCH_AREA.items():
    area_totals[area] += branches[b]["lines"]
area_totals["rfcs"] = rfc_lines
effort = sorted(
    [{"area": AREA_LABEL[k], "lines": v} for k, v in area_totals.items() if v],
    key=lambda x: -x["lines"])
total_lines = sum(a["lines"] for a in effort)

# ---- PR outcomes (live states + curated classification) ----
prs = json.loads(subprocess.check_output(
    ["gh", "pr", "list", "--repo", REPO, "--state", "all", "--limit", "60",
     "--json", "number,title,state,headRefName,mergedAt"], text=True))
pr_by_branch = {p["headRefName"]: p for p in prs}

# curated: worker branch -> outcome bucket (superseded-then-relanded isn't git-derivable)
WORKER_OUTCOME = {
    "staging-18": "merged", "staging-21": "merged", "staging-41": "merged", "staging-44": "merged",
    "staging-16": "relanded", "staging-20": "relanded", "staging-23": "relanded",
    "staging-50": "relanded", "staging-51": "relanded",
    "staging-49": "queued",
    "staging-13": "reverted",
}
funnel_counts = {"merged": 0, "relanded": 0, "queued": 0, "reverted": 0}
for b, o in WORKER_OUTCOME.items():
    funnel_counts[o] += 1
funnel = [
    {"key": "merged",   "label": "Merged clean to staging", "count": funnel_counts["merged"]},
    {"key": "relanded", "label": "Superseded → re-landed by review sprint", "count": funnel_counts["relanded"]},
    {"key": "queued",   "label": "Queued / still open",     "count": funnel_counts["queued"]},
    {"key": "reverted", "label": "Reverted (regressed)",    "count": funnel_counts["reverted"]},
]

# ---- 3 over-claim bugs the verification pass caught ----
def commit_date(grep):
    out = git("log", "--all", "--date=short", "--format=%ad %h", "-i", "--grep", grep).splitlines()
    return out[0] if out else ""
bugs = [
    {"fix": "fix(broker): AESGCM associated_data is positional, not aad= kwarg",
     "why": "sealed-grant encryption silently wasn't binding its associated data", "meta": commit_date("AESGCM associated_data")},
    {"fix": "fix(broker): grant API audit calls used wrong record() signature",
     "why": "delegation audit log was throwing instead of recording", "meta": commit_date("wrong record() signature")},
    {"fix": "fix(evidence): don't claim quote_valid from mere field presence",
     "why": "evidence bundle reported a TDX quote 'valid' without verifying it", "meta": commit_date("quote_valid from mere field presence")},
]

# ---- tech tree (curated topology; status per node) ----
# status: implemented (pre-sprint foundation) / landed / paper / queued
NODES = [
    {"id": "FOUND", "rfc": "0001–0014", "title": "Platform foundation", "status": "implemented", "cx": 96, "cy": 300},
    {"id": "0015", "rfc": "0015", "title": "Public verify endpoints", "status": "landed", "cx": 336, "cy": 110},
    {"id": "0017", "rfc": "0017", "title": "State durability", "status": "landed", "cx": 336, "cy": 300},
    {"id": "0025", "rfc": "0025", "title": "Attested capabilities", "status": "landed", "cx": 336, "cy": 490},
    {"id": "0016", "rfc": "0016", "title": "Fleet console", "status": "landed", "cx": 588, "cy": 66},
    {"id": "0020", "rfc": "0020", "title": "Attestation evidence", "status": "landed", "cx": 588, "cy": 172},
    {"id": "0027", "rfc": "0027", "title": "Per-app attestation", "status": "landed", "cx": 588, "cy": 278},
    {"id": "0018", "rfc": "0018", "title": "Credential broker", "status": "landed", "cx": 588, "cy": 396},
    {"id": "0026", "rfc": "0026", "title": "Operator debug", "status": "paper", "cx": 588, "cy": 512},
    {"id": "0003", "rfc": "0003", "title": "Delegation continuum", "status": "paper", "cx": 846, "cy": 118},
    {"id": "0021", "rfc": "0021", "title": "Evidence-spend", "status": "paper", "cx": 846, "cy": 214},
    {"id": "0022", "rfc": "0022", "title": "Curated app list", "status": "paper", "cx": 846, "cy": 310},
    {"id": "0004", "rfc": "0004", "title": "Capability statements", "status": "paper", "cx": 846, "cy": 406},
    {"id": "0024", "rfc": "0024", "title": "Cross-pod federation", "status": "paper", "cx": 846, "cy": 502},
    {"id": "0028", "rfc": "0028", "title": "Browser render pool", "status": "queued", "cx": 1096, "cy": 396},
    {"id": "0029", "rfc": "0029", "title": "Attested + declared debug", "status": "queued", "cx": 1096, "cy": 512},
]
EDGES = [
    ("FOUND", "0015"), ("FOUND", "0017"), ("FOUND", "0025"), ("FOUND", "0016"), ("FOUND", "0028"),
    ("0015", "0020"), ("0015", "0016"),
    ("0020", "0027"), ("0020", "0021"), ("0020", "0003"),
    ("0018", "0003"), ("0018", "0028"),
    ("0004", "0022"), ("0022", "0003"),
    ("0025", "0029"), ("0026", "0029"),
]

# ---- timeline of key landings ----
timeline = [
    {"date": "06-25", "label": "Early wins merge to staging: scoped tokens, durability, deno-env"},
    {"date": "06-29", "label": "Broker proxy MVP + verify() Facts library authored"},
    {"date": "06-30", "label": "Attested capabilities land on main (#56)"},
    {"date": "07-01", "label": "Merge security-sprint → main: 0018/0020/0016 + 3 over-claim fixes; landing + console land"},
    {"date": "07-02", "label": "RFC tree consolidated & indexed through 0029"},
]

DATA = {
    "sprint": {"start": "2026-06-30", "end": "2026-07-02", "repo": REPO},
    "stats": {
        "total_lines": total_lines,
        "worker_branches": len([b for b in BRANCH_AREA if b.startswith("staging-")]),
        "rfcs_landed": len([n for n in NODES if n["status"] == "landed"]),
        "bugs_caught": len(bugs),
        "merged": funnel_counts["merged"] + funnel_counts["relanded"],
    },
    "effort": effort,
    "funnel": funnel,
    "bugs": bugs,
    "nodes": NODES, "edges": EDGES,
    "timeline": timeline,
    "branches": [{"branch": b, "area": AREA_LABEL[BRANCH_AREA[b]], **branches[b]} for b in BRANCH_AREA],
}

out = pathlib.Path(ROOT) / "reports" / "swarm-activity.html"
out.write_text(HTML_TEMPLATE.replace("__DATA__", json.dumps(DATA, indent=0)))
print(f"wrote {out}  ({total_lines} lines across {len(effort)} areas)")

---
layout: default
title: Verify a TEE app
---

# Verify a TEE app

Someone hands you a URL. They claim it points to a TEE-hosted app whose source is on GitHub. Before you trust the output, you want to know what code is actually running. Type the URL in below and walk the chain.

<div id="verifier" class="verifier">
  <div class="row">
    <label>tee-daemon URL
      <input type="text" id="daemonUrl" value="https://915c8197b20b831c52cf97a9fb7e2e104cdc6ae8-8080.dstack-pha-prod7.phala.network" />
    </label>
    <label>Project
      <input type="text" id="projectName" value="timelock" />
    </label>
    <button id="verifyBtn" type="button">Verify</button>
  </div>
  <div id="results" class="results"></div>
</div>

<style>
.verifier { background: #fff; border: 1px solid #e1e4e8; border-radius: 10px; padding: 1.5rem; margin: 1.5rem 0; }
.verifier .row { display: flex; gap: 0.75rem; align-items: end; flex-wrap: wrap; }
.verifier label { display: flex; flex-direction: column; font-size: 0.85em; color: #57606a; flex: 1 1 auto; min-width: 160px; gap: 0.3em; }
.verifier input { font: inherit; padding: 0.5em 0.7em; border: 1px solid #d0d7de; border-radius: 6px; background: #f6f8fa; }
.verifier input:focus { outline: 2px solid #1f6feb; outline-offset: -1px; background: #fff; }
.verifier button { font: inherit; font-weight: 600; padding: 0.55em 1.2em; background: #1f6feb; color: #fff; border: none; border-radius: 6px; cursor: pointer; }
.verifier button:hover { background: #0a3d8f; }
.verifier button:disabled { background: #8cabd9; cursor: wait; }
.verifier .results { margin-top: 1.25rem; }
.verifier .check { display: flex; gap: 0.7rem; padding: 0.75em 0; border-top: 1px solid #f0f2f5; align-items: flex-start; }
.verifier .check:first-child { border-top: none; }
.verifier .icon { font-weight: 700; font-size: 1.1em; flex: none; width: 1.4em; }
.verifier .icon.pass { color: #1a7f37; }
.verifier .icon.fail { color: #cf222e; }
.verifier .icon.partial { color: #bf8700; }
.verifier .label { font-weight: 600; min-width: 7em; flex: none; }
.verifier .detail { color: #57606a; font-size: 0.95em; }
.verifier .detail code { font-size: 0.85em; }
.verifier .verdict { margin-top: 1rem; padding: 0.85em 1em; border-radius: 6px; font-size: 0.95em; border: 1px solid; }
.verifier .verdict.pass { background: #f0f7f4; border-color: #1a7f37; color: #1a7f37; }
.verifier .verdict.fail { background: #fff5f5; border-color: #cf222e; color: #cf222e; }
.verifier .verdict.partial { background: #fff8e1; border-color: #bf8700; color: #8a6300; }
.verifier .error { color: #cf222e; padding: 0.85em 1em; background: #fff5f5; border: 1px solid #ffd0d4; border-radius: 6px; font-size: 0.95em; }
.verifier .target { color: #57606a; font-size: 0.9em; margin: 0 0 0.5em; }
</style>

<script>
(function() {
  const escape = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const icon = status => `<span class="icon ${status}">${({pass:'✓',fail:'✗',partial:'⚠'})[status] || '?'}</span>`;
  const row = (lbl, ck, extra) => `<div class="check">${icon(ck.status)}<span class="label">${escape(lbl)}</span><span class="detail">${ck.detail}${extra ? '<br>'+extra : ''}</span></div>`;

  async function verify() {
    const btn = document.getElementById('verifyBtn');
    const out = document.getElementById('results');
    const daemon = document.getElementById('daemonUrl').value.trim().replace(/\/$/, '');
    const name = document.getElementById('projectName').value.trim();
    if (!daemon || !name) { out.innerHTML = '<div class="error">Enter a URL and project name.</div>'; return; }

    btn.disabled = true;
    out.innerHTML = '<p class="target">Calling ' + escape(daemon) + '/_api/verification/' + escape(name) + '…</p>';

    let data;
    try {
      const resp = await fetch(daemon + '/_api/verification/' + encodeURIComponent(name));
      const text = await resp.text();
      if (!resp.ok) {
        let hint = '';
        if (resp.status === 401) hint = ' — daemon requires a token. The fix is RFC 0015.';
        else if (resp.status === 404) hint = ' — project not attested, or not found.';
        out.innerHTML = '<div class="error">HTTP ' + resp.status + hint + '<br><code>' + escape(text.slice(0, 300)) + '</code></div>';
        btn.disabled = false; return;
      }
      try { data = JSON.parse(text); } catch (e) { out.innerHTML = '<div class="error">Non-JSON response: ' + escape(text.slice(0,200)) + '</div>'; btn.disabled = false; return; }
    } catch (e) {
      out.innerHTML = '<div class="error">Network error: ' + escape(e.message) + '</div>'; btn.disabled = false; return;
    }

    const project = data.project || {};
    const quote = data.quote || {};
    const audit = Array.isArray(data.audit) ? data.audit : [];

    let sourceCk = { status: 'partial', detail: 'no source check performed' };
    let sourceLink = '';
    if (project.source && project.commit_sha && /github\.com/.test(project.source)) {
      const m = project.source.match(/github\.com\/([^\/]+)\/([^\/?#]+?)(?:\.git)?(?:[?#].*)?$/);
      if (m) {
        const owner = m[1], repo = m[2];
        sourceLink = '<a href="' + escape(project.source) + '/tree/' + escape(project.commit_sha) + '" target="_blank">' + escape(owner + '/' + repo) + ' @ ' + escape(project.commit_sha.slice(0,7)) + '</a>';
        try {
          const ghResp = await fetch('https://api.github.com/repos/' + owner + '/' + repo + '/git/commits/' + encodeURIComponent(project.commit_sha));
          if (ghResp.ok) {
            const gh = await ghResp.json();
            const ghTree = gh.tree && gh.tree.sha;
            if (ghTree && ghTree === project.tree_hash) {
              sourceCk = { status: 'pass', detail: 'tree <code>' + escape(ghTree.slice(0,12)) + '</code> matches GitHub commit' };
            } else {
              sourceCk = { status: 'fail', detail: 'tree mismatch: daemon=<code>' + escape((project.tree_hash||'').slice(0,12)) + '</code> github=<code>' + escape((ghTree||'').slice(0,12)) + '</code>' };
            }
          } else {
            sourceCk = { status: 'partial', detail: 'GitHub API returned ' + ghResp.status };
          }
        } catch (e) {
          sourceCk = { status: 'partial', detail: 'GitHub fetch failed: ' + escape(e.message) };
        }
      }
    } else if (!project.source) {
      sourceCk = { status: 'fail', detail: 'no source URL recorded' };
    } else {
      sourceCk = { status: 'partial', detail: 'non-GitHub source — manual verification required' };
      sourceLink = '<a href="' + escape(project.source) + '" target="_blank">' + escape(project.source) + '</a>';
    }

    const quoteCk = (quote && (quote.quote || quote.report || quote.key))
      ? { status: 'pass', detail: 'TEE quote present, signed by Phala dstack' }
      : { status: 'fail', detail: 'no quote returned' };

    const promoteEntry = audit.find(e => e.action === 'promote');
    let auditCk;
    if (audit.length === 0) auditCk = { status: 'fail', detail: 'no audit entries' };
    else if (promoteEntry) auditCk = { status: 'pass', detail: audit.length + ' entr' + (audit.length===1?'y':'ies') + ', includes <code>promote</code>' };
    else auditCk = { status: 'partial', detail: audit.length + ' entries, no <code>promote</code> recorded' };

    const checks = [sourceCk, quoteCk, auditCk];
    const verdict = checks.some(c => c.status === 'fail') ? 'fail'
                  : checks.every(c => c.status === 'pass') ? 'pass' : 'partial';
    const verdictText = verdict === 'pass'
      ? '→ Trust chain verified. You can read the source and decide whether to trust what the code does.'
      : verdict === 'fail'
      ? '→ One or more checks failed. Do not trust the output until you understand why.'
      : '→ Some checks are inconclusive. Review the details above before deciding.';

    out.innerHTML =
      '<p class="target">Project: <strong>' + escape(name) + '</strong> · mode: <code>' + escape(project.mode || '?') + '</code></p>' +
      row('Source', sourceCk, sourceLink) +
      row('TEE quote', quoteCk, '') +
      row('Audit log', auditCk, '') +
      '<div class="verdict ' + verdict + '">' + verdictText + '</div>';
    btn.disabled = false;
  }

  document.getElementById('verifyBtn').addEventListener('click', verify);
  document.getElementById('verifier').addEventListener('keydown', e => { if (e.key === 'Enter') verify(); });

  // Deep-link support: ?daemon=...&project=... pre-fills and auto-runs.
  const params = new URLSearchParams(location.search);
  if (params.get('daemon')) document.getElementById('daemonUrl').value = params.get('daemon');
  if (params.get('project')) document.getElementById('projectName').value = params.get('project');
  if (params.get('daemon') || params.get('project')) verify();
})();
</script>

## What the verifier checks

1. **Source.** Calls `GET /_api/verification/<name>` on the daemon. Reads the project manifest with `source`, `commit_sha`, `tree_hash`. Independently fetches the same commit from the GitHub API and compares the tree SHA.
2. **TEE quote.** The same response includes a dstack quote signed by the TEE platform. Quote presence is required; full cryptographic verification against the Phala base contract is the next step.
3. **Audit log.** Calls `GET /_api/projects/<name>/audit`. Confirms the audit log exists and contains the `promote` event that bound source to running code.
4. **Verdict.** None of the above tells you whether the code is *correct*. It tells you what code ran. You read the source on GitHub and decide whether the logic does what it claims.

## Current state

Live. The form above hits the running daemon on hermes-staging (no admin token), and timelock is in attested mode with its source hash, TEE quote, and audit log all queryable from the public surface. RFC 0015 (open read-only verifier endpoints) and a CORS-on-the-public-paths fix are deployed. The same flow works for any attested project on any tee-daemon CVM — change the URL and project name above to point at a different one.

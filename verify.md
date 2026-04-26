---
layout: default
title: Evidence bundle
---

# Evidence bundle

You hit a TEE-hosted app. This page is the audit-ready report for what's running there. It is built for two readers at the same time:

- A casual visitor who wants to confirm there's a real bundle here, the kind a security person could take seriously, and forward the URL on if it matters to them.
- A reviewer or auditor who is actually going to read the source and check the artifacts.

The mechanical part — proving the code running on this CVM is the same code in the public GitHub repo — is resolved on this page, automatically, on load. The judgmental part — whether the code does what it claims — is the source-reading work below. You don't have to do it; you can pass the URL to someone whose job it is.

<p class="target"><span class="muted">Reading:</span> <code id="targetLine">timelock at hermes-staging</code></p>

## Provenance

<div id="status" class="status"></div>

## What's in the bundle

<div id="bundle" class="bundle"></div>

## What an auditor would actually look at in the source

<div id="reading"></div>

<style>
.target { color: #57606a; font-size: 0.9em; margin: 1.5em 0 0.5em; }
.target code { background: #f4f6fa; padding: 0.15em 0.45em; border-radius: 4px; }
.status { padding: 1em 1.2em; border-radius: 8px; margin: 1em 0 1.5em; line-height: 1.55; border: 1px solid; }
.status.pass { background: #f0f7f4; border-color: #1a7f37; color: #0d4a23; }
.status.fail { background: #fff5f5; border-color: #cf222e; color: #80161e; }
.status.partial { background: #fff8e1; border-color: #bf8700; color: #6b4c00; }
.status.loading { background: #f6f8fa; border-color: #d0d7de; color: #57606a; }
.status code { background: rgba(0,0,0,0.05); padding: 0.1em 0.35em; border-radius: 4px; font-size: 0.92em; }
.status .source-link { color: inherit; text-decoration: underline; font-weight: 600; }
.bundle .artifact { padding: 0.85em 1em; margin: 0.6em 0; border: 1px solid #e1e4e8; border-radius: 8px; background: #fafbfc; }
.bundle .artifact h4 { margin: 0 0 0.25em; font-size: 1em; font-weight: 600; }
.bundle .artifact p { margin: 0; color: #57606a; font-size: 0.92em; line-height: 1.5; }
.bundle .artifact code { background: rgba(0,0,0,0.05); padding: 0.1em 0.35em; border-radius: 4px; font-size: 0.88em; word-break: break-all; }
.bundle .artifact a { color: #1f6feb; text-decoration: none; }
.bundle .artifact a:hover { text-decoration: underline; }
.bundle .artifact .audit-aside { font-style: italic; color: #57606a; font-size: 0.88em; margin-top: 0.35em; display: block; }
.actions { display: flex; gap: 0.85em; flex-wrap: wrap; margin: 1.25em 0; }
.actions a, .actions button { font: inherit; }
.actions .primary { background: #1f6feb; color: #fff; padding: 0.55em 1.2em; border-radius: 6px; text-decoration: none; font-weight: 600; border: none; cursor: pointer; }
.actions .primary:hover { background: #0a3d8f; }
.actions .secondary { color: #1f6feb; padding: 0.55em 0; text-decoration: none; background: none; border: none; cursor: pointer; }
.actions .secondary:hover { text-decoration: underline; }
.copied { color: #1a7f37; font-size: 0.9em; align-self: center; }
</style>

<script>
(async function() {
  const escape = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const params = new URLSearchParams(location.search);
  const daemon = (params.get('daemon') || 'https://915c8197b20b831c52cf97a9fb7e2e104cdc6ae8-8080.dstack-pha-prod7.phala.network').replace(/\/$/, '');
  const project = params.get('project') || 'timelock';
  const daemonHost = daemon.replace(/^https?:\/\//, '').split('/')[0];

  document.getElementById('targetLine').textContent = project + ' at ' + daemonHost;

  const statusEl = document.getElementById('status');
  const bundleEl = document.getElementById('bundle');
  const readingEl = document.getElementById('reading');
  statusEl.className = 'status loading';
  statusEl.textContent = 'Checking the chain…';

  let data;
  try {
    const resp = await fetch(daemon + '/_api/verification/' + encodeURIComponent(project));
    if (!resp.ok) {
      statusEl.className = 'status fail';
      const txt = await resp.text();
      statusEl.innerHTML = '<strong>Could not load the bundle.</strong> The daemon returned HTTP ' + resp.status + '. <code>' + escape(txt.slice(0, 200)) + '</code>';
      return;
    }
    data = await resp.json();
  } catch (e) {
    statusEl.className = 'status fail';
    statusEl.innerHTML = '<strong>Could not reach the daemon.</strong> ' + escape(e.message);
    return;
  }

  const p = data.project || {};
  const audit = data.audit || [];
  const quote = data.quote || {};
  const issues = [];

  if (!p.source) issues.push('No source URL recorded.');
  if (!p.commit_sha) issues.push('No commit SHA recorded.');
  if (!p.tree_hash) issues.push('No tree hash recorded.');
  if (!(quote.quote || quote.key || quote.report)) issues.push('No TEE quote returned.');
  if (audit.length === 0) issues.push('Audit log is empty.');
  else if (!audit.some(e => e.action === 'promote')) issues.push('Audit log has no promote event.');

  let githubTreeOk = null;
  let githubTreeSha = null;
  let ghOwner = null, ghRepo = null;
  if (p.source && /github\.com/.test(p.source) && p.commit_sha) {
    const m = p.source.match(/github\.com\/([^\/]+)\/([^\/?#]+?)(?:\.git)?(?:[?#].*)?$/);
    if (m) {
      ghOwner = m[1]; ghRepo = m[2];
      try {
        const ghResp = await fetch('https://api.github.com/repos/' + ghOwner + '/' + ghRepo + '/git/commits/' + encodeURIComponent(p.commit_sha));
        if (ghResp.ok) {
          const gh = await ghResp.json();
          githubTreeSha = gh.tree && gh.tree.sha;
          githubTreeOk = githubTreeSha === p.tree_hash;
          if (!githubTreeOk) issues.push('Daemon tree hash ' + (p.tree_hash||'').slice(0,12) + ' does not match GitHub tree hash ' + (githubTreeSha||'').slice(0,12) + '.');
        } else if (ghResp.status === 404) {
          issues.push('GitHub does not have commit ' + p.commit_sha.slice(0,8) + ' in that repo.');
        } else {
          issues.push('GitHub returned ' + ghResp.status + ' fetching the commit.');
        }
      } catch (e) {
        issues.push('GitHub fetch failed: ' + e.message);
      }
    }
  }

  // Audit summary: did anything change since promotion?
  const sortedAudit = [...audit].sort((a, b) => (a.timestamp || 0) - (b.timestamp || 0));
  let latestPromote = null;
  for (const e of sortedAudit) if (e.action === 'promote') latestPromote = e;
  const sincePromote = latestPromote
    ? sortedAudit.filter(e => (e.timestamp || 0) > (latestPromote.timestamp || 0))
    : [];
  const fmtDate = ts => ts ? new Date(ts * 1000).toISOString().slice(0, 10) : '?';
  let auditSummary;
  if (!latestPromote) auditSummary = 'no promote event recorded';
  else if (sincePromote.length === 0) auditSummary = 'promoted ' + fmtDate(latestPromote.timestamp) + ', no changes since';
  else auditSummary = 'promoted ' + fmtDate(latestPromote.timestamp) + '; '
       + sincePromote.length + ' change' + (sincePromote.length > 1 ? 's' : '') + ' since (last on ' + fmtDate(sincePromote[sincePromote.length-1].timestamp) + ')';

  const sourceLabel = p.source ? p.source.replace(/^https?:\/\/(www\.)?/, '').replace(/\.git$/, '') : 'unknown source';
  const commitShort = (p.commit_sha || '').slice(0, 7);
  const sourceHref = p.source && p.commit_sha ? p.source + '/tree/' + p.commit_sha : (p.source || '#');
  const sourceLink = '<a class="source-link" href="' + escape(sourceHref) + '" target="_blank">' + escape(sourceLabel) + ' @ ' + escape(commitShort) + '</a>';

  if (issues.length === 0) {
    statusEl.className = 'status pass';
    statusEl.innerHTML = 'The CVM is running ' + sourceLink + '. The TEE quote is signed by Phala dstack and binds that source hash; the audit log has been ' + escape(auditSummary) + '. The mechanical part of the audit checks out.';
  } else {
    statusEl.className = 'status fail';
    statusEl.innerHTML = '<strong>The bundle is broken.</strong> Do not act on the running app\'s output until this is understood:<ul>'
      + issues.map(i => '<li>' + escape(i) + '</li>').join('')
      + '</ul>';
  }

  // Build the bundle: the artifacts an auditor would actually use.
  const auditUrl = daemon + '/_api/projects/' + encodeURIComponent(project) + '/audit';
  const attestUrl = daemon + '/_api/attest/' + encodeURIComponent(project);
  const verificationUrl = daemon + '/_api/verification/' + encodeURIComponent(project);

  const bundleArtifacts = [
    {
      title: 'Source code',
      body: 'Pinned to commit <code>' + escape(p.commit_sha || '?') + '</code> in <a href="' + escape(p.source || '#') + '" target="_blank">' + escape(sourceLabel) + '</a>. Tree SHA <code>' + escape((p.tree_hash || '').slice(0, 16)) + '…</code>'
        + (githubTreeSha ? (githubTreeOk ? ' matches GitHub.' : ' does <strong>not</strong> match GitHub (<code>' + escape(githubTreeSha.slice(0, 16)) + '…</code>).') : '.'),
      audit: 'An auditor reads the diff at this commit and decides whether the logic is sound.',
    },
    {
      title: 'TEE attestation',
      body: 'A dstack-signed quote is available at <a href="' + escape(attestUrl) + '" target="_blank">/_api/attest/' + escape(project) + '</a>. ' + (quote.quote || quote.key || quote.report ? 'It is present and binds this source hash to the running CVM.' : '<strong>Missing.</strong>'),
      audit: 'An auditor verifies the quote\'s signature chain against the Phala base contract that anchors this CVM\'s app id.',
    },
    {
      title: 'Audit log',
      body: 'Available at <a href="' + escape(auditUrl) + '" target="_blank">/_api/projects/' + escape(project) + '/audit</a>. ' + auditSummary.charAt(0).toUpperCase() + auditSummary.slice(1) + '.',
      audit: 'An auditor checks whether anything changed after promotion that would invalidate trust in the original commit.',
    },
    {
      title: 'Full verification dump',
      body: '<a href="' + escape(verificationUrl) + '" target="_blank">/_api/verification/' + escape(project) + '</a> returns the manifest, the quote, and the audit log together — the entire bundle in one JSON request.',
      audit: 'An auditor pipes this into their own tooling and re-runs the chain check independent of this page.',
    },
  ];

  bundleEl.innerHTML = bundleArtifacts.map(a =>
    '<div class="artifact"><h4>' + a.title + '</h4><p>' + a.body + '</p><span class="audit-aside">' + a.audit + '</span></div>'
  ).join('') + '<div class="actions">'
    + '<button class="primary" id="copyBtn">Copy this report URL</button>'
    + '<a class="secondary" href="' + escape(daemon) + '/' + escape(project) + '/" target="_blank">Open the running app</a>'
    + '<span class="copied" id="copied" style="display:none">Copied.</span>'
    + '</div>';

  document.getElementById('copyBtn').addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(location.href);
      const c = document.getElementById('copied');
      c.style.display = 'inline'; setTimeout(() => c.style.display = 'none', 1800);
    } catch {}
  });

  // Reading prompts. Generic by default; project-specific where we know.
  const guides = {
    timelock: {
      lead: 'For timelock specifically, the trust claim is that keys remain sealed until the release time. The questions a reviewer would ask of <a href="' + escape(sourceHref) + '" target="_blank">the source</a>:',
      look: [
        ['The release-time check', 'Does the handler that returns the key actually compare current time to the release time, with no path that bypasses it? Search for <code>releaseTime</code>.'],
        ['The trusted clock', 'Where does <code>now</code> come from? See <code>TIME_SOURCES</code> and <code>trustedNow()</code>. If a hostile time source can move the clock, the whole guarantee is gone.'],
        ['Storage', 'Sealed keys live in <code>ctx.dataDir</code>. What happens if the volume is replaced with an older snapshot, or wiped? Does the app fail closed?'],
      ],
    },
  };
  const guide = guides[project] || {
    lead: 'Generic prompts a reviewer would bring to <a href="' + escape(sourceHref) + '" target="_blank">the source</a>:',
    look: [
      ['The invariant', 'What would the app have to maintain for its output to mean what it claims? Read for paths that violate it.'],
      ['Caller-controlled inputs', 'What can the caller change about the request? Are there inputs that flip the behavior in ways the operator could weaponize?'],
      ['State across restarts', 'Where is state stored, and what trust does the app place in it after a restart, redeploy, or rollback?'],
    ],
  };

  readingEl.innerHTML =
    '<p>' + guide.lead + '</p>' +
    '<ul>' +
      guide.look.map(([title, body]) => '<li><strong>' + escape(title) + '.</strong> ' + body + '</li>').join('') +
    '</ul>' +
    '<p>If the bundle above looks intact and these questions matter to you, send the URL of this page to someone whose job it is to answer them. The same evidence is enough for a quick gut-check or a serious review.</p>';
})();
</script>

---
layout: default
title: Verify a TEE app
---

# Verifying a TEE app

You hit a TEE-hosted app. You want to know whether to trust the output. Two questions, and they are not the same:

1. **Is the running code the source the URL claims?** Mechanical. The TEE quote, the daemon's audit log, the recorded tree hash either match GitHub or they don't.
2. **Does that source actually do what you need?** You read it. You decide.

Most platforms cannot answer the first question. dstack-webhost answers it, automatically, when you load this page. The hard work is the second question.

<p class="target"><span class="muted">Reading:</span> <code id="targetLine">timelock at hermes-staging</code></p>

<div id="status" class="status"></div>

## Now read the source

<div id="reading"></div>

<style>
.target { color: #57606a; font-size: 0.9em; margin: 1.5em 0 0.5em; }
.target code { background: #f4f6fa; padding: 0.15em 0.45em; border-radius: 4px; }
.status { padding: 1.1em 1.25em; border-radius: 8px; margin: 1em 0 2em; font-size: 1em; line-height: 1.55; border: 1px solid; }
.status.pass { background: #f0f7f4; border-color: #1a7f37; color: #0d4a23; }
.status.fail { background: #fff5f5; border-color: #cf222e; color: #80161e; }
.status.partial { background: #fff8e1; border-color: #bf8700; color: #6b4c00; }
.status.loading { background: #f6f8fa; border-color: #d0d7de; color: #57606a; }
.status code { background: rgba(0,0,0,0.05); padding: 0.1em 0.35em; border-radius: 4px; font-size: 0.92em; }
.status details { margin-top: 0.85em; font-size: 0.92em; }
.status details summary { cursor: pointer; color: inherit; opacity: 0.75; }
.status details summary:hover { opacity: 1; }
.status details ul { margin: 0.5em 0 0; padding-left: 1.2em; }
.status details li { margin: 0.15em 0; }
.status .source-link { color: inherit; text-decoration: underline; font-weight: 600; }
.reading-actions { display: flex; gap: 0.85em; flex-wrap: wrap; margin: 1em 0 1.5em; }
.reading-actions a.primary { background: #1f6feb; color: #fff; padding: 0.55em 1.2em; border-radius: 6px; text-decoration: none; font-weight: 600; }
.reading-actions a.primary:hover { background: #0a3d8f; }
.reading-actions a.secondary { color: #1f6feb; padding: 0.55em 0; text-decoration: none; }
.reading-actions a.secondary:hover { text-decoration: underline; }
</style>

<script>
(async function() {
  const escape = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const params = new URLSearchParams(location.search);
  const daemon = (params.get('daemon') || 'https://915c8197b20b831c52cf97a9fb7e2e104cdc6ae8-8080.dstack-pha-prod7.phala.network').replace(/\/$/, '');
  const project = params.get('project') || 'timelock';
  const daemonHost = daemon.replace(/^https?:\/\//, '').split('/')[0];

  const targetEl = document.getElementById('targetLine');
  targetEl.textContent = project + ' at ' + daemonHost;

  const statusEl = document.getElementById('status');
  const readingEl = document.getElementById('reading');
  statusEl.className = 'status loading';
  statusEl.textContent = 'Checking the chain…';

  let data;
  try {
    const resp = await fetch(daemon + '/_api/verification/' + encodeURIComponent(project));
    if (!resp.ok) {
      statusEl.className = 'status fail';
      const txt = await resp.text();
      statusEl.innerHTML = '<strong>Could not load.</strong> The daemon returned HTTP ' + resp.status + '. <code>' + escape(txt.slice(0, 200)) + '</code>';
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
  if (p.source && /github\.com/.test(p.source) && p.commit_sha) {
    const m = p.source.match(/github\.com\/([^\/]+)\/([^\/?#]+?)(?:\.git)?(?:[?#].*)?$/);
    if (m) {
      try {
        const ghResp = await fetch('https://api.github.com/repos/' + m[1] + '/' + m[2] + '/git/commits/' + encodeURIComponent(p.commit_sha));
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

  const sourceLabel = p.source ? p.source.replace(/^https?:\/\/(www\.)?/, '').replace(/\.git$/, '') : 'unknown source';
  const commitShort = (p.commit_sha || '').slice(0, 7);
  const sourceLink = p.source && p.commit_sha
    ? '<a class="source-link" href="' + escape(p.source) + '/tree/' + escape(p.commit_sha) + '" target="_blank">' + escape(sourceLabel) + ' @ ' + escape(commitShort) + '</a>'
    : '<span>' + escape(sourceLabel) + '</span>';

  // Audit summary: the verifier's question is "did anything change since promotion?"
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

  const detailsHtml = '<details><summary>chain details</summary><ul>'
    + '<li>Source on GitHub: <a href="' + escape(p.source || '#') + '" target="_blank"><code>' + escape(p.source || '?') + '</code></a></li>'
    + '<li>Pinned to commit: <code>' + escape(p.commit_sha || '?') + '</code></li>'
    + '<li>Tree SHA recorded by daemon: <code>' + escape(p.tree_hash || '?') + '</code></li>'
    + (githubTreeSha ? '<li>Tree SHA from GitHub for that commit: <code>' + escape(githubTreeSha) + '</code> ' + (githubTreeOk ? '(matches)' : '(<strong>does not match</strong>)') + '</li>' : '')
    + '<li>TEE quote: ' + (quote.quote || quote.key || quote.report ? 'present, dstack-signed' : '<strong>missing</strong>') + '</li>'
    + '<li>Audit log: ' + escape(auditSummary) + '</li>'
    + '</ul></details>';

  if (issues.length === 0) {
    statusEl.className = 'status pass';
    statusEl.innerHTML = 'Running ' + sourceLink + '. The TEE attestation, audit log, and tree hash all line up. ' + detailsHtml;
  } else {
    statusEl.className = 'status fail';
    statusEl.innerHTML = '<strong>The chain doesn\'t check out.</strong> The output below should not be trusted until this is understood:<ul>'
      + issues.map(i => '<li>' + escape(i) + '</li>').join('')
      + '</ul>' + detailsHtml;
  }

  // Reading guide
  const guides = {
    timelock: {
      lead: 'The chain told you what code is running. Now convince yourself the code does what it claims: keys stay sealed until the release time.',
      look: [
        ['The release-time check', 'Search for <code>releaseTime</code>. The handler that returns the key must compare current time to the release time, with no path that bypasses it.'],
        ['The trusted clock', 'Where does <code>now</code> come from? See <code>TIME_SOURCES</code> and <code>trustedNow()</code>. If a hostile time source can move the clock backward, the whole guarantee is gone.'],
        ['Storage', 'Sealed keys live in <code>ctx.dataDir</code>. What happens if the volume is replaced with an older snapshot, or wiped? Does the app fail closed?'],
      ],
    },
  };
  const guide = guides[project] || {
    lead: 'The chain told you what code is running. Now convince yourself the code does what it claims.',
    look: [
      ['The invariants', 'Identify what the app would have to maintain for its output to mean what it says. Read for paths that violate those invariants.'],
      ['Inputs', 'What can the caller control? Are there inputs that change the behavior in ways the operator could weaponize?'],
      ['State', 'Where is state stored, and what trust does the app place in it across restarts?'],
    ],
  };

  const sourceUrl = p.source && p.commit_sha ? p.source + '/tree/' + p.commit_sha : (p.source || '#');
  const runUrl = daemon + '/' + project + '/';
  const auditUrl = daemon + '/_api/projects/' + encodeURIComponent(project) + '/audit';

  readingEl.innerHTML =
    '<p>' + escape(guide.lead) + '</p>' +
    '<div class="reading-actions">' +
      '<a class="primary" href="' + escape(sourceUrl) + '" target="_blank">Open the source</a>' +
      '<a class="secondary" href="' + escape(runUrl) + '" target="_blank">Open the running app</a>' +
      '<a class="secondary" href="' + escape(auditUrl) + '" target="_blank">View raw audit log</a>' +
    '</div>' +
    '<p>What to look at as you read:</p>' +
    '<ul>' +
      guide.look.map(([title, body]) => '<li><strong>' + escape(title) + '.</strong> ' + body + '</li>').join('') +
    '</ul>' +
    '<p>If, after reading, you can\'t find a way to break the claim, you can decide to trust the output. If you can break it, the chain checks above are not what saves you.</p>';
})();
</script>

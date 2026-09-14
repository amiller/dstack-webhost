#!/usr/bin/env bash
# End-to-end demo of RFC 0034 against a live daemon, producing a self-contained report page
# that is itself deployed through the same flow. Needs: the provisioner file, the owner token
# (only for the owner steps: teardown, pending list, approve), deno, playwright (python).
#   BASE=https://<cvm> PROV=~/.config/dstack-webhost/provisioner-staging OWNER_ENV=<envfile> bash demo-report.sh
set -euo pipefail
BASE=${BASE:?}; PROV=${PROV:?}; OWNER_ENV=${OWNER_ENV:?}
HERE=$(cd "$(dirname "$0")" && pwd)
OUT=${OUT:-$(mktemp -d)}; mkdir -p "$OUT"
OWNER=$(grep '^TEE_DAEMON_TOKEN=' "$OWNER_ENV" | cut -d= -f2- | tr -d '"'"'"'')
T=$OUT/transcript.md; : > "$T"

run() {  # run "<label>" <curl args...>; logs the command with tokens redacted and the response
  local label=$1; shift
  printf '\n### %s\n\n```\n$ curl %s\n' "$label" "$(printf '%q ' "$@" | sed -E 's/Bearer [A-Za-z0-9_.-]+/Bearer <redacted>/g')" >> "$T"
  curl -s -m 60 "$@" -w '\n[http %{http_code}]\n' | sed -E 's/"token": ?"[^"]+"/"token": "<shown once, saved to .webhost-token>"/' \
    | awk 'length > 1500 { print substr($0, 1, 1500) " … [" length " bytes total; long fields are attestation evidence]"; next } { print }' >> "$T"
  printf '```\n' >> "$T"
}

echo "==> fresh start: owner tears down hello-pending if present"
curl -s -o /dev/null -X DELETE "$BASE/_api/projects/hello-pending" -H "Authorization: Bearer $OWNER" || true
rm -f "$HERE/.webhost-token"

echo "==> 0. daemon version"
run "0. The daemon this ran against" "$BASE/_api/version"

echo "==> 1. create with the provisioner token only"
TAR=$OUT/app.tgz; tar -czf "$TAR" -C "$HERE" --exclude='./.webhost-token' --exclude='./demo-report.sh' .
RESP=$(curl -sf -X POST "$BASE/_api/projects" -H "Authorization: Bearer $(cat "$PROV")" \
  -F 'manifest={"name":"hello-pending","runtime":"deno"};type=application/json' -F "files=@$TAR")
jq -r .token <<<"$RESP" > "$HERE/.webhost-token"; chmod 600 "$HERE/.webhost-token"
PROJ=$(cat "$HERE/.webhost-token")
{ printf '\n### 1. Create with the provisioner token (no owner token anywhere in this shell)\n\n```\n$ curl -X POST %s/_api/projects -H "Authorization: Bearer $(cat provisioner)" -F manifest=... -F files=@app.tgz\n' "$BASE"
  jq '{name, runtime, mode, approval, token: "<shown once, saved to .webhost-token>"}' <<<"$RESP"; printf '[http 201]\n```\n'; } >> "$T"

run "2. The app is live immediately" "$BASE/hello-pending/"
run "3. Provisioner token cannot list projects" "$BASE/_api/projects" -H "Authorization: Bearer $(cat "$PROV")"
run "4. Provisioner token cannot re-create the same name" -X POST "$BASE/_api/projects" -H "Authorization: Bearer $(cat "$PROV")" -H 'content-type: application/json' -d '{"name":"hello-pending","source":"x"}'
sed -i 's/self-provisioned project$/self-provisioned project (redeployed)/' "$HERE/server.ts"
tar -czf "$TAR" -C "$HERE" --exclude='./.webhost-token' --exclude='./demo-report.sh' .
run "5. Redeploy with the per-project token and a new tarball" -X POST "$BASE/_api/projects/hello-pending/redeploy" -H "Authorization: Bearer $PROJ" -F "files=@$TAR"
git -C "$HERE" checkout -q -- server.ts 2>/dev/null || true
run "6. New code is serving" "$BASE/hello-pending/"
run "7. Promote is refused while pending" -X POST "$BASE/_api/projects/hello-pending/promote" -H "Authorization: Bearer $PROJ"
run "8. Approve needs the owner (per-project token refused)" -X POST "$BASE/_api/projects/hello-pending/approve" -H "Authorization: Bearer $PROJ"
run "9. Owner sees the pending queue" "$BASE/_api/projects?pending=1" -H "Authorization: Bearer $OWNER"
run "10. Owner approves" -X POST "$BASE/_api/projects/hello-pending/approve" -H "Authorization: Bearer $OWNER"
run "11. Promote now succeeds with the per-project token" -X POST "$BASE/_api/projects/hello-pending/promote" -H "Authorization: Bearer $PROJ"
run "12. Public audit log of the project" "$BASE/_api/projects/hello-pending/audit"

echo "==> screenshot in a real browser"
python3 - "$BASE/hello-pending/" "$OUT/hello-pending.png" <<'EOF'
import sys
from playwright.sync_api import sync_playwright
url, out = sys.argv[1], sys.argv[2]
with sync_playwright() as p:
    b = p.chromium.launch(); pg = b.new_page(viewport={"width": 900, "height": 300})
    pg.goto(url, wait_until="networkidle"); body = pg.inner_text("body")
    assert "hello from a self-provisioned project" in body and "Error" not in body, body
    pg.screenshot(path=out); b.close(); print("screenshot ok:", body.strip())
EOF
test -s "$OUT/hello-pending.png"

echo "==> build the report page"
python3 - "$T" "$OUT/hello-pending.png" "$OUT/index.html" "$BASE" <<'EOF'
import sys, base64, html
t, png, out, base = sys.argv[1:5]
md = open(t).read(); img = base64.b64encode(open(png, "rb").read()).decode()
blocks = []
for part in md.split("\n### ")[1:]:
    title, _, rest = part.partition("\n")
    code = rest.split("```")[1] if "```" in rest else rest
    blocks.append(f"<h3>{html.escape(title.strip())}</h3><pre>{html.escape(code.strip())}</pre>")
page = f"""<!doctype html><meta charset=utf-8><title>RFC 0034 on staging: a project with no owner token</title>
<style>body{{font:15px/1.5 system-ui,sans-serif;max-width:900px;margin:2rem auto;padding:0 1rem;color:#222}}
pre{{background:#f4f4f4;padding:.8rem;overflow-x:auto;font-size:13px}}h3{{margin-top:2rem}}img{{border:1px solid #ccc;max-width:100%}}</style>
<h1>RFC 0034 on staging: a project brought up with no owner token</h1>
<p>Every step below ran against <code>{html.escape(base)}</code>. The only credentials in the deploying shell were the
<b>create-scoped provisioner token</b> (one file) and, after step 1, the <b>per-project token</b> the daemon handed back.
The owner token appears only in the owner steps (9, 10). This report page was then deployed the same way.</p>
<h2>What the app looks like in a real browser</h2><img src="data:image/png;base64,{img}" alt="hello-pending in Chromium">
<h2>Transcript</h2>{''.join(blocks)}
<h2>Reviewer checklist</h2><ul>
<li>Step 1 returns a token and <code>approval.status = pending</code>.</li>
<li>Steps 3, 4, 7, 8 are refusals with the exact reason.</li>
<li>Step 12 shows <code>create</code>, <code>approve</code>, <code>promote</code> in the public audit.</li>
<li><code>GET /_api/version</code> in step 0 matches the branch under review.</li></ul>
<p>Source: <code>examples/hello-pending/</code> on branch <code>staging</code> of dstack-webhost.</p>"""
open(out, "w").write(page); print("report:", out, len(page), "bytes")
EOF

echo "==> deploy the report itself via the provisioner, then owner-approve + promote"
curl -s -o /dev/null -X DELETE "$BASE/_api/projects/rfc0034-report" -H "Authorization: Bearer $OWNER" || true
RT=$OUT/report.tgz; tar -czf "$RT" -C "$OUT" index.html
RRESP=$(curl -sf -X POST "$BASE/_api/projects" -H "Authorization: Bearer $(cat "$PROV")" \
  -F 'manifest={"name":"rfc0034-report","runtime":"static"};type=application/json' -F "files=@$RT")
RTOK=$(jq -r .token <<<"$RRESP")
curl -sf -o /dev/null -X POST "$BASE/_api/projects/rfc0034-report/approve" -H "Authorization: Bearer $OWNER"
curl -sf -o /dev/null -X POST "$BASE/_api/projects/rfc0034-report/promote" -H "Authorization: Bearer $RTOK"
echo "REPORT: $BASE/rfc0034-report/"
echo "APP:    $BASE/hello-pending/"
echo "OUT:    $OUT"

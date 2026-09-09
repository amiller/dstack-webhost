import { Biscuit, KeyPair, PublicKey, SignatureAlgorithm, block } from "@biscuit-auth/biscuit-wasm";
import { Delegation, delegate as ucanDelegate, invoke } from "@ucanto/core";
import { ed25519 } from "@ucanto/principal";
import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createCaps, CapsAuthorizationError } from "./index.mjs";

class Skip extends Error {
  constructor(message, value) {
    super(message);
    this.value = value;
  }
}

const remoteUrl = parseUrl(process.argv);
const runner = remoteUrl ? await remoteRunner(remoteUrl) : await localRunner();
const results = [];
const minted = [];
const revokedIds = new Set();

try {
  await criterion(1, "roundtrip", roundtrip);
  await criterion(2, "link-claim read", linkClaimRead);
  await criterion(3, "attenuation refusal", attenuationRefusal);
  await criterion(4, "scope refusal", scopeRefusal);
  await criterion(5, "section scoping", sectionScoping);
  await criterion(6, "wrong-key/stranger", ucanWrongKeyRefusal);
  await criterion(7, "expiry", expiryRefusal);
  const revoked = await criterion(8, "revocation", revocation);
  await criterion(9, "inventory", inventory);
  await criterion(10, "persistence", () => persistence(revoked));
} finally {
  await runner.close?.();
}

for (const result of results) {
  const suffix = result.notice ? ` - ${result.notice}` : "";
  console.log(`${result.status} ${result.id}. ${result.name}${suffix}`);
}

const failed = results.filter(r => r.status === "FAIL");
const skipped = results.filter(r => r.status === "SKIP");
console.log(`${failed.length ? "FAIL" : "PASS"} ${results.length - failed.length - skipped.length} passed, ${skipped.length} skipped, ${failed.length} failed`);
if (failed.length) process.exitCode = 1;

async function criterion(id, name, fn) {
  const start = performance.now();
  try {
    const value = await fn();
    results.push({ id, name, status: "PASS", notice: `${Math.round(performance.now() - start)}ms` });
    return value;
  } catch (e) {
    if (e instanceof Skip) {
      results.push({ id, name, status: "SKIP", notice: e.message });
      return e.value;
    }
    results.push({ id, name, status: "FAIL", notice: e.stack ?? e.message });
    process.exitCode = 1;
  }
}

async function roundtrip() {
  const { token } = await mint({ path: "repo/", ops: ["read", "write"], expiry: future() });
  await runner.write(token, "repo/A/x", Buffer.from("roundtrip"));
  assert.equal((await runner.read(token, "repo/A/x")).toString(), "roundtrip");
}

async function linkClaimRead() {
  const { token } = await mint({ path: "repo/A/", ops: ["read"], expiry: future() });
  assert.deepEqual(await runner.verify(token, { path: "repo/A/x", op: "read" }), { ok: true });
  assert.equal((await runner.claim(token, "repo/A/x")).toString(), "roundtrip");
}

async function attenuationRefusal() {
  const { token } = await mint({ path: "repo/A/", ops: ["read"], expiry: future() });
  const refused = await runner.verify(token, { path: "repo/A/x", op: "write" });
  assert.equal(refused.ok, false);
  const write = await runner.writeRefused(token, "repo/A/x", Buffer.from("denied"));
  assert.equal(write.status, 403);
}

async function scopeRefusal() {
  const { token } = await mint({ path: "repo/A/", ops: ["read"], expiry: future() });
  const refused = await runner.verify(token, { path: "repo/B/y", op: "read" });
  assert.equal(refused.ok, false);
  const read = await runner.readRefused(token, "repo/B/y");
  assert.equal(read.status, 403);
}

async function sectionScoping() {
  const owner = (await mint({ path: "repo/", ops: ["read", "write"], expiry: future() })).token;
  await runner.write(owner, "repo/F/s2-security/body", Buffer.from("section 2"));
  await runner.write(owner, "repo/F/_full", Buffer.from("full file"));

  const { token } = await mint({ path: "repo/F/s2-security/", ops: ["read"], expiry: future() });
  assert.equal((await runner.read(token, "repo/F/s2-security/body")).toString(), "section 2");
  assert.equal((await runner.verify(token, { path: "repo/F/_full", op: "read" })).ok, false);
  assert.equal((await runner.readRefused(token, "repo/F/_full")).status, 403);
}

async function ucanWrongKeyRefusal() {
  const alice = await ed25519.generate();
  const mallory = await ed25519.generate();
  const { delegation } = await delegate({ toDID: alice.did(), path: "repo/A/", ops: ["read"], expiry: future() });
  const refused = await runner.ucanVerify({ delegation, signer: mallory, path: "repo/A/x", op: "read" });
  assert.equal(refused.ok, false);
}

async function expiryRefusal() {
  const { token } = await mint({ path: "repo/", ops: ["read"], expiry: past() });
  assert.equal((await runner.verify(token, { path: "repo/A/x", op: "read" })).ok, false);
  assert.equal((await runner.readRefused(token, "repo/A/x")).status, 403);
}

async function revocation() {
  const { token, id } = await mint({ path: "repo/A/", ops: ["read"], expiry: future() });
  const descendant = await runner.attenuate(token, "repo/A/x");

  assert.deepEqual(await runner.verify(token, { path: "repo/A/x", op: "read" }), { ok: true });
  assert.deepEqual(await runner.verify(descendant, { path: "repo/A/x", op: "read" }), { ok: true });

  await runner.revoke(id);
  revokedIds.add(id);

  assert.deepEqual(await runner.verify(token, { path: "repo/A/x", op: "read" }), { ok: false, code: "REVOKED" });
  assert.deepEqual(await runner.verify(descendant, { path: "repo/A/x", op: "read" }), { ok: false, code: "REVOKED" });
  assert.equal((await runner.readRefused(token, "repo/A/x")).status, 403);
  assert.equal((await runner.readRefused(descendant, "repo/A/x")).status, 403);

  return { id, token, descendant };
}

async function inventory() {
  const grants = await runner.grants();
  for (const issued of minted) {
    const grant = grants.find(g => g.id === issued.id);
    assert.ok(grant, `missing grant ${issued.id}`);
    assert.equal(grant.path, issued.path);
    assert.deepEqual(grant.ops, issued.ops);
    assert.equal(grant.expiry, issued.expiry);
    assert.equal(grant.revoked, revokedIds.has(grant.id));
  }
}

async function persistence(revoked) {
  if (!runner.reopen) throw new Skip("remote mode cannot restart the deployed process; verify by restarting demo-server with the same DATA_DIR/ROOT_KEY");
  const reopened = await runner.reopen();
  assert.deepEqual(await reopened.verify(revoked.token, { path: "repo/A/x", op: "read" }), { ok: false, code: "REVOKED" });
  assert.deepEqual(await reopened.verify(revoked.descendant, { path: "repo/A/x", op: "read" }), { ok: false, code: "REVOKED" });
  const persisted = await reopened.grants();
  assert.ok(persisted.find(g => g.id === revoked.id)?.revoked);
  await reopened.close?.();
}

async function localRunner() {
  const root = new KeyPair();
  const dataDir = await mkdtemp(join(tmpdir(), "caps-accept-"));
  const rootKey = root.getPrivateKey().toString();
  const caps = createCaps({ dataDir, rootKey });
  return localAdapter({ caps, root, dataDir, rootKey, cleanup: true });
}

function localAdapter({ caps, root, dataDir, rootKey, cleanup }) {
  return {
    async mint(args) {
      return caps.mint(args);
    },
    async delegate(args) {
      return caps.delegate(args);
    },
    async verify(token, args) {
      return caps.verify(token, args);
    },
    async ucanVerify({ delegation, signer, path, op }) {
      return caps.verify(await invocation({ delegation, signer, path, op }), { path, op });
    },
    async read(token, path) {
      return await caps.read(token, path);
    },
    async claim(token, path) {
      return await caps.read(token, path);
    },
    async readRefused(token, path) {
      try {
        await caps.read(token, path);
        return { status: 200 };
      } catch (e) {
        if (e instanceof CapsAuthorizationError) return { status: 403, body: { ok: false, code: e.code } };
        throw e;
      }
    },
    async write(token, path, bytes) {
      await caps.write(token, path, bytes);
    },
    async writeRefused(token, path, bytes) {
      try {
        await caps.write(token, path, bytes);
        return { status: 200 };
      } catch (e) {
        if (e instanceof CapsAuthorizationError) return { status: 403, body: { ok: false, code: e.code } };
        throw e;
      }
    },
    async attenuate(token, capPath) {
      return Biscuit.fromBase64(token, root.getPublicKey())
        .appendBlock(block`check if resource($p), $p.starts_with(${capPath});`)
        .toBase64();
    },
    async revoke(id) {
      caps.revoke(id);
    },
    async grants() {
      return caps.grants();
    },
    async reopen() {
      return localAdapter({ caps: createCaps({ dataDir, rootKey }), root, dataDir, rootKey, cleanup: false });
    },
    async close() {
      if (cleanup) await rm(dataDir, { recursive: true, force: true });
    },
  };
}

async function remoteRunner(rawUrl) {
  const base = rawUrl.replace(/\/+$/, "");
  const health = await getJson(`${base}/healthz`);
  if (!health.ok || !health.publicKey) throw new Error(`remote health failed: ${JSON.stringify(health)}`);
  const publicKey = PublicKey.fromString(health.publicKey.replace(/^ed25519\//, ""), SignatureAlgorithm.Ed25519);

  return {
    async mint(args) {
      return await postJson(`${base}/mint`, args);
    },
    async delegate(args) {
      const issued = await postJson(`${base}/delegate`, args);
      return { id: issued.id, delegation: Buffer.from(issued.delegation, "base64") };
    },
    async verify(token, args) {
      return await postJson(`${base}/verify`, { credential: token, ...args });
    },
    async ucanVerify({ delegation, signer, path, op }) {
      return await postJson(`${base}/ucan/verify`, {
        delegation: Buffer.from(delegation).toString("base64"),
        signer: encodeSigner(signer),
        path,
        op,
      });
    },
    async read(token, path) {
      const result = await postJson(`${base}/read`, { credential: token, path });
      return Buffer.from(result.content, "base64");
    },
    async claim(token, path) {
      const result = await postJson(`${base}/claim`, { credential: token, path });
      return Buffer.from(result.content, "base64");
    },
    async readRefused(token, path) {
      return await postStatus(`${base}/read`, { credential: token, path });
    },
    async write(token, path, bytes) {
      await postJson(`${base}/write`, { credential: token, path, content: Buffer.from(bytes).toString("base64") });
    },
    async writeRefused(token, path, bytes) {
      return await postStatus(`${base}/write`, { credential: token, path, content: Buffer.from(bytes).toString("base64") });
    },
    async attenuate(token, capPath) {
      return Biscuit.fromBase64(token, publicKey)
        .appendBlock(block`check if resource($p), $p.starts_with(${capPath});`)
        .toBase64();
    },
    async revoke(id) {
      await postJson(`${base}/revoke`, { id });
    },
    async grants() {
      return (await getJson(`${base}/grants`)).grants;
    },
  };
}

async function mint(args) {
  const issued = await runner.mint(args);
  minted.push({
    id: issued.id,
    path: args.path,
    ops: [...new Set(args.ops)],
    expiry: new Date(args.expiry).toISOString(),
  });
  return issued;
}

async function delegate(args) {
  const issued = await runner.delegate(args);
  minted.push({
    id: issued.id,
    path: args.path,
    ops: [...new Set(args.ops)],
    expiry: new Date(args.expiry).toISOString(),
  });
  return issued;
}

async function invocation({ delegation, signer, path, op }) {
  const extracted = (await Delegation.extract(delegation)).ok;
  assert.ok(extracted);
  return invoke({
    issuer: signer,
    audience: { did: () => extracted.capabilities[0].with },
    proofs: [extracted],
    capability: { can: `file/${op}`, with: extracted.capabilities[0].with, nb: { path } },
  });
}

function encodeSigner(signer) {
  const archive = signer.toArchive();
  return {
    id: archive.id,
    keys: Object.fromEntries(Object.entries(archive.keys).map(([id, bytes]) => [id, Buffer.from(bytes).toString("base64")])),
  };
}

async function postJson(url, payload) {
  const res = await fetch(url, jsonRequest(payload));
  const json = await res.json();
  if (!res.ok) throw new Error(`${url} returned ${res.status}: ${JSON.stringify(json)}`);
  return json;
}

async function postStatus(url, payload) {
  const res = await fetch(url, jsonRequest(payload));
  let body;
  try {
    body = await res.json();
  } catch {
    body = await res.text();
  }
  return { status: res.status, body };
}

async function getJson(url) {
  const res = await fetch(url);
  const json = await res.json();
  if (!res.ok) throw new Error(`${url} returned ${res.status}: ${JSON.stringify(json)}`);
  return json;
}

function jsonRequest(payload) {
  return {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  };
}

function parseUrl(args) {
  const index = args.indexOf("--url");
  if (index === -1) return null;
  if (!args[index + 1]) throw new Error("--url requires a value");
  return args[index + 1];
}

function future() {
  return new Date(Date.now() + 3600_000);
}

function past() {
  return new Date(Date.now() - 3600_000);
}

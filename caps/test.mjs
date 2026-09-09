import { Biscuit, KeyPair, block } from "@biscuit-auth/biscuit-wasm";
import { Delegation, delegate as ucanDelegate, invoke } from "@ucanto/core";
import { ed25519 } from "@ucanto/principal";
import assert from "node:assert/strict";
import http from "node:http";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createCaps, CapsAuthorizationError } from "./index.mjs";

const root = new KeyPair();
const dataDir = await mkdtemp(join(tmpdir(), "caps-test-"));
const rootKey = root.getPrivateKey().toString();
const caps = createCaps({ dataDir, rootKey });
const minted = [];
const revokedIds = new Set();

try {
  await run();
} finally {
  await rm(dataDir, { recursive: true, force: true });
}

async function run() {
  await roundtrip();
  await linkClaimRead();
  await attenuationRefusal();
  await scopeRefusal();
  await sectionScoping();
  await ucanWrongKeyRefusal();
  await ucanScopeRefusal();
  await expiryRefusal();
  await ucanExpiryRefusal();
  const revoked = await revocation();
  const revokedUcan = await ucanRevocation();
  await inventory();
  await persistence(revoked, revokedUcan);
}

async function roundtrip() {
  const { token } = mint({ path: "repo/", ops: ["read", "write"], expiry: future() });
  await caps.write(token, "repo/A/x", Buffer.from("roundtrip"));
  assert.equal((await caps.read(token, "repo/A/x")).toString(), "roundtrip");
}

async function linkClaimRead() {
  const { token } = mint({ path: "repo/A/", ops: ["read"], expiry: future() });
  assert.deepEqual(caps.verify(token, { path: "repo/A/x", op: "read" }), { ok: true });
  assert.equal((await caps.read(token, "repo/A/x")).toString(), "roundtrip");
}

async function attenuationRefusal() {
  const { token } = mint({ path: "repo/A/", ops: ["read"], expiry: future() });
  const refused = caps.verify(token, { path: "repo/A/x", op: "write" });
  assert.equal(refused.ok, false);
  await assert.rejects(() => caps.write(token, "repo/A/x", Buffer.from("denied")), CapsAuthorizationError);

  const server = http.createServer(async (req, res) => {
    if (req.method !== "POST" || req.url !== "/write") {
      res.writeHead(404);
      res.end("not found");
      return;
    }
    try {
      await caps.write(token, "repo/A/x", Buffer.from("denied"));
      res.writeHead(204);
      res.end();
    } catch (e) {
      if (e instanceof CapsAuthorizationError) {
        res.writeHead(403);
        res.end("write refused");
        return;
      }
      throw e;
    }
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  try {
    const { port } = server.address();
    const res = await fetch(`http://127.0.0.1:${port}/write`, { method: "POST" });
    assert.equal(res.status, 403);
  } finally {
    await new Promise(resolve => server.close(resolve));
  }
}

async function scopeRefusal() {
  const { token } = mint({ path: "repo/A/", ops: ["read"], expiry: future() });
  const refused = caps.verify(token, { path: "repo/B/y", op: "read" });
  assert.equal(refused.ok, false);
  await assert.rejects(() => caps.read(token, "repo/B/y"), CapsAuthorizationError);
}

async function sectionScoping() {
  const owner = mint({ path: "repo/", ops: ["read", "write"], expiry: future() }).token;
  await caps.write(owner, "repo/F/s2-security/body", Buffer.from("section 2"));
  await caps.write(owner, "repo/F/_full", Buffer.from("full file"));

  const { token } = mint({ path: "repo/F/s2-security/", ops: ["read"], expiry: future() });
  assert.equal((await caps.read(token, "repo/F/s2-security/body")).toString(), "section 2");
  assert.equal(caps.verify(token, { path: "repo/F/_full", op: "read" }).ok, false);
  await assert.rejects(() => caps.read(token, "repo/F/_full"), CapsAuthorizationError);
}

async function expiryRefusal() {
  const { token } = mint({ path: "repo/", ops: ["read"], expiry: past() });
  assert.equal(caps.verify(token, { path: "repo/A/x", op: "read" }).ok, false);
  await assert.rejects(() => caps.read(token, "repo/A/x"), CapsAuthorizationError);
}

async function ucanWrongKeyRefusal() {
  const alice = await ed25519.generate();
  const mallory = await ed25519.generate();
  const { delegation } = await delegate({ toDID: alice.did(), path: "repo/A/", ops: ["read"], expiry: future() });

  const refused = await caps.verify(await invocation({ delegation, signer: mallory, path: "repo/A/x", op: "read" }), {
    path: "repo/A/x",
    op: "read",
  });
  assert.equal(refused.ok, false);
}

async function ucanScopeRefusal() {
  const alice = await ed25519.generate();
  const bob = await ed25519.generate();
  const { proof } = await delegate({ toDID: alice.did(), path: "repo/A/", ops: ["read"], expiry: future() });
  const serviceDid = proof.capabilities[0].with;
  const toBob = await ucanDelegate({
    issuer: alice,
    audience: bob,
    proofs: [proof],
    capabilities: [{ can: "file/read", with: serviceDid, nb: { path: "repo/A/x" } }],
    expiration: seconds(future()),
  });

  const ok = await invocation({ proof: toBob, signer: bob, path: "repo/A/x", op: "read" });
  assert.deepEqual(await caps.verify(ok, { path: "repo/A/x", op: "read" }), { ok: true });
  assert.equal((await caps.read(ok, "repo/A/x")).toString(), "roundtrip");

  const refused = await invocation({ proof: toBob, signer: bob, path: "repo/B/y", op: "read" });
  assert.equal((await caps.verify(refused, { path: "repo/B/y", op: "read" })).ok, false);
  await assert.rejects(() => caps.read(refused, "repo/B/y"), CapsAuthorizationError);
}

async function ucanExpiryRefusal() {
  const alice = await ed25519.generate();
  const { delegation } = await delegate({ toDID: alice.did(), path: "repo/A/", ops: ["read"], expiry: past() });
  const refused = await caps.verify(await invocation({ delegation, signer: alice, path: "repo/A/x", op: "read" }), {
    path: "repo/A/x",
    op: "read",
  });
  assert.equal(refused.ok, false);
}

async function revocation() {
  const { token, id } = mint({ path: "repo/A/", ops: ["read"], expiry: future() });
  const descendant = Biscuit.fromBase64(token, root.getPublicKey())
    .appendBlock(block`check if resource($p), $p.starts_with("repo/A/x");`)
    .toBase64();

  assert.deepEqual(caps.verify(token, { path: "repo/A/x", op: "read" }), { ok: true });
  assert.deepEqual(caps.verify(descendant, { path: "repo/A/x", op: "read" }), { ok: true });

  caps.revoke(id);
  revokedIds.add(id);

  assert.deepEqual(caps.verify(token, { path: "repo/A/x", op: "read" }), { ok: false, code: "REVOKED" });
  assert.deepEqual(caps.verify(descendant, { path: "repo/A/x", op: "read" }), { ok: false, code: "REVOKED" });
  await assert.rejects(() => caps.read(token, "repo/A/x"), CapsAuthorizationError);
  await assert.rejects(() => caps.read(descendant, "repo/A/x"), CapsAuthorizationError);

  return { id, token, descendant };
}

async function ucanRevocation() {
  const alice = await ed25519.generate();
  const bob = await ed25519.generate();
  const { id, proof } = await delegate({ toDID: alice.did(), path: "repo/A/", ops: ["read"], expiry: future() });
  const serviceDid = proof.capabilities[0].with;
  const toBob = await ucanDelegate({
    issuer: alice,
    audience: bob,
    proofs: [proof],
    capabilities: [{ can: "file/read", with: serviceDid, nb: { path: "repo/A/x" } }],
    expiration: seconds(future()),
  });
  const direct = await invocation({ proof, signer: alice, path: "repo/A/x", op: "read" });
  const descendant = await invocation({ proof: toBob, signer: bob, path: "repo/A/x", op: "read" });

  assert.deepEqual(await caps.verify(direct, { path: "repo/A/x", op: "read" }), { ok: true });
  assert.deepEqual(await caps.verify(descendant, { path: "repo/A/x", op: "read" }), { ok: true });

  caps.revoke(id);
  revokedIds.add(id);

  assert.equal((await caps.verify(direct, { path: "repo/A/x", op: "read" })).ok, false);
  assert.equal((await caps.verify(descendant, { path: "repo/A/x", op: "read" })).ok, false);
  await assert.rejects(() => caps.read(direct, "repo/A/x"), CapsAuthorizationError);
  await assert.rejects(() => caps.read(descendant, "repo/A/x"), CapsAuthorizationError);

  return { id, direct, descendant };
}


async function inventory() {
  const grants = caps.grants();
  assert.equal(grants.length, minted.length);
  for (const issued of minted) {
    const grant = grants.find(g => g.id === issued.id);
    assert.ok(grant, `missing grant ${issued.id}`);
    assert.equal(grant.path, issued.path);
    assert.deepEqual(grant.ops, issued.ops);
    assert.equal(grant.expiry, issued.expiry);
    assert.equal(grant.revoked, revokedIds.has(grant.id));
  }
}

async function persistence({ id, token, descendant }, ucan) {
  const reopened = createCaps({ dataDir, rootKey });
  assert.deepEqual(reopened.verify(token, { path: "repo/A/x", op: "read" }), { ok: false, code: "REVOKED" });
  assert.deepEqual(reopened.verify(descendant, { path: "repo/A/x", op: "read" }), { ok: false, code: "REVOKED" });
  assert.equal((await reopened.verify(ucan.direct, { path: "repo/A/x", op: "read" })).ok, false);
  assert.equal((await reopened.verify(ucan.descendant, { path: "repo/A/x", op: "read" })).ok, false);
  const persisted = reopened.grants();
  assert.equal(persisted.length, minted.length);
  assert.equal(persisted.find(g => g.id === id)?.revoked, true);
  assert.equal(persisted.find(g => g.id === ucan.id)?.revoked, true);
}

function mint(args) {
  const issued = caps.mint(args);
  minted.push({
    id: issued.id,
    path: args.path,
    ops: [...new Set(args.ops)],
    expiry: new Date(args.expiry).toISOString(),
  });
  return issued;
}

async function delegate(args) {
  const issued = await caps.delegate(args);
  const extracted = await Delegation.extract(issued.delegation);
  assert.ok(extracted.ok, extracted.error?.message);
  minted.push({
    id: issued.id,
    path: args.path,
    ops: [...new Set(args.ops)],
    expiry: new Date(args.expiry).toISOString(),
  });
  return { ...issued, proof: extracted.ok };
}

async function invocation({ delegation, proof, signer, path, op }) {
  const extracted = proof ?? (await Delegation.extract(delegation)).ok;
  assert.ok(extracted);
  return invoke({
    issuer: signer,
    audience: { did: () => extracted.capabilities[0].with },
    proofs: [extracted],
    capability: { can: `file/${op}`, with: extracted.capabilities[0].with, nb: { path } },
  });
}

function seconds(date) {
  return Math.floor(date.getTime() / 1000);
}

function future() {
  return new Date(Date.now() + 3600_000);
}

function past() {
  return new Date(Date.now() - 3600_000);
}

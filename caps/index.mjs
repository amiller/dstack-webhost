import { biscuit, authorizer, Biscuit, KeyPair, PrivateKey } from "@biscuit-auth/biscuit-wasm";
import * as Server from "@ucanto/server";
import { Absentee, ed25519 } from "@ucanto/principal";
import { Delegation, delegate as ucanDelegate } from "@ucanto/core";
import * as Client from "@ucanto/client";
import * as CAR from "@ucanto/transport/car";
import { Authorization } from "@ucanto/validator";
import { capability, Schema, ok, error, provide } from "@ucanto/server";
import { DatabaseSync } from "node:sqlite";
import { mkdirSync } from "node:fs";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { dirname, isAbsolute, join, resolve } from "node:path";

const LIMITS = { max_facts: 1000n, max_iterations: 100n, max_time_micro: 5_000_000n };
const OPS = new Set(["read", "write"]);
const UCAN_SERVICE_KEY = "ucan_service_key";

const pathAttenuates = (claim, proof) => {
  if (claim.with !== proof.with) return error(`resource ${claim.with} != delegated ${proof.with}`);
  const prefix = proof.nb.path;
  if (!claim.nb.path.startsWith(prefix)) return error(`path ${claim.nb.path} escapes delegated prefix ${prefix}`);
  return ok({});
};

const FileRead = capability({
  can: "file/read",
  with: Schema.URI.match({ protocol: "did:" }),
  nb: Schema.struct({ path: Schema.string() }),
  derives: pathAttenuates,
});

const FileWrite = capability({
  can: "file/write",
  with: Schema.URI.match({ protocol: "did:" }),
  nb: Schema.struct({ path: Schema.string(), content: Schema.string().optional() }),
  derives: pathAttenuates,
});

export function createCaps({ dataDir, rootKey, db } = {}) {
  if (!dataDir) throw new Error("createCaps requires dataDir");
  if (!rootKey) throw new Error("createCaps requires rootKey");
  if (db !== undefined) throw new Error("createCaps stores caps state at dataDir/caps.db");

  const rootDir = resolve(dataDir);
  mkdirSync(rootDir, { recursive: true });
  const store = new DatabaseSync(join(rootDir, "caps.db"));
  const root = KeyPair.fromPrivateKey(PrivateKey.fromString(rootKey));
  initDb(store);
  const serviceSigner = loadServiceSigner(store);

  const insertGrant = store.prepare(`
    INSERT INTO grants (id, path, ops, expiry)
    VALUES (?, ?, ?, ?)
  `);
  const listGrants = store.prepare(`
    SELECT grants.id, grants.path, grants.ops, grants.expiry, revoked_biscuit_ids.id IS NOT NULL AS revoked
    FROM grants
    LEFT JOIN revoked_biscuit_ids ON revoked_biscuit_ids.id = grants.id
    ORDER BY grants.issued_at, grants.rowid
  `);
  const insertRevocation = store.prepare(`
    INSERT OR IGNORE INTO revoked_biscuit_ids (id)
    VALUES (?)
  `);
  const findRevocation = store.prepare(`
    SELECT 1 FROM revoked_biscuit_ids WHERE id = ? LIMIT 1
  `);

  function mint({ path, ops, expiry }) {
    assertPath(path);
    const rights = assertOps(ops);
    const exp = new Date(expiry);
    if (Number.isNaN(exp.getTime())) throw new Error("mint expiry must be a valid Date or date string");

    const built = (rights.has("read") && rights.has("write")
      ? biscuit`right("read"); right("write"); check if resource($p), $p.starts_with(${path}); check if time($t), $t < ${exp};`
      : rights.has("write")
      ? biscuit`right("write"); check if resource($p), $p.starts_with(${path}); check if time($t), $t < ${exp};`
      : biscuit`right("read"); check if resource($p), $p.starts_with(${path}); check if time($t), $t < ${exp};`
    ).build(root.getPrivateKey());

    const id = revocationIdentifiers(built)[0];
    insertGrant.run(id, path, JSON.stringify([...rights]), exp.toISOString());
    return { token: built.toBase64(), id };
  }

  async function delegate({ toDID, path, ops, expiry }) {
    assertDID(toDID);
    assertPath(path);
    const rights = assertOps(ops);
    const exp = new Date(expiry);
    if (Number.isNaN(exp.getTime())) throw new Error("delegate expiry must be a valid Date or date string");

    const service = await serviceSigner;
    const delegation = await ucanDelegate({
      issuer: service,
      audience: Absentee.from({ id: toDID }),
      capabilities: [...rights].map(op => ({ can: ucanCan(op), with: service.did(), nb: { path } })),
      expiration: Math.floor(exp.getTime() / 1000),
    });
    const archived = await delegation.archive();
    if (!archived.ok) throw new Error(`UCAN archive failed: ${archived.error?.message ?? archived.error}`);

    const id = delegation.cid.toString();
    insertGrant.run(id, path, JSON.stringify([...rights]), exp.toISOString());
    return { delegation: archived.ok, id };
  }

  function verify(credential, { path, op }) {
    if (isUcanCredential(credential)) return verifyUcan(credential, { path, op });

    let t;
    try {
      assertPath(path);
      assertOp(op);
      if (!credential) throw new Error("missing credential");
      t = Biscuit.fromBase64(credential, root.getPublicKey());
    } catch (e) {
      return { ok: false, code: errorCode(e) };
    }

    const revoked = revocationIdentifiers(t).find(id => findRevocation.get(id));
    if (revoked) return { ok: false, code: "REVOKED" };

    try {
      authorizer`
        resource(${path});
        operation(${op});
        time(${new Date()});
        allow if right(${op});
      `.buildAuthenticated(t).authorizeWithLimits(LIMITS);
      return { ok: true };
    } catch (e) {
      return { ok: false, code: errorCode(e) };
    }
  }

  async function verifyUcan(credential, { path, op }) {
    try {
      assertPath(path);
      assertOp(op);
      const invocation = await extractInvocation(credential);
      assertInvocationMatches(invocation, { path, op });
      const service = await serviceSigner;
      const server = makeUcanServer(service, findRevocation);
      const conn = Client.connect({ id: service, codec: CAR.outbound, channel: server });
      const result = await invocation.execute(conn);
      if (result.out?.ok) return { ok: true };
      return { ok: false, code: errorCode(result.out?.error ?? result) };
    } catch (e) {
      return { ok: false, code: errorCode(e) };
    }
  }

  function revoke(id) {
    assertRevocationId(id);
    insertRevocation.run(id);
  }

  function grants() {
    return listGrants.all().map(grant => ({
      id: grant.id,
      path: grant.path,
      ops: JSON.parse(grant.ops),
      expiry: grant.expiry,
      revoked: Boolean(grant.revoked),
    }));
  }

  async function read(credential, path) {
    const az = await verify(credential, { path, op: "read" });
    if (!az.ok) throw new CapsAuthorizationError("read refused", az.code);
    return await readFile(filePath(rootDir, path));
  }

  async function write(credential, path, bytes) {
    const az = await verify(credential, { path, op: "write" });
    if (!az.ok) throw new CapsAuthorizationError("write refused", az.code);
    const target = filePath(rootDir, path);
    await mkdir(dirname(target), { recursive: true });
    await writeFile(target, bytes);
  }

  return { mint, delegate, verify, read, write, revoke, grants };
}

export class CapsAuthorizationError extends Error {
  constructor(message, code) {
    super(message);
    this.name = "CapsAuthorizationError";
    this.code = code;
  }
}

function assertOps(ops) {
  if (!Array.isArray(ops) || ops.length === 0) throw new Error("mint ops must be a non-empty array");
  const rights = new Set(ops);
  for (const op of rights) assertOp(op);
  return rights;
}

function assertOp(op) {
  if (!OPS.has(op)) throw new Error(`unsupported op: ${op}`);
}

function assertPath(path) {
  if (typeof path !== "string" || path.length === 0) throw new Error("path must be a non-empty string");
  if (path.includes("\0")) throw new Error("path contains NUL byte");
  if (isAbsolute(path) || path.split("/").includes("..")) throw new Error(`unsafe path: ${path}`);
}

function assertRevocationId(id) {
  if (typeof id !== "string" || !/^([0-9a-f]+|bafy[a-z2-7]+)$/i.test(id)) {
    throw new Error("revocation id must be a Biscuit hex id or UCAN CID");
  }
}

function assertDID(did) {
  if (typeof did !== "string" || !did.startsWith("did:")) throw new Error("toDID must be a DID string");
}

function initDb(store) {
  store.exec(`
    CREATE TABLE IF NOT EXISTS meta (
      key TEXT PRIMARY KEY,
      value TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS grants (
      id TEXT PRIMARY KEY,
      path TEXT NOT NULL,
      ops TEXT NOT NULL,
      expiry TEXT NOT NULL,
      issued_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    );
    CREATE TABLE IF NOT EXISTS revoked_biscuit_ids (
      id TEXT PRIMARY KEY,
      revoked_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    );
  `);
}

async function loadServiceSigner(store) {
  const getMeta = store.prepare("SELECT value FROM meta WHERE key = ? LIMIT 1");
  const setMeta = store.prepare("INSERT INTO meta (key, value) VALUES (?, ?)");
  const existing = getMeta.get(UCAN_SERVICE_KEY)?.value;
  if (existing) return ed25519.decode(new Uint8Array(Buffer.from(existing, "base64url")));

  const signer = await ed25519.generate();
  setMeta.run(UCAN_SERVICE_KEY, Buffer.from(signer.encode()).toString("base64url"));
  return signer;
}

function revocationIdentifiers(token) {
  const ids = token.getRevocationIdentifiers();
  if (!Array.isArray(ids) || ids.length === 0) throw new Error("Biscuit token has no revocation identifiers");
  for (const id of ids) assertRevocationId(id);
  return ids;
}

function filePath(rootDir, path) {
  assertPath(path);
  const target = resolve(rootDir, path);
  if (target !== rootDir && !target.startsWith(`${rootDir}/`)) throw new Error(`path escapes dataDir: ${path}`);
  return target;
}

function errorCode(e) {
  if (e?.FailedLogic) return "CHECK_FAILED";
  return String(e?.message ?? e).slice(0, 80);
}

function ucanCan(op) {
  assertOp(op);
  return `file/${op}`;
}

function makeUcanServer(serviceSigner, findRevocation) {
  return Server.create({
    id: serviceSigner,
    codec: CAR.inbound,
    service: {
      file: {
        read: provide(FileRead, async () => ok({})),
        write: provide(FileWrite, async () => ok({})),
      },
    },
    validateAuthorization: auth => {
      for (const cid of Authorization.iterate(auth)) {
        if (findRevocation.get(cid.toString())) return error({ name: "Revoked", message: `proof ${cid} revoked` });
      }
      return ok({});
    },
  });
}

function isUcanCredential(credential) {
  return credential instanceof Uint8Array || Boolean(credential?.execute);
}

async function extractInvocation(credential) {
  if (credential?.execute) return credential;
  const extracted = await Delegation.extract(credential);
  if (!extracted.ok) throw new Error(`UCAN extract failed: ${extracted.error?.message ?? extracted.error}`);
  if (!extracted.ok.execute) throw new Error("UCAN credential must be an invocation");
  return extracted.ok;
}

function assertInvocationMatches(invocation, { path, op }) {
  const expectedCan = ucanCan(op);
  const matches = invocation.capabilities?.some(cap =>
    cap.can === expectedCan && cap.nb?.path === path
  );
  if (!matches) throw new Error(`UCAN invocation does not authorize ${op} ${path}`);
}

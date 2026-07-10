import { KeyPair, PrivateKey } from "@biscuit-auth/biscuit-wasm";
import { Delegation, invoke } from "@ucanto/core";
import { ed25519 } from "@ucanto/principal";
import http from "node:http";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { mkdir } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { createCaps, CapsAuthorizationError } from "./index.mjs";

const PORT = Number(process.env.PORT || 3500);
const DATA_DIR = resolve(process.env.DATA_DIR || "./data");
const ROOT_KEY_FILE = join(DATA_DIR, "root.key");

await mkdir(DATA_DIR, { recursive: true });
const rootKey = loadRootKey();
const root = KeyPair.fromPrivateKey(PrivateKey.fromString(rootKey));
const caps = createCaps({ dataDir: DATA_DIR, rootKey });

http.createServer((req, res) => {
  handle(req, res).catch(e => json(res, 500, { ok: false, error: e.message }));
}).listen(PORT, () => {
  console.log(`caps demo server listening on :${PORT}`);
});

async function handle(req, res) {
  const url = new URL(req.url, `http://${req.headers.host || "localhost"}`);
  const path = url.pathname.replace(/\/+$/, "") || "/";

  if (req.method === "GET" && path === "/healthz") {
    return json(res, 200, { ok: true, publicKey: root.getPublicKey().toString() });
  }
  if (req.method === "POST" && path === "/mint") {
    return json(res, 200, { ok: true, ...(caps.mint(await body(req))) });
  }
  if (req.method === "POST" && path === "/delegate") {
    const issued = await caps.delegate(await body(req));
    return json(res, 200, { ok: true, id: issued.id, delegation: b64(issued.delegation) });
  }
  if (req.method === "POST" && path === "/verify") {
    const { credential, path: capPath, op } = await body(req);
    return json(res, 200, await caps.verify(decodeCredential(credential), { path: capPath, op }));
  }
  if (req.method === "POST" && path === "/ucan/verify") {
    const { delegation, signer, path: capPath, op } = await body(req);
    const credential = await ucanInvocation({ delegation, signer, path: capPath, op });
    return json(res, 200, await caps.verify(credential, { path: capPath, op }));
  }
  if (req.method === "POST" && (path === "/read" || path === "/claim")) {
    const { credential, path: capPath } = await body(req);
    try {
      const bytes = await caps.read(decodeCredential(credential), capPath);
      return json(res, 200, { ok: true, content: bytes.toString("base64") });
    } catch (e) {
      if (e instanceof CapsAuthorizationError) return json(res, 403, { ok: false, code: e.code });
      throw e;
    }
  }
  if (req.method === "POST" && path === "/write") {
    const { credential, path: capPath, content } = await body(req);
    try {
      await caps.write(decodeCredential(credential), capPath, Buffer.from(content, "base64"));
      return json(res, 200, { ok: true });
    } catch (e) {
      if (e instanceof CapsAuthorizationError) return json(res, 403, { ok: false, code: e.code });
      throw e;
    }
  }
  if (req.method === "POST" && path === "/revoke") {
    const { id } = await body(req);
    caps.revoke(id);
    return json(res, 200, { ok: true });
  }
  if (req.method === "GET" && path === "/grants") {
    return json(res, 200, { ok: true, grants: caps.grants() });
  }
  return json(res, 404, { ok: false, error: "not found" });
}

function loadRootKey() {
  if (process.env.ROOT_KEY) return process.env.ROOT_KEY;
  if (existsSync(ROOT_KEY_FILE)) return readFileSync(ROOT_KEY_FILE, "utf8").trim();
  const key = new KeyPair().getPrivateKey().toString();
  mkdirSync(dirname(ROOT_KEY_FILE), { recursive: true });
  writeFileSync(ROOT_KEY_FILE, `${key}\n`, { mode: 0o600 });
  return key;
}

async function ucanInvocation({ delegation, signer, path, op }) {
  const extracted = await Delegation.extract(Buffer.from(delegation, "base64"));
  if (!extracted.ok) throw new Error(`UCAN extract failed: ${extracted.error?.message ?? extracted.error}`);
  const issuer = ed25519.from(decodeSigner(signer));
  return invoke({
    issuer,
    audience: { did: () => extracted.ok.capabilities[0].with },
    proofs: [extracted.ok],
    capability: { can: `file/${op}`, with: extracted.ok.capabilities[0].with, nb: { path } },
  });
}

function decodeCredential(credential) {
  if (credential?.type === "ucan") return Buffer.from(credential.value, "base64");
  return credential?.value ?? credential;
}

function decodeSigner(archive) {
  return {
    id: archive.id,
    keys: Object.fromEntries(Object.entries(archive.keys).map(([id, bytes]) => [id, Buffer.from(bytes, "base64")])),
  };
}

async function body(req) {
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  if (chunks.length === 0) return {};
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}

function json(res, status, payload) {
  res.writeHead(status, { "content-type": "application/json; charset=utf-8" });
  res.end(JSON.stringify(payload));
}

function b64(bytes) {
  return Buffer.from(bytes).toString("base64");
}

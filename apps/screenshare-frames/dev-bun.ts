// Local-dev shim: lets the Deno-shaped handler run under Bun for testing.
// Not used in production — the daemon's runtime loads server.ts directly.
import { mkdir, writeFile, readFile, readdir, rm } from "node:fs/promises";

(globalThis as any).Deno = {
  mkdir: (p: string, o: any) => mkdir(p, o),
  writeFile: (p: string, d: Uint8Array) => writeFile(p, d),
  writeTextFile: (p: string, s: string) => writeFile(p, s),
  readFile: (p: string) => readFile(p),
  readTextFile: (p: string) => readFile(p, "utf8"),
  readDir: async function* (p: string) {
    for (const e of await readdir(p, { withFileTypes: true })) yield { name: e.name };
  },
  remove: (p: string) => rm(p),
};

const mod = await import("./server.ts");
const handler = mod.default;
const dataDir = "./.data";
await mkdir(dataDir, { recursive: true });

const port = Number(process.env.PORT || 3000);
Bun.serve({
  port,
  hostname: "0.0.0.0",
  fetch: (req) => handler(req, { env: {}, dataDir }),
});
console.log(`screenshare-frames listening on http://0.0.0.0:${port}`);

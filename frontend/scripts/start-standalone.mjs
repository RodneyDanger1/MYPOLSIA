#!/usr/bin/env node
// Cross-platform local start for a `output: standalone` build.
//
// Why this exists: `next start` REFUSES to work with standalone output ("next start does not work with
// output: standalone configuration. Use node .next/standalone/server.js instead") — so the shipped
// `npm start` could never serve the frontend this repo builds. The Dockerfile's runner stage does the
// two extra steps this script performs (copy `.next/static` and `public` INTO the standalone tree,
// because the standalone server only serves what sits beside it), so a local run reproduces the image.
//
// Usage: npm run build && npm start        (PORT=3000 by default; HOST=0.0.0.0 to expose it)
import { cp, access } from "node:fs/promises";
import { spawn } from "node:child_process";
import { constants } from "node:fs";
import path from "node:path";
import process from "node:process";

const root = process.cwd();
const standalone = path.join(root, ".next", "standalone");
const server = path.join(standalone, "server.js");

async function exists(p) {
  try {
    await access(p, constants.F_OK);
    return true;
  } catch {
    return false;
  }
}

if (!(await exists(server))) {
  console.error(
    `E_FRONTEND_NOT_BUILT: ${server} is missing.\n` +
      "howToFix: run `npm run build` first (the standalone server is produced by a production build).",
  );
  process.exit(1);
}

// The image copies these in; without them the server runs but every asset 404s.
await cp(path.join(root, ".next", "static"), path.join(standalone, ".next", "static"), {
  recursive: true,
  force: true,
});
if (await exists(path.join(root, "public"))) {
  await cp(path.join(root, "public"), path.join(standalone, "public"), { recursive: true, force: true });
}

const port = process.env.PORT ?? "3000";
// Bind all interfaces by default. The Dockerfile sets HOSTNAME=0.0.0.0 for exactly this reason: the
// standalone server takes its bind address from HOSTNAME, and on Windows that variable is the MACHINE
// NAME — so an unset override made it listen on 192.168.x.x alone and 127.0.0.1 (a local proxy, a
// health check, anything fronting it) got connection-refused. Found by this repo's own e2e run.
const host = process.env.HOST ?? "0.0.0.0";
console.log(`starting standalone server on :${port} (host ${host})`);
const child = spawn(process.execPath, [server], {
  cwd: standalone,
  stdio: "inherit",
  env: { ...process.env, PORT: port, HOSTNAME: host },
});
child.on("exit", (code) => process.exit(code ?? 0));

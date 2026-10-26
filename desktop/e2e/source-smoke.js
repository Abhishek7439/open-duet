// Provider-free checks of a source Electron window and its real local backend.
// Requires setup/build, a graphical desktop and working OS secret storage.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const { launchApp, alive, waitUntil, paths } = require("./app");

async function main() {
  const args = process.argv.slice(2);
  if (args.some((arg) => arg !== "--keep")) throw new Error("Usage: npm run e2e:smoke -- [--keep]");
  const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), "open-dots-source-smoke-"));
  const result = {
    kind: "source-desktop-smoke", platform: process.platform, arch: process.arch,
    date: new Date().toISOString(), dataDir,
    source: spawnSync("git", ["rev-parse", "HEAD"], { cwd: paths.root, encoding: "utf8" }).stdout?.trim() || "unknown",
    checks: [],
  };
  let app;
  let page;
  let success = false;
  const check = (name) => { result.checks.push(name); console.log(`ok: ${name}`); };
  try {
    let encryptedKey;
    for (let attempt = 0; attempt < 2; attempt++) {
      app = await launchApp({ dataDir, env: { OPEN_DOTS_DISABLE_UPDATE_CHECK: "1" } });
      page = await app.page();
      await page.waitFor("document.body && document.body.innerText.trim().length > 0", "rendered app");
      assert.match(page.origin, /^http:\/\/127\.0\.0\.1:\d+$/);
      check(`${attempt ? "restart" : "first launch"}: real window renders from loopback backend`);
      // The Electron session supplies its private launch token. External HTTP
      // clients must not be able to use the same API without it.
      const insideStatus = await page.evaluate("fetch('/api/v1/threads', {signal: AbortSignal.timeout(5000)}).then(r => r.status)");
      assert.equal(insideStatus, 200);
      const outside = await fetch(`${page.origin}/api/v1/threads`, { signal: AbortSignal.timeout(5000) });
      assert.equal(outside.status, 401);
      await outside.body?.cancel();
      check("window API succeeds; external request without launch token is refused");
      const keyFile = path.join(dataDir, "secrets", "encryption-key.v1");
      const keyNow = fs.readFileSync(keyFile);
      assert.ok(keyNow.length > 0);
      assert.ok(fs.existsSync(path.join(dataDir, "data", "open-dots.db")));
      if (encryptedKey) assert.deepEqual(keyNow, encryptedKey, "restart must preserve the stored encrypted key");
      encryptedKey = keyNow;
      check("isolated SQLite database and encrypted key persist");
      fs.writeFileSync(path.join(dataDir, `window-${attempt}.png`), await page.screenshot());
      const pid = app.backendPid();
      assert.ok(pid && alive(pid), "backend process is running");
      await page.evaluate("window.close()").catch(() => {});
      page.close();
      page = undefined;
      await waitUntil(() => app.exitCode !== undefined, "Electron shutdown", 35_000);
      assert.equal(app.exitCode, 0);
      await waitUntil(() => !alive(pid), "backend shutdown", 15_000);
      // Also dispose any residual renderer processes in our dedicated group.
      app.destroy();
      app = undefined;
      check("closing the window stops Electron and its backend");
    }
    success = true;
  } catch (error) {
    result.error = error.stack || String(error);
    throw error;
  } finally {
    page?.close();
    try { app?.destroy(); } finally {
      result.ok = success;
      fs.writeFileSync(path.join(dataDir, "result.json"), JSON.stringify(result, null, 2));
      if (success && !args.includes("--keep")) fs.rmSync(dataDir, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
      else console.log(`Evidence retained at ${dataDir}`);
    }
  }
}

if (require.main === module) main().catch((error) => {
  console.error(`e2e:smoke FAILED: ${error.stack || error}`);
  process.exitCode = 1;
});

module.exports = { main };

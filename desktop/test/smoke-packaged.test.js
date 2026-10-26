const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { cleanEnvironment, defaultExecutable, runSmoke } = require("../scripts/smoke-packaged");

test("packaged smoke locates the configured product, and strips Python and launch overrides", () => {
  assert.match(defaultExecutable("win32"), /Open Duet\.exe$/);
  assert.match(defaultExecutable("darwin"), /Open Duet\.app\/Contents\/MacOS\/Open Duet$/);
  const env = cleanEnvironment({ PATH: ["/a/.venv/bin", "/tools/python/bin", "/synthetic-tools"].join(path.delimiter), PYTHONPATH: "secret", OPEN_DOTS_DATA_DIR: "real-profile", OPEN_DOTS_API_TOKEN: "secret", ELECTRON_RUN_AS_NODE: "1", NORMAL: "yes" });
  assert.equal(env.PATH, "/synthetic-tools");
  assert.deepEqual(Object.keys(env).sort(), ["NORMAL", "PATH"]);
});

async function fixture(t, behavior) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "packaged-fixture-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const script = path.join(root, "app.cjs");
  const pidFile = path.join(root, "pid");
  fs.writeFileSync(script, `
    const fs = require('node:fs'); const path = require('node:path');
    const data = process.env.OPEN_DOTS_DATA_DIR;
    fs.writeFileSync(${JSON.stringify(pidFile)}, String(process.pid));
    fs.writeFileSync(${JSON.stringify(path.join(root, "profile"))}, data);
    fs.mkdirSync(path.join(data, 'logs'), {recursive:true});
    fs.mkdirSync(path.join(data, 'data'), {recursive:true});
    fs.writeFileSync(path.join(data, 'data', 'open-dots.db'), 'fixture');
    ${behavior}
  `);
  return { root, script, pidFile };
}
function assertReaped(file) {
  const pid = Number(fs.readFileSync(file, "utf8"));
  assert.throws(() => process.kill(pid, 0), { code: "ESRCH" });
}
const server = (status) => `
  const server = require('node:http').createServer((req,res) => {res.writeHead(${status}); res.end();});
  server.listen(0, '127.0.0.1', () => fs.writeFileSync(path.join(data,'logs','backend.log'), 'Serving on http://127.0.0.1:' + server.address().port));
`;

test("packaged smoke verifies page/API refusal and reaps its real child", async (t) => {
  const f = await fixture(t, server(401));
  const result = await runSmoke({ executable: process.execPath, args: [f.script], timeoutMs: 5000 });
  assertReaped(f.pidFile);
  assert.equal(fs.existsSync(result.dataRoot), false);
});

test("a responding wrong app fails, reaps the child and removes only its own profile", async (t) => {
  const f = await fixture(t, server(404));
  await assert.rejects(runSmoke({ executable: process.execPath, args: [f.script], timeoutMs: 5000 }), /expected unauthenticated HTTP 401, got 404/);
  assertReaped(f.pidFile);
  const profile = fs.readFileSync(path.join(f.root, "profile"), "utf8");
  assert.equal(fs.existsSync(profile), false);
  assert.equal(fs.existsSync(f.root), true);
});

test("a hanging response respects the deadline and is reaped", async (t) => {
  const f = await fixture(t, server(401).replace("res.writeHead(401); res.end();", ""));
  const started = Date.now();
  await assert.rejects(runSmoke({ executable: process.execPath, args: [f.script], timeoutMs: 1000 }));
  assert.ok(Date.now() - started < 6000);
  assertReaped(f.pidFile);
});

test("spawn errors reject without an unhandled error or lingering profile", async (t) => {
  if (process.platform === "win32") return t.skip("POSIX execution permissions");
  const f = await fixture(t, "");
  fs.chmodSync(f.script, 0o600);
  await assert.rejects(runSmoke({ executable: f.script, timeoutMs: 1000 }), /EACCES/);
});

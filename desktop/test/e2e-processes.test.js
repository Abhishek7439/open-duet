const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawn } = require("node:child_process");
const { once } = require("node:events");
const { test } = require("node:test");
const { terminate } = require("../e2e/processes");
const { waitForPage, attach } = require("../e2e/cdp");

test("POSIX cleanup addresses the dedicated group or just the main pid for a crash", () => {
  const calls = [];
  const kill = (...args) => calls.push(args);
  terminate(12345, { platform: "darwin", kill });
  terminate(12345, { platform: "linux", kill, tree: false });
  assert.deepEqual(calls, [[-12345, "SIGKILL"], [12345, "SIGKILL"]]);
  for (const pid of [0, 1, -12345, undefined]) assert.throws(() => terminate(pid, { kill }), /process id/);
  terminate(12345, { platform: "darwin", kill() { throw Object.assign(new Error(), { code: "ESRCH" }); } });
  assert.throws(() => terminate(12345, { platform: "darwin", kill() { throw Object.assign(new Error("denied"), { code: "EPERM" }); } }), /denied/);
});

test("Windows tree cleanup and crash simulation choose the correct taskkill flags", () => {
  const calls = [];
  const spawnSync = (...args) => { calls.push(args); return { status: 0 }; };
  terminate(12345, { platform: "win32", spawnSync });
  terminate(12345, { platform: "win32", spawnSync, tree: false });
  assert.deepEqual(calls[0], ["taskkill", ["/pid", "12345", "/T", "/F"], { windowsHide: true, timeout: 10_000 }]);
  assert.deepEqual(calls[1][1], ["/pid", "12345", "/F"]);
  assert.throws(() => terminate(12345, { platform: "win32", spawnSync() { return { error: new Error("missing taskkill") }; } }), /missing taskkill/);
});

test("CDP startup stops polling immediately when its app failed to spawn", async () => {
  await assert.rejects(waitForPage(1, "http://", 60_000, () => { throw new Error("spawn failed"); }), /spawn failed/);
});

test("closing DevTools rejects an outstanding request instead of hanging cleanup", async () => {
  const original = globalThis.WebSocket;
  class FakeSocket extends EventTarget {
    constructor() { super(); queueMicrotask(() => this.dispatchEvent(new Event("open"))); }
    send() { queueMicrotask(() => this.dispatchEvent(new Event("close"))); }
    close() {}
  }
  globalThis.WebSocket = FakeSocket;
  try {
    const page = await attach({ webSocketDebuggerUrl: "ws://fake" });
    await assert.rejects(page.evaluate("window.close()"), /connection closed/);
  } finally {
    globalThis.WebSocket = original;
  }
});

test("real POSIX cleanup stops a descendant and preserves an unrelated process", { skip: process.platform === "win32", timeout: 10_000 }, async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "open-dots-process-test-"));
  const heartbeat = path.join(dir, "heartbeat");
  const bystander = spawn(process.execPath, ["-e", "setInterval(() => {}, 1000)"], { stdio: "ignore" });
  const grandchild = `const fs=require('node:fs');setInterval(()=>fs.appendFileSync(process.argv[1],'.'),20);`;
  const parent = spawn(process.execPath, ["-e", `require('node:child_process').spawn(process.execPath,['-e',${JSON.stringify(grandchild)},process.argv[1]],{stdio:'ignore'});setInterval(()=>{},1000);`, heartbeat], { detached: true, stdio: "ignore" });
  const exited = once(parent, "exit");
  try {
    const deadline = Date.now() + 5000;
    while ((!fs.existsSync(heartbeat) || fs.statSync(heartbeat).size < 2) && Date.now() < deadline) await new Promise((r) => setTimeout(r, 20));
    assert.ok(fs.existsSync(heartbeat), "descendant started");
    terminate(parent.pid);
    await exited;
    await new Promise((r) => setTimeout(r, 100));
    const size = fs.statSync(heartbeat).size;
    await new Promise((r) => setTimeout(r, 150));
    assert.equal(fs.statSync(heartbeat).size, size, "descendant no longer writes");
    assert.doesNotThrow(() => process.kill(bystander.pid, 0));
  } finally {
    terminate(parent.pid);
    const bystanderExited = once(bystander, "exit");
    bystander.kill("SIGKILL");
    await bystanderExited;
    fs.rmSync(dir, { recursive: true, force: true });
  }
});


test("a stalled DevTools handshake times out and closes its socket", async () => {
  const original = globalThis.WebSocket;
  let closed = false;
  class StalledSocket extends EventTarget {
    close() { closed = true; }
  }
  globalThis.WebSocket = StalledSocket;
  try {
    await assert.rejects(attach({ webSocketDebuggerUrl: "ws://fake" }, { timeoutMs: 10 }), /connection timed out/);
    assert.equal(closed, true);
  } finally { globalThis.WebSocket = original; }
});

// Check a packaged executable with a disposable profile and no Python on PATH.
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawn } = require("node:child_process");
const { terminate } = require("../e2e/processes");

function defaultExecutable(platform = process.platform) {
  const desktop = path.resolve(__dirname, "..");
  const { build } = require("../package.json");
  if (platform === "win32") return path.join(desktop, "release", "win-unpacked", `${build.productName}.exe`);
  if (platform === "darwin") return path.join(desktop, "release", "mac-arm64", `${build.productName}.app`, "Contents", "MacOS", build.executableName);
  throw new Error("Pass the packaged executable path on this platform.");
}

function cleanEnvironment(environment) {
  const env = { ...environment };
  const pathKey = Object.keys(env).find((key) => key.toLowerCase() === "path") ?? "PATH";
  env[pathKey] = (env[pathKey] ?? "").split(path.delimiter).filter((dir) =>
    dir && !/python|\.venv|\buv\b/i.test(dir) &&
    !["python", "python3", "python.exe"].some((name) => fs.existsSync(path.join(dir, name)))
  ).join(path.delimiter);
  for (const name of Object.keys(env)) {
    if (/^(VIRTUAL_ENV|PYTHON.*|UV_.*|ELECTRON_RUN_AS_NODE|OPEN_DOTS_.*)$/i.test(name)) delete env[name];
  }
  return env;
}

async function runSmoke({ executable, args = [], timeoutMs = 90_000, environment = process.env, keep = false } = {}) {
  if (!executable || !fs.existsSync(executable)) throw new Error(`Packaged executable missing: ${executable}. Run npm run package first.`);
  const dataRoot = fs.mkdtempSync(path.join(os.tmpdir(), "open-dots-packaged-smoke-"));
  const env = { ...cleanEnvironment(environment), OPEN_DOTS_DATA_DIR: dataRoot };
  let child;
  let failure;
  let finished;
  let exited;
  const log = path.join(dataRoot, "logs", "backend.log");
  const deadline = Date.now() + timeoutMs;
  const remaining = () => Math.max(1, deadline - Date.now());
  const pause = () => new Promise((resolve) => setTimeout(resolve, Math.min(100, remaining())));
  try {
    child = spawn(executable, args, { env, stdio: "ignore", windowsHide: false, detached: process.platform !== "win32" });
    exited = new Promise((resolve) => {
      child.once("error", (error) => { failure = error; finished = true; resolve(); });
      child.once("exit", (code, signal) => { finished = true; failure = new Error(`App exited before verification (${signal ?? code}).`); resolve(); });
    });
    let port;
    while (Date.now() < deadline && !port) {
      if (finished) throw failure;
      const text = fs.existsSync(log) ? fs.readFileSync(log, "utf8") : "";
      const matches = [...text.matchAll(/Serving on http:\/\/127\.0\.0\.1:(\d+)/g)];
      port = matches.at(-1)?.[1];
      if (!port) await pause();
    }
    if (!port) throw new Error("Backend did not report readiness before the deadline.");
    const origin = `http://127.0.0.1:${port}`;
    // No token is available outside Electron. Both the page and API must refuse.
    for (const pathname of ["/chat/", "/api/v1/threads"]) {
      const response = await fetch(origin + pathname, { redirect: "manual", signal: AbortSignal.timeout(remaining()) });
      await response.body?.cancel();
      if (response.status !== 401) throw new Error(`${pathname}: expected unauthenticated HTTP 401, got ${response.status}.`);
    }
    if (finished) throw failure;
    if (!fs.existsSync(path.join(dataRoot, "data", "open-dots.db"))) throw new Error("Packaged backend did not create its SQLite database.");
    return { origin, dataRoot };
  } catch (error) {
    error.message += ` Profile: ${dataRoot}`;
    throw error;
  } finally {
    if (child?.pid) terminate(child.pid);
    if (exited) {
      let timer;
      await Promise.race([exited, new Promise((resolve) => { timer = setTimeout(resolve, 5000); })]);
      clearTimeout(timer);
    }
    if (!keep) fs.rmSync(dataRoot, { recursive: true, force: true });
  }
}

if (require.main === module) {
  Promise.resolve().then(() => runSmoke({ executable: process.argv.slice(2).find((arg) => arg !== "--keep") ?? defaultExecutable(), keep: process.argv.includes("--keep") }))
    .then(({ origin, dataRoot }) => console.log(`smoke:packaged OK: backend at ${origin} refused unauthenticated page/API access. Profile: ${dataRoot}`))
    .catch((error) => { console.error(`smoke:packaged FAILED: ${error.message}`); process.exitCode = 1; });
}
module.exports = { defaultExecutable, cleanEnvironment, runSmoke };

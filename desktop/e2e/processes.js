// Only use tree=true for a process this harness launched with detached=true on
// POSIX. Negative pids address a process group, never an arbitrary process tree.
const childProcess = require("node:child_process");

function terminate(pid, { tree = true, platform = process.platform, kill = process.kill, spawnSync = childProcess.spawnSync } = {}) {
  if (!Number.isSafeInteger(pid) || pid <= 1) throw new Error("A launched process id is required.");
  if (platform === "win32") {
    const args = ["/pid", String(pid), ...(tree ? ["/T"] : []), "/F"];
    const result = spawnSync("taskkill", args, { windowsHide: true, timeout: 10_000 });
    if (result.error) throw result.error;
    // A nonzero exit also means that the process already ended.
    return;
  }
  try {
    kill(tree ? -pid : pid, "SIGKILL");
  } catch (error) {
    if (error.code !== "ESRCH") throw error;
  }
}

module.exports = { terminate };

"""Run planner execution UI against an isolated server and fake engine.

Build frontend/out with a relative API path first. Requires desktop's installed
Playwright dependency and Chrome. No real credentials or model calls are used.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
spec = importlib.util.spec_from_file_location("server_chat_smoke", ROOT / "scripts/smoke-server-chat.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix="open-dots-planner-ui-"))
    backend = module.ServerBackend(root / "server", ROOT / "frontend/out")
    owner = None
    try:
        backend.start()
        owner = module.Owner(backend)
        owner.setup()
        state = {"cookies": [{"name": cookie.name, "value": cookie.value, "url": backend.url}
                             for cookie in owner.http.cookies.jar]}
        state_path = root / "browser-state.json"
        state_path.write_text(json.dumps(state))
        subprocess.run(["node", str(ROOT / "scripts/smoke-planner.cjs"), backend.url, str(state_path), str(root)],
                       cwd=ROOT, check=True, timeout=150)
        print("PASS: planner start, completion, Review, scheduled cancellation, calendar history, phone layout")
    finally:
        if owner:
            owner.close()
        backend.stop()
        if args.keep:
            print(f"Artifacts: {root}")
        else:
            shutil.rmtree(root)


if __name__ == "__main__":
    main()

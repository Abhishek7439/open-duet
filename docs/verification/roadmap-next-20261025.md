# Roadmap source verification — 2026-10-25

Branch `roadmap/next-increments-20261025`, feature code `6b7fba1`, integrated
checkpoint `cbc62b7` (includes fixture correction `36276bc`), based on
`15caa55`. This record covers source and automated checks on macOS ARM64.

Implemented: routine result cards and owner-only settings Undo, a local Today
brief over durable records, and separately enabled Linux network isolation
preview. See [chat management](../CHAT_MANAGEMENT.md), [Today](../TEAMS.md#today-brief),
[sandbox configuration](../SELF-HOSTING.md#optional-network-isolation-stage-d-preview)
and [the capability matrix](../CAPABILITIES.md).

## Checks

- Full SQLite backend suite at feature checkpoint `6b7fba1`: **2,938 passed,
  35 skipped**, four warnings, 16 subtests passed (404.28 seconds).
- After integrating the fixture-only change `36276bc`, the full team and
  thread-runtime selection passed **237 tests** (19.94 seconds).
- Network policy/transport and sandbox selection: **63 passed, 1 skipped**.
  Real Linux/bubblewrap coverage skips on macOS. Local UNIX/socket tests ran
  with socket permission; they do not verify actual namespace enforcement.
- Frontend **221 passed**, TypeScript and contrast passed. ESLint has no
  errors and two existing hook warnings.
- Desktop **100 passed**, including the loopback native-control fixture.
- Production frontend build: **passed** (`npm run build -- --webpack`),
  including TypeScript and all eight static pages.
- Whitespace checks passed. No database migration or schema change is needed.

Tests used the existing Python 3.14 environment and Node dependencies,
disposable databases and temporary workspaces. No production data was used.

## Qualification limits

These checks do not qualify actual Linux network enforcement or engine proxy
compatibility, sign-in/resume, approval delivery, stop/timeouts and orphan
cleanup. Provider credentials remain readable where explicitly granted;
credential brokering and syscall filtering are separate work. The backend's
own HTTP adapters and engine probes are outside the turn box.

Real-engine routine cards/Undo, PostgreSQL concurrent Undo and packaged UI
remain open. Calendar and Gmail are unavailable. No installed-app,
upgrade/recovery, live-provider or release qualification is inferred.

Baseline [CI at 36276bc](https://github.com/Anil-matcha/open-duet/actions/runs/40219723105)
passed SQLite, PostgreSQL, desktop and process/server smoke. That includes
the earlier webhook receipt implementation; it does not qualify this feature
branch's added paths. No tag or public release was created for this branch.

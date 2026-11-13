# Arcadia Effects upstream

- Repository: https://github.com/xaidan777/Arcadia-Effects
- Pinned commit: `ed2bd1b371ecdaae9274f207c77535c61610f0f8` (v 1.02)
- License: MIT; Copyright (c) 2026 ANTON CHUEV. See `LICENSE`.
- Source directory: `Arcada Effects/`.

`vendor/` contains the upstream editor, documentation, agent skills and developer tools. The upstream `library/` samples, `.git`, development server and launch script are excluded. The built-in procedural Factory presets remain available. Developer tools, skills and documentation are not public HTTP routes.

Community integration changes:

- The page synchronously loads the community session helper, authenticated `fantasy-session.js` bootstrap and `integration.js` before upstream scripts, plus `integration.css`. Relative assets and the trusted bootstrap `api_base` support the same-origin `/effects/` mount.
- `app.js`, `catalog.js`, and `i18n.js` use the adapter's fixed account-scoped storage facade. Upstream unscoped browser data is never imported.
- Catalog operations require the authenticated server; failed saves/deletes retain workspace state. A successful save only clears the dirty marker if the current document still matches the sent snapshot, preserving edits made during the request. The offline shared-library fallback is removed. Original local JSON/PNG/ZIP exports remain browser downloads.
- The adapter binds API requests to the original account, attaches server-supplied CSRF credentials on writes, verifies responses, checks the session periodically and locks a stale tab. Chinese platform navigation surrounds the original English/Russian editor.
- The community portal verifies its revocable session and CSRF before proxying requests, replacing caller-supplied authorization with a short-lived `fantasy-effects` identity. The renderer additionally checks its own CSRF and original account header. Storage is independent of other deployments.
- The i18n checker receives an adapter storage stub; new editor messages have English/Russian translations.

No built-in LLM service is present in this upstream. The editor renders and exports in the browser; this module does not start or require a model service. The authenticated platform MCP can author effect JSON and request validation or PNG export using its existing AI account.

`render.mjs` / `renderer-worker.mjs` add an optional CPU PNG exporter for the authenticated API. It reads only the fixed trusted upstream `utils`, `track`, `curve`, `path`, `sprites`, `model`, `sim`, `engine`, and `atlas` scripts, with `@napi-rs/canvas` pinned to **1.1.0** in the lockfile (verified from the official npm registry on 2026-10-30). None of these rendering-core files is modified. Effect JSON is never executed as code. `VENDOR_DIR` is a trusted deployment environment setting, never an API argument. Embedded PNG bytes are accepted for textures; network URLs, file URLs and SVG are rejected. PNGs are returned in memory and do not create or change library files.

Server export limits: one dedicated worker per export with no wall-clock deadline, 12 MiB effect JSON, 24 layers, 8 emitters, 8000 total live particles, 15-second composition, 64 atlas frames, 1024-pixel composition/cell side, 4096-pixel atlas side and 16 MiPixel atlas area. Texture limits are 16 embedded PNGs, 2048-pixel side, 4 MiPixel per image and 16 MiPixel combined. Native canvas allocations are additionally bounded. The HTTP server serializes validation/export computations. Browser exports keep their upstream behavior and do not use these server resource limits.

`POST /api/exports` creates an account-owned background export. Short status requests and a separate result download avoid gateway/client connection deadlines without extending those deadlines for other traffic. The owner can cancel a job; cancelling terminates its worker. Completed results are temporary in-memory cache, released by MCP only after the PNG and manifest are safely saved locally, or expired one hour after completion. Running jobs do not expire. The legacy synchronous `/api/export` remains for compatibility; long renders should use the background API. The 10-second input validator deadline remains separate from rendering.

The authenticated `GET /api/guide` response provides `frontend/AI_GUIDE.md` as `guide` and upstream `docs/FORMAT.md` as `format`, alongside `usage`, `upstream`, and `limits` metadata. Upstream `skills/afx-effect-authoring/SKILL.md` remains a server-maintenance reference only; it is not returned by this endpoint and has no public HTTP route. Agents should use the provided response fields rather than requesting internal file paths.

Verification: run `node frontend/integration.test.mjs` from this directory and `node tools/i18ncheck.mjs` from `vendor/`. The renderer test is `node tests/render.test.mjs` in a Docker Node environment after `npm ci --omit=dev --ignore-scripts`; the verified test container had networking disabled. These tests use fixtures in memory or temporary directories and do not change a user's effects. No dependencies are installed on the host.

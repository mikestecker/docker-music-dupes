# music-dupes

Self-hosted duplicate-music finder, shipped as a Docker image. Read docs/HANDOFF.md for full context (architecture, every decision rule, API shape, known gaps). README.md is the user-facing install guide.

## Hard rules
- Keep the app runnable from just app/app.py and app/index.html: no frontend build step, no new runtime deps without asking. deploy/compose.no-build.yaml depends on this.
- Pinned deps live in requirements.txt: fastapi==0.115.6 uvicorn==0.34.0 mutagen==1.47.0. Python 3.12. Keep the pins in deploy/compose.no-build.yaml in sync.
- Never weaken the safety invariants in docs/HANDOFF.md section 5 (one copy always survives, quarantine before delete, path containment, manual clusters never pre-select).
- Never use FLAC bitrate as a quality signal.
- Bump the tag cache table name (files_v2 -> files_v3) whenever the cached dict shape changes.
- No secrets in the repo; placeholders only. TrueNAS Custom Apps don't read .env, so deploy/truenas.yaml inlines placeholder values.
- The image must run as any UID (users set `user:` to match their library owner). Don't write anywhere but /config, /music and /tmp.
- Frontend API calls use relative paths (`api/...`) so sub-path reverse proxies work, and every POST sends `Content-Type: application/json` (the request guard rejects anything else).
- File operations go through `library_path()`, which refuses symlinks, quarantine and anything outside the library. Quarantine re-checks files against the disk before moving. Don't bypass either.

## Conventions
- UI copy: sentence case, plain verbs, no em dashes, no emojis, no all-caps labels, no middle-dot separators.
- Keep the quality chip as the single loud visual element; light and dark themes via CSS variables.
- Every destructive action gets an Undo toast where possible.

## Testing
- `pytest` (needs ffmpeg). tests/mkfix.py builds the fixture library; expected results are in tests/test_classify.py and docs/HANDOFF.md section 10.1.
- tests/e2e.py is a Playwright flow against a running server on a fresh fixture library. Check light and dark, desktop and 390px wide.

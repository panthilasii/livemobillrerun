# AGENTS.md

Context for AI coding agents working in this repo. Read this first before
exploring — it'll save you 5-10 grep round-trips on every session.

## What this product is

**NP Create** — a Thai-market desktop app (`vcam-pc/`) for TikTok Live
sellers that lets the customer stream a PC video file as the phone's
camera. Two paths ship in the same app:

- **Phase 5 (current main path, stock Android)** — PC FFmpeg → TCP →
  `adb reverse` → Android receiver app → fullscreen → TikTok **Live
  Screen Share** via MediaProjection. No root, no patching.
- **LSPatch path (legacy / opt-in)** — pull the customer's TikTok APKs,
  fuse `vcam-app` as an embedded Xposed module via LSPatch, re-install.
  Still maintained because some OEMs lock MediaProjection.

Customer is non-technical, Thai-speaking, on **Windows** (majority) or
**macOS**. Support is via **Line OA: @npcreate**. See
`vcam-pc/src/branding.py` for the canonical `BRAND` constant.

## Submodules (monorepo)

| Path           | Job                                                | Language / build       | Entry                              |
| -------------- | -------------------------------------------------- | ---------------------- | ---------------------------------- |
| `vcam-pc/`     | Desktop GUI, streamer, LSPatch wizard, dashboard   | Python 3.10+, customtkinter | `python3 -m src.main [--gui]` |
| `vcam-app/`    | Android receiver + Xposed hook module              | Kotlin 2.0.21, AGP 8.7.3, JDK 17, min SDK 33 | `gradle :app:assembleDebug` |
| `vcam-server/` | FastAPI license + admin server                     | Python, FastAPI, SQLite | `uvicorn app.main:app`            |
| `vcam-magisk/` | Magisk/Zygisk Camera HAL hook (Phase 4b)           | C++/CMake, NDK, shell  | `bash build_native.sh arm64-v8a && bash build.sh` |

Most day-to-day work lives in `vcam-pc/`.

## Commands you'll actually use

The user is on **macOS** — always use `python3`, never `python`.

```bash
# vcam-pc: run tests
cd vcam-pc && python3 -m pytest tests/ -q

# vcam-pc: run a single test file
cd vcam-pc && python3 -m pytest tests/test_pull_apk_fallback.py -v

# vcam-pc: GUI dev run
cd vcam-pc && source tools/bin/env.sh && python3 -m src.main --gui

# vcam-pc: bootstrap portable adb/ffmpeg on dev macOS
bash vcam-pc/tools/bootstrap_macos.sh

# vcam-app: debug build
cd vcam-app && LANG=C gradle :app:assembleDebug   # LANG=C on Thai-locale macOS

# vcam-server: dev
cd vcam-server && python3 -m venv .venv && source .venv/bin/activate \
  && pip install -r requirements.txt && python -m app.cli init-db \
  && uvicorn app.main:app --reload
```

CI lives at `.github/workflows/release.yml` and uses **Python 3.13**,
PyInstaller, Inno Setup (Windows), `build_dmg.sh` (macOS).
No committed Ruff/flake8 config — just keep style consistent with
surrounding code.

## Language conventions (CRITICAL)

This is the one rule that's easy to get wrong:

- **Customer-facing strings → Thai.** Error dialogs, button labels,
  log lines the customer might see, README onboarding.
- **Code, comments, log statements → English.** Every docstring,
  every `log.info(...)`, every inline comment.
- **`vcam-pc/src/ui/studio_pages.py` does NOT use `i18n.T(...)`** — it
  hard-codes Thai because it's the Thai-only studio (see file's top
  docstring lines 15-21). Don't "fix" this by wrapping in `T(...)`.
- **`i18n.T` exists** at `vcam-pc/src/ui/i18n.py` for the parts that do
  need translation, but defaults to `th` via `VCAM_LANG`.
- **Error dialog format:** multi-line, lead with cause, then bullet
  list of fixes prefixed with `•`. Look at any `messagebox.showerror`
  in `studio_pages.py` for the house style.

## ADB interaction

Always go through `vcam-pc/src/adb.py::AdbController`. Don't sprinkle
raw `subprocess.run([adb_path, ...])` calls in new code.

- `AdbController._run(*args)` — thin wrapper with timeout + `check=False`
- `AdbController.restart_server()` — kill + start, populates
  `self.last_restart_error` (Thai diagnostic) on failure
- `LSPatchPipeline._adb_shell(cmd, serial)` — for shell commands in the
  patcher path; uses bundled adb at `self.cfg.adb_path`
- For ad-hoc shell calls: prefer `subprocess.run` with `text=True,
  capture_output=True, timeout=N, check=False` — never `check=True`
  (we surface our own Thai error instead of letting Python raise)

## Code style essentials

- **Paths**: always `pathlib.Path`, never bare strings in APIs
- **Logging**: `log = logging.getLogger(__name__)` at module top; never
  `print(...)` in shipping code
- **Broad excepts**: `except Exception as e:  # noqa: BLE001` in
  best-effort helpers (keep-awake, probes, cleanups). Never silently
  swallow without `log.debug(...)` at minimum
- **Docstrings**: multi-paragraph, explain *why*, name the customer
  bug that motivated the code if applicable. See top of
  `lspatch_pipeline.py` for the house tone
- **Don't narrate code in comments** — comments explain non-obvious
  intent, trade-offs, OEM quirks. Not "increment counter"

## Test conventions

- Tests live in `<submodule>/tests/test_*.py`, no `conftest.py` in
  `vcam-pc/`
- Scenario-named functions: `test_pull_succeeds_first_try`,
  `test_pre_flight_blocks_pull_when_device_offline`
- Mock `subprocess.run` at the module-level import:
  `patch.object(lspatch_pipeline.subprocess, "run", side_effect=...)`
- Construct `AdbController` via `__new__` to bypass filesystem
  resolution (see `tests/test_adb_restart.py::_make_controller`)
- Use `tmp_path`, `monkeypatch`, `pytest.MonkeyPatch` fixtures
- Tests must run on Linux/macOS/Windows — never depend on a real
  `adb` or `java` being installed (mock them all)
- When adding fixes for customer bugs, pin the behavior with a test
  that explicitly references the bug (see "v1.8.x recurrence fix"
  block in `test_adb_restart.py` for the pattern)

## Anti-patterns (don't do these)

From the codebase docstrings:

- **`lspatch_pipeline.py`** — never patch & install in one step
  without explicit user confirmation (destroys TikTok session); always
  `install-multiple` with all splits; never re-run LSPatch on
  already-patched APKs without clearing stale cache.
- **`log_setup.py`** — diagnostics MUST NEVER leak the license key,
  admin private signing key, or TikTok Shop OAuth tokens.
- **`license_server.py`** — every license call is **fail-open**. A
  server outage must NEVER prevent the customer from using the app.
- **`backup_restore.py`** — backups MUST NOT contain `.private_key`.
- **`_pubkey.py`** — DO NOT edit by hand; regenerate via
  `tools/init_keys.py`.
- **`studio_pages.py`** — the wizard tells customers "DO NOT tap
  Update inside TikTok"; preserve that UX.

## Customer-environment gotchas

These keep coming back. When debugging a customer issue, suspect them
in roughly this order:

1. **Port 5037 hijacked** by another adb daemon (Bluestacks, MEmu,
   NoxPlayer, LDPlayer, Microsoft Phone Link, Mi PC Suite, Samsung
   Smart Switch, scrcpy, Android Studio, Vysor). `AdbController.
   _find_port_5037_holder()` identifies the holder by PID + exe name.
2. **OneDrive / iCloud / Dropbox / Google Drive** holding the install
   folder — `_CLOUD_SYNC_HINTS` in `lspatch_pipeline.py`. Bundled
   `adb.exe` may be a cloud placeholder (the `☁️` icon) that won't
   execute.
3. **Vivo / Oppo / MIUI aggressive battery management** suspending
   `adbd` mid-`adb pull`. Mitigated by `_keep_device_awake` (`input
   keyevent KEYCODE_WAKEUP` + `svc power stayon usb`) in the pull
   pipeline.
4. **Mid-pull USB drop** — `adb pull` returns rc=1 with stderr empty
   after `[ NN%]` progress is stripped. `_pull_apk_with_fallback`
   detects this signature and auto-retries via `_wait_for_device_back`.
5. **Windows Defender / Bitdefender** quarantining `adb.exe` or
   slowing `java -version` from 5s → 30s on first launch. Look for
   Mark-of-the-Web (`Unblock-File`) and AV exclusions in the
   `lspatch_pipeline.py` rationale comments.
6. **Stale ADB authorisation** — Vivo Funtouch defaults to ~1 hour,
   not the 7-day AOSP default; customer sees `unauthorized` mid-
   session.
7. **macOS Gatekeeper** notarising the bundled JDK on first `java
   -version` — 10-30 s delay; covered by `_probe_java_version`
   quarantine retry.
8. **USB selective suspend on Windows** — host-side suspend of "idle"
   USB devices during long transfers.

## Bundled tools layout

Canonical: `<workspace>/.tools/<os>/{platform-tools, ffmpeg, jdk-21,
lspatch, scrcpy}/...` — see `vcam-pc/src/platform_tools.py` for the
resolver. Dev macOS bootstrap puts a duplicate set at
`vcam-pc/tools/bin/` for convenience.

CI populates `.tools/` via `python tools/setup_scrcpy.py` +
`python tools/setup_ci_tools.py` in the release workflow.

## When you're stuck

- Read the top docstring of the module you're editing first —
  almost every file in `vcam-pc/src/` has a customer-bug-rationale
  block at the top.
- Customer logs land at `vcam-pc/logs/npcreate.log` +
  `startup-diagnostic.txt` — ask for these from support tickets.
- Don't add a new feature without a Thai customer-facing message
  and at least one test that pins the new behavior.

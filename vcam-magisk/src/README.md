# Native sources for the Magisk module

Currently one strategy is scaffolded:

- `zygisk/`  — Zygisk + framework hook (Strategy A, preferred).
  Builds to `libvcam_zygisk.so`. See top-level `../README.md` for the
  high-level overview and `../build_native.sh` for the build command.

Strategy B (HAL overlay for MediaTek mt6769) is intentionally not
scaffolded yet — only attempt it if a target app explicitly hardens
against framework-level hooks. The reverse-engineering checklist lives
in `../../docs/PHASE4_HAL_HOOK.md`.

## Quick build (Strategy A)

```bash
export ANDROID_NDK=$HOME/Library/Android/sdk/ndk/26.1.10909125
bash ../build_native.sh arm64-v8a
```

The resulting `.so` is picked up by `../build.sh` and packaged into the
flashable module zip at `../dist/vcam-magisk.zip`.

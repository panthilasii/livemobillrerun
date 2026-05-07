# Third-party native dependencies

The Zygisk module needs an inline hook engine to actually replace the
camera service callbacks. We pick **Dobby** because it's small, MIT-
licensed, and works well on arm64 Android.

## Adding Dobby

1. Clone or download a release.

   ```bash
   git clone --depth 1 https://github.com/jmpews/Dobby.git \
     dobby
   ```

2. The build script will pick this up automatically — `CMakeLists.txt`
   does an `add_subdirectory(third_party/dobby)` if present and links
   `dobby` into `libvcam_zygisk.so`.

3. Rebuild:

   ```bash
   bash ../../build_native.sh arm64-v8a
   ```

The `.gitignore` at the repo root already excludes `third_party/dobby/`
so cloning here doesn't pollute git history.

## Why Dobby vs alternatives

| Engine                                      | Notes                                      |
| ------------------------------------------- | ------------------------------------------ |
| [Dobby](https://github.com/jmpews/Dobby)    | small, MIT, arm64 stable                   |
| [shadowhook](https://github.com/bytedance/shadowhook) | from ByteDance, used in TikTok itself |
| Substrate (cydia substrate)                 | unmaintained on Android                    |
| `xhook` / `bhook`                           | PLT hooking only, won't reach camera service inline calls |

If you swap to `shadowhook` later, only `camera_hook.cpp` needs
adjusting — `main.cpp` and `yuv_reader.cpp` are engine-agnostic.

# Build and share the full NP Create stack from source

Use this when moving the project to another Mac/PC or handing a colleague the **entire codebase** (not only the installer ZIP).

## What’s in the repo

| Path | Role |
|------|------|
| `vcam-pc/` | Desktop app (Python): encode/push, LSPatch workflow, device dashboard |
| `vcam-app/` | Android module embedded into TikTok via LSPatch (camera hooks, `FlipRenderer`, etc.) |
| `apk/` | Drop zone for signed **`vcam-app-release.apk`** used by Re-Patch (after you sign Gradle output) |
| `vcam-magisk/`, `vcam-server/`, … | Optional / ancillary — only if your workflow uses them |

## Prerequisites

- **Python 3.10+** (3.13 is fine) with Tk available (usually bundled on macOS).
- **Android**: JDK 17+ and Android SDK **build-tools** / **platform-tools** (`adb`) on `PATH`, or use the `.tools/` bundle your team already ships with the customer package.
- **Gradle**: install system Gradle or use Android Studio’s embedded Gradle to build `vcam-app`.

## Copy the whole tree

**Option A — Git (recommended)**  
From the machine that already has the repo:

```bash
cd /path/to/parent
git clone <your-remote-url> livemobillrerun
cd livemobillrerun
git status
```

**Option B — Archive without `.git`**  

```bash
cd /path/to/parent
tar --exclude='.git' -czvf livemobillrerun-src.tgz livemobillrerun
```

Copy `livemobillrerun-src.tgz` to the other machine and extract.

## Build the Android hook module (`vcam-app`)

```bash
cd livemobillrerun/vcam-app
gradle :app:assembleRelease
```

Unsigned APK:

`vcam-app/app/build/outputs/apk/release/app-release-unsigned.apk`

Sign it with your release keystore (same process you already use for production), then install the signed artifact as:

`livemobillrerun/apk/vcam-app-release.apk`

so **`find_vcam_apk()`** in `vcam-pc` picks it up when running from the dev tree.

## Run the desktop dashboard (`vcam-pc`)

```bash
cd livemobillrerun/vcam-pc
python3 -m src.main
```

Install Python deps if imports fail (your team may use a venv or `requirements.txt` / `pyproject` — follow whatever is committed next to `vcam-pc`).

## Customer-style bundle (optional)

If you package installers with **`tools/build_release.py`**, run that from documented usage in-repo so branding (`vcam-pc/src/branding.py`) and bundled `apk/` stay in sync with **`tools/installer.iss`**.

## Version alignment

Before tagging a release, keep **`vcam-pc/src/branding.py`**, **`vcam-pc/tools/installer.iss`** (`MyAppVersion`), and your shipped **`apk/vcam-app-release.apk`** on the same version line.

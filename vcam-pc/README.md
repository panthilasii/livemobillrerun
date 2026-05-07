# vcam-pc

PC-side streamer for `livemobillrerun`. Loops video files from `videos/`,
pipes them through FFmpeg with the right rotation for your phone, and serves
H.264 over TCP for the Android receiver to consume.

## Requirements

- Python 3.10+
- FFmpeg 6.0+ in `PATH`  (or set `ffmpeg_path` in `config.json`)
- Android Platform Tools (adb)  — only needed if you want auto `adb reverse`
- Tk (bundled with python.org installers; for Homebrew Python on macOS run
  `brew install python-tk@3.12`)

## Install — zero-config (macOS, no Homebrew)

If you don't already have `ffmpeg` and `adb`, the bootstrap script downloads
arm64-native portable copies into `tools/bin/`:

```bash
cd vcam-pc
bash tools/bootstrap_macos.sh        # one-time, ~6 MB adb + 70 MB ffmpeg
source tools/bin/env.sh              # puts adb + ffmpeg on PATH for this shell
```

## Install — manual

```bash
cd vcam-pc
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# macOS  : brew install ffmpeg android-platform-tools
# Linux  : sudo apt install ffmpeg adb
```

## Tests

```bash
pytest -v                            # 26 unit tests, no ffmpeg/adb needed
bash tools/smoke_test.sh             # PC-only e2e: streamer + fake_phone + ffmpeg validate
bash tools/check_phone.sh            # 5 s diagnostic — is your phone connected and ready?
bash tools/phone_smoke.sh            # full e2e via adb reverse to a real phone
```

## Run

### GUI

```bash
python -m src.main --gui
```

A Tkinter window opens with:

- **Device profile** dropdown (loaded from `device_profiles.json`)
- **Playlist** picker (drop `.mp4` files into `videos/`)
- **Resolution / FPS / Bitrate** controls
- **Start / Stop** button
- Live status line (frames sent, current video, TCP clients)

### Headless / CLI

```bash
python -m src.main --cli --profile "Redmi 13C" --port 8888
```

## Test without a phone

In another terminal:

```bash
ffplay tcp://localhost:8888
```

You should see your video looping with the right rotation. If yes, vcam-pc
is fully working — phone-side is Phase 4.

## Configuration

### `config.json`

```json
{
  "ffmpeg_path": "ffmpeg",
  "adb_path": "adb",
  "tcp_port": 8888,
  "resolution": "1280x720",
  "fps": 30,
  "video_bitrate": "2000k",
  "loop_playlist": true,
  "auto_adb_reverse": true,
  "videos_dir": "videos"
}
```

### `device_profiles.json`

Per-device rotation filters. The Redmi 13C front camera is mounted with
its sensor rotated 270°, so we use `transpose=2,vflip` to compensate.

If your video appears upside-down or sideways on the phone, switch to a
different profile or open `device_profiles.json` and adjust
`rotation_filter`. Valid FFmpeg filter values:

| Filter                | Effect                          |
| --------------------- | ------------------------------- |
| `none`                | no rotation                     |
| `transpose=0`         | 90° counter-clockwise + vflip   |
| `transpose=1`         | 90° clockwise                   |
| `transpose=2`         | 90° counter-clockwise           |
| `transpose=3`         | 90° clockwise + vflip           |
| `transpose=2,vflip`   | 270° clockwise (180° flipped)   |
| `hflip,vflip`         | 180°                            |

## Layout

```
vcam-pc/
├── config.json
├── device_profiles.json
├── requirements.txt
├── videos/                  ← drop .mp4 here
├── scripts/
│   └── check_device.sh      ← Phase 1 helper
├── src/
│   ├── main.py
│   ├── config.py
│   ├── adb.py
│   ├── ffmpeg_streamer.py
│   ├── playlist.py
│   ├── tcp_server.py
│   └── ui/app.py
├── tests/                   ← 26 pytest unit tests
└── tools/
    ├── bootstrap_macos.sh   ← downloads arm64 ffmpeg + adb to bin/
    ├── bin/env.sh           ← `source` to put portable tools on PATH
    ├── make_sample_video.sh ← generates a 5 s SMPTE bars mp4
    ├── fake_phone.py        ← TCP client used to smoke-test the streamer
    ├── smoke_test.sh        ← PC-only e2e check
    ├── check_phone.sh       ← 5 s diagnostic — adb / device state / nc / reverse
    └── phone_smoke.sh       ← e2e against a real phone over adb reverse
```

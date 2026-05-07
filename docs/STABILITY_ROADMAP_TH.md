# Roadmap เสถียรภาพ — ทำให้ระบบเรา "เสถียรเหมือน UltimateRerun"

**สถานะ:** ✅ Phase 4d (LSPatch) ทำงาน, hook fire ทุกตัวใน TikTok process แล้ว.
**เป้าหมาย:** ปิด gap ที่เหลือเพื่อให้ใช้งานยาวๆ ได้โดยไม่ต้องคอยรีสตาร์ท

---

## เปรียบเทียบสถานะปัจจุบัน vs. UltimateRerun

| ฟีเจอร์ | UltimateRerun | ของเรา | สถานะ |
|---|---|---|---|
| LSPatch ฝัง module ลง TikTok | ✅ | ✅ | **เท่ากัน** |
| Hook `MediaCodec.createInputSurface` | ✅ | ✅ | **เท่ากัน** |
| Hook `MediaCodec.queueInputBuffer` (audio) | ✅ | ✅ | **เท่ากัน** |
| Hook `AudioRecord.read` | ✅ | ✅ | **เท่ากัน** |
| In-process `BroadcastReceiver` | ✅ | ✅ | **เท่ากัน** |
| **Hot reload** เมื่อ MP4 เปลี่ยน | ✅ | ✅ ใหม่! | **ทำเสร็จ** |
| Auto-recovery เมื่อ `MediaPlayer` error | ✅ | 🟡 partial | กำลังทำ |
| **GLES SurfaceTexture pipeline** (rotate/zoom/flip live) | ✅ | ❌ | ค้างอยู่ |
| `setOutputSurface()` for hot Surface swap | ✅ | ❌ | ยังไม่มี |
| Audio decoder hot-reload | ✅ | ❌ | ยังไม่มี |
| `MediaCodec.configure(Format, Surface, ...)` hook (อีก variant) | ✅ | 🟡 partial | ใส่บางตัว |
| Disable AEC/NoiseSuppressor | ✅ | ✅ | **เท่ากัน** |
| Mode receiver (live ปรับ rotate/zoom) | ✅ | 🟡 stub | UI พร้อม wiring TODO |
| GUI ภาษาไทย | n/a | ✅ ใหม่! | **ทำเสร็จ** |

---

## สิ่งที่ทำเสร็จแล้วในรอบนี้

### 1. Hot-reload watchdog (เสถียรภาพ ↑)
`VideoFeeder.kt` เพิ่ม watchdog poll ทุก 2 วินาที:
- ถ้า MP4 บนดิสก์เปลี่ยน mtime → rebuild MediaPlayer ใส่ Surface เดิม
- ถ้า MediaPlayer error → flag mtime = -1 → tick ถัดไป rebuild
- ผู้ใช้ encode + push ใหม่ได้เลย ไม่ต้อง kill TikTok

### 2. Rotation picker
แทน toggle "Show as portrait" ด้วย:
- Spinner: `0° / 90° / 180° / 270°`
- Checkboxes: `Mirror H / Mirror V`
- จำค่าด้วย `SharedPreferences` ครั้งเดียวพอ

### 3. Thai locale
- `vcam-app/res/values-th/strings.xml` — Android เลือกอัตโนมัติเมื่อโทรศัพท์ตั้ง ภาษาไทย
- `vcam-pc/src/ui/i18n.py` — Tkinter GUI ใช้ `T("English")` lookup จาก dict ไทย
- ตั้ง `VCAM_LANG=en` ถ้าอยากกลับมาเป็นอังกฤษ

---

## ค้างไว้ทำต่อ

### Phase E1 — GLES SurfaceTexture pipeline (สำคัญสุด)
**ทำไมต้องมี:** ตอนนี้ rotate/zoom/flip ทำได้ใน preview เท่านั้น (กระทบที่ Bitmap). ใน
TikTok Live ตัว `MediaPlayer` วาดลง encoder Surface ตรงๆ — เปลี่ยนอะไรไม่ได้
runtime. UltimateRerun แทรก `SurfaceTexture → GLES → output Surface` ตรงกลาง
เลยปรับ transform ได้ live.

**Stub ที่ต้องเขียน:** `vcam-app/.../FlipRenderer.kt`
- เปิด `SurfaceTexture` ขนาดวิดีโอ
- สร้าง GL context, vertex+fragment shader (ตัว fragment ใช้ samplerExternalOES)
- ทุกเฟรมที่ SurfaceTexture ส่งมา: ใช้ matrix transform ปัจจุบัน → วาดลง output
  Surface ของ encoder
- เปลี่ยน `MediaPlayer.setSurface(surface)` เป็น `setSurface(textureSurface)`

**ผลลัพธ์:** กดปรับ rotate/zoom/flip ใน vcam-app → ใน TikTok Live เห็นเปลี่ยนทันที

### Phase E2 — Auto-recovery แบบเต็ม
- ถ้า `MediaPlayer.OnErrorListener` ตี → exponential backoff retry (1s, 2s, 4s, max 30s)
- ถ้า encoder Surface ถูก destroyed → เก็บ path/transform ไว้ พอ Surface ใหม่มาก็
  resume
- log timestamp ทุก event เพื่อ debug ภายหลัง

### Phase E3 — Audio hot-reload
ตอนนี้ `AudioFeeder.start(path)` decode ครั้งเดียว ไม่ track mtime. ทำเหมือน video:
poll mtime → re-init decoder ถ้า file เปลี่ยน

### Phase E4 — Mode receiver wiring
`VCamModeReceiver` รับ `mode/videoPath/rotation/zoom/flipX/flipY` ผ่าน intent
extras อยู่แล้ว แต่ยัง **ไม่ได้** route ไปอัพเดต `FlipRenderer` (ที่ยังไม่มี).
ทำพร้อม Phase E1.

### Phase E5 — Hook coverage
UltimateRerun hook `MediaCodec.configure` หลาย variant:
- `(format, surface, crypto, flags)` — ของเรามี
- `(format, surface, flags, descriptor)` — ของเรา**ไม่มี**, ต้องเพิ่ม
- `(format, surface, crypto, descriptor, flags)` (Android 14+)

ถ้า TikTok เลือก variant ที่เราไม่ได้ hook → encoder ไม่เข้า `videoEncoders` set
→ audio replacement ไม่ทำงาน

---

## ขั้นตอนการใช้งาน (ภาษาไทย)

1. **เครื่องโทรศัพท์** — เปิด Developer Options → USB Debugging + "Install via USB"
2. **PC** — เปิด GUI: `python3 -m src.main --gui` ใน `vcam-pc/`
3. **Section 7** กด **"Patch + ติดตั้ง TikTok"** (ครั้งเดียวพอ — TikTok ที่ patched
   จะโหลด hook ทุกครั้งที่เปิด)
4. **Section 6** กด **"Encode + push MP4"** เมื่อต้องการเปลี่ยนวีดีโอ
   - watchdog ใน TikTok detect ภายใน 2 วินาที → MediaPlayer reload
5. **เปิด TikTok → Go Live → กล้องเซลฟี่** — ภาพจะถูกแทนด้วย MP4 ที่ push

ถ้าภาพหมุนผิดทิศ:
- เปิด vcam-app → **"หมุนพรีวิว"** → ลองค่าต่างๆ จนได้ตรง
- ค่ามีผลแค่ใน preview ของ vcam-app (TikTok เห็นไฟล์ตรงๆ ตามที่ encode)
- ถ้าอยากให้ TikTok หมุนตามไปด้วย ต้อง re-encode พร้อม `-vf transpose=...` ใน
  PC streamer (หรือรอ Phase E1)

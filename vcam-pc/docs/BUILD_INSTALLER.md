# คู่มือ build Installer (.exe / .dmg) — สำหรับแอดมิน

ไฟล์นี้ admin-only ครับ — ลูกค้าจะไม่เห็น (ตัด out จาก customer
bundle อัตโนมัติผ่าน `tools/build_release.py`).

---

## 3 วิธีที่ใช้ build installer

| วิธี | ใช้เมื่อ | ความง่าย |
|------|----------|----------|
| **A. GitHub Actions** ✅ แนะนำ | Push tag → CI build อัตโนมัติทั้ง Windows .exe + macOS .dmg | ง่ายสุด ตั้งครั้งเดียว |
| **B. Build บน Windows เอง** | ต้องการทดสอบ installer ก่อน push | ปานกลาง ต้องมี Windows |
| **C. Build บน Mac เอง** (เฉพาะ .dmg) | ออก patch macOS ด่วน | ง่าย — ใช้ Mac อยู่แล้ว |

---

## A. GitHub Actions (แนะนำ)

### ตั้งครั้งแรก

1. สร้าง repo บน GitHub (private OK):
   ```bash
   cd ~/livemobillrerun
   git remote add origin git@github.com:<USERNAME>/np-create.git
   git push -u origin master
   ```

2. ไม่ต้องตั้งค่าอะไรเพิ่ม — workflow ใน `.github/workflows/release.yml`
   ใช้ `secrets.GITHUB_TOKEN` ที่ GitHub สร้างให้ทุก repo อัตโนมัติ

### Build รุ่นใหม่

```bash
# 1. เพิ่ม version ใน vcam-pc/src/branding.py
# 2. commit และ tag
git add -A
git commit -m "v1.4.6: <สิ่งที่เพิ่ม>"
git tag v1.4.6
git push origin master --tags
```

3. ภายใน ~10 นาที GitHub Actions จะ build เสร็จ:
   - เข้า https://github.com/<USERNAME>/np-create/releases
   - จะเห็น release `v1.4.6` พร้อมไฟล์ทั้ง 4:
     - `NP-Create-Setup-1.4.6.exe` ← ส่งลูกค้า Windows
     - `NP-Create-1.4.6.dmg` ← ส่งลูกค้า Mac
     - `NP-Create-customer-windows-1.4.6.zip` ← portable
     - `NP-Create-customer-macos-1.4.6.zip` ← portable

4. ดาวน์โหลดไฟล์ → ส่ง Line ลูกค้าได้เลย

### ค่าใช้จ่าย

ฟรี — GitHub Actions ให้ Windows + macOS runner ฟรี 2,000 นาที/เดือน
สำหรับ private repo (public repo ไม่จำกัด) — เราใช้ ~5 นาที/build
= สบาย ๆ build ได้ ~400 ครั้ง/เดือน

---

## B. Build บน Windows เอง

ใช้เมื่อ:
- ต้องการเทสต์ installer ก่อน push GitHub
- ลูกค้าต้องการ build เฉพาะกิจ ก่อน CI run จบ

### ติดตั้งครั้งแรก (1 ครั้ง)

1. **Python 3.13** — https://www.python.org/downloads → ติ๊ก "Add to PATH"
2. **Inno Setup 6** — https://jrsoftware.org/isinfo.php → ติดตั้งปกติ
3. ลงโปรเจกต์: `git clone <repo>` หรือ unzip workspace มาวาง

### Build

ใน Command Prompt (เปิดที่โฟลเดอร์ `vcam-pc`):

```bat
tools\build_installer.bat
```

ใช้เวลา ~3 นาที — output ที่ `vcam-pc\dist\installer\NP-Create-Setup-*.exe`

---

## C. Build .dmg บน macOS เอง

ใช้เมื่อ ออก patch ด่วน เฉพาะ macOS

### ติดตั้งครั้งแรก (1 ครั้ง)

```bash
brew install create-dmg
pip3 install pyinstaller
```

### Build

```bash
cd vcam-pc
python3 tools/build_pyinstaller.py    # สร้าง NP-Create.app
bash tools/build_dmg.sh                # ห่อเป็น .dmg
```

Output ที่ `vcam-pc/dist/installer/NP-Create-<version>.dmg`

---

## ตรวจไฟล์ก่อนส่งลูกค้า (Audit)

ก่อนส่งทุกครั้ง:

```bash
# 1. ทดสอบ installer ติดตั้งใน sandbox / VM
#    (Windows Sandbox built-in ใช้ฟรี: เปิดที่ Settings → Apps → Optional features)
# 2. เช็คว่าไม่มีของแอดมินหลุด:
unzip -l NP-Create-customer-windows-*.zip | grep -E "(\.private_key|gen_license|init_keys|build_release|license_history)"
# ↑ ต้อง "ไม่เจอ" — ถ้าเจอ = stop, ไม่ส่งลูกค้า, รายงานผม
```

---

## Troubleshooting Build

### "PyInstaller flagged as Trojan:Wacatac" บน Windows Defender

**สาเหตุ:** PyInstaller .exe เป็น "binary ที่ไม่เคย sign" — Defender heuristic flag ผิด

**แก้:**
1. **ระยะสั้น:** ลูกค้ากด "More info → Run anyway" ผ่าน
2. **ระยะกลาง:** ส่ง .exe ไปที่ https://www.microsoft.com/en-us/wdsi/filesubmission
   เพื่อรายงาน false positive (ฟรี ใช้เวลา ~24 ชม.)
3. **ระยะยาว:** ซื้อ Code Signing Certificate (~$100-300/ปี)
   - DigiCert / Sectigo มี cert สำหรับ individual ราคา ~3,000 บาท/ปี
   - ลง cert ใน GitHub Secrets แล้วใส่ใน workflow

### "create-dmg: command not found" บน Mac

```bash
brew install create-dmg
```

### "ISCC: not found" บน Windows

ลง Inno Setup 6 จาก https://jrsoftware.org/isinfo.php
หรือ: `choco install innosetup -y` ถ้ามี chocolatey

---

## Code Signing (ถ้าจะลงทุนเพิ่ม)

ปัจจุบัน installer ของเรา **ไม่ได้ sign** ดังนั้น:
- Windows: SmartScreen เตือน "Unknown publisher" ครั้งแรก
- macOS: Gatekeeper เตือน "ไม่ได้รับการ notarize"

ทั้งคู่ลูกค้าสามารถ override ได้ แต่ดู professional น้อย

| Platform | ราคา/ปี | ความซับซ้อน | ผลลัพธ์ |
|----------|---------|--------------|---------|
| **Apple Developer** | $99 (~3,400 ฿) | ง่าย | macOS Gatekeeper เงียบ + notarization |
| **Windows Code Signing** | $100-300 (~3,500-10,500 ฿) | ปานกลาง | SmartScreen เงียบหลัง warm-up reputation ~1-3 เดือน |

แนะนำเริ่มจาก **Apple Developer** ก่อน เพราะถูกและได้ผลทันที
Windows ยอมให้ลูกค้ากดผ่านได้อยู่แล้ว ไม่จำเป็นเร่งด่วน

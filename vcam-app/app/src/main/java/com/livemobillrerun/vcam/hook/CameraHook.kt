package com.livemobillrerun.vcam.hook

import android.app.Application
import android.content.Context
import android.graphics.SurfaceTexture
import android.hardware.Camera
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraDevice
import android.hardware.camera2.CameraManager
import android.hardware.camera2.CaptureRequest
import android.media.AudioRecord
import android.media.MediaCodec
import android.media.MediaCrypto
import android.media.MediaFormat
import android.os.Handler
import android.view.Surface
import de.robv.android.xposed.IXposedHookLoadPackage
import de.robv.android.xposed.XC_MethodHook
import de.robv.android.xposed.XposedBridge
import de.robv.android.xposed.XposedHelpers
import de.robv.android.xposed.callbacks.XC_LoadPackage
import java.io.File
import java.nio.ByteBuffer
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.Executor

/**
 * Xposed entry point for the vcam camera-replacement module.
 *
 * Loaded by LSPosed into TikTok's process. Installs hooks at the
 * MediaCodec / AudioRecord / Camera1 / Camera2 boundaries that let us
 * replace TikTok's camera feed with a video file from disk (which our
 * PC streamer keeps fresh via `adb push`).
 *
 * High-level architecture is documented in
 * `docs/CAMERAHOOK_ANALYSIS.md`. This class is the orchestrator; the
 * heavy lifting lives in [VideoFeeder] and [AudioFeeder].
 *
 * **Activation switch.** Mode is determined by [resolvedMode]:
 *  - `0` → passthrough (do nothing, real camera reaches encoder).
 *  - `1` → block (encoder gets no frames; viewers see black).
 *  - `2` → replace (encoder receives [VideoFeeder]'s frames).
 *
 * If [currentMode] is still `0` but `/data/local/tmp/vcam_enabled`
 * exists, the file presence is treated as mode `2`. That gives us a
 * one-line `adb` switch:
 *
 * ```
 * adb shell touch /data/local/tmp/vcam_enabled   # ON
 * adb shell rm    /data/local/tmp/vcam_enabled   # OFF
 * ```
 */
class CameraHook : IXposedHookLoadPackage {

    companion object {
        const val TAG = "VCAM_HOOK"

        /** TikTok International — primary target. */
        const val TIKTOK_PKG = "com.ss.android.ugc.trill"

        /** TikTok Musically (also international). */
        const val TIKTOK_PKG_MUSICALLY = "com.zhiliaoapp.musically"

        /** Douyin (China). Unlikely to be used here, kept for completeness. */
        const val TIKTOK_PKG_AWEME = "com.ss.android.ugc.aweme"

        /** Path of the "vcam is enabled" sentinel file on /data/local/tmp. */
        const val ENABLED_FLAG_PATH = "/data/local/tmp/vcam_enabled"

        /**
         * Optional flag to enable the GLES live-rotation pipeline.
         * When this file is **absent** (default), MediaPlayer feeds
         * the encoder Surface directly — same as UltimateRerun's
         * baseline. This is the most reliable path: zero races, zero
         * extra GL state, zero presentation-time tricks. Rotation is
         * still possible by re-encoding the MP4 with the right
         * orientation on the PC side.
         *
         * Touch this file to opt into FlipRenderer once we've ironed
         * out the EGL timing issues.
         */
        const val FLIP_RENDERER_FLAG_PATH = "/data/local/tmp/vcam_use_fliprenderer"

        /** When set by [VCamModeReceiver], overrides the on-disk flag. */
        @JvmField var currentMode: Int = 0

        /**
         * Optional explicit video path. If null, [VideoFeeder] will
         * walk a list of well-known fallback paths.
         */
        @JvmField var activeVideoPath: String? = null

        // ────────────────────────────────────────────────────────
        //  Camera-facing tracking (v1.8.8 — back-only bypass)
        // ────────────────────────────────────────────────────────
        //
        // Pre-v1.8.8 the hook injected the MP4 onto *every* camera
        // session. That tripped TikTok Live's pre-broadcast face /
        // liveness probe — the probe runs against the front-camera
        // preview (default) and the looped MP4 is too "perfect" to
        // pass. Customers had to physically uninstall the patch to
        // get past the gate.
        //
        // The fix: track which camera (front vs back) TikTok last
        // asked the framework to open, and bypass *only when the
        // back camera is active*. The front camera always shows
        // the real lens — that's what TikTok's detector sees, so
        // the broadcast gate opens. The customer then flips to
        // back-cam inside the live UI, which is when our injection
        // kicks in.
        //
        // Facing values follow the **Camera2** convention because
        // that's what TikTok uses on every API 33+ device we ship
        // to. Camera1 uses the inverted convention (0=BACK there)
        // so we normalise via [normaliseCamera1Facing].
        //
        //   FACING_FRONT   = 0   (CameraCharacteristics.LENS_FACING_FRONT)
        //   FACING_BACK    = 1   (CameraCharacteristics.LENS_FACING_BACK)
        //   FACING_EXTERNAL= 2   (USB / OTG camera — treated like back)
        //   FACING_UNKNOWN =-1   (no openCamera fired yet → safe = don't bypass)
        // ``val`` not ``const val`` — Kotlin only inlines Java
        // ``public static final`` constants from the SDK in some
        // build configurations, and the slight performance gain
        // from inlining vs. a static field load is negligible
        // against everything else this hook does per camera open.
        @JvmField val FACING_FRONT: Int = CameraCharacteristics.LENS_FACING_FRONT
        @JvmField val FACING_BACK: Int = CameraCharacteristics.LENS_FACING_BACK
        @JvmField val FACING_EXTERNAL: Int = CameraCharacteristics.LENS_FACING_EXTERNAL
        const val FACING_UNKNOWN = -1

        /**
         * Rear-lens upright correction is applied in **texture space**
         * ([FlipRenderer.rearLensCorrectTex180]) — not via stacking degrees
         * on [rotationDegrees], because MVP rotation alone failed to counter
         * [SurfaceTexture.getTransformMatrix] on some TikTok pipelines.
         */
        const val REAR_CAMERA_EXTRA_ROTATION_DEGREES: Int = 0

        /** Last camera TikTok asked to open. TikTok keeps one
         *  camera session at a time so a single global suffices
         *  for v1; multi-physical-camera devices (Pixel logical
         *  groups, S2x dual-cam Live) would benefit from a per-
         *  CameraDevice map but the v1 single-int approach already
         *  covers the customer's primary phones (Redmi Note 13,
         *  POCO X5, MediaTek Helio G81/G85). */
        @JvmField @Volatile var lastOpenedFacing: Int = FACING_UNKNOWN

        /** Per-Camera1-instance facing. Camera1 doesn't have an
         *  equivalent of CameraManager.getCameraCharacteristics
         *  reachable from the preview hook, so we resolve facing
         *  at `Camera.open(int)` time and cache it. */
        @JvmField val camera1Facing: ConcurrentHashMap<Camera, Int> =
            ConcurrentHashMap()

        /**
         * @return true when [facing] should receive the MP4
         *         replacement. v1.8.8 hardcodes "back only" — so
         *         the back lens (and external USB cams, treated
         *         the same) gets the loop; the front lens keeps
         *         producing real frames for TikTok's detector.
         *
         * Returns **false on FACING_UNKNOWN**. That's deliberate:
         * if openCamera hasn't fired yet, the safe assumption is
         * "we don't know — let the real camera through" rather
         * than "inject MP4 and hope". Wrong choice triggers the
         * detection step we're trying to evade.
         */
        @JvmStatic
        fun shouldBypass(facing: Int): Boolean {
            return facing == FACING_BACK || facing == FACING_EXTERNAL
        }

        /**
         * Camera1's `CameraInfo.facing` is the inverse of Camera2's
         * `LENS_FACING_*` constants:
         *
         *   Camera1: 0=BACK, 1=FRONT
         *   Camera2: 0=FRONT, 1=BACK
         *
         * Normalise to Camera2 so the rest of the hook can use a
         * single facing-int regardless of API path.
         */
        @JvmStatic
        fun normaliseCamera1Facing(c1Facing: Int): Int = when (c1Facing) {
            Camera.CameraInfo.CAMERA_FACING_BACK -> FACING_BACK
            Camera.CameraInfo.CAMERA_FACING_FRONT -> FACING_FRONT
            else -> FACING_UNKNOWN
        }

        /**
         * Encoders TikTok creates during a Live session. Tracked so
         * `queueInputBuffer` audio replacement can target only the
         * audio encoder.
         */
        @JvmField val videoEncoders: MutableSet<MediaCodec> = ConcurrentHashMap.newKeySet()
        @JvmField val audioEncoders: MutableSet<MediaCodec> = ConcurrentHashMap.newKeySet()

        /** Surfaces we've already attached a player to — avoids double-feeding. */
        @JvmField val encoderSurfaces: MutableSet<Surface> = ConcurrentHashMap.newKeySet()

        /**
         * Maps each encoder input [Surface] from [hookMediaCodecCreateInputSurface]
         * to its owning [MediaCodec]. Needed when injection is deferred to
         * [hookCaptureRequestAddTarget] so FlipRenderer / [StreamReceiver] can
         * recover the codec's width/height hints.
         */
        @JvmField val encoderSurfaceToCodec:
            ConcurrentHashMap<Surface, MediaCodec> = ConcurrentHashMap()

        /** TikTok process [Application]; set in [hookAppOnCreate]. Used to
         *  resolve [CameraManager] when stamping facing from [CameraDevice]. */
        @JvmField @Volatile var hostApplication: Application? = null

        /** Per-encoder MediaFormat captured from `configure()`. Used by
         *  [hookMediaCodecCreateInputSurface] to size the FlipRenderer. */
        @JvmField val videoFormats:
            ConcurrentHashMap<MediaCodec, MediaFormat> = ConcurrentHashMap()

        /** User-set transforms applied live by [FlipRenderer]. Updated
         *  by [InProcessModeReceiver] on each SET_MODE broadcast. */
        @JvmField @Volatile var liveRotationDegrees: Int = 0
        @JvmField @Volatile var liveMirrorH: Boolean = false
        @JvmField @Volatile var liveMirrorV: Boolean = false
        @JvmField @Volatile var liveZoom: Float = 1.0f

        /**
         * @return effective mode: explicit value if non-zero, else
         *         `2` if the disk flag exists, else `0`.
         */
        fun resolvedMode(): Int {
            if (currentMode > 0) return currentMode
            return if (File(ENABLED_FLAG_PATH).exists()) 2 else 0
        }

        fun log(msg: String) = XposedBridge.log("[$TAG] $msg")
    }

    override fun handleLoadPackage(lpparam: XC_LoadPackage.LoadPackageParam) {
        val pkg = lpparam.packageName
        val isTikTok = pkg == TIKTOK_PKG ||
                pkg == TIKTOK_PKG_MUSICALLY ||
                pkg == TIKTOK_PKG_AWEME
        if (!isTikTok) return

        log("✅ loaded into $pkg — installing hooks")
        // Facing trackers must install BEFORE the gate hooks so
        // that by the time `addTarget` / `createInputSurface` /
        // `setPreviewTexture` fires, `lastOpenedFacing` already
        // reflects which camera TikTok asked for. Failure here
        // (e.g. a vendor-modified CameraManager that ART can't
        // resolve) is logged but non-fatal — the gate hooks then
        // see FACING_UNKNOWN and refuse to inject, which is the
        // safe default (real camera passes through, no MP4 leak).
        try { hookCameraManagerOpenCamera() } catch (t: Throwable) { log("hookCameraManagerOpenCamera failed: $t") }
        try { hookCamera1Open() } catch (t: Throwable) { log("hookCamera1Open failed: $t") }
        try { hookCameraDeviceCreateCaptureRequest() } catch (t: Throwable) { log("hookCameraDeviceCreateCaptureRequest failed: $t") }

        try { hookMediaCodecConfigure() } catch (t: Throwable) { log("hookMediaCodecConfigure failed: $t") }
        try { hookMediaCodecCreateInputSurface() } catch (t: Throwable) { log("hookMediaCodecCreateInputSurface failed: $t") }
        try { hookMediaCodecAudioInput() } catch (t: Throwable) { log("hookMediaCodecAudioInput failed: $t") }
        try { hookCaptureRequestAddTarget() } catch (t: Throwable) { log("hookCaptureRequestAddTarget failed: $t") }
        try { hookCamera1Preview() } catch (t: Throwable) { log("hookCamera1Preview failed: $t") }
        try { hookAudioRecord() } catch (t: Throwable) { log("hookAudioRecord failed: $t") }
        try { hookAudioRecordSource() } catch (t: Throwable) { log("hookAudioRecordSource failed: $t") }
        try { hookDisableAEC() } catch (t: Throwable) { log("hookDisableAEC failed: $t") }
        try { hookAppOnCreate() } catch (t: Throwable) { log("hookAppOnCreate failed: $t") }
    }

    /* ───────────────────────────────────────────────────────── *
     *  Facing trackers — stamp [lastOpenedFacing] on every camera
     *  open so the gate hooks know who's writing to the
     *  surface(s) they're about to be asked to wrap.
     * ───────────────────────────────────────────────────────── */

    /**
     * Hook every overload of `CameraManager.openCamera` to stamp
     * `lastOpenedFacing` from `CameraCharacteristics.LENS_FACING`
     * *before* the framework returns the [CameraDevice]. The map
     * lookup goes through the same CameraManager instance so we
     * pick up vendor-extended cameras (Samsung's "logical" rear
     * cam group) using whatever ID the OEM assigned.
     *
     * Reading characteristics inside `beforeHookedMethod` adds a
     * one-time disk/IPC trip per camera open — negligible against
     * the 100-500 ms TikTok already spends initialising the
     * preview pipeline. If the read itself throws (e.g. permission
     * race when the app hasn't been granted CAMERA yet), we leave
     * `lastOpenedFacing` untouched so the gate falls back to its
     * previous value rather than flipping to UNKNOWN and exposing
     * the front camera to MP4 injection mid-session.
     */
    private fun hookCameraManagerOpenCamera() {
        // Overload A — pre-API-28 signature with a Handler.
        runCatching {
            XposedHelpers.findAndHookMethod(
                CameraManager::class.java, "openCamera",
                String::class.java,
                CameraDevice.StateCallback::class.java,
                Handler::class.java,
                facingStamperHook,
            )
            log("hook CameraManager.openCamera(String,Cb,Handler) installed")
        }.onFailure { log("openCamera handler-overload hook failed: $it") }

        // Overload B — API 28+ Executor signature.
        runCatching {
            XposedHelpers.findAndHookMethod(
                CameraManager::class.java, "openCamera",
                String::class.java,
                Executor::class.java,
                CameraDevice.StateCallback::class.java,
                facingStamperHook,
            )
            log("hook CameraManager.openCamera(String,Executor,Cb) installed")
        }.onFailure { log("openCamera executor-overload hook failed: $it") }
    }

    /** Shared body for both `openCamera` overloads. Reads facing
     *  from [CameraCharacteristics] for the camera ID being opened
     *  and stamps it onto [lastOpenedFacing] so subsequent
     *  surface-wrap decisions know who's driving the session. */
    private val facingStamperHook = object : XC_MethodHook() {
        override fun beforeHookedMethod(p: MethodHookParam) {
            val mgr = p.thisObject as? CameraManager ?: return
            val cameraId = p.args.getOrNull(0) as? String ?: return
            runCatching {
                val chars = mgr.getCameraCharacteristics(cameraId)
                val facing = chars.get(CameraCharacteristics.LENS_FACING)
                if (facing != null) {
                    lastOpenedFacing = facing
                    log(
                        "openCamera($cameraId) → facing=$facing " +
                            "(${facingName(facing)}); " +
                            "bypass=${shouldBypass(facing)}"
                    )
                }
            }.onFailure {
                log("openCamera facing probe failed for $cameraId: $it")
            }
        }
    }

    /**
     * Camera1 path: `Camera.open(int)` returns a `Camera` and
     * `Camera.CameraInfo.facing` tells us which lens. We cache
     * the result so the later `setPreviewTexture` hook can look
     * up "who's writing here" by the Camera instance alone.
     *
     * Camera1 is rare on API 33+ but a handful of TikTok login /
     * profile-photo flows still touch it, and ignoring those
     * would let the front-cam profile-photo capture leak through
     * the MP4 injection (the customer would silently upload an
     * MP4 frame as their avatar — embarrassing failure mode).
     */
    private fun hookCamera1Open() {
        runCatching {
            XposedHelpers.findAndHookMethod(
                Camera::class.java, "open",
                Int::class.javaPrimitiveType,
                object : XC_MethodHook() {
                    override fun afterHookedMethod(p: MethodHookParam) {
                        val cam = p.result as? Camera ?: return
                        val id = p.args.getOrNull(0) as? Int ?: return
                        val info = Camera.CameraInfo()
                        runCatching {
                            Camera.getCameraInfo(id, info)
                            val facing = normaliseCamera1Facing(info.facing)
                            camera1Facing[cam] = facing
                            lastOpenedFacing = facing
                            log(
                                "Camera.open($id) → facing=$facing " +
                                    "(${facingName(facing)}); " +
                                    "bypass=${shouldBypass(facing)}"
                            )
                        }.onFailure {
                            log("Camera.getCameraInfo($id) failed: $it")
                        }
                    }
                }
            )
            log("hook Camera.open(int) installed")
        }.onFailure { log("hookCamera1Open failed: $it") }

        // Camera1 also has a no-arg `open()` that opens the first
        // back camera. Stamp facing accordingly so the cache is
        // populated even for the legacy entry point.
        runCatching {
            XposedHelpers.findAndHookMethod(
                Camera::class.java, "open",
                object : XC_MethodHook() {
                    override fun afterHookedMethod(p: MethodHookParam) {
                        val cam = p.result as? Camera ?: return
                        // ``Camera.open()`` (no args) → first back cam.
                        camera1Facing[cam] = FACING_BACK
                        lastOpenedFacing = FACING_BACK
                        log("Camera.open() → assumed FACING_BACK")
                    }
                }
            )
            log("hook Camera.open() installed")
        }.onFailure { /* most TikTok builds skip this — keep silent */ }
    }

    /**
     * Stamp [lastOpenedFacing] from the [CameraDevice] that is about to
     * build a [CaptureRequest]. This fixes ordering bugs where
     * [hookMediaCodecCreateInputSurface] runs while [lastOpenedFacing] still
     * reflects a *different* camera (e.g. rear still cached when the user is
     * on the front preview), and it beats relying on [hookCameraManagerOpenCamera]
     * alone on OEMs that open logical / auxiliary cameras in unexpected order.
     */
    private fun hookCameraDeviceCreateCaptureRequest() {
        runCatching {
            XposedHelpers.findAndHookMethod(
                CameraDevice::class.java,
                "createCaptureRequest",
                Int::class.javaPrimitiveType,
                object : XC_MethodHook() {
                    override fun beforeHookedMethod(p: MethodHookParam) {
                        val dev = p.thisObject as? CameraDevice ?: return
                        stampFacingFromCameraId(dev.id)
                    }
                },
            )
            log("hook CameraDevice.createCaptureRequest(int) installed")
        }.onFailure { log("CameraDevice.createCaptureRequest hook failed: $it") }
    }

    /**
     * Resolve [CameraCharacteristics.LENS_FACING] for [cameraId] via the
     * host app's [CameraManager] and assign [lastOpenedFacing].
     */
    private fun stampFacingFromCameraId(cameraId: String) {
        val app = hostApplication
        if (app == null) {
            log("stampFacingFromCameraId: hostApplication=null (cameraId=$cameraId)")
            return
        }
        runCatching {
            val mgr = app.getSystemService(Context.CAMERA_SERVICE) as CameraManager
            val chars = mgr.getCameraCharacteristics(cameraId)
            val facing = chars.get(CameraCharacteristics.LENS_FACING)
            if (facing != null) {
                lastOpenedFacing = facing
                log(
                    "CameraDevice id=$cameraId → facing=$facing " +
                        "(${facingName(facing)}); bypass=${shouldBypass(facing)}",
                )
            }
        }.onFailure {
            log("stampFacingFromCameraId($cameraId) failed: $it")
        }
    }

    /** Pretty-print facing for log readability — saves the support
     *  engineer a Camera2-vs-Camera1 lookup when grepping logs. */
    private fun facingName(facing: Int): String = when (facing) {
        FACING_FRONT -> "FRONT"
        FACING_BACK -> "BACK"
        FACING_EXTERNAL -> "EXTERNAL"
        else -> "UNKNOWN($facing)"
    }

    /* ───────────────────────────────────────────────────────── *
     *  Each hook below mirrors the role of the same-named
     *  function in `UltimateRerun`'s `hook/CameraHook.kt`. The
     *  bodies are TODOs for the porting pass; this scaffolding
     *  exists so that the project still compiles & links against
     *  the Xposed API while we work on the rest.
     * ───────────────────────────────────────────────────────── */

    /**
     * Common body for every `MediaCodec.configure(...)` variant.
     * Extracted so we can attach the same handler to whichever
     * overload the running TikTok build happens to use.
     */
    private fun configureBodyFor(format: MediaFormat?, flags: Int, codec: MediaCodec) {
        val isEnc = (flags and MediaCodec.CONFIGURE_FLAG_ENCODE) != 0
        if (!isEnc) return
        val mime = format?.getString("mime") ?: return
        when {
            mime.startsWith("video/") -> {
                if (videoEncoders.add(codec)) {
                    log("video encoder registered: $mime (n=${videoEncoders.size})")
                }
                // Remember dimensions for FlipRenderer sizing later.
                videoFormats[codec] = format
            }
            mime.startsWith("audio/") -> {
                if (audioEncoders.add(codec)) {
                    log("audio encoder registered: $mime (n=${audioEncoders.size})")
                }
                val path = activeVideoPath ?: VideoFeeder.activeVideoPath()
                if (path != null && !AudioFeeder.enabled) {
                    AudioFeeder.start(path)
                    log("🔊 AudioFeeder auto-started")
                }
            }
        }
    }

    private fun hookMediaCodecConfigure() {
        // Variant A — original 4-arg. (format, surface, crypto, flags).
        runCatching {
            XposedHelpers.findAndHookMethod(
                MediaCodec::class.java, "configure",
                MediaFormat::class.java, Surface::class.java,
                MediaCrypto::class.java, Int::class.javaPrimitiveType,
                object : XC_MethodHook() {
                    override fun beforeHookedMethod(p: MethodHookParam) {
                        val format = p.args.getOrNull(0) as? MediaFormat
                        val flags = (p.args.getOrNull(3) as? Int) ?: return
                        configureBodyFor(format, flags, p.thisObject as MediaCodec)
                    }
                }
            )
            log("hook configure(format,surface,crypto,flags) installed")
        }.onFailure { log("configure variant A failed: $it") }

        // Variant B — Android 8+. (format, surface, flags, descriptor).
        // The 3-arg flags index is at position 2 here.
        runCatching {
            val descriptorClass = Class.forName(
                "android.media.MediaDescrambler"
            )
            XposedHelpers.findAndHookMethod(
                MediaCodec::class.java, "configure",
                MediaFormat::class.java, Surface::class.java,
                Int::class.javaPrimitiveType, descriptorClass,
                object : XC_MethodHook() {
                    override fun beforeHookedMethod(p: MethodHookParam) {
                        val format = p.args.getOrNull(0) as? MediaFormat
                        val flags = (p.args.getOrNull(2) as? Int) ?: return
                        configureBodyFor(format, flags, p.thisObject as MediaCodec)
                    }
                }
            )
            log("hook configure(format,surface,flags,descriptor) installed")
        }.onFailure { /* MediaDescrambler not always present — silent */ }
    }

    private fun hookMediaCodecCreateInputSurface() {
        XposedHelpers.findAndHookMethod(
            MediaCodec::class.java, "createInputSurface",
            object : XC_MethodHook() {
                override fun afterHookedMethod(p: MethodHookParam) {
                    val encoderSurface = p.result as? Surface ?: return
                    val mode = resolvedMode()
                    val codec = p.thisObject as? MediaCodec
                    val facing = lastOpenedFacing
                    log(
                        "createInputSurface fired — mode=$mode " +
                            "facing=${facingName(facing)} surface=$encoderSurface"
                    )
                    if (mode == 0) return
                    if (mode == 2) {
                        // v1.8.9: NEVER inject from createInputSurface — TikTok
                        // often creates the video encoder *before* the camera
                        // pipeline stamps [lastOpenedFacing], so feeding here
                        // poisoned the front preview with stale BACK state from
                        // an earlier session or auxiliary open. Registration +
                        // deferred inject in [hookCaptureRequestAddTarget] only.
                        encoderSurfaces.add(encoderSurface)
                        if (codec != null) {
                            encoderSurfaceToCodec[encoderSurface] = codec
                        }
                        log(
                            "createInputSurface: mode=2 registered " +
                                "$encoderSurface (inject deferred to addTarget); " +
                                "facingHint=${facingName(facing)}",
                        )
                        return
                    }
                    encoderSurfaces.add(encoderSurface)
                }
            }
        )
        log("hook MediaCodec.createInputSurface installed")
    }

    /**
     * Build a [FlipRenderer] for [outputSurface] and return its input
     * Surface. MediaPlayer feeds *that*; FlipRenderer redraws every
     * frame onto [outputSurface] applying the user's live transform.
     *
     * @param widthHint  output width to draw at — used for the GL
     *                   viewport. If null, falls back to the codec's
     *                   captured MediaFormat or 720.
     * @param heightHint output height for the GL viewport. Default
     *                   1280 in the absence of any other signal.
     *
     * Returns null on any GL setup failure — caller falls back to the
     * direct path so playback still works, just without live rotation.
     */
    private fun wrapWithFlipRenderer(
        codec: MediaCodec?,
        outputSurface: Surface,
        widthHint: Int? = null,
        heightHint: Int? = null,
        rearLensExtraRotation: Int = 0,
        /** Horizontal flip baseline like TikTok front preview (selfie mirror). */
        mirrorLikeFrontCamera: Boolean = false,
    ): Surface? {
        // Re-use an existing renderer if we already wrapped this surface.
        FlipRenderer.instances[outputSurface]?.let { return it.inputSurface }
        // NB: we used to call FlipRenderer.stopOthers() here to keep
        // memory bounded, but that killed the preview pipeline the
        // moment TikTok created a *second* encoder for Live broadcast
        // (preview + live encoder coexist for the duration of the
        // broadcast). The preview Surface's MediaPlayer/FlipRenderer
        // got torn down → preview froze on its last frame while audio
        // kept flowing through the independent AudioFeeder path.
        //
        // Cleanup is now done lazily by VideoFeeder.watchdog, which
        // checks Surface.isValid() every tick and stops the chain
        // only when the OS has actually released the Surface.
        return runCatching {
            val format = codec?.let { videoFormats[it] }
            val w = widthHint
                ?: format?.getInteger(MediaFormat.KEY_WIDTH)
                ?: 720
            val h = heightHint
                ?: format?.getInteger(MediaFormat.KEY_HEIGHT)
                ?: 1280
            val fr = FlipRenderer(w, h, outputSurface).also { r ->
                r.rearLensExtraRotation = rearLensExtraRotation
                r.rotationDegrees = liveRotationDegrees + rearLensExtraRotation
                r.rearLensMirrorLikeFront = mirrorLikeFrontCamera
                r.rearLensCorrectTex180 = mirrorLikeFrontCamera
                r.mirrorH = liveMirrorH != mirrorLikeFrontCamera
                r.mirrorV = liveMirrorV
                r.zoom = liveZoom
                r.start()  // synchronous — blocks until inputSurface is non-null
            }
            FlipRenderer.instances[outputSurface] = fr
            log("🎞 FlipRenderer ${w}×$h installed on $outputSurface")
            fr.inputSurface
        }.onFailure {
            log("FlipRenderer setup failed, falling back to direct: $it")
        }.getOrNull()
    }

    private fun hookMediaCodecAudioInput() {
        XposedHelpers.findAndHookMethod(
            MediaCodec::class.java, "queueInputBuffer",
            Int::class.javaPrimitiveType, Int::class.javaPrimitiveType,
            Int::class.javaPrimitiveType, Long::class.javaPrimitiveType,
            Int::class.javaPrimitiveType,
            object : XC_MethodHook() {
                override fun beforeHookedMethod(p: MethodHookParam) {
                    if (!AudioFeeder.enabled) return
                    val codec = p.thisObject as? MediaCodec ?: return
                    if (codec !in audioEncoders) return
                    val flags = p.args[4] as Int
                    if ((flags and MediaCodec.BUFFER_FLAG_END_OF_STREAM) != 0) return
                    val bufIdx = p.args[0] as Int
                    val offset = p.args[1] as Int
                    val size = p.args[2] as Int
                    if (size <= 0) return
                    val input = codec.getInputBuffer(bufIdx) ?: return
                    val pcm = ByteArray(size)
                    AudioFeeder.read(pcm, 0, size)
                    input.position(offset)
                    input.put(pcm, 0, size)
                }
            }
        )
        log("MediaCodec.queueInputBuffer audio hook installed")
    }

    private fun hookCaptureRequestAddTarget() {
        XposedHelpers.findAndHookMethod(
            CaptureRequest.Builder::class.java, "addTarget",
            Surface::class.java,
            object : XC_MethodHook() {
                override fun beforeHookedMethod(p: MethodHookParam) {
                    val surface = p.args[0] as? Surface ?: return
                    val mode = resolvedMode()
                    if (mode == 0) return
                    if (mode == 2) {
                        // v1.8.9: [stampFacingFromCameraId] runs from
                        // CameraDevice.createCaptureRequest right before
                        // builders add outputs — [lastOpenedFacing] matches
                        // the camera driving *this* session (not a stale open).
                        val facing = lastOpenedFacing
                        if (!shouldBypass(facing)) {
                            stopAnyInjectionFor(surface)
                            log(
                                "addTarget: facing=${facingName(facing)} " +
                                    "→ pass-through (real camera)",
                            )
                            return
                        }

                        if (
                            StreamReceiver.enabled() &&
                            encoderSurfaces.contains(surface)
                        ) {
                            val codecHint = encoderSurfaceToCodec[surface]
                            val playSurface =
                                wrapWithFlipRenderer(
                                    codecHint,
                                    surface,
                                    rearLensExtraRotation =
                                        REAR_CAMERA_EXTRA_ROTATION_DEGREES,
                                    mirrorLikeFrontCamera = false,
                                )
                                    ?: surface
                            val format = codecHint?.let { videoFormats[it] }
                            val w =
                                format?.getInteger(MediaFormat.KEY_WIDTH) ?: 720
                            val h =
                                format?.getInteger(MediaFormat.KEY_HEIGHT)
                                    ?: 1280
                            val rx = StreamReceiver(playSurface, w, h)
                            StreamReceiver.instances[surface] = rx
                            rx.start()
                            log(
                                "📡 live stream via addTarget " +
                                    "(${w}×$h facing=${facingName(facing)})",
                            )
                        } else {
                            val path =
                                activeVideoPath ?: VideoFeeder.activeVideoPath()
                            if (path != null) {
                                val codecHint = encoderSurfaceToCodec[surface]
                                val targetSurface =
                                    wrapWithFlipRenderer(
                                        codecHint,
                                        surface,
                                        rearLensExtraRotation =
                                            REAR_CAMERA_EXTRA_ROTATION_DEGREES,
                                        mirrorLikeFrontCamera = false,
                                    )
                                        ?: surface
                                VideoFeeder.feedToSurface(targetSurface, path)
                                log(
                                    "🎬 video injected via addTarget " +
                                        "(facing=${facingName(facing)})",
                                )
                            }
                        }
                    }
                    p.result = null
                    log("🚫 camera blocked (mode=$mode)")
                }
            }
        )
        log("hook CaptureRequest.Builder.addTarget installed")
    }

    /**
     * Tear down any FlipRenderer + VideoFeeder previously attached
     * to [surface]. Called when we decide to **stop** injecting for
     * that surface — e.g. the customer flipped from back camera
     * (bypass) to front camera (real). Without this, the previous
     * session's MediaPlayer keeps drawing onto the same encoder
     * Surface in parallel with the now-attached real camera, and
     * the encoder sees a torn mixture of MP4 frames and live frames.
     *
     * Safe to call on a Surface that was never wrapped — both
     * lookups return null and the method becomes a no-op.
     */
    private fun stopAnyInjectionFor(surface: Surface) {
        val fr = FlipRenderer.instances[surface]
        if (fr != null) {
            // Stop the MediaPlayer first so its callbacks can't
            // race with the renderer teardown — VideoFeeder.stopFor
            // is UI-thread-safe (it posts) so we can call it inline.
            val input = fr.inputSurface
            if (input != null) VideoFeeder.stopFor(input)
            runCatching { fr.stop() }
                .onFailure { log("FlipRenderer.stop() failed on $surface: $it") }
            log("⏹ stopped injection for $surface (facing change)")
        } else {
            // No FlipRenderer wrap — but the camera/preview path
            // may have fed the surface directly. Stop just in case.
            VideoFeeder.stopFor(surface)
        }
    }

    private fun hookCamera1Preview() {
        XposedHelpers.findAndHookMethod(
            Camera::class.java, "setPreviewTexture",
            SurfaceTexture::class.java,
            object : XC_MethodHook() {
                override fun beforeHookedMethod(p: MethodHookParam) {
                    val orig = p.args[0] as? SurfaceTexture ?: return
                    val mode = resolvedMode()
                    if (mode == 0) return
                    // mode == 1 (block): always replace with dummy
                    // so the encoder gets no frames; facing is
                    // irrelevant — the customer asked for a black
                    // broadcast and that's what they get. Fall
                    // through past the mode==2 branch into the
                    // shared dummy-swap below.
                    if (mode == 2) {
                        // v1.8.8: per-Camera-instance facing lookup.
                        // Camera1 doesn't have CameraCharacteristics
                        // — the source of truth is the map populated
                        // by [hookCamera1Open]. Fall back to
                        // [lastOpenedFacing] when the instance isn't
                        // in the map (legacy Camera.open() variants
                        // that bypassed our hook); FACING_UNKNOWN
                        // then triggers the safe "no inject" path.
                        val cam = p.thisObject as? Camera
                        val facing = cam?.let { camera1Facing[it] }
                            ?: lastOpenedFacing
                        if (!shouldBypass(facing)) {
                            log(
                                "Camera1 setPreviewTexture: " +
                                    "facing=${facingName(facing)} → " +
                                    "pass-through (real camera)"
                            )
                            // Leave p.args[0] alone so the real
                            // camera writes to TikTok's real
                            // SurfaceTexture; preview shows the
                            // front lens for the detection step.
                            return
                        }
                        val path = activeVideoPath ?: VideoFeeder.activeVideoPath()
                        if (path != null) {
                            VideoFeeder.feedToSurface(Surface(orig), path)
                        }
                    }
                    // Shared between mode=1 (block) and mode=2-BACK
                    // (bypass): hand the camera a dummy SurfaceTexture
                    // so its frames go nowhere on the customer's
                    // pre-existing path. For mode=2 we already
                    // started VideoFeeder above, which writes the
                    // MP4 onto the real `orig` surface in parallel.
                    p.args[0] = SurfaceTexture(0)
                    log("🎬 Camera1 setPreviewTexture intercepted (mode=$mode)")
                }
            }
        )
        log("hook Camera.setPreviewTexture installed")
    }

    private fun hookAudioRecord() {
        val audioHook3 = object : XC_MethodHook() {
            override fun beforeHookedMethod(p: MethodHookParam) {
                if (!AudioFeeder.enabled) return
                val buf = p.args[0] as? ByteArray ?: return
                val offset = p.args[1] as Int
                val size = p.args[2] as Int
                p.result = AudioFeeder.read(buf, offset, size)
            }
        }
        val audioHook4 = object : XC_MethodHook() {
            override fun beforeHookedMethod(p: MethodHookParam) {
                if (!AudioFeeder.enabled) return
                val buf = p.args[0] as? ByteArray ?: return
                val offset = p.args[1] as Int
                val size = p.args[2] as Int
                p.result = AudioFeeder.read(buf, offset, size)
            }
        }
        val audioBufHook = object : XC_MethodHook() {
            override fun beforeHookedMethod(p: MethodHookParam) {
                if (!AudioFeeder.enabled) return
                val buf = p.args[0] as? ByteBuffer ?: return
                val size = p.args[1] as Int
                p.result = AudioFeeder.read(buf, size)
            }
        }
        runCatching {
            XposedHelpers.findAndHookMethod(
                AudioRecord::class.java, "read",
                ByteArray::class.java, Int::class.javaPrimitiveType,
                Int::class.javaPrimitiveType, audioHook3
            )
        }.onFailure { log("AudioRecord.read(byte[],int,int) hook failed: $it") }
        runCatching {
            XposedHelpers.findAndHookMethod(
                AudioRecord::class.java, "read",
                ByteArray::class.java, Int::class.javaPrimitiveType,
                Int::class.javaPrimitiveType, Int::class.javaPrimitiveType, audioHook4
            )
        }.onFailure { log("AudioRecord.read(byte[],int,int,int) hook failed: $it") }
        runCatching {
            XposedHelpers.findAndHookMethod(
                AudioRecord::class.java, "read",
                ByteBuffer::class.java, Int::class.javaPrimitiveType, audioBufHook
            )
        }.onFailure { log("AudioRecord.read(ByteBuffer,int) hook failed: $it") }
        log("AudioRecord hooks installed")
    }

    private fun hookAudioRecordSource() {
        runCatching {
            XposedHelpers.findAndHookConstructor(
                AudioRecord::class.java,
                Int::class.javaPrimitiveType, Int::class.javaPrimitiveType,
                Int::class.javaPrimitiveType, Int::class.javaPrimitiveType,
                Int::class.javaPrimitiveType,
                object : XC_MethodHook() {
                    override fun beforeHookedMethod(p: MethodHookParam) {
                        // 9 = MediaRecorder.AudioSource.UNPROCESSED — bypass DSP.
                        log("AudioRecord ctor src=${p.args[0]} → forcing UNPROCESSED(9)")
                        p.args[0] = 9
                    }
                }
            )
        }.onFailure { log("AudioRecord ctor hook failed: $it") }
        runCatching {
            XposedHelpers.findAndHookMethod(
                "android.media.AudioRecord\$Builder", null, "setAudioSource",
                Int::class.javaPrimitiveType,
                object : XC_MethodHook() {
                    override fun beforeHookedMethod(p: MethodHookParam) {
                        log("AudioRecord.Builder.setAudioSource → UNPROCESSED(9)")
                        p.args[0] = 9
                    }
                }
            )
        }.onFailure { log("AudioRecord.Builder hook failed: $it") }
    }

    private fun hookDisableAEC() {
        runCatching {
            XposedHelpers.findAndHookMethod(
                "android.media.audiofx.AcousticEchoCanceler", null, "create",
                Int::class.javaPrimitiveType,
                object : XC_MethodHook() {
                    override fun beforeHookedMethod(p: MethodHookParam) {
                        p.result = null
                        log("AEC blocked")
                    }
                }
            )
        }.onFailure { log("AEC hook failed: $it") }
        runCatching {
            XposedHelpers.findAndHookMethod(
                "android.media.audiofx.NoiseSuppressor", null, "create",
                Int::class.javaPrimitiveType,
                object : XC_MethodHook() {
                    override fun beforeHookedMethod(p: MethodHookParam) {
                        p.result = null
                        log("NoiseSuppressor blocked")
                    }
                }
            )
        }.onFailure { log("NoiseSuppressor hook failed: $it") }
    }

    private fun hookAppOnCreate() {
        XposedHelpers.findAndHookMethod(
            Application::class.java, "onCreate",
            object : XC_MethodHook() {
                override fun afterHookedMethod(p: MethodHookParam) {
                    log("🚀 Application.onCreate fired in TikTok process")
                    // Register an in-process receiver so SET_MODE
                    // broadcasts from vcam-pc reach THIS Application's
                    // copy of CameraHook static state (the receiver
                    // declared in vcam-app's manifest only updates
                    // the receiver's own process — different VM).
                    runCatching {
                        val app = p.thisObject as android.app.Application
                        hostApplication = app
                        val recv = InProcessModeReceiver()
                        val filter = android.content.IntentFilter(
                            "com.livemobillrerun.vcam.SET_MODE"
                        )
                        if (android.os.Build.VERSION.SDK_INT >= 33) {
                            app.registerReceiver(
                                recv, filter,
                                android.content.Context.RECEIVER_EXPORTED,
                            )
                        } else {
                            @Suppress("UnspecifiedRegisterReceiverFlag")
                            app.registerReceiver(recv, filter)
                        }
                        log("📡 in-process SET_MODE receiver registered")
                    }.onFailure {
                        log("registerReceiver failed: $it")
                    }
                }
            }
        )
    }

    /**
     * BroadcastReceiver that runs **inside the host TikTok process**
     * so updating [CameraHook.currentMode] / [CameraHook.activeVideoPath]
     * actually affects the live hook state (vs. updating a sibling
     * static field in vcam-app's separate VM).
     */
    private class InProcessModeReceiver : android.content.BroadcastReceiver() {
        override fun onReceive(
            ctx: android.content.Context,
            intent: android.content.Intent,
        ) {
            if (intent.action != "com.livemobillrerun.vcam.SET_MODE") return
            val mode = intent.getIntExtra("mode", -1)
            val path = intent.getStringExtra("videoPath")
            val forceReload = intent.getBooleanExtra("forceReload", false)
            if (mode in 0..2) currentMode = mode
            if (!path.isNullOrBlank()) {
                activeVideoPath = path
                VideoFeeder.activeVideoPath = path
            }

            // Live transform parameters — applied via FlipRenderer.
            // Rotation may arrive as int (--ei) or float (--ef) depending on sender.
            if (intent.hasExtra("rotation")) {
                val raw = intent.extras?.get("rotation")
                val deg = when (raw) {
                    is Int -> raw
                    is Long -> raw.toInt()
                    is Float -> raw.toInt()
                    is Double -> raw.toInt()
                    else -> intent.getIntExtra("rotation", 0)
                }
                liveRotationDegrees = ((deg % 360) + 360) % 360
            }
            if (intent.hasExtra("flipX")) liveMirrorH = intent.getBooleanExtra("flipX", false)
            if (intent.hasExtra("flipY")) liveMirrorV = intent.getBooleanExtra("flipY", false)
            val z = intent.getFloatExtra("zoom", -1f)
            if (z > 0f) liveZoom = z

            // Push the new transforms to all live FlipRenderers.
            for (fr in FlipRenderer.instances.values) {
                fr.rotationDegrees =
                    liveRotationDegrees + fr.rearLensExtraRotation
                fr.mirrorH = liveMirrorH != fr.rearLensMirrorLikeFront
                fr.mirrorV = liveMirrorV
                fr.zoom = liveZoom
            }

            log(
                "📡 SET_MODE → mode=$currentMode " +
                    "path=${activeVideoPath?.substringAfterLast('/')} " +
                    "rot=$liveRotationDegrees flipH=$liveMirrorH flipV=$liveMirrorV " +
                    "zoom=$liveZoom reload=$forceReload"
            )
            if (forceReload) {
                val target = activeVideoPath ?: VideoFeeder.activeVideoPath()
                if (target != null) VideoFeeder.reloadVideo(target)
            }
        }
    }
}

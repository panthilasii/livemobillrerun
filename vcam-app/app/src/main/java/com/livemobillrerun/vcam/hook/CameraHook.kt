package com.livemobillrerun.vcam.hook

import android.app.Application
import android.graphics.SurfaceTexture
import android.hardware.Camera
import android.hardware.camera2.CaptureRequest
import android.media.AudioRecord
import android.media.MediaCodec
import android.media.MediaCrypto
import android.media.MediaFormat
import android.view.Surface
import de.robv.android.xposed.IXposedHookLoadPackage
import de.robv.android.xposed.XC_MethodHook
import de.robv.android.xposed.XposedBridge
import de.robv.android.xposed.XposedHelpers
import de.robv.android.xposed.callbacks.XC_LoadPackage
import java.io.File
import java.nio.ByteBuffer
import java.util.concurrent.ConcurrentHashMap

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

        /**
         * Encoders TikTok creates during a Live session. Tracked so
         * `queueInputBuffer` audio replacement can target only the
         * audio encoder.
         */
        @JvmField val videoEncoders: MutableSet<MediaCodec> = ConcurrentHashMap.newKeySet()
        @JvmField val audioEncoders: MutableSet<MediaCodec> = ConcurrentHashMap.newKeySet()

        /** Surfaces we've already attached a player to — avoids double-feeding. */
        @JvmField val encoderSurfaces: MutableSet<Surface> = ConcurrentHashMap.newKeySet()

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
                    log("createInputSurface fired — mode=$mode surface=$encoderSurface")
                    if (mode == 0) return
                    if (mode == 2) {
                        // Wrap encoder Surface with FlipRenderer so the
                        // user's rotation/mirror picker (in vcam-app)
                        // takes effect live. Fixed race: start() is now
                        // blocking, so inputSurface is guaranteed
                        // non-null when wrapWithFlipRenderer returns.
                        val playSurface: Surface =
                            wrapWithFlipRenderer(codec, encoderSurface)
                                ?: encoderSurface
                        encoderSurfaces.add(encoderSurface)

                        // Live-stream takes precedence over the static
                        // MP4 loop — the user can flip between them by
                        // touching/removing /data/local/tmp/vcam_stream_url
                        if (StreamReceiver.enabled()) {
                            log("📡 live stream mode: attaching StreamReceiver")
                            val format = codec?.let { videoFormats[it] }
                            val w = format?.getInteger(MediaFormat.KEY_WIDTH) ?: 720
                            val h = format?.getInteger(MediaFormat.KEY_HEIGHT) ?: 1280
                            val rx = StreamReceiver(playSurface, w, h)
                            StreamReceiver.instances[encoderSurface] = rx
                            rx.start()
                            return
                        }

                        val path = activeVideoPath ?: VideoFeeder.activeVideoPath()
                        if (path != null) {
                            log("🎬 mp4 loop mode: ${path.substringAfterLast('/')}")
                            VideoFeeder.feedToSurface(playSurface, path)
                            return
                        }
                        log("⚠ createInputSurface: mode=2 but no source available")
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
                r.rotationDegrees = liveRotationDegrees
                r.mirrorH = liveMirrorH
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
                        val path = activeVideoPath ?: VideoFeeder.activeVideoPath()
                        if (path != null) {
                            // TikTok Live takes this path: the camera
                            // target Surface is sized to the camera's
                            // sensor (typically landscape), and any
                            // rotation TikTok applies for the portrait
                            // viewport happens DOWNSTREAM of us. Wrap
                            // it with FlipRenderer so the user can
                            // dial in the right rotation from app UI
                            // and the broadcast hits FlipRenderer
                            // immediately.
                            val targetSurface =
                                wrapWithFlipRenderer(null, surface)
                                    ?: surface
                            VideoFeeder.feedToSurface(targetSurface, path)
                            log("🎬 video injected via CaptureRequest.addTarget")
                        }
                    }
                    p.result = null
                    log("🚫 camera blocked (mode=$mode)")
                }
            }
        )
        log("hook CaptureRequest.Builder.addTarget installed")
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
                    if (mode == 2) {
                        val path = activeVideoPath ?: VideoFeeder.activeVideoPath()
                        if (path != null) {
                            VideoFeeder.feedToSurface(Surface(orig), path)
                        }
                    }
                    // Hand the camera a dummy texture so its frames go nowhere.
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
            // Use sentinel ints so unspecified extras don't reset.
            val rot = intent.getIntExtra("rotation", -1)
            if (rot != -1) liveRotationDegrees = rot
            if (intent.hasExtra("flipX")) liveMirrorH = intent.getBooleanExtra("flipX", false)
            if (intent.hasExtra("flipY")) liveMirrorV = intent.getBooleanExtra("flipY", false)
            val z = intent.getFloatExtra("zoom", -1f)
            if (z > 0f) liveZoom = z

            // Push the new transforms to all live FlipRenderers.
            for (fr in FlipRenderer.instances.values) {
                fr.rotationDegrees = liveRotationDegrees
                fr.mirrorH = liveMirrorH
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

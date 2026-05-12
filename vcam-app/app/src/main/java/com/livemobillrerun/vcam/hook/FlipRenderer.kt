package com.livemobillrerun.vcam.hook

import android.graphics.SurfaceTexture
import android.opengl.EGL14
import android.opengl.EGLConfig
import android.opengl.EGLExt
import android.opengl.EGLContext
import android.opengl.EGLDisplay
import android.opengl.EGLSurface
import android.opengl.GLES11Ext
import android.opengl.GLES20
import android.opengl.Matrix
import android.os.Handler
import android.os.HandlerThread
import android.view.Surface
import de.robv.android.xposed.XposedBridge
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.nio.FloatBuffer
import java.util.concurrent.ConcurrentHashMap

/**
 * GLES-based middleman that lets us apply a live rotation / zoom /
 * mirror to a [MediaPlayer] feed before it lands on TikTok's encoder
 * input Surface.
 *
 * Pipeline:
 *
 * ```
 *   MediaPlayer ─► FlipRenderer.inputSurface (SurfaceTexture)
 *                                │
 *                                ├─ EGL context bound to ────────────┐
 *                                ▼                                  ▼
 *                          GLES draw call                     [outputSurface]
 *                          (rotation/zoom/flip applied)       (TikTok encoder)
 * ```
 *
 * Without this middleman, [MediaPlayer.setSurface] writes frames
 * straight into the encoder Surface — leaving us no way to apply
 * runtime transforms. By inserting our own [SurfaceTexture] we get
 * a free GLES texture handle on every frame, run a fragment shader,
 * and re-render with whatever transform [params] currently says.
 *
 * **Status:** functional baseline. Rotates/mirrors but doesn't yet
 * implement zoom (`scale != 1.0`) — it's a single quad with the same
 * vertex coordinates each frame.
 */
class FlipRenderer(
    /** Width of the output Surface (encoder). */
    private val outputW: Int,
    /** Height of the output Surface. */
    private val outputH: Int,
    /** Final encoder Surface to draw into. */
    private val outputSurface: Surface,
) {
    @Volatile var rotationDegrees: Int = 0
    @Volatile var mirrorH: Boolean = false
    @Volatile var mirrorV: Boolean = false
    @Volatile var zoom: Float = 1.0f

    /**
     * Offset baked into [rotationDegrees] for rear-facing injection only.
     * When SET_MODE updates [CameraHook.liveRotationDegrees], we re-apply
     * `liveRotationDegrees + rearLensExtraRotation` so the dashboard knob
     * stays relative to an upright baseline (see [CameraHook]).
     */
    @Volatile var rearLensExtraRotation: Int = 0

    /**
     * When true, [mirrorH] is driven as `liveMirrorH != true` from SET_MODE
     * — i.e. default horizontal flip matches front-camera (selfie) preview;
     * user toggle "flip X" on the PC still inverts that baseline.
     */
    @Volatile var rearLensMirrorLikeFront: Boolean = false

    /**
     * After [SurfaceTexture.getTransformMatrix], multiply in a 180° fix in
     * texture space for rear-lens injection: [rot180] * [texMatrix] so the
     * OEM matrix applies to UVs first, then the upright correction (MVP-only
     * rotation was insufficient on some TikTok / encoder pipelines).
     */
    @Volatile var rearLensCorrectTex180: Boolean = false

    private val thread = HandlerThread("vcam-fliprender").apply { start() }
    private val handler = Handler(thread.looper)

    private var eglDisplay: EGLDisplay = EGL14.EGL_NO_DISPLAY
    private var eglContext: EGLContext = EGL14.EGL_NO_CONTEXT
    private var eglSurface: EGLSurface = EGL14.EGL_NO_SURFACE
    private var program: Int = 0
    private var aPosLoc: Int = -1
    private var aTexLoc: Int = -1
    private var uMvpLoc: Int = -1
    private var uTexMatLoc: Int = -1
    private var uSamplerLoc: Int = -1
    private var oesTexture: Int = 0

    /** The Surface MediaPlayer writes into. Available after [start]. */
    @Volatile var inputSurface: Surface? = null
        private set

    private var inputTexture: SurfaceTexture? = null
    private val texMatrix = FloatArray(16)
    private val mvpMatrix = FloatArray(16)
    private val identityMatrix = FloatArray(16).apply { Matrix.setIdentityM(this, 0) }

    /**
     * Initialise EGL synchronously. Blocks the caller until
     * [inputSurface] is non-null so that whoever needs it (typically
     * MediaPlayer) doesn't accidentally fall back to writing into
     * [outputSurface] directly while we're still owning the EGL window.
     */
    fun start() {
        val latch = java.util.concurrent.CountDownLatch(1)
        handler.post {
            try { setupGl() } catch (t: Throwable) {
                XposedBridge.log("[FlipRenderer] setupGl failed: $t")
            }
            latch.countDown()
        }
        // Cap the wait so a misbehaving GL driver can't deadlock the
        // hooked thread forever — we'd rather render into the encoder
        // Surface directly than freeze TikTok.
        runCatching {
            latch.await(2, java.util.concurrent.TimeUnit.SECONDS)
        }
    }

    fun stop() {
        handler.post { teardownGl() }
        thread.quitSafely()
    }

    /* ─── GL setup ───────────────────────────────────────────── */

    /** Actual width of the EGL window surface — discovered after
     *  eglCreateWindowSurface, used for glViewport so we don't distort
     *  when the caller's hints don't match the real Surface size
     *  (common when the addTarget Surface comes from camera2 directly). */
    @Volatile private var actualW: Int = 0
    @Volatile private var actualH: Int = 0

    /** Wall-clock baseline used to synthesise a monotonic PTS for the
     *  encoder. Using SurfaceTexture.timestamp directly was hitting
     *  zero every time MediaPlayer looped (seek-to-0 → PTS reset),
     *  which made the H.264 encoder freeze on the last good frame
     *  while audio kept flowing — exactly the symptom the user saw
     *  during Live ("video freezes, audio fine"). */
    private var ptsBaseNanos: Long = 0L

    private fun setupGl() {
        eglDisplay = EGL14.eglGetDisplay(EGL14.EGL_DEFAULT_DISPLAY)
        EGL14.eglInitialize(eglDisplay, IntArray(2), 0, IntArray(2), 1)

        val cfgAttribs = intArrayOf(
            EGL14.EGL_RED_SIZE, 8,
            EGL14.EGL_GREEN_SIZE, 8,
            EGL14.EGL_BLUE_SIZE, 8,
            EGL14.EGL_ALPHA_SIZE, 8,
            EGL14.EGL_RENDERABLE_TYPE, EGL14.EGL_OPENGL_ES2_BIT,
            EGL14.EGL_SURFACE_TYPE, EGL14.EGL_WINDOW_BIT,
            EGL14.EGL_NONE,
        )
        val configs = arrayOfNulls<EGLConfig>(1)
        EGL14.eglChooseConfig(
            eglDisplay, cfgAttribs, 0, configs, 0, 1, IntArray(1), 0,
        )
        val ctxAttribs = intArrayOf(EGL14.EGL_CONTEXT_CLIENT_VERSION, 2, EGL14.EGL_NONE)
        eglContext = EGL14.eglCreateContext(
            eglDisplay, configs[0], EGL14.EGL_NO_CONTEXT, ctxAttribs, 0,
        )
        eglSurface = EGL14.eglCreateWindowSurface(
            eglDisplay, configs[0], outputSurface, intArrayOf(EGL14.EGL_NONE), 0,
        )
        EGL14.eglMakeCurrent(eglDisplay, eglSurface, eglSurface, eglContext)

        // Discover the real Surface dimensions so glViewport draws to
        // the entire window — not whatever 720×1280 hint the caller
        // guessed.
        val q = IntArray(1)
        EGL14.eglQuerySurface(eglDisplay, eglSurface, EGL14.EGL_WIDTH, q, 0)
        actualW = q[0]
        EGL14.eglQuerySurface(eglDisplay, eglSurface, EGL14.EGL_HEIGHT, q, 0)
        actualH = q[0]
        if (actualW <= 0 || actualH <= 0) { actualW = outputW; actualH = outputH }

        program = compileProgram(VS, FS)
        aPosLoc = GLES20.glGetAttribLocation(program, "aPos")
        aTexLoc = GLES20.glGetAttribLocation(program, "aTexCoord")
        uMvpLoc = GLES20.glGetUniformLocation(program, "uMvp")
        uTexMatLoc = GLES20.glGetUniformLocation(program, "uTexMat")
        uSamplerLoc = GLES20.glGetUniformLocation(program, "sTex")

        val tex = IntArray(1)
        GLES20.glGenTextures(1, tex, 0)
        oesTexture = tex[0]
        GLES20.glBindTexture(GLES11Ext.GL_TEXTURE_EXTERNAL_OES, oesTexture)
        GLES20.glTexParameterf(
            GLES11Ext.GL_TEXTURE_EXTERNAL_OES, GLES20.GL_TEXTURE_MIN_FILTER,
            GLES20.GL_LINEAR.toFloat(),
        )
        GLES20.glTexParameterf(
            GLES11Ext.GL_TEXTURE_EXTERNAL_OES, GLES20.GL_TEXTURE_MAG_FILTER,
            GLES20.GL_LINEAR.toFloat(),
        )
        GLES20.glTexParameterf(
            GLES11Ext.GL_TEXTURE_EXTERNAL_OES, GLES20.GL_TEXTURE_WRAP_S,
            GLES20.GL_CLAMP_TO_EDGE.toFloat(),
        )
        GLES20.glTexParameterf(
            GLES11Ext.GL_TEXTURE_EXTERNAL_OES, GLES20.GL_TEXTURE_WRAP_T,
            GLES20.GL_CLAMP_TO_EDGE.toFloat(),
        )

        val st = SurfaceTexture(oesTexture)
        // Use the real Surface size so MediaPlayer renders into a
        // buffer that matches downstream consumer expectations.
        st.setDefaultBufferSize(actualW, actualH)
        st.setOnFrameAvailableListener { handler.post { drawFrame() } }
        inputTexture = st
        inputSurface = Surface(st)

        XposedBridge.log(
            "[FlipRenderer] EGL ready, real=${actualW}×$actualH " +
                "(hint=${outputW}×$outputH) → $outputSurface"
        )
    }

    private fun teardownGl() {
        runCatching { inputTexture?.release() }
        runCatching { inputSurface?.release() }
        if (eglDisplay != EGL14.EGL_NO_DISPLAY) {
            EGL14.eglMakeCurrent(
                eglDisplay, EGL14.EGL_NO_SURFACE,
                EGL14.EGL_NO_SURFACE, EGL14.EGL_NO_CONTEXT,
            )
            EGL14.eglDestroySurface(eglDisplay, eglSurface)
            EGL14.eglDestroyContext(eglDisplay, eglContext)
            EGL14.eglReleaseThread()
            EGL14.eglTerminate(eglDisplay)
        }
        eglDisplay = EGL14.EGL_NO_DISPLAY
        eglContext = EGL14.EGL_NO_CONTEXT
        eglSurface = EGL14.EGL_NO_SURFACE
    }

    /* ─── frame draw ─────────────────────────────────────────── */

    private fun drawFrame() {
        val st = inputTexture ?: return
        // EGL surface can be destroyed underneath us when TikTok tears
        // down the camera target. Guard the entire draw so a single
        // dead-surface exception doesn't kill the renderer thread (and
        // wedge MessageQueue with a "blocked" trace, which is what we
        // saw in logcat at 13:41:33).
        if (eglSurface == EGL14.EGL_NO_SURFACE) return
        try {
            drawFrameInner(st)
        } catch (t: Throwable) {
            XposedBridge.log("[FlipRenderer] drawFrame failed: $t — tearing down")
            runCatching { teardownGl() }
            instances.entries.removeAll { it.value === this }
        }
    }

    private fun drawFrameInner(st: SurfaceTexture) {
        st.updateTexImage()
        st.getTransformMatrix(texMatrix)
        if (rearLensCorrectTex180) {
            val rot180 = FloatArray(16)
            Matrix.setIdentityM(rot180, 0)
            Matrix.rotateM(rot180, 0, 180f, 0f, 0f, 1f)
            val combined = FloatArray(16)
            // Texture coords: OEM/ST matrix first on samples, then 180° upright
            // fix → combined = rot180 * texMatrix (see drawFrameInner comment
            // above; texMatrix * rot180 left some rear-camera pipelines upside-down).
            Matrix.multiplyMM(combined, 0, rot180, 0, texMatrix, 0)
            System.arraycopy(combined, 0, texMatrix, 0, 16)
        }

        // Build mvp = userTransform. Identity by default; user-set
        // rotation/mirror/zoom modify it in place.
        Matrix.setIdentityM(mvpMatrix, 0)
        if (mirrorH) Matrix.scaleM(mvpMatrix, 0, -1f, 1f, 1f)
        if (mirrorV) Matrix.scaleM(mvpMatrix, 0, 1f, -1f, 1f)
        if (rotationDegrees != 0) {
            Matrix.rotateM(mvpMatrix, 0, rotationDegrees.toFloat(), 0f, 0f, 1f)
        }
        if (zoom != 1.0f) {
            Matrix.scaleM(mvpMatrix, 0, zoom, zoom, 1f)
        }

        GLES20.glViewport(0, 0, actualW, actualH)
        GLES20.glClearColor(0f, 0f, 0f, 1f)
        GLES20.glClear(GLES20.GL_COLOR_BUFFER_BIT)

        GLES20.glUseProgram(program)
        GLES20.glUniformMatrix4fv(uMvpLoc, 1, false, mvpMatrix, 0)
        GLES20.glUniformMatrix4fv(uTexMatLoc, 1, false, texMatrix, 0)

        GLES20.glActiveTexture(GLES20.GL_TEXTURE0)
        GLES20.glBindTexture(GLES11Ext.GL_TEXTURE_EXTERNAL_OES, oesTexture)
        GLES20.glUniform1i(uSamplerLoc, 0)

        GLES20.glEnableVertexAttribArray(aPosLoc)
        GLES20.glVertexAttribPointer(
            aPosLoc, 2, GLES20.GL_FLOAT, false, 0, vertexBuf,
        )
        GLES20.glEnableVertexAttribArray(aTexLoc)
        GLES20.glVertexAttribPointer(
            aTexLoc, 2, GLES20.GL_FLOAT, false, 0, texBuf,
        )

        GLES20.glDrawArrays(GLES20.GL_TRIANGLE_STRIP, 0, 4)

        GLES20.glDisableVertexAttribArray(aPosLoc)
        GLES20.glDisableVertexAttribArray(aTexLoc)

        // Tell the encoder the PTS of this frame. We deliberately
        // *don't* use st.timestamp because MediaPlayer resets it to
        // zero on every loop iteration, and a non-monotonic PTS
        // makes the encoder stall. Instead, synthesise a wall-clock
        // PTS so each presentation is strictly newer than the last.
        if (ptsBaseNanos == 0L) ptsBaseNanos = System.nanoTime()
        val pts = System.nanoTime() - ptsBaseNanos
        runCatching {
            EGLExt.eglPresentationTimeANDROID(eglDisplay, eglSurface, pts)
        }
        EGL14.eglSwapBuffers(eglDisplay, eglSurface)
    }

    private fun compileProgram(vs: String, fs: String): Int {
        val v = compileShader(GLES20.GL_VERTEX_SHADER, vs)
        val f = compileShader(GLES20.GL_FRAGMENT_SHADER, fs)
        val p = GLES20.glCreateProgram()
        GLES20.glAttachShader(p, v)
        GLES20.glAttachShader(p, f)
        GLES20.glLinkProgram(p)
        val status = IntArray(1)
        GLES20.glGetProgramiv(p, GLES20.GL_LINK_STATUS, status, 0)
        if (status[0] == 0) {
            XposedBridge.log("[FlipRenderer] link failed: " + GLES20.glGetProgramInfoLog(p))
        }
        GLES20.glDeleteShader(v)
        GLES20.glDeleteShader(f)
        return p
    }

    private fun compileShader(type: Int, src: String): Int {
        val s = GLES20.glCreateShader(type)
        GLES20.glShaderSource(s, src)
        GLES20.glCompileShader(s)
        val status = IntArray(1)
        GLES20.glGetShaderiv(s, GLES20.GL_COMPILE_STATUS, status, 0)
        if (status[0] == 0) {
            XposedBridge.log("[FlipRenderer] shader compile failed: " + GLES20.glGetShaderInfoLog(s))
        }
        return s
    }

    companion object {
        /** Map of encoder Surface → its renderer, so the Xposed hook
         *  can reuse a single FlipRenderer per Surface. */
        @JvmField
        val instances: MutableMap<Surface, FlipRenderer> = ConcurrentHashMap()

        /**
         * Tear down every renderer **except** the one keyed by [keep].
         * Called whenever the camera hook wraps a new Surface so we
         * don't accumulate one HandlerThread + EGL context per
         * abandoned capture session. (We saw this cause >5 active GL
         * pipelines after a few minutes of TikTok navigation, which
         * tanks frame rate.)
         */
        @JvmStatic
        fun stopOthers(keep: Surface) {
            val toStop = instances.entries
                .filter { it.key != keep }
                .toList()
            if (toStop.isEmpty()) return
            XposedBridge.log(
                "[FlipRenderer] stopOthers: releasing ${toStop.size} stale renderer(s)"
            )
            for ((s, r) in toStop) {
                // Drop the MediaPlayer feeding this renderer's input
                // first — otherwise it'll keep pushing frames into a
                // SurfaceTexture whose GL texture we're about to delete
                // and SurfaceFlinger throws.
                r.inputSurface?.let { VideoFeeder.stopFor(it) }
                runCatching { r.stop() }
                instances.remove(s)
            }
        }

        private val vertexBuf: FloatBuffer by lazy {
            // Full-screen quad, NDC coords.
            val arr = floatArrayOf(
                -1f, -1f,
                1f, -1f,
                -1f, 1f,
                1f, 1f,
            )
            ByteBuffer.allocateDirect(arr.size * 4).order(ByteOrder.nativeOrder())
                .asFloatBuffer().apply { put(arr); position(0) }
        }
        private val texBuf: FloatBuffer by lazy {
            val arr = floatArrayOf(
                0f, 0f,
                1f, 0f,
                0f, 1f,
                1f, 1f,
            )
            ByteBuffer.allocateDirect(arr.size * 4).order(ByteOrder.nativeOrder())
                .asFloatBuffer().apply { put(arr); position(0) }
        }

        private const val VS = """
            attribute vec4 aPos;
            attribute vec4 aTexCoord;
            uniform mat4 uMvp;
            uniform mat4 uTexMat;
            varying vec2 vTexCoord;
            void main() {
                gl_Position = uMvp * aPos;
                vTexCoord = (uTexMat * aTexCoord).xy;
            }
        """

        private const val FS = """
            #extension GL_OES_EGL_image_external : require
            precision mediump float;
            uniform samplerExternalOES sTex;
            varying vec2 vTexCoord;
            void main() {
                gl_FragColor = texture2D(sTex, vTexCoord);
            }
        """
    }
}

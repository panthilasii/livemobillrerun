package com.livemobillrerun.vcam

import android.Manifest
import android.content.Intent
import android.content.pm.ActivityInfo
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.graphics.Matrix
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.View
import android.view.WindowManager
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat
import com.livemobillrerun.vcam.databinding.ActivityMainBinding
import com.livemobillrerun.vcam.io.YuvFileReader
import com.livemobillrerun.vcam.preview.PreviewBus
import com.livemobillrerun.vcam.preview.YuvToBitmap
import com.livemobillrerun.vcam.util.AppLogger

class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private val mainHandler = Handler(Looper.getMainLooper())
    private val logSink: (String) -> Unit = { line ->
        mainHandler.post { appendLog(line) }
    }

    /** Background thread for the I420 → JPEG → Bitmap conversion. */
    private val previewWorker = Handler(
        android.os.HandlerThread("vcam-preview").apply { start() }.looper
    )

    /** Sliding-window FPS state for the preview overlay. */
    private var fpsWindowStartMs = 0L
    private var fpsWindowStartFrames = 0L
    private var displayedFps = 0.0

    /** When true, MainActivity renders preview from the YUV file on
     *  disk instead of from the in-memory PreviewBus. Useful as a
     *  smoke test of the file format the Magisk HAL hook will read. */
    @Volatile
    private var loopbackEnabled: Boolean = false
    private val loopbackReader by lazy { YuvFileReader(appContext = applicationContext) }
    private var lastLoopbackIndex: Int = -1
    private var loopbackFramesRead: Long = 0L

    /** Display-only: un-rotate the preview ImageView so the captured
     *  portrait video shows portrait on screen. Has no effect on what
     *  gets written to disk (which always matches the device profile's
     *  pre-rotation, as the Magisk HAL hook expects).
     *
     *  Replaced in UI by the rotation Spinner; this field still drives
     *  the legacy 90°+vflip path so existing intent extras keep
     *  working. */
    @Volatile
    private var portraitDisplay: Boolean = false

    /** Free-form preview rotation, in degrees clockwise. One of
     *  0 / 90 / 180 / 270. Combined with [mirrorH] and [mirrorV]
     *  this can express every right-angle orientation. */
    @Volatile
    private var rotationDegrees: Int = 0
    @Volatile
    private var mirrorH: Boolean = false
    @Volatile
    private var mirrorV: Boolean = false

    /** When ON, the activity hides every chrome element except the
     *  preview ImageView and goes immersive-fullscreen. The user can
     *  then switch to TikTok and start "Live → Screen Share"; the
     *  MediaProjection screen capture only sees the streamed video.
     *  Tap anywhere on the live overlay to exit. */
    @Volatile
    private var liveMode: Boolean = false

    private val previewTick = object : Runnable {
        override fun run() {
            renderPreviewIfNeeded()
            mainHandler.postDelayed(this, PREVIEW_PERIOD_MS)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        // Enforce 9:16 aspect on the preview ImageView at runtime so the
        // visual matches what TikTok will actually see. Width is capped
        // by the container; height is computed as width × 16 / 9 and
        // limited to 45 % of screen height so the controls stay visible.
        applyPortraitPreviewSize()
        binding.svMain.viewTreeObserver.addOnGlobalLayoutListener {
            applyPortraitPreviewSize()
        }

        binding.btnStart.setOnClickListener { onStartClick() }
        binding.btnStop.setOnClickListener { onStopClick() }
        binding.cbLoopback.setOnCheckedChangeListener { _, checked ->
            loopbackEnabled = checked
            lastLoopbackIndex = -1
            loopbackFramesRead = 0L
            binding.tvPreviewSource.setText(
                if (checked) R.string.preview_source_disk
                else R.string.preview_source_memory
            )
            AppLogger.i(TAG, "loopback ${if (checked) "ON" else "OFF"}")
        }
        binding.cbPortrait.setOnCheckedChangeListener { _, checked ->
            portraitDisplay = checked
            AppLogger.i(TAG, "portrait display ${if (checked) "ON" else "OFF"}")
        }

        // Rotation spinner — populates with the localized rotation strings
        // and persists the selection across app restarts.
        val prefs = getSharedPreferences("vcam_ui", MODE_PRIVATE)
        val rotItems = listOf(
            getString(R.string.rotation_0) to 0,
            getString(R.string.rotation_90) to 90,
            getString(R.string.rotation_180) to 180,
            getString(R.string.rotation_270) to 270,
        )
        binding.spRotation.adapter = android.widget.ArrayAdapter(
            this,
            android.R.layout.simple_spinner_dropdown_item,
            rotItems.map { it.first },
        )
        val savedRot = prefs.getInt("rotation_deg", 0)
        binding.spRotation.setSelection(
            rotItems.indexOfFirst { it.second == savedRot }.coerceAtLeast(0)
        )
        rotationDegrees = savedRot
        binding.spRotation.onItemSelectedListener =
            object : android.widget.AdapterView.OnItemSelectedListener {
                override fun onItemSelected(
                    parent: android.widget.AdapterView<*>?,
                    view: View?, position: Int, id: Long,
                ) {
                    rotationDegrees = rotItems[position].second
                    prefs.edit().putInt("rotation_deg", rotationDegrees).apply()
                    AppLogger.i(TAG, "rotation = $rotationDegrees°")
                    pushTransformToHook()
                }
                override fun onNothingSelected(p: android.widget.AdapterView<*>?) {}
            }

        // Mirror checkboxes.
        mirrorH = prefs.getBoolean("mirror_h", false)
        mirrorV = prefs.getBoolean("mirror_v", false)
        binding.cbMirrorH.isChecked = mirrorH
        binding.cbMirrorV.isChecked = mirrorV
        binding.cbMirrorH.setOnCheckedChangeListener { _, c ->
            mirrorH = c
            prefs.edit().putBoolean("mirror_h", c).apply()
            AppLogger.i(TAG, "mirror H = $c")
            pushTransformToHook()
        }
        binding.cbMirrorV.setOnCheckedChangeListener { _, c ->
            mirrorV = c
            prefs.edit().putBoolean("mirror_v", c).apply()
            AppLogger.i(TAG, "mirror V = $c")
            pushTransformToHook()
        }

        binding.btnGoLive.setOnClickListener { enterLiveMode() }
        binding.liveOverlay.setOnClickListener { exitLiveMode() }

        AppLogger.addListener(logSink)

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            if (
                ActivityCompat.checkSelfPermission(
                    this,
                    Manifest.permission.POST_NOTIFICATIONS
                ) != PackageManager.PERMISSION_GRANTED
            ) {
                ActivityCompat.requestPermissions(
                    this,
                    arrayOf(Manifest.permission.POST_NOTIFICATIONS),
                    REQ_NOTIF_PERM,
                )
            }
        }

        handleIntentExtras(intent)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleIntentExtras(intent)
    }

    /**
     * If the launching Intent carries:
     *   - EXTRA_AUTO_START=true  → click Start as if user pressed it
     *   - EXTRA_LIVE_MODE=true   → enter immersive Live Mode for
     *                              TikTok Screen Share capture
     * The PC streamer triggers both via:
     *   adb shell am start -n com.livemobillrerun.vcam/.MainActivity \
     *     --ez vcam_auto_start true --ez vcam_live true
     */
    private fun handleIntentExtras(intent: Intent?) {
        if (intent == null) return
        if (intent.getBooleanExtra(EXTRA_AUTO_START, false) && binding.btnStart.isEnabled) {
            mainHandler.postDelayed({ onStartClick() }, 200L)
        }
        if (intent.getBooleanExtra(EXTRA_LIVE_MODE, false)) {
            mainHandler.postDelayed({ enterLiveMode() }, 800L)
        }
    }

    override fun onStart() {
        super.onStart()
        mainHandler.postDelayed(previewTick, PREVIEW_PERIOD_MS)
    }

    override fun onStop() {
        mainHandler.removeCallbacks(previewTick)
        super.onStop()
    }

    override fun onDestroy() {
        AppLogger.removeListener(logSink)
        previewWorker.looper.quitSafely()
        super.onDestroy()
    }

    // ── click handlers ──────────────────────────────────────────

    private fun onStartClick() {
        val host = binding.etHost.text.toString().ifBlank { VcamService.DEFAULT_HOST }
        val port = binding.etPort.text.toString().toIntOrNull() ?: VcamService.DEFAULT_PORT
        val intent = Intent(this, VcamService::class.java)
            .setAction(VcamService.ACTION_START)
            .putExtra(VcamService.EXTRA_HOST, host)
            .putExtra(VcamService.EXTRA_PORT, port)
        startForegroundService(intent)
        binding.btnStart.isEnabled = false
        binding.btnStop.isEnabled = true
        binding.tvStatus.setText(R.string.status_running)
        // Reset FPS window so a new session starts fresh.
        fpsWindowStartMs = 0L
        fpsWindowStartFrames = 0L
        displayedFps = 0.0
    }

    private fun onStopClick() {
        val intent = Intent(this, VcamService::class.java)
            .setAction(VcamService.ACTION_STOP)
        startService(intent)
        binding.btnStart.isEnabled = true
        binding.btnStop.isEnabled = false
        binding.tvStatus.setText(R.string.status_idle)
    }

    // ── preview ─────────────────────────────────────────────────

    private fun renderPreviewIfNeeded() {
        if (loopbackEnabled) {
            renderFromDisk()
        } else {
            renderFromMemory()
        }
    }

    /** Default path: use the in-memory bus filled by the decoder. */
    private fun renderFromMemory() {
        val frame = PreviewBus.peek() ?: return
        val now = System.currentTimeMillis()
        val totalSeen = PreviewBus.framesPublished()

        if (fpsWindowStartMs == 0L) {
            fpsWindowStartMs = now
            fpsWindowStartFrames = totalSeen
        }
        val win = now - fpsWindowStartMs
        if (win >= FPS_WINDOW_MS) {
            val delta = (totalSeen - fpsWindowStartFrames).coerceAtLeast(0L)
            displayedFps = delta * 1000.0 / win
            fpsWindowStartMs = now
            fpsWindowStartFrames = totalSeen
        }
        previewWorker.post {
            val bmp: Bitmap? = try {
                YuvToBitmap.convert(frame.i420, frame.width, frame.height)
                    ?.let { applyPortraitRotation(it) }
            } catch (e: Throwable) {
                AppLogger.w(TAG, "preview convert failed: ${e.message}")
                null
            }
            mainHandler.post {
                if (bmp != null) {
                    binding.ivPreview.setImageBitmap(bmp)
                    if (liveMode) binding.ivLive.setImageBitmap(bmp)
                }
                binding.tvPreviewOverlay.text = "%dx%d · %.1f fps · %d total".format(
                    frame.width, frame.height, displayedFps, totalSeen,
                )
            }
        }
    }

    /**
     * Optionally rotate the bitmap 90° clockwise so the preview reads
     * as portrait on screen. This is purely cosmetic — the on-disk
     * YUV file remains in the device-profile orientation expected by
     * the Magisk HAL hook.
     */
    /**
     * Apply the user-selected rotation + mirror to the preview frame.
     *
     * The PC streamer may pre-rotate the H.264 stream (depending on
     * the device-profile's `rotation_filter`), and the user's source
     * video itself may carry an orientation flag. Rather than try to
     * out-think every combination, we just give the user a 4-way
     * rotation spinner + horizontal/vertical mirror checkboxes and
     * let them dial it in once. The selection is persisted across
     * launches via SharedPreferences.
     *
     * The legacy [portraitDisplay] toggle still works (for any intent
     * extras that flip it) and applies `transpose=2,vflip`-inverse —
     * the same transform that worked for the original Redmi 14C
     * profile.
     */
    private fun applyPortraitRotation(bmp: Bitmap): Bitmap {
        // Fast path: nothing to do.
        if (rotationDegrees == 0 && !mirrorH && !mirrorV && !portraitDisplay) {
            return bmp
        }
        val m = Matrix()
        if (rotationDegrees != 0) m.postRotate(rotationDegrees.toFloat())
        if (mirrorH) m.postScale(-1f, 1f)
        if (mirrorV) m.postScale(1f, -1f)
        if (portraitDisplay && rotationDegrees == 0 && !mirrorH && !mirrorV) {
            // Legacy intent-driven path: transpose=2,vflip-inverse.
            m.postRotate(-90f)
            m.postScale(1f, -1f)
        }
        return try {
            Bitmap.createBitmap(bmp, 0, 0, bmp.width, bmp.height, m, true)
        } catch (e: Throwable) {
            AppLogger.w(TAG, "rotate failed: ${e.message}")
            bmp
        }
    }

    /**
     * Broadcast the current rotation/mirror to the in-process
     * [com.livemobillrerun.vcam.hook.CameraHook.InProcessModeReceiver]
     * inside TikTok so the live FlipRenderer reflects the new
     * transform without needing a re-encode or restart.
     */
    private fun pushTransformToHook() {
        runCatching {
            val intent = Intent("com.livemobillrerun.vcam.SET_MODE").apply {
                setPackage("com.ss.android.ugc.trill")
                putExtra("rotation", rotationDegrees)
                putExtra("flipX", mirrorH)
                putExtra("flipY", mirrorV)
                addFlags(Intent.FLAG_INCLUDE_STOPPED_PACKAGES)
            }
            sendBroadcast(intent)
            AppLogger.i(TAG, "→ TikTok SET_MODE rot=$rotationDegrees mirrorH=$mirrorH mirrorV=$mirrorV")
        }.onFailure { AppLogger.w(TAG, "broadcast failed: ${it.message}") }
    }

    /**
     * Sets [binding.ivPreview] to a 9:16 portrait box. Width is the
     * container's available width (capped at 60 % of screen width so
     * it doesn't dominate small phones). Height is width × 16 / 9,
     * further capped at 45 % of screen height.
     */
    private fun applyPortraitPreviewSize() {
        val dm = resources.displayMetrics
        val maxW = (dm.widthPixels * 0.60).toInt()
        val maxH = (dm.heightPixels * 0.45).toInt()

        var w = maxW
        var h = w * 16 / 9
        if (h > maxH) {
            h = maxH
            w = h * 9 / 16
        }
        val lp = binding.ivPreview.layoutParams
        if (lp.width != w || lp.height != h) {
            lp.width = w
            lp.height = h
            binding.ivPreview.layoutParams = lp
        }
    }

    /**
     * Loopback path: read back the YUV file the writer just produced.
     * If the rendered image looks identical to the in-memory preview,
     * the on-disk format is byte-for-byte what the Magisk HAL hook
     * will eventually consume.
     */
    private fun renderFromDisk() {
        previewWorker.post {
            val frame = loopbackReader.read()
            if (frame == null) {
                mainHandler.post {
                    binding.tvPreviewOverlay.text = "loopback: no readable yuv on disk"
                }
                return@post
            }
            val isNew = frame.frameIndex != lastLoopbackIndex
            if (isNew) loopbackFramesRead++
            lastLoopbackIndex = frame.frameIndex

            val bmp: Bitmap? = try {
                YuvToBitmap.convert(frame.i420, frame.width, frame.height)
                    ?.let { applyPortraitRotation(it) }
            } catch (e: Throwable) {
                AppLogger.w(TAG, "loopback convert failed: ${e.message}")
                null
            }
            mainHandler.post {
                if (bmp != null) {
                    binding.ivPreview.setImageBitmap(bmp)
                    if (liveMode) binding.ivLive.setImageBitmap(bmp)
                }
                binding.tvPreviewOverlay.text =
                    "loopback %dx%d · idx %d · %d read".format(
                        frame.width, frame.height,
                        frame.frameIndex, loopbackFramesRead,
                    )
            }
        }
    }

    // ── live mode (fullscreen for TikTok Screen Share) ──────────

    private fun enterLiveMode() {
        if (liveMode) return
        liveMode = true
        AppLogger.i(TAG, "entering Live Mode (fullscreen for screen-share capture)")

        // 1. Lock orientation portrait so a screen rotation can't
        //    flip the captured frame mid-stream.
        requestedOrientation = ActivityInfo.SCREEN_ORIENTATION_PORTRAIT

        // 2. Keep the screen on — TikTok's Live capture stops if the
        //    display sleeps.
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)

        // 3. Hide system bars (status + navigation) so the captured
        //    frame is pure video, no chrome.
        WindowCompat.setDecorFitsSystemWindows(window, false)
        WindowInsetsControllerCompat(window, window.decorView).apply {
            hide(WindowInsetsCompat.Type.systemBars())
            systemBarsBehavior =
                WindowInsetsControllerCompat.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
        }

        // 4. Bring the live overlay to the front.
        binding.svMain.visibility = View.GONE
        binding.liveOverlay.visibility = View.VISIBLE
        binding.liveOverlay.bringToFront()

        // 5. Push the latest preview frame into iv_live immediately so
        //    the user doesn't see a black frame for the first ~200 ms.
        (binding.ivPreview.drawable as? android.graphics.drawable.BitmapDrawable)?.bitmap?.let {
            binding.ivLive.setImageBitmap(it)
        }
    }

    private fun exitLiveMode() {
        if (!liveMode) return
        liveMode = false
        AppLogger.i(TAG, "leaving Live Mode")

        requestedOrientation = ActivityInfo.SCREEN_ORIENTATION_UNSPECIFIED
        window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)

        WindowCompat.setDecorFitsSystemWindows(window, true)
        WindowInsetsControllerCompat(window, window.decorView).apply {
            show(WindowInsetsCompat.Type.systemBars())
        }

        binding.liveOverlay.visibility = View.GONE
        binding.svMain.visibility = View.VISIBLE
    }

    @Deprecated("Deprecated in Java")
    override fun onBackPressed() {
        if (liveMode) {
            exitLiveMode()
            return
        }
        @Suppress("DEPRECATION")
        super.onBackPressed()
    }

    // ── log ─────────────────────────────────────────────────────

    private fun appendLog(line: String) {
        val current = binding.tvLog.text
        val next = if (current.isNullOrEmpty()) line else "$current\n$line"
        val trimmed = next.lineSequence().toList()
            .let { lines ->
                if (lines.size > MAX_LOG_LINES) {
                    lines.takeLast(MAX_LOG_LINES).joinToString("\n")
                } else {
                    next.toString()
                }
            }
        binding.tvLog.text = trimmed
    }

    private companion object {
        const val TAG = "MainActivity"
        const val REQ_NOTIF_PERM = 1001
        const val MAX_LOG_LINES = 200

        /** Refresh the preview ImageView at ~5 Hz to stay light on CPU. */
        const val PREVIEW_PERIOD_MS = 200L

        /** Recompute FPS every second over the past second's frames. */
        const val FPS_WINDOW_MS = 1000L

        // Intent extras used by the PC GUI to drive a one-click launch.
        const val EXTRA_AUTO_START = "vcam_auto_start"
        const val EXTRA_LIVE_MODE = "vcam_live"
    }
}

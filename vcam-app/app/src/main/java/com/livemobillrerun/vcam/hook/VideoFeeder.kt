package com.livemobillrerun.vcam.hook

import android.media.MediaPlayer
import android.os.Handler
import android.os.Looper
import android.view.Surface
import de.robv.android.xposed.XposedBridge
import java.io.File
import java.util.LinkedHashMap

/**
 * Pumps a video file (MP4/H.264) onto an arbitrary [Surface] using
 * [MediaPlayer]. Surface ↔ player mapping is 1-to-1 so multiple
 * encoder Surfaces can each receive a video stream concurrently.
 *
 * **Status: stub.** This file currently provides:
 *  - the public surface area [CameraHook] expects,
 *  - basic Surface→MediaPlayer attach, and
 *  - looping playback.
 *
 * Pending real-port work (mirrors UltimateRerun's ~426 lines):
 *  - `applyTransformToActivePlayers()` for runtime rotate/zoom/flip
 *  - `reloadVideo(newPath)` for hot-swapping the active video without
 *    tearing down the encoder Surface
 *  - precise position tracking for OBS-style "loop point" UI
 *  - GLES-backed [FlipRenderer] integration for mirror correction
 */
object VideoFeeder {
    private const val TAG = "VCAM_FEEDER"

    /**
     * Active video file path. Either set explicitly via
     * [VCamModeReceiver] / [CameraHook.activeVideoPath] or auto-resolved
     * from a list of well-known fallback locations.
     */
    @Volatile var activeVideoPath: String? = null

    @JvmField var loopEnabled: Boolean = true
    @JvmField var rotationDegrees: Float = 0f
    @JvmField var zoomLevel: Float = 1f
    @JvmField var flipX: Boolean = false
    @JvmField var flipY: Boolean = false

    private val players: MutableMap<Surface, MediaPlayer> = LinkedHashMap()
    private val playingPaths: MutableMap<Surface, String> = LinkedHashMap()
    /** Last-modified time of the file behind each Surface. We poll
     *  this every [WATCH_PERIOD_MS] and rebind the player when it
     *  changes, so re-encode + push from the PC is picked up without
     *  restarting TikTok. Mirrors UltimateRerun's "hot reload"
     *  behaviour. NB: `adb push` over MIUI's scoped storage layer is
     *  notorious for *preserving* mtime when the destination file
     *  already exists — that's why we also track [playingSizes]. */
    private val playingMtimes: MutableMap<Surface, Long> = LinkedHashMap()
    /** File size at the last successful bind. Combined with mtime, a
     *  change in *either* triggers a hot-reload — so even if the
     *  storage layer hides the timestamp update, a different-length
     *  encode is picked up reliably. */
    private val playingSizes: MutableMap<Surface, Long> = LinkedHashMap()
    /** Per-Surface error counters used by [scheduleRetry] for
     *  exponential backoff (1s, 2s, 4s, 8s, capped at 30s). */
    private val errorCounts: MutableMap<Surface, Int> = LinkedHashMap()
    private val ui = Handler(Looper.getMainLooper())

    private const val WATCH_PERIOD_MS = 2_000L
    private const val RETRY_MAX_MS = 30_000L

    /**
     * Auto-resolve video path from a list of well-known fallback paths
     * if none has been set explicitly.
     *
     * Order matters here. We hit the TikTok-package-scoped paths
     * **first** because those are the only places we can `adb push`
     * to AND the host TikTok process is guaranteed to have read
     * permission for (it owns the directory). Paths under raw
     * `/sdcard` may pass `exists()` but fail to open with EACCES on
     * Android 11+ scoped storage — so we check `canRead()` too.
     */
    fun activeVideoPath(): String? {
        activeVideoPath?.let { return it }
        val candidates = listOf(
            // TikTok International — scoped sandbox dir. Safest target.
            "/sdcard/Android/data/com.ss.android.ugc.trill/files/vcam_final.mp4",
            "/sdcard/Android/data/com.ss.android.ugc.trill/files/vcam.mp4",
            "/storage/emulated/0/Android/data/com.ss.android.ugc.trill/files/vcam_final.mp4",
            "/storage/emulated/0/Android/data/com.ss.android.ugc.trill/files/vcam.mp4",
            // TikTok Musically (alt international package).
            "/sdcard/Android/data/com.zhiliaoapp.musically/files/vcam_final.mp4",
            "/sdcard/Android/data/com.zhiliaoapp.musically/files/vcam.mp4",
            // Douyin (CN). Unlikely but cheap.
            "/sdcard/Android/data/com.ss.android.ugc.aweme/files/vcam_final.mp4",
            // Playlist text files — let the user point at any path on disk.
            "/data/local/tmp/vcam_hook_playlist.txt",
            "/sdcard/vcam_hook_playlist.txt",
            "/sdcard/Android/data/com.livemobillrerun.vcam/files/vcam_hook_playlist.txt",
            "/storage/emulated/0/Android/data/com.livemobillrerun.vcam/files/vcam_hook_playlist.txt",
            "/data/data/com.livemobillrerun.vcam/files/vcam_hook_playlist.txt",
            // Last-resort raw /sdcard locations. These usually fail for
            // 3rd-party app reads because of scoped storage; we keep
            // them only for rooted phones that don't enforce it.
            "/data/local/tmp/vcam_final.mp4",
            "/storage/emulated/0/vcam_final.mp4",
            "/sdcard/vcam_final.mp4",
            "/storage/emulated/0/vcam_active.mp4",
            "/sdcard/vcam_active.mp4",
            "/storage/emulated/0/Movies/vcam_active.mp4",
        )
        for (path in candidates) {
            val f = File(path)
            if (path.endsWith(".txt")) {
                if (!f.canRead()) continue
                runCatching {
                    f.readLines()
                        .map { it.trim() }
                        .firstOrNull {
                            it.isNotBlank() && (
                                it.startsWith("http") ||
                                    File(it).canRead()
                            )
                        }
                }.getOrNull()?.let { return it }
            } else if (f.canRead()) {
                return path
            }
        }
        return null
    }

    fun isActive(): Boolean = players.isNotEmpty()

    /**
     * Bind a [MediaPlayer] for [videoPath] to [surface]. Idempotent —
     * if the surface already has a player on the same path AND the
     * file hasn't changed on disk, no-op. Otherwise rebuild the player.
     *
     * Also kicks off the [watchdog] poll the first time it's called so
     * subsequent file replacements trigger an automatic reload.
     */
    fun feedToSurface(surface: Surface, videoPath: String) {
        ui.post {
            val f = File(videoPath)
            val curMtime = runCatching { f.lastModified() }.getOrDefault(0L)
            val curSize = runCatching { f.length() }.getOrDefault(0L)
            // Idempotent: same surface + same path + same mtime + same
            // size → no-op. Comparing both size *and* mtime is the
            // belt-and-braces fix for MIUI scoped storage masking
            // timestamp updates.
            if (
                playingPaths[surface] == videoPath
                && players.containsKey(surface)
                && playingMtimes[surface] == curMtime
                && playingSizes[surface] == curSize
            ) {
                return@post
            }
            stopForLocked(surface)
            try {
                val mp = MediaPlayer().apply {
                    setSurface(surface)
                    setDataSource(videoPath)
                    isLooping = loopEnabled
                    setOnPreparedListener { it.start() }
                    setOnErrorListener { _, what, extra ->
                        XposedBridge.log("[$TAG] MediaPlayer error what=$what extra=$extra — scheduling retry")
                        // Drop this player and re-bind after a backoff
                        // delay. Using delayed retry instead of waiting
                        // for the next watchdog tick lets us recover
                        // faster on transient errors (network blip,
                        // codec hiccup) and back off on real failures.
                        scheduleRetry(surface, videoPath)
                        true  // we consumed the error; don't propagate
                    }
                    setOnCompletionListener {
                        if (loopEnabled) {
                            it.seekTo(0)
                            it.start()
                        }
                    }
                    prepareAsync()
                }
                players[surface] = mp
                playingPaths[surface] = videoPath
                playingMtimes[surface] = curMtime
                playingSizes[surface] = curSize
                // Reset retry counter — we got a fresh player up.
                errorCounts.remove(surface)
                XposedBridge.log(
                    "[$TAG] feeding ${f.name} (mtime=$curMtime size=$curSize) → $surface"
                )
                ensureWatchdog()
            } catch (t: Throwable) {
                XposedBridge.log("[$TAG] feedToSurface failed: $t")
            }
        }
    }

    /** Set when the watchdog has been scheduled. Idempotent. */
    @Volatile private var watchdogStarted: Boolean = false

    /**
     * Periodically checks each active Surface's file mtime. If the
     * MP4 was replaced on disk (e.g. PC pushed a fresh encode), or
     * the player flagged an error, rebuild the MediaPlayer in place.
     */
    private val watchdog = object : Runnable {
        override fun run() {
            try {
                // ── 1. lazy GC: drop chains whose Surface the OS has
                //      already released. Replaces the eager
                //      FlipRenderer.stopOthers(keep) approach which
                //      mistakenly killed the preview pipeline as soon
                //      as TikTok created its Live encoder.
                for ((surface, _) in playingPaths.toMap()) {
                    if (!surface.isValid) {
                        XposedBridge.log("[$TAG] gc: stopping invalidated surface=$surface")
                        stopForLocked(surface)
                    }
                }
                for ((outSurface, fr) in FlipRenderer.instances.toMap()) {
                    if (!outSurface.isValid) {
                        XposedBridge.log(
                            "[FlipRenderer] gc: stopping invalidated outSurface=$outSurface"
                        )
                        // Stop MediaPlayer feeding this renderer first
                        fr.inputSurface?.let { stopForLocked(it) }
                        runCatching { fr.stop() }
                        FlipRenderer.instances.remove(outSurface)
                    }
                }

                // ── 2. hot-reload: detect file size/mtime change.
                for ((surface, path) in playingPaths.toMap()) {
                    val f = File(path)
                    val curMtime = runCatching { f.lastModified() }.getOrDefault(0L)
                    val curSize = runCatching { f.length() }.getOrDefault(0L)
                    val seenMtime = playingMtimes[surface] ?: 0L
                    val seenSize = playingSizes[surface] ?: 0L
                    val mtimeChanged = curMtime > 0 && curMtime != seenMtime
                    val sizeChanged = curSize > 0 && curSize != seenSize
                    if (mtimeChanged || sizeChanged) {
                        XposedBridge.log(
                            "[$TAG] hot-reload: ${f.name} " +
                                "mtime=$seenMtime→$curMtime size=$seenSize→$curSize"
                        )
                        feedToSurface(surface, path)
                    }
                }
            } catch (t: Throwable) {
                XposedBridge.log("[$TAG] watchdog tick failed: $t")
            }
            ui.postDelayed(this, WATCH_PERIOD_MS)
        }
    }

    private fun ensureWatchdog() {
        if (watchdogStarted) return
        watchdogStarted = true
        ui.postDelayed(watchdog, WATCH_PERIOD_MS)
        XposedBridge.log("[$TAG] hot-reload watchdog started (period=${WATCH_PERIOD_MS}ms)")
    }

    /**
     * Re-attach a player to [surface] after [computeBackoff]ms — used
     * when the previous MediaPlayer hit `OnErrorListener`. Counter is
     * persisted in [errorCounts] so repeated failures back off
     * exponentially up to [RETRY_MAX_MS].
     */
    private fun scheduleRetry(surface: Surface, path: String) {
        val n = (errorCounts[surface] ?: 0) + 1
        errorCounts[surface] = n
        val delayMs = computeBackoff(n)
        XposedBridge.log("[$TAG] retry #$n in ${delayMs}ms for ${File(path).name}")
        // Free the broken player synchronously so the Surface isn't
        // held by a zombie native handle.
        runCatching { players.remove(surface)?.release() }
        playingMtimes[surface] = -1L
        ui.postDelayed({ feedToSurface(surface, path) }, delayMs)
    }

    /** 1s, 2s, 4s, 8s, 16s, 30s (capped). */
    private fun computeBackoff(attempt: Int): Long {
        if (attempt <= 0) return 1_000L
        val base = 1_000L shl (attempt - 1).coerceAtMost(5)
        return base.coerceAtMost(RETRY_MAX_MS)
    }

    /**
     * Force every active Surface to rebind its MediaPlayer to
     * [newPath]. Unlike a watchdog tick, this **bypasses the
     * idempotent mtime/size check** so a same-shape re-encode (e.g.
     * "same playlist, slightly tweaked filter") still rolls over.
     * Triggered by a `forceReload=true` SET_MODE broadcast.
     */
    fun reloadVideo(newPath: String) {
        ui.post {
            for ((surface, _) in players.toMap()) {
                // Clear cached signature so feedToSurface can't take
                // the no-op fast path.
                playingMtimes[surface] = -1L
                playingSizes[surface] = -1L
                feedToSurface(surface, newPath)
            }
            XposedBridge.log("[$TAG] reloadVideo: forced rebind on ${players.size} surface(s)")
        }
    }

    /**
     * Stop and release every active MediaPlayer **except** the ones in
     * [keep]. Called from the camera hook each time TikTok hands us a
     * fresh camera Surface so we don't leak players from previous
     * capture sessions (each session starts a new Surface; without
     * this, the previous MediaPlayer hangs on to GPU memory + audio
     * threads forever).
     */
    fun stopOthers(keep: Surface) {
        ui.post {
            val toStop = players.keys.toList().filter { it != keep }
            if (toStop.isEmpty()) return@post
            XposedBridge.log("[$TAG] stopOthers: releasing ${toStop.size} stale player(s)")
            for (s in toStop) stopForLocked(s)
        }
    }

    fun applyTransformToActivePlayers() {
        // TODO: when porting [FlipRenderer], route MediaPlayer's output
        // through a SurfaceTexture → GLES → output Surface chain so
        // rotation / zoom / flip can be applied without rebuilding
        // the player. For now, transforms are inert.
        XposedBridge.log("[$TAG] applyTransformToActivePlayers: not yet implemented")
    }

    fun stopAll() {
        ui.post {
            for ((surface, _) in players.toMap()) stopForLocked(surface)
        }
    }

    fun stopFor(surface: Surface) {
        ui.post { stopForLocked(surface) }
    }

    private fun stopForLocked(surface: Surface) {
        players.remove(surface)?.let {
            runCatching { it.stop() }
            runCatching { it.release() }
        }
        playingPaths.remove(surface)
        playingMtimes.remove(surface)
        playingSizes.remove(surface)
        errorCounts.remove(surface)
    }
}

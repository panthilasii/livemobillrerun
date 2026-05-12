package com.livemobillrerun.vcam.hook

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import de.robv.android.xposed.XposedBridge

/**
 * Control surface for [CameraHook]. Receives broadcasts of the form:
 *
 * ```
 * adb shell am broadcast \
 *   -a com.livemobillrerun.vcam.SET_MODE \
 *   --ei mode 2 \
 *   --es videoPath /sdcard/vcam_final.mp4 \
 *   --ez loop true \
 *   --ef rotation 0.0 \
 *   --ef zoom 1.0 \
 *   --ez flipX false \
 *   --ez flipY false \
 *   --ez audio true
 * ```
 *
 * Modes:
 *   `0` — passthrough (real camera)
 *   `1` — block (encoder gets nothing)
 *   `2` — replace (with [videoPath] or auto-resolved file)
 */
class VCamModeReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context?, intent: Intent?) {
        val mode = intent?.getIntExtra("mode", 0) ?: 0
        CameraHook.currentMode = mode
        XposedBridge.log("[VCAM_HOOK] 📡 mode → $mode")

        if (mode == 2) {
            val path = intent?.getStringExtra("videoPath") ?: VideoFeeder.activeVideoPath()
            if (path != null) {
                val changed = CameraHook.activeVideoPath != path
                CameraHook.activeVideoPath = path
                VideoFeeder.activeVideoPath = path
                XposedBridge.log("[VCAM_HOOK] 📂 video path → $path (changed=$changed)")
                val forceReload = intent?.getBooleanExtra("forceReload", false) == true
                if ((forceReload || changed) && VideoFeeder.isActive()) {
                    VideoFeeder.reloadVideo(path)
                }
                val wantAudio = intent?.getBooleanExtra("audio", true) ?: true
                val audioReload = intent?.getBooleanExtra("audioReload", false) == true
                when {
                    !wantAudio -> AudioFeeder.stop()
                    audioReload -> AudioFeeder.reload(fallbackVideoPath = path)
                    else -> AudioFeeder.start(path)
                }
            }
        } else {
            AudioFeeder.stop()
        }

        intent?.let {
            VideoFeeder.loopEnabled = it.getBooleanExtra("loop", VideoFeeder.loopEnabled)
            if (it.hasExtra("rotation")) {
                val raw = it.extras?.get("rotation")
                VideoFeeder.rotationDegrees = when (raw) {
                    is Number -> raw.toFloat()
                    else -> it.getFloatExtra("rotation", 0f)
                }
            }
            if (it.hasExtra("zoom")) VideoFeeder.zoomLevel = it.getFloatExtra("zoom", 1f)
            if (it.hasExtra("flipX")) VideoFeeder.flipX = it.getBooleanExtra("flipX", false)
            if (it.hasExtra("flipY")) VideoFeeder.flipY = it.getBooleanExtra("flipY", false)
        }
        if (VideoFeeder.isActive()) VideoFeeder.applyTransformToActivePlayers()
    }
}

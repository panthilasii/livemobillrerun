package com.livemobillrerun.vcam.io

import android.content.Context
import com.livemobillrerun.vcam.util.AppLogger
import java.io.File
import java.io.FileOutputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.concurrent.atomic.AtomicInteger

/**
 * Writes YUV420 (I420) frames to a file with a 16-byte little-endian
 * header consumed by the Magisk HAL hook:
 *
 *   uint32 magic = 'VCAM' (0x564D4143)
 *   uint32 width
 *   uint32 height
 *   uint32 frame_counter
 *
 * Primary target: `/data/local/tmp/vcam.yuv` (the Magisk module reads
 * here once Phase 4b is in place).
 *
 * Fallback: if `/data/local/tmp` is not writable from the app uid (the
 * normal case on unrooted phones), we transparently fall back to the
 * app's own files dir. Phase 4a smoke testing still works end-to-end —
 * you just won't be exposed as the system camera.
 *
 * Atomic swap: write to `vcam.yuv.tmp`, then `rename(2)`.
 */
object YuvFileWriter {
    private const val MAGIC = 0x564D4143
    private const val PRIMARY_PATH = "/data/local/tmp/vcam.yuv"
    private const val HEADER_SIZE = 16
    private const val TAG = "YuvFileWriter"

    private val frameCounter = AtomicInteger(0)

    @Volatile private var resolvedTarget: File? = null
    @Volatile private var resolvedTmp: File? = null
    @Volatile private var fallbackContext: Context? = null
    @Volatile private var primaryProbed: Boolean = false
    @Volatile private var usingFallback: Boolean = false

    /**
     * Optional one-time setup. If the host service calls this with a
     * Context, we'll fall back to `<filesDir>/vcam.yuv` when the
     * primary path is unwritable.
     */
    fun init(ctx: Context?) {
        fallbackContext = ctx?.applicationContext
    }

    fun write(yuv: ByteArray, width: Int, height: Int): Boolean {
        val expected = width * height * 3 / 2
        if (yuv.size != expected) {
            AppLogger.w(TAG, "yuv size mismatch: got=${yuv.size}, expected=$expected")
            return false
        }

        val target = resolveTarget() ?: return false
        val tmp = resolvedTmp!!

        return try {
            val header = ByteBuffer.allocate(HEADER_SIZE).order(ByteOrder.LITTLE_ENDIAN).apply {
                putInt(MAGIC)
                putInt(width)
                putInt(height)
                putInt(frameCounter.incrementAndGet())
            }.array()

            FileOutputStream(tmp).use { out ->
                out.write(header)
                out.write(yuv)
                out.fd.sync()
            }
            if (!tmp.renameTo(target)) {
                tmp.copyTo(target, overwrite = true)
                tmp.delete()
            }
            true
        } catch (e: Exception) {
            AppLogger.e(TAG, "write failed (target=$target)", e)
            false
        }
    }

    fun reset() {
        frameCounter.set(0)
        resolvedTarget?.runCatching { delete() }
    }

    fun framesWritten(): Int = frameCounter.get()

    fun activePath(): String = resolvedTarget?.absolutePath ?: "(unresolved)"

    fun isUsingFallback(): Boolean = usingFallback

    /**
     * Probe the primary path once; on failure, fall back to app-private.
     */
    private fun resolveTarget(): File? {
        resolvedTarget?.let { return it }
        synchronized(this) {
            resolvedTarget?.let { return it }
            if (!primaryProbed) {
                primaryProbed = true
                val primary = File(PRIMARY_PATH)
                if (canWriteAt(primary.parentFile, "vcam.probe")) {
                    resolvedTarget = primary
                    resolvedTmp = File("$PRIMARY_PATH.tmp")
                    usingFallback = false
                    AppLogger.i(TAG, "writing to primary path $PRIMARY_PATH")
                    return resolvedTarget
                }
                AppLogger.w(
                    TAG,
                    "primary path $PRIMARY_PATH not writable; falling back to app-private dir"
                )
            }
            val ctx = fallbackContext
            if (ctx != null) {
                val fallback = File(ctx.filesDir, "vcam.yuv")
                resolvedTarget = fallback
                resolvedTmp = File(ctx.filesDir, "vcam.yuv.tmp")
                usingFallback = true
                AppLogger.i(TAG, "writing to fallback ${fallback.absolutePath}")
                return resolvedTarget
            }
            AppLogger.e(TAG, "no writable target — call YuvFileWriter.init(context) first")
            return null
        }
    }

    private fun canWriteAt(dir: File?, name: String): Boolean {
        if (dir == null) return false
        return try {
            val probe = File(dir, name)
            FileOutputStream(probe).use { it.write(0) }
            probe.delete()
            true
        } catch (_: Exception) {
            false
        }
    }
}

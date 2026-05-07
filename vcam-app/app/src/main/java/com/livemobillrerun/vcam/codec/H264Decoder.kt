package com.livemobillrerun.vcam.codec

import android.media.Image
import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaFormat
import com.livemobillrerun.vcam.util.AppLogger
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Async MediaCodec H.264 decoder. Feed Annex-B bytes via [feed]; receive
 * I420-packed YUV frames via [onYuv].
 *
 * The output `width` × `height` reflects the decoder's actual output, NOT
 * the configured input. Trust the values you receive in the callback.
 */
class H264Decoder(
    private val width: Int,
    private val height: Int,
    private val onYuv: (yuv: ByteArray, width: Int, height: Int) -> Unit,
) {
    private val running = AtomicBoolean(false)
    private val pending = LinkedBlockingQueue<ByteArray>(QUEUE_CAPACITY)
    private var codec: MediaCodec? = null
    @Volatile
    private var droppedSinceLastWarn: Long = 0L
    @Volatile
    private var lastWarnNanos: Long = 0L

    /** Wall-clock time we last saw a decoded output buffer. Used by the
     *  external stall watchdog to decide when to flush the codec. */
    @Volatile
    private var lastOutputAtMs: Long = 0L

    /** True while we're already in the middle of recovering from a
     *  stall, to avoid stacking flushes on top of each other. */
    @Volatile
    private var recovering: Boolean = false

    fun start() {
        if (!running.compareAndSet(false, true)) return
        val mime = MediaFormat.MIMETYPE_VIDEO_AVC
        val format = MediaFormat.createVideoFormat(mime, width, height).apply {
            setInteger(
                MediaFormat.KEY_COLOR_FORMAT,
                MediaCodecInfo.CodecCapabilities.COLOR_FormatYUV420Flexible,
            )
        }
        codec = MediaCodec.createDecoderByType(mime).apply {
            setCallback(callback)
            configure(format, null, null, 0)
            start()
        }
        lastOutputAtMs = System.currentTimeMillis()
        AppLogger.i(TAG, "started ${width}x$height")
    }

    /**
     * Returns the milliseconds since the most recent decoded output
     * frame. The pipeline can use this to decide whether to call
     * [flushAndRestart] when it has reason to believe the decoder has
     * gone silent (e.g. TCP data is flowing but nothing reaches
     * `onYuv` for a while).
     */
    fun millisSinceLastOutput(): Long {
        if (!running.get() || lastOutputAtMs == 0L) return 0L
        return System.currentTimeMillis() - lastOutputAtMs
    }

    /**
     * Recover from a stalled MediaCodec by tearing down and rebuilding
     * the codec. We deliberately don't try `codec.flush()` first — on
     * MTK SoCs we've observed flushes silently fail to recover the
     * output port, so a hard restart is more reliable. Pending input
     * is dropped (the next keyframe in the H.264 stream will resync).
     */
    fun flushAndRestart() {
        if (!running.get()) return
        if (recovering) return
        recovering = true
        try {
            AppLogger.w(TAG, "stall detected — flush + restart codec")
            try { codec?.stop() } catch (_: Exception) {}
            codec?.release()
            codec = null
            pending.clear()

            val mime = MediaFormat.MIMETYPE_VIDEO_AVC
            val format = MediaFormat.createVideoFormat(mime, width, height).apply {
                setInteger(
                    MediaFormat.KEY_COLOR_FORMAT,
                    MediaCodecInfo.CodecCapabilities.COLOR_FormatYUV420Flexible,
                )
            }
            codec = MediaCodec.createDecoderByType(mime).apply {
                setCallback(callback)
                configure(format, null, null, 0)
                start()
            }
            lastOutputAtMs = System.currentTimeMillis()
        } finally {
            recovering = false
        }
    }

    /**
     * Push another chunk of Annex-B bytes. Applies natural back-pressure
     * by blocking briefly when the queue is full — this propagates back
     * to the TCP reader and then to ffmpeg, instead of silently dropping
     * NAL units.
     */
    fun feed(buf: ByteArray, len: Int) {
        if (!running.get()) return
        val copy = buf.copyOf(len)
        // Try non-blocking first; if the queue is full, wait up to 200 ms.
        if (pending.offer(copy)) return
        try {
            if (pending.offer(copy, FEED_BLOCK_MS, TimeUnit.MILLISECONDS)) return
        } catch (_: InterruptedException) {
            Thread.currentThread().interrupt()
            return
        }
        // Still full → drop, but throttle the warning so the log doesn't
        // turn into a fire-hose if the decoder is genuinely stalled.
        droppedSinceLastWarn++
        val now = System.nanoTime()
        if (now - lastWarnNanos > 1_000_000_000L) {
            AppLogger.w(
                TAG,
                "input queue full — dropped $droppedSinceLastWarn chunks in last 1s"
            )
            droppedSinceLastWarn = 0L
            lastWarnNanos = now
        }
    }

    fun stop() {
        if (!running.compareAndSet(true, false)) return
        try {
            codec?.stop()
        } catch (_: Exception) {
        }
        codec?.release()
        codec = null
        pending.clear()
    }

    private val callback = object : MediaCodec.Callback() {
        override fun onInputBufferAvailable(c: MediaCodec, idx: Int) {
            if (!running.get()) return
            val payload = try {
                pending.poll(50, TimeUnit.MILLISECONDS)
            } catch (_: InterruptedException) {
                null
            } ?: run {
                try {
                    c.queueInputBuffer(idx, 0, 0, 0, 0)
                } catch (_: IllegalStateException) {
                }
                return
            }
            val buf = c.getInputBuffer(idx) ?: return
            buf.clear()
            buf.put(payload)
            try {
                c.queueInputBuffer(idx, 0, payload.size, System.nanoTime() / 1000, 0)
            } catch (e: IllegalStateException) {
                AppLogger.w(TAG, "queueInput failed: ${e.message}")
            }
        }

        override fun onOutputBufferAvailable(
            c: MediaCodec,
            idx: Int,
            info: MediaCodec.BufferInfo,
        ) {
            try {
                val image = c.getOutputImage(idx)
                if (image != null) {
                    val yuv = imageToI420(image)
                    onYuv(yuv, image.width, image.height)
                    image.close()
                    lastOutputAtMs = System.currentTimeMillis()
                }
            } catch (e: Exception) {
                AppLogger.e(TAG, "output handling failed", e)
            } finally {
                try {
                    c.releaseOutputBuffer(idx, false)
                } catch (_: IllegalStateException) {
                }
            }
        }

        override fun onError(c: MediaCodec, e: MediaCodec.CodecException) {
            AppLogger.e(TAG, "codec error: ${e.message}", e)
            // Schedule a restart on a worker — we can't restart the
            // codec from inside its own callback thread.
            Thread {
                try { Thread.sleep(50) } catch (_: InterruptedException) {}
                flushAndRestart()
            }.apply { isDaemon = true }.start()
        }

        override fun onOutputFormatChanged(c: MediaCodec, f: MediaFormat) {
            AppLogger.i(TAG, "format changed: $f")
        }
    }

    /**
     * Pack any YUV_420_888 [Image] into a contiguous I420 byte array
     * (Y plane, U plane, V plane — each tightly packed).
     */
    private fun imageToI420(image: Image): ByteArray {
        val w = image.width
        val h = image.height
        val ySize = w * h
        val uvSize = ySize / 4
        val out = ByteArray(ySize + uvSize * 2)

        copyPlane(image.planes[0], w, h, out, 0)
        copyPlane(image.planes[1], w / 2, h / 2, out, ySize)
        copyPlane(image.planes[2], w / 2, h / 2, out, ySize + uvSize)
        return out
    }

    private fun copyPlane(
        plane: Image.Plane,
        widthPx: Int,
        heightPx: Int,
        dst: ByteArray,
        dstOffset: Int,
    ) {
        val src = plane.buffer
        val rowStride = plane.rowStride
        val pixelStride = plane.pixelStride
        var pos = dstOffset
        if (pixelStride == 1 && rowStride == widthPx) {
            // tight, single contiguous copy
            src.position(0)
            src.get(dst, pos, widthPx * heightPx)
            return
        }
        val rowBuf = ByteArray(rowStride)
        for (row in 0 until heightPx) {
            src.position(row * rowStride)
            val toRead = minOf(rowStride, src.remaining())
            src.get(rowBuf, 0, toRead)
            if (pixelStride == 1) {
                System.arraycopy(rowBuf, 0, dst, pos, widthPx)
            } else {
                var s = 0
                for (col in 0 until widthPx) {
                    dst[pos + col] = rowBuf[s]
                    s += pixelStride
                }
            }
            pos += widthPx
        }
    }

    private companion object {
        const val TAG = "H264Decoder"

        /** Holds enough chunks to absorb keyframe bursts at 2–4 Mbps without
         *  losing data, while still capping memory at a few MB. */
        const val QUEUE_CAPACITY = 256

        /** When the queue is full, wait up to this long for a slot before
         *  giving up and dropping the chunk. */
        const val FEED_BLOCK_MS = 200L
    }
}

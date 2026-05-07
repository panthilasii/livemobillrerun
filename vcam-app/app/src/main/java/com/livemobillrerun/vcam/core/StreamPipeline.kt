package com.livemobillrerun.vcam.core

import android.os.Handler
import android.os.HandlerThread
import com.livemobillrerun.vcam.codec.H264Decoder
import com.livemobillrerun.vcam.io.YuvFileWriter
import com.livemobillrerun.vcam.net.TcpClient
import com.livemobillrerun.vcam.preview.PreviewBus
import com.livemobillrerun.vcam.util.AppLogger
import java.util.concurrent.atomic.AtomicLong

/**
 * Wires together: TcpClient → H264Decoder → YuvFileWriter.
 *
 * Width/height are passed in because the streamer side knows them; the
 * decoder will renegotiate format anyway, and `onYuv` reports actual
 * dimensions per frame.
 */
class StreamPipeline(
    private val host: String,
    private val port: Int,
    private val width: Int = DEFAULT_W,
    private val height: Int = DEFAULT_H,
    private val onState: (String) -> Unit = {},
) {
    private var tcp: TcpClient? = null
    private var decoder: H264Decoder? = null
    private var watchdogThread: HandlerThread? = null
    private var watchdogHandler: Handler? = null

    private val bytesIn = AtomicLong(0)
    private val framesOut = AtomicLong(0)
    private val lastBytesAtRunCheck = AtomicLong(0)

    fun start() {
        AppLogger.i(TAG, "starting → $host:$port  ${width}x$height")
        YuvFileWriter.reset()
        PreviewBus.reset()

        decoder = H264Decoder(width, height) { yuv, w, h ->
            if (YuvFileWriter.write(yuv, w, h)) {
                framesOut.incrementAndGet()
            }
            PreviewBus.publish(yuv, w, h)
        }.also { it.start() }

        tcp = TcpClient(
            host = host,
            port = port,
            onBytes = { buf, n ->
                bytesIn.addAndGet(n.toLong())
                decoder?.feed(buf, n)
            },
            onState = { state -> onState("tcp: $state") },
        ).also { it.start() }

        startWatchdog()
    }

    /**
     * Periodically check whether the decoder has gone silent while bytes
     * are still flowing in over TCP. On MTK SoCs we've seen the codec
     * reach a state where input slots stay full but no output is ever
     * produced — flushing and restarting is the only thing that
     * recovers it. We deliberately allow some slack (8 s) before
     * triggering, to not interfere with normal startup or short network
     * stalls.
     */
    private fun startWatchdog() {
        stopWatchdog()
        val hThread = HandlerThread("vcam-watchdog").apply { start() }
        val handler = Handler(hThread.looper)
        watchdogThread = hThread
        watchdogHandler = handler
        lastBytesAtRunCheck.set(bytesIn.get())

        val task = object : Runnable {
            override fun run() {
                val dec = decoder ?: return
                val sinceOutput = dec.millisSinceLastOutput()
                val bytesNow = bytesIn.get()
                val bytesPrev = lastBytesAtRunCheck.getAndSet(bytesNow)
                val bytesGrew = bytesNow - bytesPrev > 16 * 1024  // >16 KiB / interval

                if (sinceOutput >= STALL_THRESHOLD_MS && bytesGrew) {
                    AppLogger.w(
                        TAG,
                        "watchdog: ${sinceOutput}ms with no output but bytes flowing — restart"
                    )
                    dec.flushAndRestart()
                }
                handler.postDelayed(this, WATCHDOG_INTERVAL_MS)
            }
        }
        handler.postDelayed(task, WATCHDOG_INTERVAL_MS)
    }

    private fun stopWatchdog() {
        watchdogHandler?.removeCallbacksAndMessages(null)
        watchdogHandler = null
        watchdogThread?.quitSafely()
        watchdogThread = null
    }

    fun stop() {
        AppLogger.i(TAG, "stopping")
        stopWatchdog()
        tcp?.stop()
        decoder?.stop()
        tcp = null
        decoder = null
    }

    fun stats(): String =
        "in=${bytesIn.get() / 1024} KiB · out=${framesOut.get()} frames"

    private companion object {
        const val TAG = "Pipeline"
        const val DEFAULT_W = 1280
        const val DEFAULT_H = 720

        /** How often the watchdog inspects decoder + TCP state. */
        const val WATCHDOG_INTERVAL_MS = 2000L

        /** Decoder is considered stalled if no output frame for this long. */
        const val STALL_THRESHOLD_MS = 8000L
    }
}

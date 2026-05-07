package com.livemobillrerun.vcam.hook

import android.media.MediaCodec
import android.media.MediaFormat
import android.view.Surface
import de.robv.android.xposed.XposedBridge
import java.io.File
import java.net.InetSocketAddress
import java.net.Socket
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Live H.264 stream receiver that runs **inside the TikTok process**
 * (loaded by LSPatch) and decodes Annex-B bytes from the PC streamer
 * straight onto a [Surface].
 *
 * Architecture
 * ============
 * ```
 *   ┌─ PC ─────────────────┐         ┌─ TikTok process ─────────┐
 *   │ FFmpeg ─► TcpServer  │  TCP    │ StreamReceiver           │
 *   │     127.0.0.1:8888   │◄───────►│  └─ MediaCodec(decoder)  │
 *   └──────────────────────┘         │       └─ Surface ────────┼─► encoder
 *                                    └──────────────────────────┘
 * ```
 *
 * The Surface is normally [FlipRenderer.inputSurface] so the user can
 * still rotate / mirror live, but the receiver is happy to write
 * directly to TikTok's encoder Surface if the renderer is missing.
 *
 * Activation
 * ----------
 * The receiver is enabled when the file
 * `/data/local/tmp/vcam_stream_url` exists — its first non-blank line
 * is parsed as `host:port`. Falling back to "127.0.0.1:8888" if the
 * file is empty (the most common case, since `adb reverse` already
 * tunnels the PC's port to the phone's loopback).
 *
 * The hook tries the StreamReceiver first; if no flag file exists it
 * falls back to [VideoFeeder]'s MP4-on-disk path. This means the user
 * can switch between live-streaming and pre-encoded loop just by
 * touching / removing one file:
 *
 * ```
 *   adb shell touch /data/local/tmp/vcam_stream_url   # live stream
 *   adb shell rm    /data/local/tmp/vcam_stream_url   # MP4 loop
 * ```
 */
class StreamReceiver(
    private val outputSurface: Surface,
    private val targetWidth: Int,
    private val targetHeight: Int,
) {
    companion object {
        const val TAG = "VCAM_RX"
        const val FLAG_PATH = "/data/local/tmp/vcam_stream_url"
        const val DEFAULT_HOST = "127.0.0.1"
        const val DEFAULT_PORT = 8888
        const val SOCKET_BUF = 64 * 1024
        const val RETRY_BACKOFF_MAX_MS = 30_000L

        /** Active receivers keyed by the encoder Surface they feed.
         *  Lets [stopAll] tear them down on app exit. */
        @JvmField
        val instances: MutableMap<Surface, StreamReceiver> =
            java.util.concurrent.ConcurrentHashMap()

        /** True when the activation file exists. Cheap to call. */
        fun enabled(): Boolean = File(FLAG_PATH).exists()

        /**
         * Parse [FLAG_PATH] — first non-blank line is `host:port`. If
         * the file is empty or unparseable, fall back to defaults.
         */
        fun resolveAddress(): Pair<String, Int> {
            val raw = runCatching {
                File(FLAG_PATH).readLines()
                    .map { it.trim() }
                    .firstOrNull { it.isNotEmpty() && !it.startsWith("#") }
            }.getOrNull()
            if (raw.isNullOrBlank()) return DEFAULT_HOST to DEFAULT_PORT
            val (h, p) = raw.split(":").let {
                if (it.size == 2) it[0] to it[1].toIntOrNull()
                else raw to null
            }
            return h to (p ?: DEFAULT_PORT)
        }

        fun stopAll() {
            for (r in instances.values) r.stop()
            instances.clear()
        }
    }

    private val running = AtomicBoolean(false)
    private val thread = Thread(::ioLoop, "vcam-rx")
        .apply { isDaemon = true }
    private var socket: Socket? = null
    private var codec: MediaCodec? = null

    fun start() {
        if (!running.compareAndSet(false, true)) return
        thread.start()
        XposedBridge.log("[$TAG] starting → $outputSurface (${targetWidth}×$targetHeight)")
    }

    fun stop() {
        if (!running.compareAndSet(true, false)) return
        runCatching { socket?.close() }
        runCatching { codec?.stop() }
        runCatching { codec?.release() }
        socket = null
        codec = null
    }

    /* ─── I/O loop ──────────────────────────────────────────── */

    private fun ioLoop() {
        var attempt = 0
        while (running.get()) {
            try {
                val (host, port) = resolveAddress()
                XposedBridge.log("[$TAG] connecting $host:$port (attempt #${attempt + 1})")
                val s = Socket().apply {
                    receiveBufferSize = SOCKET_BUF
                    connect(InetSocketAddress(host, port), 5_000)
                    soTimeout = 8_000
                }
                socket = s
                attempt = 0  // reset backoff on successful connect

                pumpFromSocket(s)
            } catch (t: Throwable) {
                if (!running.get()) break
                attempt++
                val delay = backoffMs(attempt)
                XposedBridge.log("[$TAG] connect/read failed ($t) — retry in ${delay}ms")
                Thread.sleep(delay)
            } finally {
                runCatching { socket?.close() }
                runCatching { codec?.stop() }
                runCatching { codec?.release() }
                socket = null
                codec = null
            }
        }
        XposedBridge.log("[$TAG] receiver thread exiting")
    }

    /**
     * Read raw Annex-B bytes from [s] in chunks. The first chunk is
     * inspected for an SPS/PPS pair; once those are in hand the
     * decoder is configured + started, and subsequent chunks are
     * queued straight into the input port. Decoded frames land on
     * [outputSurface] — TikTok's encoder picks them up automatically.
     */
    private fun pumpFromSocket(s: Socket) {
        val input = s.getInputStream()
        val buf = ByteArray(SOCKET_BUF)
        // We need a complete SPS+PPS before we can configure the
        // decoder; cache bytes until we have them.
        val warmup = java.io.ByteArrayOutputStream()
        var sps: ByteArray? = null
        var pps: ByteArray? = null

        while (running.get()) {
            val n = input.read(buf)
            if (n < 0) {
                XposedBridge.log("[$TAG] EOF")
                return
            }
            if (n == 0) continue

            if (codec == null) {
                warmup.write(buf, 0, n)
                val (foundSps, foundPps, leftover) = tryExtractCsd(warmup.toByteArray())
                if (foundSps != null) sps = foundSps
                if (foundPps != null) pps = foundPps
                if (sps != null && pps != null) {
                    XposedBridge.log("[$TAG] SPS+PPS captured, configuring decoder")
                    codec = configureDecoder(sps!!, pps!!)
                    warmup.reset()
                    if (leftover.isNotEmpty()) feedDecoder(codec!!, leftover)
                }
            } else {
                feedDecoder(codec!!, buf.copyOf(n))
            }
            drainOutput(codec)
        }
    }

    /**
     * Scan an Annex-B byte string for the first SPS (NAL type 7) and
     * PPS (NAL type 8). Returns whichever it found plus any bytes
     * past the end of the PPS so we can hand them straight to the
     * decoder once we configure it.
     */
    private fun tryExtractCsd(
        data: ByteArray,
    ): Triple<ByteArray?, ByteArray?, ByteArray> {
        val nals = splitAnnexB(data)
        var sps: ByteArray? = null
        var pps: ByteArray? = null
        var ppsEndIdx = -1
        var cursor = 0
        for (nal in nals) {
            if (nal.size < 5) {
                cursor += nal.size
                continue
            }
            val type = nal[4].toInt() and 0x1F
            when (type) {
                7 -> sps = nal
                8 -> {
                    pps = nal
                    ppsEndIdx = cursor + nal.size
                }
            }
            cursor += nal.size
        }
        val leftover = if (ppsEndIdx in 0 until data.size) {
            data.copyOfRange(ppsEndIdx, data.size)
        } else byteArrayOf()
        return Triple(sps, pps, leftover)
    }

    /**
     * Split [data] on Annex-B start codes (`00 00 00 01` and
     * `00 00 01`). Each returned slice **includes** its leading start
     * code so MediaCodec can identify the NAL type.
     */
    private fun splitAnnexB(data: ByteArray): List<ByteArray> {
        val out = mutableListOf<ByteArray>()
        var start = 0
        var i = 0
        while (i < data.size - 3) {
            val isLong = data[i] == 0.toByte() && data[i + 1] == 0.toByte() &&
                data[i + 2] == 0.toByte() && data[i + 3] == 1.toByte()
            val isShort = data[i] == 0.toByte() && data[i + 1] == 0.toByte() &&
                data[i + 2] == 1.toByte()
            if (isLong || isShort) {
                if (i > start) out.add(data.copyOfRange(start, i))
                start = i
                i += if (isLong) 4 else 3
            } else {
                i++
            }
        }
        if (start < data.size) out.add(data.copyOfRange(start, data.size))
        return out
    }

    private fun configureDecoder(sps: ByteArray, pps: ByteArray): MediaCodec {
        val format = MediaFormat.createVideoFormat(
            MediaFormat.MIMETYPE_VIDEO_AVC, targetWidth, targetHeight,
        ).apply {
            setByteBuffer("csd-0", java.nio.ByteBuffer.wrap(sps))
            setByteBuffer("csd-1", java.nio.ByteBuffer.wrap(pps))
        }
        val c = MediaCodec.createDecoderByType(MediaFormat.MIMETYPE_VIDEO_AVC)
        c.configure(format, outputSurface, null, 0)
        c.start()
        return c
    }

    private fun feedDecoder(c: MediaCodec, payload: ByteArray) {
        val idx = try {
            c.dequeueInputBuffer(10_000)
        } catch (e: Throwable) {
            XposedBridge.log("[$TAG] dequeueInput failed: $e")
            -1
        }
        if (idx < 0) return
        val ib = c.getInputBuffer(idx) ?: return
        ib.clear()
        ib.put(payload)
        try {
            c.queueInputBuffer(idx, 0, payload.size, System.nanoTime() / 1_000, 0)
        } catch (e: Throwable) {
            XposedBridge.log("[$TAG] queueInput failed: $e")
        }
    }

    private fun drainOutput(c: MediaCodec?) {
        if (c == null) return
        val info = MediaCodec.BufferInfo()
        while (true) {
            val outIdx = try {
                c.dequeueOutputBuffer(info, 0)
            } catch (e: Throwable) {
                XposedBridge.log("[$TAG] dequeueOutput failed: $e")
                return
            }
            if (outIdx < 0) return
            try {
                // Releasing with render=true blits the frame onto our
                // output Surface synchronously — no YUV copy.
                c.releaseOutputBuffer(outIdx, true)
            } catch (e: Throwable) {
                XposedBridge.log("[$TAG] releaseOutput failed: $e")
                return
            }
        }
    }

    private fun backoffMs(attempt: Int): Long {
        if (attempt <= 0) return 500L
        val base = 500L shl (attempt - 1).coerceAtMost(6)
        return base.coerceAtMost(RETRY_BACKOFF_MAX_MS)
    }
}

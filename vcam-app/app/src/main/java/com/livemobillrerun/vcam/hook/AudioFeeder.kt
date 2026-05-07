package com.livemobillrerun.vcam.hook

import android.media.MediaCodec
import android.media.MediaExtractor
import android.media.MediaFormat
import de.robv.android.xposed.XposedBridge
import java.io.File
import java.nio.ByteBuffer
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.concurrent.thread

/**
 * Decodes the audio track of an MP4 (or a standalone audio file) to
 * PCM and serves it to the `AudioRecord.read()` and
 * `MediaCodec.queueInputBuffer()` hooks installed by [CameraHook].
 *
 * Two source modes:
 *
 *  * **Override audio file** — if a file lives at one of the
 *    [AUDIO_OVERRIDE_PATHS] locations, that file is used and the
 *    video's own audio track is ignored. This lets the customer
 *    drop an MP3/WAV/M4A/AAC voice-over alongside a silent video
 *    clip and have TikTok Live receive that audio. MediaExtractor
 *    auto-detects the container, so any common audio format works.
 *  * **MP4 audio track** — the default; whatever audio sits inside
 *    the encoded vcam_final.mp4.
 *
 *  Reload behaviour
 *  ----------------
 *
 *  Calling [start] while an old decoder is running first stops the
 *  old thread, drains the ring buffer, then re-enters the loop with
 *  the new path. The PC's "Push audio" button broadcasts a
 *  ``forceReload=true`` so the swap happens within ~100 ms.
 */
object AudioFeeder {
    private const val TAG = "VCAM_AUDIO"

    @Volatile var enabled: Boolean = false
        private set

    /** Last path the decoder thread was asked to use (for diagnostics). */
    @Volatile var currentPath: String? = null
        private set

    /** Most recent decoded PCM. New decoder runs append to the tail. */
    private val ring = ArrayDeque<ByteArray>()
    private val ringLock = Any()
    private val running = AtomicBoolean(false)

    /**
     * Where we look for a standalone audio file. Mirror of
     * [VideoFeeder.activeVideoPath] but for audio. Order matters —
     * scoped-storage paths first, then root-only fallbacks.
     */
    private val AUDIO_OVERRIDE_PATHS: List<String> = run {
        val pkgs = listOf(
            "com.ss.android.ugc.trill",
            "com.zhiliaoapp.musically",
            "com.ss.android.ugc.aweme",
        )
        val exts = listOf("mp3", "m4a", "aac", "wav", "ogg")
        val out = mutableListOf<String>()
        for (pkg in pkgs) for (ext in exts) {
            out += "/sdcard/Android/data/$pkg/files/vcam_audio.$ext"
            out += "/storage/emulated/0/Android/data/$pkg/files/vcam_audio.$ext"
        }
        for (ext in exts) {
            out += "/data/local/tmp/vcam_audio.$ext"
            out += "/storage/emulated/0/vcam_audio.$ext"
            out += "/sdcard/vcam_audio.$ext"
        }
        out
    }

    /**
     * @return path to the first readable override audio file, or
     *         null if the customer hasn't pushed a separate one.
     */
    fun activeAudioPath(): String? {
        for (path in AUDIO_OVERRIDE_PATHS) {
            val f = File(path)
            if (f.canRead() && f.length() > 0L) return path
        }
        return null
    }

    fun start(videoPath: String) {
        // Prefer the standalone audio file when one exists; otherwise
        // fall back to whatever audio track sits inside the video.
        val source = activeAudioPath() ?: videoPath

        if (running.get()) {
            // Already decoding; only restart if the source actually
            // changed. This keeps Live streams from glitching when
            // the camera hook gets called multiple times for the
            // same path.
            if (currentPath == source) {
                XposedBridge.log("[$TAG] start() ignored — same source $source")
                return
            }
            XposedBridge.log("[$TAG] reloading: ${currentPath} → $source")
            stop()
            // Brief yield so the decoder thread releases its codec
            // before we spin up a new one — without it we sometimes
            // see "Decoder.start() is called" on a closed instance.
            Thread.sleep(50)
        }

        if (running.compareAndSet(false, true).not()) {
            XposedBridge.log("[$TAG] start() race lost — bailing")
            return
        }
        enabled = true
        currentPath = source
        thread(name = "AudioFeeder/$source", isDaemon = true) {
            try {
                decodeLoop(source)
            } catch (t: Throwable) {
                XposedBridge.log("[$TAG] decode loop crashed: $t")
            } finally {
                running.set(false)
            }
        }
    }

    /**
     * Force the decoder to re-evaluate which file to use right now.
     * Useful after the PC pushes a new audio file: the path may have
     * changed (e.g. the user added an override on top of an existing
     * MP4) without anybody calling [start] again.
     */
    fun reload(fallbackVideoPath: String?) {
        val newSource = activeAudioPath() ?: fallbackVideoPath
        if (newSource == null) {
            XposedBridge.log("[$TAG] reload — no source available, stopping")
            stop()
            return
        }
        if (newSource == currentPath && running.get()) {
            XposedBridge.log("[$TAG] reload — already on $newSource, no-op")
            return
        }
        XposedBridge.log("[$TAG] reload → $newSource")
        start(newSource)
    }

    fun stop() {
        enabled = false
        running.set(false)
        currentPath = null
        synchronized(ringLock) { ring.clear() }
    }

    fun read(buf: ByteArray, offset: Int, size: Int): Int {
        return drain(buf, offset, size)
    }

    fun read(buf: ByteBuffer, size: Int): Int {
        val tmp = ByteArray(size)
        val n = drain(tmp, 0, size)
        buf.put(tmp, 0, n)
        return n
    }

    private fun drain(out: ByteArray, offset: Int, size: Int): Int {
        synchronized(ringLock) {
            var written = 0
            while (written < size && ring.isNotEmpty()) {
                val head = ring.first()
                val take = minOf(head.size, size - written)
                System.arraycopy(head, 0, out, offset + written, take)
                written += take
                if (take == head.size) ring.removeFirst()
                else {
                    val rest = ByteArray(head.size - take)
                    System.arraycopy(head, take, rest, 0, rest.size)
                    ring[0] = rest
                }
            }
            // Pad the rest with silence so the encoder doesn't choke.
            if (written < size) {
                java.util.Arrays.fill(out, offset + written, offset + size, 0)
                written = size
            }
            return written
        }
    }

    private fun decodeLoop(videoPath: String) {
        XposedBridge.log("[$TAG] decoding $videoPath")
        val extractor = MediaExtractor().apply { setDataSource(videoPath) }
        var trackIdx = -1
        var format: MediaFormat? = null
        for (i in 0 until extractor.trackCount) {
            val f = extractor.getTrackFormat(i)
            if (f.getString(MediaFormat.KEY_MIME)?.startsWith("audio/") == true) {
                trackIdx = i
                format = f
                break
            }
        }
        if (trackIdx < 0 || format == null) {
            XposedBridge.log("[$TAG] no audio track in $videoPath")
            return
        }
        extractor.selectTrack(trackIdx)
        val decoder = MediaCodec.createDecoderByType(format.getString(MediaFormat.KEY_MIME)!!)
        decoder.configure(format, null, null, 0)
        decoder.start()

        val info = MediaCodec.BufferInfo()
        var sawEOS = false
        while (running.get()) {
            if (!sawEOS) {
                val inIdx = decoder.dequeueInputBuffer(10_000)
                if (inIdx >= 0) {
                    val inBuf = decoder.getInputBuffer(inIdx)!!
                    val n = extractor.readSampleData(inBuf, 0)
                    if (n < 0) {
                        decoder.queueInputBuffer(inIdx, 0, 0, 0L,
                            MediaCodec.BUFFER_FLAG_END_OF_STREAM)
                        sawEOS = true
                    } else {
                        decoder.queueInputBuffer(inIdx, 0, n, extractor.sampleTime, 0)
                        extractor.advance()
                    }
                }
            }
            val outIdx = decoder.dequeueOutputBuffer(info, 10_000)
            if (outIdx >= 0) {
                val outBuf = decoder.getOutputBuffer(outIdx)
                if (outBuf != null && info.size > 0) {
                    val chunk = ByteArray(info.size)
                    outBuf.position(info.offset)
                    outBuf.limit(info.offset + info.size)
                    outBuf.get(chunk)
                    synchronized(ringLock) {
                        ring.addLast(chunk)
                        // Cap the buffer so we don't grow unbounded.
                        while (ring.size > 32) ring.removeFirst()
                    }
                }
                decoder.releaseOutputBuffer(outIdx, false)
                if ((info.flags and MediaCodec.BUFFER_FLAG_END_OF_STREAM) != 0) {
                    extractor.seekTo(0L, MediaExtractor.SEEK_TO_CLOSEST_SYNC)
                    sawEOS = false
                    decoder.flush()
                }
            }
        }
        runCatching { decoder.stop() }
        runCatching { decoder.release() }
        runCatching { extractor.release() }
        XposedBridge.log("[$TAG] decode loop exited")
    }
}

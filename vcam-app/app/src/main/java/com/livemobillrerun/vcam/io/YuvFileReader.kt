package com.livemobillrerun.vcam.io

import android.content.Context
import com.livemobillrerun.vcam.util.AppLogger
import java.io.File
import java.io.RandomAccessFile
import java.nio.ByteBuffer
import java.nio.ByteOrder

/**
 * Reads back YUV frames previously written by [YuvFileWriter].
 *
 * This is the in-process equivalent of the Magisk Zygisk module's
 * `yuv_reader.cpp` — it lets us prove, on an unrooted phone, that the
 * file format on disk is well-formed and that a downstream consumer
 * (Camera HAL, in production) would see exactly the bytes we expect.
 *
 * Header layout (must match the writer, see `YuvFileWriter.kt`):
 *   [0..3]   magic 0x564D4143 ('VCAM' little-endian)
 *   [4..7]   width   (uint32_le)
 *   [8..11]  height  (uint32_le)
 *   [12..15] frame counter (uint32_le)
 *   [16..]   I420 payload — Y (w*h), U (w*h/4), V (w*h/4)
 *
 * The writer rewrites the file in-place via atomic rename, so each
 * `read()` opens a fresh fd and reads to EOF — never mmap, because the
 * inode changes on every frame.
 */
class YuvFileReader(
    candidatePaths: List<String> = DEFAULT_CANDIDATE_PATHS,
    appContext: Context? = null,
) {
    private var resolvedCandidates: List<String> = run {
        val privFromCtx = appContext?.let {
            File(it.filesDir, "vcam.yuv").absolutePath
        }
        // Insert the app-private fallback at the front of the list so
        // the in-process reader finds it without needing root.
        if (privFromCtx != null && privFromCtx !in candidatePaths) {
            listOf(privFromCtx) + candidatePaths
        } else {
            candidatePaths
        }
    }
    private var resolvedPath: String? = null

    data class Frame(
        val i420: ByteArray,
        val width: Int,
        val height: Int,
        val frameIndex: Int,
        val sourcePath: String,
        val mtimeMs: Long,
    ) {
        override fun equals(other: Any?): Boolean = this === other
        override fun hashCode(): Int = System.identityHashCode(this)
    }

    /**
     * Read the latest frame from disk, or null if the file is missing,
     * truncated, or has a bad magic number. Always reopens the file —
     * this is cheap and necessary because the writer renames it.
     */
    fun read(): Frame? {
        val path = resolvedPath ?: resolvePath() ?: return null
        return try {
            val file = File(path)
            val total = file.length()
            if (total < HEADER_SIZE) return null

            val raf = RandomAccessFile(file, "r")
            val header = ByteArray(HEADER_SIZE)
            try {
                raf.seek(0)
                raf.readFully(header)
                val bb = ByteBuffer.wrap(header).order(ByteOrder.LITTLE_ENDIAN)
                val magic = bb.int
                if (magic != MAGIC) {
                    AppLogger.w(TAG, "bad magic 0x%08x".format(magic))
                    return null
                }
                val w = bb.int
                val h = bb.int
                val idx = bb.int

                val payloadLen = (w.toLong() * h * 3 / 2).toInt()
                val expected = HEADER_SIZE + payloadLen
                if (total < expected) {
                    AppLogger.w(TAG, "short read: $total < $expected")
                    return null
                }
                val payload = ByteArray(payloadLen)
                raf.readFully(payload)
                Frame(
                    i420 = payload,
                    width = w,
                    height = h,
                    frameIndex = idx,
                    sourcePath = path,
                    mtimeMs = file.lastModified(),
                )
            } finally {
                raf.close()
            }
        } catch (e: Exception) {
            AppLogger.w(TAG, "read failed: ${e.message}")
            null
        }
    }

    private fun resolvePath(): String? {
        for (candidate in resolvedCandidates) {
            val f = File(candidate)
            if (f.canRead() && f.length() >= HEADER_SIZE) {
                resolvedPath = candidate
                AppLogger.i(TAG, "loopback reader using $candidate")
                return candidate
            }
        }
        return null
    }

    companion object {
        private const val TAG = "YuvFileReader"
        private const val MAGIC = 0x564D4143
        private const val HEADER_SIZE = 16

        val DEFAULT_CANDIDATE_PATHS = listOf(
            "/data/local/tmp/vcam.yuv",
            // App-private path is also added dynamically by the
            // ctor via the Context, but listing it here as a literal
            // covers the common case for the same package's own uid.
        )
    }
}

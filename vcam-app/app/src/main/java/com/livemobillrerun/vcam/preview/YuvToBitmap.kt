package com.livemobillrerun.vcam.preview

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.ImageFormat
import android.graphics.Rect
import android.graphics.YuvImage
import java.io.ByteArrayOutputStream

/**
 * Converts an I420 frame (Y plane, U plane, V plane — each tightly packed)
 * into a [Bitmap] for display in an `ImageView`.
 *
 * Path: I420 → NV21 → JPEG (via [YuvImage]) → Bitmap.
 *
 * This is intentionally a "slow path"; it's only invoked at preview FPS
 * (~5 Hz), not at the decoder's 30 Hz, so the JPEG round-trip is fine
 * and avoids any RenderScript / GLES dependency.
 */
internal object YuvToBitmap {

    /** JPEG quality used for the intermediate encode. 60 is plenty for preview. */
    private const val JPEG_QUALITY = 60

    fun convert(i420: ByteArray, width: Int, height: Int): Bitmap? {
        if (width <= 0 || height <= 0) return null
        val ySize = width * height
        val uvSize = ySize / 4
        val expected = ySize + uvSize * 2
        if (i420.size < expected) return null

        val nv21 = ByteArray(expected)
        // Y plane, copied verbatim.
        System.arraycopy(i420, 0, nv21, 0, ySize)

        // Interleave V then U (NV21 = YYYY...VUVU...).
        var dst = ySize
        val uStart = ySize
        val vStart = ySize + uvSize
        for (i in 0 until uvSize) {
            nv21[dst++] = i420[vStart + i]
            nv21[dst++] = i420[uStart + i]
        }

        val out = ByteArrayOutputStream(64 * 1024)
        val ok = YuvImage(nv21, ImageFormat.NV21, width, height, null)
            .compressToJpeg(Rect(0, 0, width, height), JPEG_QUALITY, out)
        if (!ok) return null
        val jpeg = out.toByteArray()
        return BitmapFactory.decodeByteArray(jpeg, 0, jpeg.size)
    }
}

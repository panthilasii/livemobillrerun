package com.livemobillrerun.vcam.preview

/**
 * Tiny pub/sub for the latest decoded I420 frame, used by [MainActivity]
 * to draw a live preview without coupling UI to the streaming service.
 *
 * Holds at most one frame at a time (the most recent) — the UI polls at
 * its own pace so the producer never blocks. The byte arrays handed in
 * are produced fresh per frame by [H264Decoder.imageToI420], so we can
 * keep the reference without copying.
 */
object PreviewBus {

    data class Frame(val i420: ByteArray, val width: Int, val height: Int) {
        // Don't generate equals/hashCode on the array (and we never need it).
        override fun equals(other: Any?): Boolean = this === other
        override fun hashCode(): Int = System.identityHashCode(this)
    }

    @Volatile
    private var latest: Frame? = null

    @Volatile
    private var totalPublished: Long = 0L

    fun publish(i420: ByteArray, width: Int, height: Int) {
        latest = Frame(i420, width, height)
        totalPublished++
    }

    /** Returns the most recent frame, or null if none has arrived yet. */
    fun peek(): Frame? = latest

    fun framesPublished(): Long = totalPublished

    fun reset() {
        latest = null
        totalPublished = 0L
    }
}

package com.livemobillrerun.vcam.util

import android.util.Log
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.concurrent.CopyOnWriteArrayList

object AppLogger {
    private const val TAG = "vcam"
    private val ts = SimpleDateFormat("HH:mm:ss.SSS", Locale.US)
    private val listeners = CopyOnWriteArrayList<(String) -> Unit>()

    fun addListener(l: (String) -> Unit) { listeners += l }
    fun removeListener(l: (String) -> Unit) { listeners -= l }

    fun d(tag: String, msg: String) = log("D", tag, msg) { Log.d(TAG, "[$tag] $msg") }
    fun i(tag: String, msg: String) = log("I", tag, msg) { Log.i(TAG, "[$tag] $msg") }
    fun w(tag: String, msg: String) = log("W", tag, msg) { Log.w(TAG, "[$tag] $msg") }
    fun e(tag: String, msg: String, t: Throwable? = null) =
        log("E", tag, msg + (t?.let { "  // ${it.message}" } ?: "")) {
            Log.e(TAG, "[$tag] $msg", t)
        }

    private inline fun log(level: String, tag: String, msg: String, doLog: () -> Unit) {
        doLog()
        val line = "${ts.format(Date())} $level [$tag] $msg"
        listeners.forEach { runCatching { it(line) } }
    }
}

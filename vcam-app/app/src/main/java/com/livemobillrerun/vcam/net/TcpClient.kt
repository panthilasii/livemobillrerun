package com.livemobillrerun.vcam.net

import com.livemobillrerun.vcam.util.AppLogger
import java.net.InetSocketAddress
import java.net.Socket
import java.util.concurrent.atomic.AtomicBoolean

/** TCP byte-stream client with automatic reconnect (1 s back-off). */
class TcpClient(
    private val host: String,
    private val port: Int,
    private val onBytes: (ByteArray, Int) -> Unit,
    private val onState: (State) -> Unit = {},
) {
    enum class State { Idle, Connecting, Connected, Disconnected, Stopped }

    private val running = AtomicBoolean(false)
    private var thread: Thread? = null

    fun start() {
        if (!running.compareAndSet(false, true)) return
        thread = Thread(::loop, "TcpClient").apply {
            isDaemon = true
            start()
        }
    }

    fun stop() {
        if (!running.compareAndSet(true, false)) return
        thread?.interrupt()
        thread = null
        onState(State.Stopped)
    }

    private fun loop() {
        val buf = ByteArray(64 * 1024)
        while (running.get()) {
            onState(State.Connecting)
            try {
                Socket().use { sock ->
                    sock.soTimeout = 5000
                    sock.connect(InetSocketAddress(host, port), 3000)
                    onState(State.Connected)
                    AppLogger.i(TAG, "connected $host:$port")
                    val input = sock.getInputStream()
                    while (running.get()) {
                        val n = try {
                            input.read(buf)
                        } catch (_: java.net.SocketTimeoutException) {
                            continue
                        }
                        if (n <= 0) break
                        onBytes(buf, n)
                    }
                }
            } catch (e: InterruptedException) {
                break
            } catch (e: Exception) {
                AppLogger.w(TAG, "tcp dropped: ${e.message}")
            }
            onState(State.Disconnected)
            if (!running.get()) break
            try {
                Thread.sleep(1000)
            } catch (_: InterruptedException) {
                break
            }
        }
        onState(State.Stopped)
    }

    private companion object {
        const val TAG = "TcpClient"
    }
}

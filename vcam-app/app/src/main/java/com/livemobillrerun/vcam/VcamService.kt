package com.livemobillrerun.vcam

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import androidx.core.app.NotificationCompat
import com.livemobillrerun.vcam.core.StreamPipeline
import com.livemobillrerun.vcam.io.YuvFileWriter
import com.livemobillrerun.vcam.util.AppLogger

class VcamService : Service() {

    private var pipeline: StreamPipeline? = null
    private var wakeLock: PowerManager.WakeLock? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> {
                stopSelfClean()
                return START_NOT_STICKY
            }
        }

        val host = intent?.getStringExtra(EXTRA_HOST) ?: DEFAULT_HOST
        val port = intent?.getIntExtra(EXTRA_PORT, DEFAULT_PORT) ?: DEFAULT_PORT

        ensureChannel()
        startForegroundCompat()
        acquireWakeLock()
        YuvFileWriter.init(this)

        if (pipeline == null) {
            pipeline = StreamPipeline(host = host, port = port).also {
                it.start()
            }
            AppLogger.i(TAG, "service started → $host:$port")
        }
        return START_STICKY
    }

    override fun onDestroy() {
        stopSelfClean()
        super.onDestroy()
    }

    private fun stopSelfClean() {
        pipeline?.stop()
        pipeline = null
        releaseWakeLock()
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
        AppLogger.i(TAG, "service stopped")
    }

    private fun startForegroundCompat() {
        val openIntent = PendingIntent.getActivity(
            this, 0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val stopIntent = PendingIntent.getService(
            this, 1,
            Intent(this, VcamService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val notif: Notification = NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle(getString(R.string.app_name))
            .setContentText(getString(R.string.notif_text))
            .setContentIntent(openIntent)
            .addAction(0, getString(R.string.btn_stop), stopIntent)
            .setOngoing(true)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .build()

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(
                NOTIF_ID,
                notif,
                ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC,
            )
        } else {
            startForeground(NOTIF_ID, notif)
        }
    }

    private fun ensureChannel() {
        val nm = getSystemService(NOTIFICATION_SERVICE) as NotificationManager
        if (nm.getNotificationChannel(CHANNEL_ID) == null) {
            nm.createNotificationChannel(
                NotificationChannel(
                    CHANNEL_ID,
                    getString(R.string.notif_channel_name),
                    NotificationManager.IMPORTANCE_LOW,
                )
            )
        }
    }

    private fun acquireWakeLock() {
        val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
        wakeLock = pm.newWakeLock(
            PowerManager.PARTIAL_WAKE_LOCK,
            "vcam:streamer"
        ).apply {
            setReferenceCounted(false)
            acquire(WAKE_LOCK_TIMEOUT_MS)
        }
    }

    private fun releaseWakeLock() {
        try {
            wakeLock?.release()
        } catch (_: Exception) {
        }
        wakeLock = null
    }

    companion object {
        const val ACTION_START = "com.livemobillrerun.vcam.action.START"
        const val ACTION_STOP = "com.livemobillrerun.vcam.action.STOP"
        const val EXTRA_HOST = "host"
        const val EXTRA_PORT = "port"
        const val DEFAULT_HOST = "127.0.0.1"
        const val DEFAULT_PORT = 8888

        private const val CHANNEL_ID = "vcam_streamer"
        private const val NOTIF_ID = 4242
        private const val TAG = "VcamService"
        private const val WAKE_LOCK_TIMEOUT_MS = 6L * 60 * 60 * 1000  // 6 h
    }
}

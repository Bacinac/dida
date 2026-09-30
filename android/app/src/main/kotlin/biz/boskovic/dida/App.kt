package biz.boskovic.dida

import android.app.Application
import android.app.NotificationChannel
import android.app.NotificationManager
import android.webkit.CookieManager
import biz.boskovic.dida.shared.CrashReporter

class App : Application() {
    override fun onCreate() {
        super.onCreate()
        CrashReporter.install(this, BuildConfig.VERSION_NAME)
        // Auth rides the WebView's session cookie, same as FcmRegistrar — signed
        // out means the report waits on disk for a start that is signed in.
        CrashReporter.flush(
            this, Prefs.baseUrl(this), "DIDA-App/${BuildConfig.VERSION_NAME} (Android)"
        ) {
            CookieManager.getInstance().getCookie(Prefs.baseUrl(this))?.let { "Cookie" to it }
        }
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(
            NotificationChannel(
                CHANNEL_UPDATES,
                getString(R.string.channel_updates),
                NotificationManager.IMPORTANCE_DEFAULT,
            )
        )
        nm.createNotificationChannel(
            NotificationChannel(
                CHANNEL_ALERTS,
                getString(R.string.channel_alerts),
                NotificationManager.IMPORTANCE_HIGH,
            )
        )
        // Defensive re-arm: if the OS recreated the process, make sure the
        // reporting pipeline is subscribed (idempotent — same PendingIntent).
        if (Prefs.isProvisioned(this)) {
            LocationEngine.start(this)
        }
    }

    companion object {
        const val CHANNEL_UPDATES = "dida.updates"
        const val CHANNEL_ALERTS = "dida.alerts"
    }
}

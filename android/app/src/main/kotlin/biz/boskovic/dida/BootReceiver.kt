package biz.boskovic.dida

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/** FLP subscriptions and geofences are both wiped on reboot (and geofences on
 * app update too) — re-arm the whole pipeline so location survives without
 * anyone opening the app. */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_BOOT_COMPLETED &&
            intent.action != Intent.ACTION_MY_PACKAGE_REPLACED
        ) {
            return
        }
        val ctx = context.applicationContext
        if (!Prefs.isProvisioned(ctx)) return
        LocationEngine.start(ctx)
        GeofenceManager.reRegister(ctx)
    }
}

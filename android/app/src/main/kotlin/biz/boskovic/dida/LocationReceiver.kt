package biz.boskovic.dida

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import com.google.android.gms.location.LocationResult
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch

/** Receives batched FLP fixes (see LocationEngine). Runs without any activity —
 * this plus GeofenceReceiver IS the "works when the app is not in focus" part. */
class LocationReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val loc = LocationResult.extractResult(intent)?.lastLocation ?: return
        val revision = intent.getStringExtra("revision") ?: return
        if (revision != Prefs.revision(context)) return
        // Very coarse fixes are noise the server drops anyway — save the bytes.
        if (loc.accuracy > 200f) return
        val pending = goAsync()
        CoroutineScope(Dispatchers.IO).launch {
            try {
                OwnTracksClient.postLocation(context.applicationContext, loc, revision)
            } finally {
                pending.finish()
            }
        }
    }
}

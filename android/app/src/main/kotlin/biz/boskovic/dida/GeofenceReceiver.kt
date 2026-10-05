package biz.boskovic.dida

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log
import com.google.android.gms.location.Geofence
import com.google.android.gms.location.GeofencingEvent

/** Zone enter/leave edges, delivered by the OS even in deep Doze. Each edge is
 * handed to TransitionWorker (guaranteed delivery with backoff) because presence
 * edges are what automations hang off — they must never be silently lost. */
class GeofenceReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val event = GeofencingEvent.fromIntent(intent) ?: return
        if (event.hasError()) {
            Log.w("DidaGeofence", "geofence event error ${event.errorCode}")
            return
        }
        val kind = when (event.geofenceTransition) {
            Geofence.GEOFENCE_TRANSITION_ENTER -> "enter"
            Geofence.GEOFENCE_TRANSITION_EXIT -> "leave"
            else -> return
        }
        val revision = Prefs.revision(context) ?: return
        val observedAtMs = System.currentTimeMillis()
        for (fence in event.triggeringGeofences.orEmpty()) {
            val zone = GeofenceManager.zone(context, fence.requestId) ?: continue
            TransitionWorker.enqueue(context, kind, zone, revision, observedAtMs)
        }
        // A fresh fix alongside the edge keeps the map current, not just presence.
        LocationEngine.requestOneFix(context.applicationContext)
    }
}

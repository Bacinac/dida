package biz.boskovic.dida

import android.annotation.SuppressLint
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.util.Log
import com.google.android.gms.location.Geofence
import com.google.android.gms.location.GeofencingRequest
import com.google.android.gms.location.LocationServices
import org.json.JSONArray

/** DIDA zones registered as native geofences. This is what "wakes the phone":
 * the OS delivers enter/leave edges to GeofenceReceiver even in Doze, with no
 * persistent service and near-zero battery cost — the reliable presence edge
 * for a stationary phone. */
object GeofenceManager {
    private const val TAG = "DidaGeofence"

    // The home zone's waypoint name carries the OwnTracks-iOS mode-switch suffix
    // ("Home|1|2") the server appends for iPhones; strip it so we register and
    // echo the clean DB zone name.
    private fun cleanName(desc: String) = desc.replace(Regex("\\|\\d+\\|\\d+$"), "")

    private fun pendingIntent(ctx: Context): PendingIntent =
        PendingIntent.getBroadcast(
            ctx, 42, Intent(ctx, GeofenceReceiver::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_MUTABLE
        )

    /** Replace the registered geofence set with `wps` (OwnTracks waypoint dicts:
     * desc/lat/lon/rad) and persist them for the post-reboot re-arm. */
    @SuppressLint("MissingPermission")
    fun sync(ctx: Context, wps: JSONArray) {
        Prefs.setWaypoints(ctx, wps)
        if (!Permissions.hasBackgroundLocation(ctx)) return
        val fences = mutableListOf<Geofence>()
        for (i in 0 until wps.length()) {
            val wp = wps.optJSONObject(i) ?: continue
            val name = cleanName(wp.optString("desc"))
            if (name.isEmpty()) continue
            fences += Geofence.Builder()
                .setRequestId(name)
                .setCircularRegion(
                    wp.optDouble("lat"),
                    wp.optDouble("lon"),
                    // Tiny radii produce flappy edges on coarse indoor fixes; the
                    // platform guidance floor is ~100 m, we allow down to 50.
                    wp.optDouble("rad", 100.0).toFloat().coerceAtLeast(50f),
                )
                .setExpirationDuration(Geofence.NEVER_EXPIRE)
                .setTransitionTypes(
                    Geofence.GEOFENCE_TRANSITION_ENTER or Geofence.GEOFENCE_TRANSITION_EXIT
                )
                .build()
        }
        val client = LocationServices.getGeofencingClient(ctx)
        client.removeGeofences(pendingIntent(ctx)).addOnCompleteListener {
            if (fences.isEmpty()) return@addOnCompleteListener
            val req = GeofencingRequest.Builder()
                // Fire ENTER for zones we are already inside at registration, so a
                // phone provisioned at home reports "home" immediately.
                .setInitialTrigger(GeofencingRequest.INITIAL_TRIGGER_ENTER)
                .addGeofences(fences)
                .build()
            client.addGeofences(req, pendingIntent(ctx))
                .addOnSuccessListener { Log.i(TAG, "registered ${fences.size} geofences") }
                .addOnFailureListener { e -> Log.w(TAG, "addGeofences failed: ${e.message}") }
        }
    }

    /** Geofences are lost on reboot and app update — re-arm from the stored copy. */
    fun reRegister(ctx: Context) {
        Prefs.waypoints(ctx)?.let { sync(ctx, it) }
    }
}

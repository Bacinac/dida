package biz.boskovic.dida

import android.annotation.SuppressLint
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import androidx.core.net.toUri
import androidx.work.WorkManager
import com.google.android.gms.location.LocationRequest
import com.google.android.gms.location.CurrentLocationRequest
import com.google.android.gms.location.LocationServices
import com.google.android.gms.location.Priority
import com.google.android.gms.tasks.CancellationTokenSource
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch

/** Continuous background reporting via FLP + PendingIntent: the OS batches and
 * delivers fixes to LocationReceiver with the app not in focus (and not even
 * running) — no foreground service, no persistent notification. Cadence mirrors
 * the OwnTracks config the family ran before (5 min / 100 m, 15-min heartbeat). */
object LocationEngine {

    @SuppressLint("MissingPermission")
    fun start(ctx: Context) {
        if (!Prefs.isProvisioned(ctx) || !Permissions.hasBackgroundLocation(ctx)) return
        val revision = Prefs.revision(ctx) ?: return
        val req = LocationRequest.Builder(Priority.PRIORITY_BALANCED_POWER_ACCURACY, 300_000L)
            .setMinUpdateIntervalMillis(120_000L)
            .setMinUpdateDistanceMeters(100f)
            .build()
        // Same PendingIntent → re-registering replaces, so this is idempotent.
        LocationServices.getFusedLocationProviderClient(ctx)
            .requestLocationUpdates(req, pendingIntent(ctx, revision))
        HeartbeatWorker.schedule(ctx)
    }

    fun stop(ctx: Context) {
        val revision = Prefs.revision(ctx)
        if (revision != null) {
            LocationServices.getFusedLocationProviderClient(ctx)
                .removeLocationUpdates(pendingIntent(ctx, revision))
        }
        WorkManager.getInstance(ctx).cancelAllWorkByTag("dida-location")
        GeofenceManager.stop(ctx)
    }

    private fun pendingIntent(ctx: Context, revision: String): PendingIntent =
        PendingIntent.getBroadcast(
            ctx, 41, Intent(ctx, LocationReceiver::class.java).apply {
                data = "dida-location:$revision".toUri()
                putExtra("revision", revision)
            },
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_MUTABLE
        )

    /** One immediate fix (provisioning / geofence edge), posted right away so the
     * map is current the moment something happened. */
    @SuppressLint("MissingPermission")
    fun requestOneFix(ctx: Context) {
        if (!Prefs.isProvisioned(ctx) || !Permissions.hasFineLocation(ctx)) return
        val revision = Prefs.revision(ctx) ?: return
        LocationServices.getFusedLocationProviderClient(ctx)
            .getCurrentLocation(CurrentLocationRequest.Builder()
                .setPriority(Priority.PRIORITY_BALANCED_POWER_ACCURACY).setMaxUpdateAgeMillis(0).build(),
                CancellationTokenSource().token)
            .addOnSuccessListener { loc ->
                if (loc != null) {
                    CoroutineScope(Dispatchers.IO).launch {
                        OwnTracksClient.postLocation(ctx.applicationContext, loc, revision)
                    }
                }
            }
    }
}

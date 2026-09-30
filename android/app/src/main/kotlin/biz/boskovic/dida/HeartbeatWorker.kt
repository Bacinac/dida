package biz.boskovic.dida

import android.annotation.SuppressLint
import android.content.Context
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.NetworkType
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import com.google.android.gms.location.LocationServices
import com.google.android.gms.location.Priority
import com.google.android.gms.tasks.CancellationTokenSource
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.tasks.await
import kotlinx.coroutines.withContext

/** The stationary baseline: a parked phone stops producing FLP movement updates,
 * so a 15-minute WorkManager tick posts a fresh fix anyway — same role as the
 * `ping: 15` in the old OwnTracks config. Always returns success (a periodic
 * chain must never be poisoned by one bad cycle). */
class HeartbeatWorker(ctx: Context, params: WorkerParameters) : CoroutineWorker(ctx, params) {
    @SuppressLint("MissingPermission")
    override suspend fun doWork(): Result {
        val ctx = applicationContext
        if (!Prefs.isProvisioned(ctx) || !Permissions.hasFineLocation(ctx)) return Result.success()
        val loc = try {
            LocationServices.getFusedLocationProviderClient(ctx)
                .getCurrentLocation(
                    Priority.PRIORITY_BALANCED_POWER_ACCURACY,
                    CancellationTokenSource().token,
                )
                .await()
        } catch (_: Exception) {
            null
        } ?: return Result.success()
        withContext(Dispatchers.IO) { OwnTracksClient.postLocation(ctx, loc) }
        return Result.success()
    }

    companion object {
        fun schedule(ctx: Context) {
            WorkManager.getInstance(ctx).enqueueUniquePeriodicWork(
                "dida-heartbeat",
                ExistingPeriodicWorkPolicy.KEEP,
                PeriodicWorkRequestBuilder<HeartbeatWorker>(15, TimeUnit.MINUTES)
                    .setConstraints(
                        Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build()
                    )
                    .build(),
            )
        }
    }
}

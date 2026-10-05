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
import androidx.work.workDataOf
import com.google.android.gms.location.LocationServices
import com.google.android.gms.location.CurrentLocationRequest
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
        val revision = inputData.getString("revision") ?: return Result.success()
        if (revision != Prefs.revision(ctx)) return Result.success()
        if (!Prefs.isProvisioned(ctx) || !Permissions.hasFineLocation(ctx)) return Result.success()
        val loc = try {
            LocationServices.getFusedLocationProviderClient(ctx)
                .getCurrentLocation(
                    CurrentLocationRequest.Builder().setPriority(Priority.PRIORITY_BALANCED_POWER_ACCURACY)
                        .setMaxUpdateAgeMillis(0).build(),
                    CancellationTokenSource().token,
                )
                .await()
        } catch (_: Exception) {
            null
        } ?: return Result.success()
        withContext(Dispatchers.IO) { OwnTracksClient.postLocation(ctx, loc, revision) }
        return Result.success()
    }

    companion object {
        fun schedule(ctx: Context) {
            WorkManager.getInstance(ctx).enqueueUniquePeriodicWork(
                "dida-heartbeat",
                ExistingPeriodicWorkPolicy.UPDATE,
                PeriodicWorkRequestBuilder<HeartbeatWorker>(15, TimeUnit.MINUTES)
                    .setInputData(workDataOf("revision" to Prefs.revision(ctx)))
                    .addTag("dida-location")
                    .setConstraints(
                        Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build()
                    )
                    .build(),
            )
        }
    }
}

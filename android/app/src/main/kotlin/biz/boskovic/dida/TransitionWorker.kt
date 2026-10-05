package biz.boskovic.dida

import android.content.Context
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import androidx.work.workDataOf
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

/** Delivers one geofence transition to the server, retrying with exponential
 * backoff until it lands (a zone edge crossed in a tunnel with no signal must
 * still arrive once the network is back). */
class TransitionWorker(ctx: Context, params: WorkerParameters) : CoroutineWorker(ctx, params) {
    override suspend fun doWork(): Result {
        val event = inputData.getString("event") ?: return Result.failure()
        val zone = inputData.getString("zone") ?: return Result.failure()
        val revision = inputData.getString("revision") ?: return Result.failure()
        val stamp = LocationStamp(revision, inputData.getLong("observed_at", 0), inputData.getLong("sequence", -1))
        if (!stamp.belongsTo(Prefs.revision(applicationContext))) return Result.success()
        val ok = withContext(Dispatchers.IO) {
            OwnTracksClient.postTransition(applicationContext, event, zone, stamp)
        }
        return when {
            ok -> Result.success()
            runAttemptCount < 8 -> Result.retry()
            else -> Result.failure()
        }
    }

    companion object {
        fun enqueue(ctx: Context, event: String, zone: String, revision: String, observedAtMs: Long) {
            val stamp = Prefs.stamp(ctx, observedAtMs, revision) ?: return
            WorkManager.getInstance(ctx).enqueueUniqueWork(
                "dida-transitions-${stamp.revision}", ExistingWorkPolicy.APPEND_OR_REPLACE,
                OneTimeWorkRequestBuilder<TransitionWorker>()
                    .setInputData(workDataOf("event" to event, "zone" to zone, "revision" to stamp.revision,
                        "observed_at" to stamp.observedAtMs, "sequence" to stamp.sequence))
                    .addTag("dida-location")
                    .setConstraints(
                        Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build()
                    )
                    .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS)
                    .build()
            )
        }
    }
}

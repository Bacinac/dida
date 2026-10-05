package biz.boskovic.dida

import android.content.Context
import android.location.Location
import android.os.BatteryManager
import android.os.Build
import android.util.Base64
import android.util.Log
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/** Speaks the OwnTracks HTTP protocol against DIDA's existing `/api/owntracks`
 * receiver: Basic auth = username + endpoint-scoped location token, `_type:
 * location` / `_type: transition` frames out, and `setWaypoints` commands (zone
 * edits) parsed out of the reply. */
object OwnTracksClient {
    private const val TAG = "DidaOwnTracks"

    /** Tail of the provisioned receiver URL — the origin in front of it is the
     * publicly reachable one, which every other app call needs too. */
    private val JSON_TYPE = "application/json; charset=utf-8".toMediaType()

    val http: OkHttpClient by lazy {
        OkHttpClient.Builder()
            .connectTimeout(10, TimeUnit.SECONDS)
            .readTimeout(15, TimeUnit.SECONDS)
            .addInterceptor { chain ->
                chain.proceed(
                    chain.request().newBuilder()
                        .header("User-Agent", "DIDA-App/${BuildConfig.VERSION_NAME} (Android)")
                        .build()
                )
            }
            .build()
    }

    private fun basicAuth(ctx: Context): String? {
        val user = Prefs.username(ctx) ?: return null
        val token = Prefs.locationToken(ctx) ?: return null
        return "Basic " + Base64.encodeToString("$user:$token".toByteArray(), Base64.NO_WRAP)
    }

    private fun batteryPercent(ctx: Context): Int? =
        (ctx.getSystemService(Context.BATTERY_SERVICE) as? BatteryManager)
            ?.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY)
            ?.takeIf { it in 0..100 }

    /** POST a location fix. Fire-and-forget semantics: a stale fix is worthless,
     * so callers drop failures — the 15-minute heartbeat is the natural retry.
     * Blocking; call off the main thread. */
    fun postLocation(ctx: Context, loc: Location, revision: String): Boolean {
        val stamp = Prefs.stamp(ctx, loc.time, revision) ?: return true
        val frame = JSONObject()
            .put("_type", "location")
            .put("lat", loc.latitude)
            .put("lon", loc.longitude)
            .put("acc", loc.accuracy.toInt())
            .put("tst", stamp.observedAtMs / 1000)
        batteryPercent(ctx)?.let { frame.put("batt", it) }
        return postFrame(ctx, frame, stamp)
    }

    /** POST a geofence edge. Blocking; TransitionWorker retries with backoff —
     * presence edges are what automations hang off, they must not be lost. */
    fun postTransition(ctx: Context, event: String, zone: String, stamp: LocationStamp): Boolean {
        val frame = JSONObject()
            .put("_type", "transition")
            .put("event", event)
            .put("desc", zone)
            .put("tst", stamp.observedAtMs / 1000)
        return postFrame(ctx, frame, stamp)
    }

    /** Answer a wake push: did the location engine arm, and if not, why. Rides
     * the location credential because a push arrives with no WebView session,
     * and goes to the same public origin the receiver was provisioned on — the
     * phone may well be on another continent. Blocking; call off the main
     * thread. */
    fun postEngineStatus(ctx: Context, outcome: String): Boolean =
        postAppStatus(ctx, "/app/location-status", JSONObject().put("outcome", outcome))

    /** A notification went up without the camera frame it carried, and why.
     * Blocking; call off the main thread. */
    fun postPushImageLost(ctx: Context, failure: String): Boolean =
        postAppStatus(ctx, "/app/push-image", JSONObject().put("failure", failure.take(300)))

    private fun postAppStatus(ctx: Context, path: String, body: JSONObject): Boolean {
        val post = Prefs.postUrl(ctx) ?: return false
        val url = OwnTracksUrls.appStatus(post, path) ?: return false
        val auth = basicAuth(ctx) ?: return false
        body.put("version", BuildConfig.VERSION_NAME)
            .put("device", "${Build.MANUFACTURER} ${Build.MODEL} / Android ${Build.VERSION.RELEASE}")
        return try {
            http.newCall(
                Request.Builder().url(url)
                    .header("Authorization", auth)
                    .post(body.toString().toRequestBody(JSON_TYPE))
                    .build()
            ).execute().use { it.isSuccessful }
        } catch (e: Exception) {
            Log.w(TAG, "$path failed: ${e.message}")
            false
        }
    }

    @Synchronized
    private fun postFrame(ctx: Context, frame: JSONObject, stamp: LocationStamp): Boolean {
        if (!stamp.belongsTo(Prefs.revision(ctx))) return true
        val url = Prefs.postUrl(ctx) ?: return false
        val auth = basicAuth(ctx) ?: return false
        frame.put("tst_ms", stamp.observedAtMs).put("seq", stamp.sequence).put("reporter", stamp.revision)
        if (!stamp.belongsTo(Prefs.revision(ctx))) return true
        return try {
            http.newCall(
                Request.Builder().url(url)
                    .header("Authorization", auth)
                    .post(frame.toString().toRequestBody(JSON_TYPE))
                    .build()
            ).execute().use { resp ->
                if (!resp.isSuccessful) {
                    Log.w(TAG, "post ${frame.optString("_type")} -> HTTP ${resp.code}")
                    return false
                }
                handleReply(ctx, resp.body.string(), stamp.revision)
                true
            }
        } catch (e: Exception) {
            Log.w(TAG, "post failed: ${e.message}")
            false
        }
    }

    /** The receiver piggybacks zone edits on its reply: a `setWaypoints` command
     * carries the full current zone set → resync the native geofences. A QR scan
     * is a one-time bootstrap, never a recurring chore. */
    private fun handleReply(ctx: Context, body: String?, revision: String) {
        if (revision != Prefs.revision(ctx)) return
        if (body.isNullOrBlank()) return
        val arr = runCatching { JSONArray(body) }.getOrNull() ?: return
        for (i in 0 until arr.length()) {
            val msg = arr.optJSONObject(i) ?: continue
            if (msg.optString("_type") == "cmd" && msg.optString("action") == "setWaypoints") {
                val wps = msg.optJSONObject("waypoints")?.optJSONArray("waypoints") ?: continue
                Log.i(TAG, "zone set changed — resyncing ${wps.length()} geofences")
                GeofenceManager.sync(ctx, wps, revision)
            }
        }
    }
}

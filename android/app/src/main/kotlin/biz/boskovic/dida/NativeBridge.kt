package biz.boskovic.dida

import android.util.Log
import org.json.JSONObject

class NativeBridge(private val activity: MainActivity, private val origin: String,
                   private val currentOrigin: () -> String = { Prefs.baseUrl(activity) }) {
    fun dispatch(data: String, sourceOrigin: String, isMainFrame: Boolean): String? {
        if (!isMainFrame || LocationIdentity.origin(sourceOrigin) != origin ||
            LocationIdentity.origin(currentOrigin()) != origin) {
            Log.w("DIDA", "Rejected native message from an untrusted frame")
            return null
        }
        var id = 0L
        return try {
            require(data.length <= 16_384) { "Native message is too large" }
            val request = JSONObject(data)
            id = request.getLong("id")
            val args = request.optJSONObject("args") ?: JSONObject()
            val result: Any = when (request.getString("method")) {
                "appVersion" -> BuildConfig.VERSION_NAME
                "status" -> status()
                "startLocationSetup" -> { activity.startLocationSetup(); JSONObject.NULL }
                "syncIdentity" -> { activity.syncIdentity(args.getString("userId"), args.getString("origin")); JSONObject.NULL }
                "scanQr" -> { activity.launchQrScan(); JSONObject.NULL }
                "checkUpdate" -> { UpdateManager.check(activity, force = true); JSONObject.NULL }
                "setLocale" -> { activity.applyLocale(args.getString("tag")); JSONObject.NULL }
                else -> error("Unknown native method")
            }
            JSONObject().put("id", id).put("result", result).toString()
        } catch (exc: Exception) {
            Log.e("DIDA", "Native message failed", exc)
            JSONObject().put("id", id).put("error", exc.message ?: "Native message failed").toString()
        }
    }

    private fun status(): JSONObject {
        val ctx = activity.applicationContext
        return JSONObject()
            .put("version", BuildConfig.VERSION_NAME)
            .put("provisioned", Prefs.isProvisioned(ctx))
            .put("username", Prefs.username(ctx) ?: JSONObject.NULL)
            .put("userId", Prefs.userId(ctx) ?: JSONObject.NULL)
            .put("origin", Prefs.identity(ctx)?.origin ?: JSONObject.NULL)
            .put("fineLocation", Permissions.hasFineLocation(ctx))
            .put("backgroundLocation", Permissions.hasBackgroundLocation(ctx))
            .put("batteryExempt", Permissions.isBatteryExempt(ctx))
            .put("zones", Prefs.waypoints(ctx)?.length() ?: 0)
    }
}

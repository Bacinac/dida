package biz.boskovic.dida

import android.annotation.SuppressLint
import android.content.Context
import android.content.SharedPreferences
import androidx.core.content.edit
import org.json.JSONArray
import java.util.UUID

/** Single store of app state: the server origin plus the endpoint-scoped
 * location credentials handed over by `/api/me/mobile-config` during
 * onboarding. The location token deliberately cannot log into the UI (it is
 * only valid against /api/owntracks), so plain SharedPreferences is the right
 * weight — a lost phone is revoked server-side by rotating the token. */
object Prefs {
    private fun sp(ctx: Context): SharedPreferences =
        ctx.getSharedPreferences("dida", Context.MODE_PRIVATE)

    fun baseUrl(ctx: Context): String =
        sp(ctx).getString("base_url", null) ?: BuildConfig.DEFAULT_BASE_URL

    @Synchronized
    fun setBaseUrl(ctx: Context, url: String) {
        val origin = requireNotNull(LocationIdentity.origin(url)) { "Invalid server origin" }
        if (LocationIdentity.origin(baseUrl(ctx)) != origin) clearCredentials(ctx)
        sp(ctx).edit(commit = true) { putString("base_url", origin) }
    }

    /** The DIDA UI language the user picked (BCP-47 tag), applied to the app's own
     * strings in attachBaseContext. Null = follow the device locale. */
    fun uiLocale(ctx: Context): String? = sp(ctx).getString("ui_locale", null)

    fun setUiLocale(ctx: Context, tag: String) =
        sp(ctx).edit { putString("ui_locale", tag) }

    fun username(ctx: Context): String? = sp(ctx).getString("username", null)
    fun locationToken(ctx: Context): String? = sp(ctx).getString("location_token", null)

    /** Absolute URL of the OwnTracks receiver (server-provided, rides the public
     * tunnel origin so reporting works away from home). */
    fun postUrl(ctx: Context): String? = sp(ctx).getString("post_url", null)

    fun userId(ctx: Context): String? = sp(ctx).getString("user_id", null)
    fun revision(ctx: Context): String? = sp(ctx).getString("location_revision", null)
    fun geofenceRevision(ctx: Context): String? = sp(ctx).getString("geofence_revision", null)
    fun identity(ctx: Context): LocationIdentity? {
        val user = userId(ctx) ?: return null
        val origin = postUrl(ctx)?.let { LocationIdentity.origin(it) } ?: return null
        return LocationIdentity(user, origin)
    }

    fun isProvisioned(ctx: Context): Boolean {
        val identity = identity(ctx) ?: return false
        return username(ctx) != null && locationToken(ctx) != null && revision(ctx) != null &&
            identity.origin == LocationIdentity.origin(baseUrl(ctx))
    }

    @Synchronized
    fun matchesCredentials(ctx: Context, userId: String, username: String, token: String, postUrl: String): Boolean {
        val origin = LocationIdentity.origin(postUrl) ?: return false
        return identity(ctx) == LocationIdentity(userId, origin) &&
            username(ctx) == username && locationToken(ctx) == token && revision(ctx) != null
    }

    @Synchronized
    fun setCredentials(ctx: Context, userId: String, username: String, token: String, postUrl: String) {
        val origin = requireNotNull(LocationIdentity.origin(postUrl)) { "Invalid location receiver origin" }
        require(origin == LocationIdentity.origin(baseUrl(ctx))) { "Location receiver belongs to another server" }
        val changed = !matchesCredentials(ctx, userId, username, token, postUrl)
        sp(ctx).edit(commit = true) {
            putString("user_id", userId)
            putString("username", username)
            putString("location_token", token)
            putString("post_url", postUrl)
            if (changed) {
                putString("location_revision", UUID.randomUUID().toString())
                putLong("provisioned_at", System.currentTimeMillis())
                putLong("location_sequence", 0)
                remove("waypoints")
                remove("geofence_revision")
            }
        }
    }

    @Synchronized
    fun clearCredentials(ctx: Context) {
        sp(ctx).edit(commit = true) {
            for (key in listOf("user_id", "username", "location_token", "post_url", "location_revision",
                "provisioned_at", "location_sequence", "waypoints", "geofence_revision")) remove(key)
        }
    }

    @Synchronized
    @SuppressLint("UseKtx")
    fun stamp(ctx: Context, observedAtMs: Long, expectedRevision: String): LocationStamp? {
        if (!isProvisioned(ctx) || expectedRevision != revision(ctx) ||
            observedAtMs < sp(ctx).getLong("provisioned_at", Long.MAX_VALUE)) return null
        val sequence = Math.addExact(sp(ctx).getLong("location_sequence", 0), 1)
        // The KTX edit helper discards commit's success flag.
        check(sp(ctx).edit().putLong("location_sequence", sequence).commit()) { "Could not store location sequence" }
        return LocationStamp(expectedRevision, observedAtMs, sequence)
    }

    /** Last synced zone set (OwnTracks waypoint dicts, verbatim JSON). Geofences
     * do not survive a reboot — BootReceiver re-registers from this copy. */
    fun waypoints(ctx: Context): JSONArray? =
        sp(ctx).getString("waypoints", null)?.let { runCatching { JSONArray(it) }.getOrNull() }

    @Synchronized
    fun setWaypoints(ctx: Context, wps: JSONArray, geofenceRevision: String, expectedRevision: String): Boolean {
        if (revision(ctx) != expectedRevision) return false
        sp(ctx).edit(commit = true) {
            putString("waypoints", wps.toString())
            putString("geofence_revision", geofenceRevision)
        }
        return true
    }

    @Synchronized
    fun waypointName(ctx: Context, generation: String, index: Int): String? {
        if (generation != geofenceRevision(ctx)) return null
        return waypoints(ctx)?.optJSONObject(index)?.optString("desc")
    }

    fun lastUpdateCheckMs(ctx: Context): Long = sp(ctx).getLong("last_update_check", 0L)
    fun setLastUpdateCheckMs(ctx: Context, t: Long) =
        sp(ctx).edit { putLong("last_update_check", t) }
}

package biz.boskovic.dida

import android.content.Context
import android.content.SharedPreferences
import androidx.core.content.edit
import org.json.JSONArray

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

    fun setBaseUrl(ctx: Context, url: String) =
        sp(ctx).edit { putString("base_url", url.trimEnd('/')) }

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

    fun isProvisioned(ctx: Context): Boolean =
        username(ctx) != null && locationToken(ctx) != null && postUrl(ctx) != null

    fun setCredentials(ctx: Context, username: String, token: String, postUrl: String) =
        sp(ctx).edit {
            putString("username", username)
            putString("location_token", token)
            putString("post_url", postUrl)
        }

    /** Last synced zone set (OwnTracks waypoint dicts, verbatim JSON). Geofences
     * do not survive a reboot — BootReceiver re-registers from this copy. */
    fun waypoints(ctx: Context): JSONArray? =
        sp(ctx).getString("waypoints", null)?.let { runCatching { JSONArray(it) }.getOrNull() }

    fun setWaypoints(ctx: Context, wps: JSONArray) =
        sp(ctx).edit { putString("waypoints", wps.toString()) }

    fun lastUpdateCheckMs(ctx: Context): Long = sp(ctx).getLong("last_update_check", 0L)
    fun setLastUpdateCheckMs(ctx: Context, t: Long) =
        sp(ctx).edit { putLong("last_update_check", t) }
}

package biz.boskovic.dida

import android.webkit.JavascriptInterface
import org.json.JSONObject

/** `window.DidaApp` inside the WebView. A JavascriptInterface is injected into
 * every page the WebView loads, so each call first checks that the page is ours
 * (MainActivity.onOwnOrigin) and does nothing for any other origin. */
class NativeBridge(private val activity: MainActivity) {

    @JavascriptInterface
    fun appVersion(): String = if (activity.onOwnOrigin) BuildConfig.VERSION_NAME else ""

    /** Snapshot for the onboarding page: what's granted, what's armed. The page
     * re-reads this on every `dida-native-status` window event. */
    @JavascriptInterface
    fun status(): String {
        if (!activity.onOwnOrigin) return "{}"
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
            .toString()
    }

    /** Kicks the native permission walkthrough → provisioning. */
    @JavascriptInterface
    fun startLocationSetup() {
        if (!activity.onOwnOrigin) return
        activity.runOnUiThread { activity.startLocationSetup() }
    }

    @JavascriptInterface
    fun syncIdentity(userId: String, origin: String) {
        if (!activity.onOwnOrigin) return
        activity.runOnUiThread { activity.syncIdentity(userId, origin) }
    }

    /** Opens the in-app QR scanner (signed-out onboarding): scanning the setup
     * QR redeems it in THIS WebView's cookie jar — signed in, walkthrough next. */
    @JavascriptInterface
    fun scanQr() {
        if (!activity.onOwnOrigin) return
        activity.runOnUiThread { activity.launchQrScan() }
    }

    /** "Update now" from the account page — force an update check + one-tap install. */
    @JavascriptInterface
    fun checkUpdate() {
        if (!activity.onOwnOrigin) return
        activity.runOnUiThread { UpdateManager.check(activity, force = true) }
    }

    /** Apply the DIDA UI language to the app's OWN strings (toasts, update dialog,
     * permission prompts) so they match the language the user picked in the web,
     * not the device locale. Called by the page on load and on a language switch. */
    @JavascriptInterface
    fun setLocale(tag: String) {
        if (!activity.onOwnOrigin) return
        activity.runOnUiThread { activity.applyLocale(tag) }
    }
}

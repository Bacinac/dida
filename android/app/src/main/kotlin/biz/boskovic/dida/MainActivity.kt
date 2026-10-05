package biz.boskovic.dida

import android.Manifest
import android.annotation.SuppressLint
import android.app.AlertDialog
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.util.Log
import android.webkit.CookieManager
import android.webkit.GeolocationPermissions
import android.webkit.PermissionRequest
import android.webkit.RenderProcessGoneDetail
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.FrameLayout
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.addCallback
import androidx.activity.result.contract.ActivityResultContracts
import com.journeyapps.barcodescanner.ScanContract
import com.journeyapps.barcodescanner.ScanOptions
import android.content.res.Configuration
import java.util.Locale
import androidx.core.net.toUri
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import androidx.lifecycle.lifecycleScope
import androidx.webkit.WebViewCompat
import androidx.webkit.WebViewFeature
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.Request
import org.json.JSONObject

/** The whole app UI is the existing DIDA web (this is a companion shell, the
 * HA-companion pattern): a WebView pinned to our origin plus the native layer
 * the web cannot provide — background location, geofences, self-update. The
 * setup QR's App Link lands here, redeems the one-time token in the WebView's
 * cookie jar, and /onboard (detecting the DIDA-App UA) drives the native
 * permission walkthrough through the NativeBridge. */
class MainActivity : ComponentActivity() {

    private lateinit var webView: WebView
    private var fileChooserCallback: ValueCallback<Array<Uri>>? = null
    private var setupInProgress = false
    private var nudgedSetup = false
    private var activeUserId: String? = null
    private var provisionRevision = 0L

    // --- Setup walkthrough steps, chained via Activity Result callbacks. Order
    // is OS-mandated: fine location first, only then background ("all the time"),
    // then the battery exemption, notifications, and finally provisioning. ---

    private val finePermLauncher =
        registerForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { grants ->
            when {
                grants[Manifest.permission.ACCESS_FINE_LOCATION] == true ->
                    requestBackground()
                // Denied with no dialog shown = permanently denied on an earlier
                // attempt: the OS silently auto-rejects, which reads as "the app
                // won't let me enable location". Route through app settings and
                // re-enter the chain on return.
                !shouldShowRequestPermissionRationale(Manifest.permission.ACCESS_FINE_LOCATION) &&
                    !settingsTried -> openAppSettings()
                else -> {
                    Toast.makeText(this, R.string.perm_location_denied, Toast.LENGTH_LONG).show()
                    finishSetup()
                }
            }
        }

    // One settings round-trip per setup attempt — a permanently denied permission
    // auto-rejects instantly, and without this guard settings would reopen forever.
    private var settingsTried = false

    private val appSettingsLauncher =
        registerForActivityResult(ActivityResultContracts.StartActivityForResult()) {
            // Back from the app-settings page: continue with whatever was granted.
            setupInProgress = false
            startLocationSetup(fresh = false)
        }

    private fun openAppSettings() {
        settingsTried = true
        Toast.makeText(this, R.string.perm_open_settings, Toast.LENGTH_LONG).show()
        try {
            appSettingsLauncher.launch(
                Intent(
                    Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                    "package:$packageName".toUri(),
                )
            )
        } catch (_: Exception) {
            finishSetup()
        }
    }

    private val bgPermLauncher =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) {
            requestBatteryExemption()
        }

    private val batteryLauncher =
        registerForActivityResult(ActivityResultContracts.StartActivityForResult()) {
            requestNotifications()
        }

    private val notifLauncher =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) {
            provision()
        }

    /** Microphone for the assistant's push-to-talk. Nothing to do on the result:
     *  the page's next tap re-asks, and by then the grant is in place. */
    private val micLauncher =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { }

    /** In-app QR scan for the signed-out onboarding screen: redeeming the setup
     *  QR in THIS WebView's cookie jar signs the app in — however the app was
     *  opened (installer's "Open", the icon), one button recovers the flow. Only
     *  our own auth link is accepted; any other QR is ignored. */
    private val qrScanLauncher = registerForActivityResult(ScanContract()) { result ->
        val text = result.contents ?: return@registerForActivityResult
        val uri = text.toUri()
        if (uri.scheme == "https" && uri.host != null && uri.path == "/api/auth/link") {
            val origin = LocationIdentity.origin(text) ?: return@registerForActivityResult
            if (LocationIdentity.origin(Prefs.baseUrl(this)) != origin) {
                LocationEngine.stop(this)
                activeUserId = null
                provisionRevision++
            }
            Prefs.setBaseUrl(this, origin)
            if (!installNativeBridge()) return@registerForActivityResult
            webView.loadUrl(text)
        }
    }

    /** Wrap the base context in the user's chosen DIDA language so the app's OWN
     *  strings (toasts, dialogs, permission prompts) match the UI, not the device
     *  locale. Applied here rather than via AppCompatDelegate because this is a
     *  ComponentActivity (no AppCompat), where that API silently no-ops. */
    override fun attachBaseContext(base: Context) {
        val tag = Prefs.uiLocale(base)
        if (tag.isNullOrBlank()) { super.attachBaseContext(base); return }
        val loc = Locale.forLanguageTag(tag)
        Locale.setDefault(loc)
        val cfg = Configuration(base.resources.configuration).apply { setLocale(loc) }
        super.attachBaseContext(base.createConfigurationContext(cfg))
    }

    /** Store the DIDA UI locale and re-create so attachBaseContext re-applies it —
    *   only when it actually changed, to avoid a recreate loop. */
    fun applyLocale(tag: String) {
        val want = tag.trim().lowercase()
        if (want.isEmpty() || want == (Prefs.uiLocale(this)?.lowercase())) return
        Prefs.setUiLocale(this, want)
        recreate()
    }

    fun launchQrScan() {
        qrScanLauncher.launch(
            ScanOptions().apply {
                setDesiredBarcodeFormats(ScanOptions.QR_CODE)
                setPrompt("")
                setBeepEnabled(false)
                setOrientationLocked(true)  // portrait — no landscape flip mid-scan
            }
        )
    }

    private val fileChooserLauncher =
        registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { res ->
            fileChooserCallback?.onReceiveValue(
                WebChromeClient.FileChooserParams.parseResult(res.resultCode, res.data)
            )
            fileChooserCallback = null
        }

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // targetSdk 35+ is edge-to-edge by decree. The inset padding goes on a
        // wrapper, NOT on the WebView: WebView is documented-flaky about its own
        // padding (renders underneath it), which put the page header under the
        // status bar and the tab bar under the gesture bar on Android 15.
        val root = FrameLayout(this)
        root.setBackgroundColor(getColor(R.color.dida_bg))
        webView = WebView(this)
        root.addView(
            webView,
            FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.MATCH_PARENT,
            ),
        )
        setContentView(root)
        ViewCompat.setOnApplyWindowInsetsListener(root) { v, insets ->
            val bars = insets.getInsets(
                WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.ime()
            )
            v.setPadding(bars.left, bars.top, bars.right, bars.bottom)
            WindowInsetsCompat.CONSUMED
        }

        with(webView.settings) {
            javaScriptEnabled = true
            domStorageEnabled = true
            mediaPlaybackRequiresUserGesture = false // camera streams autoplay
            userAgentString = "$userAgentString DIDA-App/${BuildConfig.VERSION_NAME}"
        }
        CookieManager.getInstance().setAcceptCookie(true)
        if (!installNativeBridge()) return

        webView.webViewClient = ShellClient()

        webView.webChromeClient = object : WebChromeClient() {
            override fun onGeolocationPermissionsShowPrompt(
                origin: String,
                callback: GeolocationPermissions.Callback,
            ) {
                // Page-side geolocation (map centering) rides the app's own grant.
                val allow = isOwnOrigin(origin) && Permissions.hasFineLocation(this@MainActivity)
                callback.invoke(origin, allow, false)
            }

            // The page's push-to-talk asks the WebView for the microphone. A
            // WebView denies every such request unless this is implemented — which
            // is why the assistant reported "not-allowed" in the app while the same
            // page worked in Chrome. We can only grant what the APP already holds,
            // so an ungranted mic sends the user through the normal Android prompt
            // first and the page can be asked again after.
            override fun onPermissionRequest(request: PermissionRequest) {
                val wantsMic = request.resources.contains(PermissionRequest.RESOURCE_AUDIO_CAPTURE)
                if (!wantsMic || !isOwnOrigin(request.origin.toString())) {
                    request.deny()
                    return
                }
                if (Permissions.hasMicrophone(this@MainActivity)) {
                    request.grant(arrayOf(PermissionRequest.RESOURCE_AUDIO_CAPTURE))
                } else {
                    request.deny()
                    micLauncher.launch(Manifest.permission.RECORD_AUDIO)
                }
            }

            override fun onShowFileChooser(
                view: WebView,
                cb: ValueCallback<Array<Uri>>,
                params: FileChooserParams,
            ): Boolean {
                fileChooserCallback?.onReceiveValue(null)
                fileChooserCallback = cb
                return try {
                    fileChooserLauncher.launch(params.createIntent())
                    true
                } catch (_: Exception) {
                    fileChooserCallback = null
                    false
                }
            }
        }

        // WebView never downloads a file on its own, so an in-app APK link (a car
        // app from /account) would silently do nothing. Fetch it and hand it to
        // the installer — same path as the self-updater.
        webView.setDownloadListener { url, _, _, _, _ -> downloadApk(url) }

        onBackPressedDispatcher.addCallback(this) {
            if (webView.canGoBack()) {
                webView.goBack()
            } else {
                isEnabled = false
                onBackPressedDispatcher.onBackPressed()
            }
        }

        handleIntent(intent, coldStart = true)
    }

    // WebKit 1.17.1's Java PSI lookup misses the compiled Kotlin override below.
    @SuppressLint("MissingOnRenderProcessGone")
    private inner class ShellClient : WebViewClient() {
        override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
            if (!request.isForMainFrame) return !isOwnOrigin(request.url.toString())
            if (isOwnOrigin(request.url.toString())) return false
            runCatching { startActivity(Intent(Intent.ACTION_VIEW, request.url)) }
            return true
        }

        override fun onRenderProcessGone(view: WebView, detail: RenderProcessGoneDetail): Boolean {
            Log.e("DIDA", "WebView renderer stopped; crashed=${detail.didCrash()}")
            view.destroy()
            recreate()
            return true
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleIntent(intent, coldStart = false)
    }

    /** Deep links (setup QR / onboard handoff) load in the WebView so the
     * one-time token is redeemed into THIS app's cookie jar — the app lands
     * signed-in on /onboard with no manual login. */
    private fun handleIntent(intent: Intent, coldStart: Boolean) {
        val link = intent.data
        val path = intent.getStringExtra("open_path")
        when {
            // The activity is exported, so any app can send it a URL. Only our own
            // auth and onboarding links are followed; the base URL changes only
            // through the in-app QR scan, never from an incoming intent.
            link != null && isOwnOrigin(link.toString()) &&
                (link.path == "/onboard" || link.path?.startsWith("/api/auth/link") == true) -> {
                nudgedSetup = true // don't clobber the token redeem
                webView.loadUrl(link.toString())
            }
            // A notification tap can name a target page (FcmService's open_path).
            path != null -> ownUrl(path)?.let { webView.loadUrl(it) }
            // Until location is provisioned, every cold start lands on the
            // onboarding walkthrough — installing the APK from the browser and
            // then opening the app by icon (no deep link, no session) must not
            // strand the person on a login screen with no path to setup. Proven
            // by the first family rollout: both phones did exactly that.
            coldStart && !Prefs.isProvisioned(this) ->
                webView.loadUrl(Prefs.baseUrl(this) + "/onboard")
            coldStart -> webView.loadUrl(Prefs.baseUrl(this) + "/")
        }
    }

    private fun installNativeBridge(): Boolean {
        if (WebViewFeature.isFeatureSupported(WebViewFeature.WEB_MESSAGE_LISTENER)) {
            val origin = requireNotNull(LocationIdentity.origin(Prefs.baseUrl(this)))
            val bridge = NativeBridge(this, origin)
            WebViewCompat.removeWebMessageListener(webView, "DidaNative")
            WebViewCompat.addWebMessageListener(webView, "DidaNative", setOf(origin)) { _, message, source, mainFrame, reply ->
                if (WebViewFeature.isFeatureSupported(WebViewFeature.WEB_MESSAGE_LISTENER)) {
                    bridge.dispatch(message.data ?: "", source.toString(), mainFrame)?.let { reply.postMessage(it) }
                }
            }
            return true
        }
        Toast.makeText(this, R.string.webview_outdated, Toast.LENGTH_LONG).show()
        finish()
        return false
    }

    fun isOwnOrigin(url: String?): Boolean {
        val u = url?.toUri() ?: return false
        val base = Prefs.baseUrl(this).toUri()
        return u.scheme == "https" && u.host != null && u.host == base.host && u.port == base.port
    }

    /** A same-origin absolute URL, or a site-relative path resolved against the base. */
    private fun ownUrl(target: String): String? = when {
        target.startsWith("/") && !target.startsWith("//") -> Prefs.baseUrl(this) + target
        isOwnOrigin(target) -> target
        else -> null
    }

    override fun onResume() {
        super.onResume()
        // Cheap idempotent re-arm — covers permission flips made in Settings.
        // Missing credentials silence start() exactly like a missing grant, and
        // provision() only ever ran from the walkthrough's launcher chain — so an
        // install signed in outside it stays dark forever. Retry on the session
        // cookie like FcmRegistrar; a missing grant then falls to the nudge below.
        if (Prefs.isProvisioned(this)) LocationEngine.start(this)
        if (!setupInProgress) provision(announce = false, expectedUserId = null)
        UpdateManager.maybeCheck(this)
        FcmRegistrar.ensure(this)  // (re)register the push token once signed in
        // A provisioned install without the background grant (reinstall that
        // skipped the walkthrough, OS-side revoke) reports no location and says
        // nothing — LocationEngine.start() above just returned. Steer to the
        // setup checklist once per launch instead of staying silently dark.
        if (!nudgedSetup && Prefs.isProvisioned(this) && !Permissions.hasBackgroundLocation(this)) {
            nudgedSetup = true
            webView.loadUrl(Prefs.baseUrl(this) + "/onboard")
        }
    }

    // --- Native setup walkthrough (kicked by the onboarding page's bridge call) ---

    fun startLocationSetup(fresh: Boolean = true) {
        if (setupInProgress) return
        setupInProgress = true
        if (fresh) settingsTried = false // a new user-initiated attempt may retry settings once
        if (!Permissions.hasFineLocation(this)) {
            finePermLauncher.launch(
                arrayOf(
                    Manifest.permission.ACCESS_FINE_LOCATION,
                    Manifest.permission.ACCESS_COARSE_LOCATION,
                )
            )
        } else {
            requestBackground()
        }
    }

    private fun requestBackground() {
        if (Build.VERSION.SDK_INT >= 29 && !Permissions.hasBackgroundLocation(this)) {
            // On API 30+ the OS routes this straight to the settings screen where
            // the user picks "Dopusti cijelo vrijeme".
            bgPermLauncher.launch(Manifest.permission.ACCESS_BACKGROUND_LOCATION)
        } else {
            requestBatteryExemption()
        }
    }

    @SuppressLint("BatteryLife")
    private fun requestBatteryExemption() {
        if (!Permissions.isBatteryExempt(this)) {
            try {
                batteryLauncher.launch(
                    Intent(
                        Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                        "package:$packageName".toUri(),
                    )
                )
                return
            } catch (_: Exception) {
                // Some OEMs hide the dialog — not fatal, continue the chain.
            }
        }
        requestNotifications()
    }

    private fun requestNotifications() {
        if (Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) !=
            PackageManager.PERMISSION_GRANTED
        ) {
            notifLauncher.launch(Manifest.permission.POST_NOTIFICATIONS)
        } else {
            provision()
        }
    }

    /** Download an APK the WebView asked to fetch (a car app from /account) and
     * launch the system installer — the FileProvider + REQUEST_INSTALL_PACKAGES
     * the self-updater already relies on. Same-origin, unauthenticated artifact. */
    private fun downloadApk(url: String) {
        Toast.makeText(this, R.string.dl_started, Toast.LENGTH_SHORT).show()
        lifecycleScope.launch {
            val ok = withContext(Dispatchers.IO) {
                try {
                    val name = url.substringAfterLast('/').substringBefore('?')
                        .ifEmpty { "download.apk" }
                    val apk = java.io.File(
                        java.io.File(cacheDir, "updates").apply { mkdirs() }, name
                    )
                    OwnTracksClient.http.newCall(Request.Builder().url(url).build())
                        .execute().use { resp ->
                            if (!resp.isSuccessful) return@withContext false
                            apk.outputStream().use { out -> resp.body.byteStream().copyTo(out) }
                        }
                    val uri = androidx.core.content.FileProvider.getUriForFile(
                        this@MainActivity, "${BuildConfig.APPLICATION_ID}.fileprovider", apk
                    )
                    withContext(Dispatchers.Main) {
                        startActivity(
                            Intent(Intent.ACTION_VIEW).apply {
                                setDataAndType(uri, "application/vnd.android.package-archive")
                                addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                            }
                        )
                    }
                    true
                } catch (e: Exception) {
                    false
                }
            }
            if (!ok) Toast.makeText(this@MainActivity, R.string.dl_failed, Toast.LENGTH_SHORT).show()
        }
    }

    fun syncIdentity(userId: String, origin: String) {
        val normalized = LocationIdentity.origin(origin) ?: return
        if (normalized != LocationIdentity.origin(Prefs.baseUrl(this))) return
        activeUserId = userId
        if (Prefs.identity(this) != LocationIdentity(userId, normalized)) {
            LocationEngine.stop(this)
            Prefs.clearCredentials(this)
            notifyNativeStatus()
        }
        if (!setupInProgress) provision(announce = false, expectedUserId = userId)
    }

    /** Exchange the WebView's session cookie for the endpoint-scoped location
     * credentials (`/api/me/mobile-config`), then arm geofences + FLP and post
     * the first fix. */
    private fun provision(announce: Boolean = true, expectedUserId: String? = activeUserId) {
        val base = Prefs.baseUrl(this)
        val cookie = CookieManager.getInstance().getCookie(base)
        if (!announce && cookie == null) return
        val revision = ++provisionRevision
        lifecycleScope.launch {
            val cfg = withContext(Dispatchers.IO) {
                try {
                    OwnTracksClient.http.newCall(
                        Request.Builder().url("$base/api/me/mobile-config")
                            .header("Cookie", cookie ?: "").build()
                    ).execute().use { response ->
                        if (!response.isSuccessful) {
                            if (response.code != 401) Log.w("DidaProvision", "Mobile provisioning HTTP ${response.code}")
                            return@use null
                        }
                        JSONObject(response.body.string())
                    }
                } catch (e: Exception) {
                    Log.w("DidaProvision", "Mobile provisioning failed", e)
                    null
                }
            }
            if (revision != provisionRevision || base != Prefs.baseUrl(this@MainActivity) ||
                cookie != CookieManager.getInstance().getCookie(base)) return@launch
            val ok = if (cfg != null && (expectedUserId == null || cfg.optString("user_id") == expectedUserId)) {
                try {
                    val userId = cfg.get("user_id").toString()
                    val username = cfg.getString("username")
                    val token = cfg.getString("token")
                    val postUrl = cfg.getString("url")
                    if (!Prefs.matchesCredentials(this@MainActivity, userId, username, token, postUrl)) {
                        LocationEngine.stop(this@MainActivity)
                    }
                    Prefs.setCredentials(applicationContext, userId, username, token, postUrl)
                    val locationRevision = requireNotNull(Prefs.revision(applicationContext))
                    cfg.optJSONArray("waypoints")?.let { GeofenceManager.sync(applicationContext, it, locationRevision) }
                    activeUserId = userId
                    true
                } catch (e: Exception) {
                    Log.w("DidaProvision", "Mobile configuration rejected", e)
                    false
                }
            } else false
            if (ok) {
                LocationEngine.start(applicationContext)
                LocationEngine.requestOneFix(applicationContext)
            }
            if (announce) {
                Toast.makeText(this@MainActivity, if (ok) R.string.setup_ok else R.string.setup_failed,
                    Toast.LENGTH_LONG).show()
                finishSetup()
            } else notifyNativeStatus()
        }
    }

    private fun finishSetup() {
        setupInProgress = false
        notifyNativeStatus()
    }

    private fun notifyNativeStatus() {
        // The page listens for this and re-reads DidaApp.status().
        webView.evaluateJavascript("window.dispatchEvent(new Event('dida-native-status'))", null)
    }

    // --- Self-update ---

    fun showUpdateDialog(meta: UpdateManager.ApkMeta) {
        AlertDialog.Builder(this)
            .setTitle(getString(R.string.update_title, meta.versionName))
            .setMessage(getString(R.string.update_body, BuildConfig.VERSION_NAME))
            .setPositiveButton(R.string.update_now) { _, _ ->
                Toast.makeText(this, R.string.update_downloading, Toast.LENGTH_SHORT).show()
                lifecycleScope.launch {
                    if (!UpdateManager.downloadAndInstall(this@MainActivity, meta)) {
                        Toast.makeText(
                            this@MainActivity, R.string.update_failed, Toast.LENGTH_LONG
                        ).show()
                    }
                }
            }
            .setNegativeButton(R.string.update_later, null)
            .show()
    }
}

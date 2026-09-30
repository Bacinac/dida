package biz.boskovic.dida

import android.content.Context
import android.util.Log
import android.webkit.CookieManager
import com.google.firebase.messaging.FirebaseMessaging
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.tasks.await
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject

/** Registers this install's FCM token with the server for the signed-in user.
 * Auth rides the WebView's session cookie (same as provisioning), so this only
 * lands once the app is logged in; called on every resume — cheap and
 * idempotent server-side (ON CONFLICT moves/refreshes the row). */
object FcmRegistrar {
    private const val TAG = "DidaFcm"
    private val JSON = "application/json; charset=utf-8".toMediaType()

    /** Fetch the current token and register it, if we have a session. */
    fun ensure(ctx: Context) {
        val app = ctx.applicationContext
        CoroutineScope(Dispatchers.IO).launch {
            try {
                val token = FirebaseMessaging.getInstance().token.await()
                register(app, token)
            } catch (e: Exception) {
                Log.w(TAG, "token fetch failed: ${e.message}")
            }
        }
    }

    fun register(ctx: Context, token: String) {
        val app = ctx.applicationContext
        val base = Prefs.baseUrl(app)
        val cookie = CookieManager.getInstance().getCookie(base) ?: return  // not signed in yet
        CoroutineScope(Dispatchers.IO).launch {
            try {
                val body = JSONObject().put("token", token).toString().toRequestBody(JSON)
                OwnTracksClient.http.newCall(
                    Request.Builder()
                        .url("$base/api/me/fcm-token")
                        .header("Cookie", cookie)
                        .post(body)
                        .build()
                ).execute().use { resp ->
                    if (!resp.isSuccessful) Log.w(TAG, "register HTTP ${resp.code}")
                }
            } catch (e: Exception) {
                Log.w(TAG, "register failed: ${e.message}")
            }
        }
    }
}

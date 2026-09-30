package biz.boskovic.dida.shared

import android.content.Context
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.Interceptor
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.TimeUnit

class BadCredentials : IOException("bad credentials")

/** HTTP core shared by the car apps: bearer car token, JSON in/out, throws
 * IOException on transport errors or non-2xx — callers surface, never retry. */
object CarHttp {
    val client: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(5, TimeUnit.SECONDS)
        .readTimeout(10, TimeUnit.SECONDS)
        .build()
    val jsonType = "application/json; charset=utf-8".toMediaType()
    val emptyPost = ByteArray(0).toRequestBody(null)

    /** Client whose every request carries the CURRENT token — for media
     * datasources that outlive a single call site (ExoPlayer, bitmap loader). */
    fun authedClient(ctx: Context): OkHttpClient {
        val app = ctx.applicationContext
        return client.newBuilder()
            .addInterceptor(Interceptor { chain ->
                val token = Prefs.token(app)
                chain.proceed(
                    if (token == null) chain.request()
                    else chain.request().newBuilder()
                        .header("Authorization", "Bearer $token")
                        .header("User-Agent", ua(app))
                        .build()
                )
            })
            .build()
    }

    fun ua(ctx: Context): String =
        "${ctx.getString(R.string.ua_label)}/${ctx.getString(R.string.app_ver)}"

    fun req(ctx: Context, path: String): Request.Builder {
        val token = Prefs.token(ctx) ?: throw IOException("not configured")
        return Request.Builder()
            .url(Prefs.baseUrl(ctx) + "/api" + path)
            .header("Authorization", "Bearer $token")
            .header("User-Agent", ua(ctx))
    }

    suspend fun fetch(r: Request): String = withContext(Dispatchers.IO) {
        client.newCall(r).execute().use { resp ->
            if (!resp.isSuccessful) throw IOException("HTTP ${resp.code}")
            resp.body.string()
        }
    }

    /** One-time pairing: interactive login → mint the long-lived car token.
     * The session cookie lives only for this exchange; the password is never stored. */
    suspend fun pair(base: String, username: String, password: String): String =
        withContext(Dispatchers.IO) {
            val origin = base.trimEnd('/')
            val login = Request.Builder()
                .url("$origin/api/auth/login")
                .post(
                    JSONObject().put("username", username).put("password", password)
                        .toString().toRequestBody(jsonType)
                )
                .build()
            val cookie = client.newCall(login).execute().use { resp ->
                if (resp.code == 401) throw BadCredentials()
                if (!resp.isSuccessful) throw IOException("HTTP ${resp.code}")
                resp.headers("Set-Cookie")
                    .firstOrNull { it.startsWith("dida_session=") }
                    ?.substringBefore(';')
                    ?: throw IOException("no session cookie")
            }
            val mint = Request.Builder()
                .url("$origin/api/me/car-token")
                .header("Cookie", cookie)
                .post(emptyPost)
                .build()
            client.newCall(mint).execute().use { resp ->
                if (!resp.isSuccessful) throw IOException("HTTP ${resp.code}")
                JSONObject(resp.body.string()).getString("token")
            }
        }
}

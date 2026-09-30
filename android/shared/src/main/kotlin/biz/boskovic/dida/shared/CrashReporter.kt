package biz.boskovic.dida.shared

import android.app.Application
import android.content.Context
import android.os.Build
import android.util.Log
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.File

/** Uncaught-exception reporter shared by all three DIDA apps.
 *
 * The dying process only WRITES: the handler serializes the trace to a private
 * file (synchronously — there is no later) and then defers to the previous
 * handler, so Android's own crash dialog and dropbox record still happen. The
 * NEXT start uploads the file to /api/app/crash-report, where it becomes an
 * ERROR row in app_logs — a phone crash shows up in the same log viewer as a
 * service crash. Auth differs per app (car token bearer vs WebView session
 * cookie), so the caller supplies the headers; null means "not signed in yet"
 * and the file simply waits for a start that is. */
object CrashReporter {
    private const val TAG = "DidaCrash"
    private const val FILE = "crash-report.json"

    fun install(app: Application, version: String) {
        val previous = Thread.getDefaultUncaughtExceptionHandler()
        Thread.setDefaultUncaughtExceptionHandler { thread, e ->
            try {
                write(app, version, thread, e)
            } catch (_: Throwable) {
                // The report must never eclipse the crash itself.
            }
            previous?.uncaughtException(thread, e)
        }
    }

    private fun write(ctx: Context, version: String, thread: Thread, e: Throwable) {
        val body = JSONObject()
            .put("app", ctx.packageName)
            .put("version", version)
            .put("thread", thread.name)
            .put("stack", Log.getStackTraceString(e).take(8000))
            .put("occurred_at_ms", System.currentTimeMillis())
            .put("device", "${Build.MANUFACTURER} ${Build.MODEL} / Android ${Build.VERSION.RELEASE}")
        File(ctx.filesDir, FILE).writeText(body.toString())
    }

    /** Upload the pending report, if any. Call once from Application.onCreate.
     * Deleted on acceptance and on permanent rejection; kept (and retried next
     * start) across transport errors, server errors and missing auth. */
    fun flush(ctx: Context, baseUrl: String, ua: String, auth: () -> Pair<String, String>?) {
        val app = ctx.applicationContext
        val file = File(app.filesDir, FILE)
        if (!file.exists()) return
        val (header, value) = auth() ?: return
        Thread {
            try {
                val req = Request.Builder()
                    .url(baseUrl.trimEnd('/') + "/api/app/crash-report")
                    .header(header, value)
                    .header("User-Agent", ua)
                    .post(file.readText().toRequestBody(CarHttp.jsonType))
                    .build()
                CarHttp.client.newCall(req).execute().use { resp ->
                    // 401 stays: a signed-out report may still land after the next
                    // pairing. Other 4xx never will — a malformed file must not
                    // re-upload forever.
                    if (resp.isSuccessful || (resp.code in 400..499 && resp.code != 401)) {
                        file.delete()
                    }
                    if (!resp.isSuccessful) Log.w(TAG, "upload HTTP ${resp.code}")
                }
            } catch (e: Exception) {
                Log.w(TAG, "upload failed: ${e.message}")
            }
        }.start()
    }
}

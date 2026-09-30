package biz.boskovic.dida

import android.app.Activity
import android.content.Context
import android.widget.Toast
import android.content.Intent
import android.util.Log
import androidx.core.content.FileProvider
import java.io.File
import java.security.MessageDigest
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.Request
import org.json.JSONObject

/** Self-update against our own origin: `/api/app/apk.json` carries the served
 * APK's versionCode; when it is newer than the running build, one tap downloads
 * the APK (sha256-verified) and hands it to the system installer. Silent
 * zero-tap installs don't exist outside device-owner MDM — this is the floor. */
object UpdateManager {
    private const val TAG = "DidaUpdate"
    // Check on every resume (an apk.json GET is cheap) so a newer build is
    // offered promptly — a long throttle stranded phones on a stale version
    // during active development. The dialog only appears when there IS something
    // newer, so this never nags on a current install.
    fun maybeCheck(activity: MainActivity) = check(activity, force = false)

    data class ApkMeta(val versionCode: Int, val versionName: String, val sha256: String?)

    /** `force` = user tapped "Update now" — always fetch and, if there is nothing
     *  newer, say so instead of silently doing nothing. */
    fun check(activity: MainActivity, force: Boolean) {
        val ctx = activity.applicationContext
        CoroutineScope(Dispatchers.IO).launch {
            val meta = fetchMeta(ctx)
            withContext(Dispatchers.Main) {
                if (activity.isFinishing) return@withContext
                when {
                    meta != null && meta.versionCode > BuildConfig.VERSION_CODE ->
                        activity.showUpdateDialog(meta)
                    force && meta != null ->
                        Toast.makeText(activity, R.string.update_current, Toast.LENGTH_SHORT).show()
                    force ->
                        Toast.makeText(activity, R.string.update_check_failed, Toast.LENGTH_SHORT).show()
                }
            }
        }
    }

    private fun fetchMeta(ctx: Context): ApkMeta? = try {
        OwnTracksClient.http.newCall(
            Request.Builder().url(Prefs.baseUrl(ctx) + "/api/app/apk.json").build()
        ).execute().use { resp ->
            if (!resp.isSuccessful) {
                null
            } else {
                val o = JSONObject(resp.body.string())
                ApkMeta(
                    o.optInt("versionCode"),
                    o.optString("versionName"),
                    o.optString("sha256").ifEmpty { null },
                )
            }
        }
    } catch (e: Exception) {
        Log.w(TAG, "apk.json check failed: ${e.message}")
        null
    }

    /** Download to app cache, verify the digest, launch the installer. */
    suspend fun downloadAndInstall(activity: Activity, meta: ApkMeta): Boolean =
        withContext(Dispatchers.IO) {
            val ctx = activity.applicationContext
            val apk = File(File(ctx.cacheDir, "updates").apply { mkdirs() }, "dida.apk")
            try {
                OwnTracksClient.http.newCall(
                    // ?v= splits edge-cache keys per version — the stable URL is
                    // CDN-cached and stale bytes would fail the sha256 check.
                    Request.Builder()
                        .url(Prefs.baseUrl(ctx) + "/api/app/dida.apk?v=${meta.versionCode}")
                        .build()
                ).execute().use { resp ->
                    if (!resp.isSuccessful) return@withContext false
                    apk.outputStream().use { out -> resp.body.byteStream().copyTo(out) }
                }
                if (meta.sha256 != null && sha256(apk) != meta.sha256.lowercase()) {
                    Log.w(TAG, "downloaded APK digest mismatch — discarding")
                    apk.delete()
                    return@withContext false
                }
                val uri = FileProvider.getUriForFile(
                    ctx, "${BuildConfig.APPLICATION_ID}.fileprovider", apk
                )
                withContext(Dispatchers.Main) {
                    activity.startActivity(
                        Intent(Intent.ACTION_VIEW).apply {
                            setDataAndType(uri, "application/vnd.android.package-archive")
                            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                        }
                    )
                }
                true
            } catch (e: Exception) {
                Log.w(TAG, "update download failed: ${e.message}")
                false
            }
        }

    private fun sha256(f: File): String {
        val md = MessageDigest.getInstance("SHA-256")
        f.inputStream().use { ins ->
            val buf = ByteArray(64 * 1024)
            while (true) {
                val n = ins.read(buf)
                if (n < 0) break
                md.update(buf, 0, n)
            }
        }
        return md.digest().joinToString("") { "%02x".format(it) }
    }
}

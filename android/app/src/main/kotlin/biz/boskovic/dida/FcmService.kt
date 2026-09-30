package biz.boskovic.dida

import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import androidx.core.app.NotificationCompat
import com.google.firebase.messaging.FirebaseMessagingService
import com.google.firebase.messaging.RemoteMessage
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import java.net.HttpURLConnection
import java.net.URL

/** Receives DIDA notifications over FCM (Web Push has no WebView support). A new
 * token is registered with the server; an incoming message becomes a native
 * notification on the high-importance alerts channel, tapping which opens the
 * app. Payloads are data-only from our server (title/message[/image][/url]) so
 * we fully control how they render whether the app is foreground or not. */
class FcmService : FirebaseMessagingService() {

    override fun onNewToken(token: String) {
        FcmRegistrar.register(applicationContext, token)
    }

    override fun onMessageReceived(message: RemoteMessage) {
        val data = message.data
        if (data["type"] == WAKE_LOCATION) {
            wakeLocation()
            return
        }
        val title = data["title"] ?: message.notification?.title ?: getString(R.string.app_name)
        val body = data["message"] ?: message.notification?.body ?: ""
        val open = Intent(this, MainActivity::class.java).apply {
            addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP)
            data["url"]?.let { putExtra("open_path", it) }
        }
        val pending = PendingIntent.getActivity(
            this, 0, open,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val builder = NotificationCompat.Builder(this, App.CHANNEL_ALERTS)
            .setSmallIcon(R.mipmap.ic_launcher)
            .setContentTitle(title)
            .setContentText(body)
            .setStyle(NotificationCompat.BigTextStyle().bigText(body))
            .setAutoCancel(true)
            .setContentIntent(pending)
        // A camera frame minted for this notification (an expiring, sessionless
        // link). Losing it must never cost the notification itself.
        data["image"]?.let { fetchBitmap(it) }?.let { frame ->
            builder.setLargeIcon(frame)
                .setStyle(
                    NotificationCompat.BigPictureStyle()
                        .bigPicture(frame)
                        .bigLargeIcon(null as Bitmap?)
                        .setSummaryText(body)
                )
        }
        getSystemService(NotificationManager::class.java)
            .notify(title.hashCode() xor body.hashCode(), builder.build())
    }

    /** Arm background reporting on a phone whose engine is not running.
     *
     * The engine starts only from onResume or a reboot, so a phone that stopped
     * reporting cannot recover on its own — and nobody can open an app that is
     * in someone else's pocket in another country. This is the one way in.
     * Silent by design: it asks the phone to do something, it does not tell its
     * owner anything.
     *
     * The outcome goes back to the server either way. A phone that will not arm
     * and a phone that never received this look the same from the server, and
     * guessing between them is what cost weeks. */
    private fun wakeLocation() {
        val ctx = applicationContext
        val outcome = when {
            // No credentials, so not even the report below can authenticate.
            // MainActivity.onResume re-provisions on the next open.
            !Prefs.isProvisioned(ctx) -> "unprovisioned"
            !Permissions.hasBackgroundLocation(ctx) -> "no-background-grant"
            else -> {
                LocationEngine.start(ctx)
                LocationEngine.requestOneFix(ctx)
                "armed"
            }
        }
        CoroutineScope(Dispatchers.IO).launch {
            OwnTracksClient.postEngineStatus(ctx, outcome)
        }
    }

    // onMessageReceived runs on a background thread with a ~10 s budget, so the
    // frame gets 4 s. The notification goes up without it rather than not at all,
    // which is exactly why a lost frame has to be reported: on the phone it just
    // looks like a notification that never had one.
    private fun fetchBitmap(url: String): Bitmap? {
        val failure = try {
            val conn = URL(url).openConnection() as HttpURLConnection
            conn.connectTimeout = 4_000
            conn.readTimeout = 4_000
            if (conn.responseCode != HttpURLConnection.HTTP_OK) {
                "HTTP ${conn.responseCode}"
            } else {
                conn.inputStream.use { BitmapFactory.decodeStream(it) }?.let { return it }
                    ?: "frame did not decode"
            }
        } catch (e: Exception) {
            "${e.javaClass.simpleName}: ${e.message}"
        }
        val ctx = applicationContext
        CoroutineScope(Dispatchers.IO).launch {
            OwnTracksClient.postPushImageLost(ctx, failure)
        }
        return null
    }

    companion object {
        /** Data key the notify adapter's wake push carries. */
        const val WAKE_LOCATION = "wake_location"
    }
}

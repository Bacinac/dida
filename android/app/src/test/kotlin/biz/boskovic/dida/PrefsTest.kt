package biz.boskovic.dida

import android.app.Application
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [26, 28, 36], application = Application::class)
class PrefsTest {
    @Test
    fun accountChangeRejectsPendingLocationAndOldGeofences() {
        val ctx = RuntimeEnvironment.getApplication()
        Prefs.setBaseUrl(ctx, "https://dida.example")
        Prefs.setCredentials(ctx, "A", "alice", "token-a", "https://dida.example/api/owntracks/A")
        val first = requireNotNull(Prefs.revision(ctx))
        assertNotNull(Prefs.stamp(ctx, System.currentTimeMillis() + 1, first))
        Prefs.setCredentials(ctx, "B", "bob", "token-b", "https://dida.example/api/owntracks/B")
        assertNotEquals(first, Prefs.revision(ctx))
        assertNull(Prefs.stamp(ctx, System.currentTimeMillis() + 1, first))
        assertFalse(Prefs.setWaypoints(ctx, org.json.JSONArray(), "old-geofences", first))
        Prefs.clearCredentials(ctx)
        assertFalse(Prefs.isProvisioned(ctx))
        assertNull(Prefs.stamp(ctx, System.currentTimeMillis() + 1, first))
    }

    @Test
    fun serverChangeClearsCredentialsAndForeignReceiverIsRejected() {
        val ctx = RuntimeEnvironment.getApplication()
        Prefs.setBaseUrl(ctx, "https://dida.example")
        Prefs.setCredentials(ctx, "A", "alice", "token", "https://dida.example/api/owntracks/A")
        Prefs.setBaseUrl(ctx, "https://other.example")
        assertFalse(Prefs.isProvisioned(ctx))
        assertThrows(IllegalArgumentException::class.java) {
            Prefs.setCredentials(ctx, "A", "alice", "token", "https://dida.example/api/owntracks/A")
        }
    }
}

package biz.boskovic.dida

import android.app.Application
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [26, 36], application = Application::class)
class NativeBridgeTest {
    @Test
    fun rejectsForeignAndNestedFramesBeforeDispatch() {
        val activity = Robolectric.buildActivity(MainActivity::class.java).get()
        var currentOrigin = "https://dida.example"
        val bridge = NativeBridge(activity, currentOrigin) { currentOrigin }
        for (source in listOf("https://attacker.example", "http://dida.example", "null", "file:///private")) {
            assertNull(bridge.dispatch("{\"id\":1,\"method\":\"appVersion\"}", source, true))
        }
        assertNull(bridge.dispatch("{\"id\":1,\"method\":\"status\"}", "https://dida.example", false))
        val response = JSONObject(requireNotNull(bridge.dispatch("{\"id\":7,\"method\":\"appVersion\"}", "https://dida.example", true)))
        assertEquals(7, response.getInt("id"))
        assertEquals(BuildConfig.VERSION_NAME, response.getString("result"))
        val failed = JSONObject(requireNotNull(bridge.dispatch("{\"id\":8,\"method\":\"unknown\"}", "https://dida.example", true)))
        assertTrue(failed.has("error"))
        currentOrigin = "https://other.example"
        assertNull(bridge.dispatch("{\"id\":1,\"method\":\"status\"}", "https://dida.example", true))
    }
}

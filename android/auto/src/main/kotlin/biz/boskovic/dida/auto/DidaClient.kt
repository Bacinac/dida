package biz.boskovic.dida.auto

import android.content.Context
import biz.boskovic.dida.shared.CarHttp
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject

data class Zone(val name: String, val lat: Double, val lon: Double, val isHome: Boolean)

/** DIDA API surface the car needs, on the shared bearer HTTP core. */
object DidaClient {
    /** Is the car gate (entry slot "car") configured? Gates the one button. */
    suspend fun carGateConfigured(ctx: Context): Boolean {
        val cfg = JSONObject(CarHttp.fetch(CarHttp.req(ctx, "/entry/config").build()))
        val slots = cfg.optJSONArray("slots") ?: return false
        return (0 until slots.length()).any { slots.getJSONObject(it).optString("slot") == "car" }
    }

    /** Open the car gate — server enforces the same per-user control boundary as
     * /command. The one command the car actually needs. */
    suspend fun openCarGate(ctx: Context) {
        val body = JSONObject().put("slot", "car")
        CarHttp.fetch(
            CarHttp.req(ctx, "/entry/action")
                .post(body.toString().toRequestBody(CarHttp.jsonType))
                .build()
        )
    }

    /** DIDA geofence zones — navigation destinations. Home first (server order). */
    suspend fun zones(ctx: Context): List<Zone> {
        val arr = JSONArray(CarHttp.fetch(CarHttp.req(ctx, "/zones").build()))
        return (0 until arr.length()).map { i ->
            val o = arr.getJSONObject(i)
            Zone(
                o.getString("name"),
                o.getDouble("latitude"),
                o.getDouble("longitude"),
                o.optBoolean("is_home"),
            )
        }
    }
}

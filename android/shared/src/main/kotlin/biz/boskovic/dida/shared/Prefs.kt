package biz.boskovic.dida.shared

import android.content.Context
import android.content.SharedPreferences
import androidx.core.content.edit

/** Server origin + the long-lived car token minted by /api/me/car-token during
 * setup. The token is revoked server-side (token_version bump), so plain
 * SharedPreferences is the right weight — same reasoning as the phone app. */
object Prefs {
    private fun sp(ctx: Context): SharedPreferences =
        ctx.getSharedPreferences("dida_auto", Context.MODE_PRIVATE)

    fun baseUrl(ctx: Context): String =
        sp(ctx).getString("base_url", null) ?: ctx.getString(R.string.default_base_url)

    fun token(ctx: Context): String? = sp(ctx).getString("token", null)

    fun configured(ctx: Context): Boolean = token(ctx) != null

    fun save(ctx: Context, baseUrl: String, token: String) = sp(ctx).edit {
        putString("base_url", baseUrl.trimEnd('/'))
        putString("token", token)
    }
}

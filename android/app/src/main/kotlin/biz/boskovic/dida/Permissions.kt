package biz.boskovic.dida

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import android.os.PowerManager
import androidx.core.content.ContextCompat

object Permissions {
    fun hasFineLocation(ctx: Context): Boolean =
        ContextCompat.checkSelfPermission(ctx, Manifest.permission.ACCESS_FINE_LOCATION) ==
            PackageManager.PERMISSION_GRANTED

    /** "Dopusti cijelo vrijeme" — required for geofence edges and background FLP
     * delivery; without it the app is exactly as blind as a closed browser tab. */
    fun hasBackgroundLocation(ctx: Context): Boolean =
        hasFineLocation(ctx) && (Build.VERSION.SDK_INT < 29 ||
            ContextCompat.checkSelfPermission(ctx, Manifest.permission.ACCESS_BACKGROUND_LOCATION) ==
            PackageManager.PERMISSION_GRANTED)

    /** Push-to-talk in the assistant. The WebView can only pass on a grant the app
     * itself holds, so this gates onPermissionRequest. */
    fun hasMicrophone(ctx: Context): Boolean =
        ContextCompat.checkSelfPermission(ctx, Manifest.permission.RECORD_AUDIO) ==
            PackageManager.PERMISSION_GRANTED

    /** OEM battery managers (Samsung/Xiaomi) kill background reporters that are
     * not exempted; the setup walkthrough requests this explicitly. */
    fun isBatteryExempt(ctx: Context): Boolean =
        (ctx.getSystemService(Context.POWER_SERVICE) as PowerManager)
            .isIgnoringBatteryOptimizations(ctx.packageName)
}

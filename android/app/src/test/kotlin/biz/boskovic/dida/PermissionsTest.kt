package biz.boskovic.dida

import android.Manifest
import android.app.Application
import android.content.Intent
import android.os.Build
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [26, 28, 29, 36], application = Application::class)
class PermissionsTest {
    @Test
    fun bootAndPackageUpdateDoNotRearmWithoutLocationPermission() {
        val ctx = RuntimeEnvironment.getApplication()
        shadowOf(ctx).denyPermissions(Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_BACKGROUND_LOCATION)
        Prefs.setBaseUrl(ctx, "https://dida.example")
        Prefs.setCredentials(ctx, "A", "alice", "token", "https://dida.example/api/owntracks/A")
        BootReceiver().onReceive(ctx, Intent(Intent.ACTION_BOOT_COMPLETED))
        BootReceiver().onReceive(ctx, Intent(Intent.ACTION_MY_PACKAGE_REPLACED))
        assertFalse(Permissions.hasBackgroundLocation(ctx))
    }

    @Test
    fun grantAndRevokeLocation() {
        val ctx = RuntimeEnvironment.getApplication()
        val app = shadowOf(ctx)
        app.denyPermissions(Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_BACKGROUND_LOCATION)
        assertFalse(Permissions.hasBackgroundLocation(ctx))
        app.grantPermissions(Manifest.permission.ACCESS_COARSE_LOCATION)
        assertFalse(Permissions.hasBackgroundLocation(ctx))
        app.grantPermissions(Manifest.permission.ACCESS_FINE_LOCATION)
        assertTrue(Permissions.hasFineLocation(ctx))
        if (Build.VERSION.SDK_INT >= 29) {
            assertFalse(Permissions.hasBackgroundLocation(ctx))
            app.grantPermissions(Manifest.permission.ACCESS_BACKGROUND_LOCATION)
        }
        assertTrue(Permissions.hasBackgroundLocation(ctx))
        app.denyPermissions(Manifest.permission.ACCESS_FINE_LOCATION)
        assertFalse(Permissions.hasBackgroundLocation(ctx))
    }
}

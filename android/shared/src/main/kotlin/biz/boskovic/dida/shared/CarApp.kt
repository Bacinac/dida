package biz.boskovic.dida.shared

import android.app.Application

/** Application base for both car apps: arms the crash reporter before any
 * service or screen exists (a crash during CarAppService binding is exactly
 * the kind worth catching) and uploads whatever the previous run left. */
class CarApp : Application() {
    override fun onCreate() {
        super.onCreate()
        CrashReporter.install(this, getString(R.string.app_ver))
        CrashReporter.flush(this, Prefs.baseUrl(this), CarHttp.ua(this)) {
            Prefs.token(this)?.let { "Authorization" to "Bearer $it" }
        }
    }
}

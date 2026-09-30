package biz.boskovic.dida.shared

import android.os.Bundle
import android.widget.Button
import android.widget.EditText
import android.widget.TextView
import androidx.activity.ComponentActivity
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.launch

/** One-time pairing UI shared by the car apps: DIDA login → long-lived car
 * token into Prefs. After this the car screen works with no phone interaction. */
class SetupActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_setup)

        findViewById<TextView>(R.id.title).text = getString(R.string.app_name)
        val server = findViewById<EditText>(R.id.server)
        val username = findViewById<EditText>(R.id.username)
        val password = findViewById<EditText>(R.id.password)
        val connect = findViewById<Button>(R.id.connect)
        val status = findViewById<TextView>(R.id.status)

        server.setText(Prefs.baseUrl(this))
        if (Prefs.configured(this)) {
            status.setText(R.string.setup_connected)
        }

        connect.setOnClickListener {
            val base = server.text.toString().trim()
            connect.isEnabled = false
            status.text = ""
            lifecycleScope.launch {
                try {
                    val token = CarHttp.pair(
                        base, username.text.toString().trim(), password.text.toString()
                    )
                    Prefs.save(this@SetupActivity, base, token)
                    password.text.clear()
                    status.setText(R.string.setup_connected)
                } catch (e: BadCredentials) {
                    status.setText(R.string.setup_bad_credentials)
                } catch (e: Exception) {
                    status.setText(R.string.setup_failed)
                }
                connect.isEnabled = true
            }
        }
    }
}

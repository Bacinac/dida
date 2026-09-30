package biz.boskovic.dida.auto

import androidx.car.app.CarContext
import androidx.car.app.CarToast
import androidx.car.app.Screen
import androidx.car.app.model.Action
import androidx.car.app.model.ActionStrip
import androidx.car.app.model.CarIcon
import androidx.car.app.model.ItemList
import androidx.car.app.model.ListTemplate
import androidx.car.app.model.MessageTemplate
import androidx.car.app.model.Row
import androidx.car.app.model.Template
import biz.boskovic.dida.shared.Prefs
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.launch

/** One screen, one command: open the car gate. Navigation to a DIDA zone lives
 * behind the header button. Nothing else — a car needs the gate open, not the
 * house. */
class MainScreen(carContext: CarContext) : Screen(carContext) {
    private var configured = false
    private var loading = true
    private var error = false
    private var opening = false

    init {
        lifecycleScope.launch {
            if (Prefs.configured(carContext)) {
                try {
                    configured = DidaClient.carGateConfigured(carContext)
                } catch (e: Exception) {
                    error = true
                }
            }
            loading = false
            invalidate()
        }
    }

    private fun openGate() {
        opening = true
        invalidate()
        lifecycleScope.launch {
            try {
                DidaClient.openCarGate(carContext)
                toast(carContext.getString(R.string.car_gate_opened))
            } catch (e: Exception) {
                toast(carContext.getString(R.string.car_command_failed))
            }
            opening = false
            invalidate()
        }
    }

    private fun toast(msg: String) =
        CarToast.makeText(carContext, msg, CarToast.LENGTH_SHORT).show()

    override fun onGetTemplate(): Template {
        val navStrip = ActionStrip.Builder()
            .addAction(
                Action.Builder()
                    .setTitle(carContext.getString(R.string.car_nav))
                    .setOnClickListener { screenManager.push(NavScreen(carContext)) }
                    .build()
            )
            .build()

        if (!Prefs.configured(carContext)) {
            return MessageTemplate.Builder(carContext.getString(R.string.car_not_configured))
                .setTitle(carContext.getString(R.string.app_name))
                .setIcon(CarIcon.APP_ICON)
                .build()
        }
        if (loading) {
            return ListTemplate.Builder()
                .setTitle(carContext.getString(R.string.app_name))
                .setHeaderAction(Action.APP_ICON)
                .setLoading(true)
                .build()
        }
        if (error || !configured) {
            return MessageTemplate.Builder(
                carContext.getString(if (error) R.string.car_error else R.string.car_no_gate)
            )
                .setTitle(carContext.getString(R.string.app_name))
                .setIcon(if (error) CarIcon.ERROR else CarIcon.APP_ICON)
                .setActionStrip(navStrip)
                .build()
        }

        val row = Row.Builder()
            .setTitle(carContext.getString(R.string.car_open_gate))
            .setOnClickListener { if (!opening) openGate() }
            .build()
        return ListTemplate.Builder()
            .setTitle(carContext.getString(R.string.app_name))
            .setHeaderAction(Action.APP_ICON)
            .setActionStrip(navStrip)
            .setSingleList(ItemList.Builder().addItem(row).build())
            .build()
    }
}

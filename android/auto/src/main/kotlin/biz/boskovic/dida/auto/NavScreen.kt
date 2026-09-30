package biz.boskovic.dida.auto

import android.content.Intent
import androidx.car.app.CarContext
import androidx.car.app.CarToast
import androidx.car.app.Screen
import androidx.car.app.model.Action
import androidx.car.app.model.CarIcon
import androidx.car.app.model.ItemList
import androidx.car.app.model.ListTemplate
import androidx.car.app.model.MessageTemplate
import androidx.core.net.toUri
import androidx.car.app.model.Row
import androidx.car.app.model.Template
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.launch

/** DIDA zones as navigation destinations: a tap hands the coordinates to the
 * car's default navigation app (ACTION_NAVIGATE). */
class NavScreen(carContext: CarContext) : Screen(carContext) {
    private var zones: List<Zone> = emptyList()
    private var loading = true
    private var error = false

    init {
        lifecycleScope.launch {
            try {
                zones = DidaClient.zones(carContext)
            } catch (e: Exception) {
                error = true
            }
            loading = false
            invalidate()
        }
    }

    private fun navigateTo(zone: Zone) {
        try {
            carContext.startCarApp(
                Intent(CarContext.ACTION_NAVIGATE, "geo:${zone.lat},${zone.lon}".toUri())
            )
        } catch (e: Exception) {
            CarToast.makeText(
                carContext, carContext.getString(R.string.car_command_failed), CarToast.LENGTH_SHORT
            ).show()
        }
    }

    override fun onGetTemplate(): Template {
        if (error || (!loading && zones.isEmpty())) {
            return MessageTemplate.Builder(
                carContext.getString(if (error) R.string.car_error else R.string.car_nav_empty)
            )
                .setTitle(carContext.getString(R.string.car_nav))
                .setIcon(if (error) CarIcon.ERROR else CarIcon.APP_ICON)
                .setHeaderAction(Action.BACK)
                .build()
        }
        val builder = ListTemplate.Builder()
            .setTitle(carContext.getString(R.string.car_nav))
            .setHeaderAction(Action.BACK)
        if (loading) return builder.setLoading(true).build()

        val list = ItemList.Builder()
        zones.forEach { zone ->
            val row = Row.Builder()
                .setTitle(zone.name)
                .setOnClickListener { navigateTo(zone) }
            if (zone.isHome) row.addText(carContext.getString(R.string.car_nav_home))
            list.addItem(row.build())
        }
        return builder.setSingleList(list.build()).build()
    }
}

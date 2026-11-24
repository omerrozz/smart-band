package band.app

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.net.Uri
import android.os.Build
import android.os.PowerManager
import android.provider.Settings
import android.util.Log
import android.view.View
import android.widget.Button
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.Switch
import android.widget.TextView

/**
 * "Telefon ayarları": arka planda çalışma, pil, izinler, durdurma. Yerel (WebView dışı) bir paneldir;
 * sayfadaki Saat ekranına yalnızca uygulamanın içinde eklenen satırdan açılır.
 */
class SettingsPanel(
    private val activity: Activity,
    private val onPickApps: () -> Unit,
    private val onPickPlayer: () -> Unit,
    private val onClose: () -> Unit,
) {

    private val ink = Color.parseColor("#f1efff")
    private val mut = Color.parseColor("#b3afd8")
    private val good = Color.parseColor("#7ad9a5")
    private val warn = Color.parseColor("#ffb740")

    private fun dp(v: Int) = (v * activity.resources.displayMetrics.density).toInt()
    private fun s(id: Int, vararg args: Any?) = L10n.str(activity, id, *args)
    private fun label(text: String, size: Float = 14f, color: Int = mut) = TextView(activity).apply {
        this.text = text; textSize = size; setTextColor(color)
    }

    private val batteryStatus = label("")
    private val batteryButton = Button(activity)
    private val notifyStatus = label("")
    private val notifyButton = Button(activity)
    private val btStatus = label("")
    private val btButton = Button(activity)
    @SuppressLint("UseSwitchCompatOrMaterialCode")
    private val bootSwitch = Switch(activity)
    private val accessStatus = label("")
    private val accessButton = Button(activity)
    private val appsButton = Button(activity)
    private val playerButton = Button(activity)
    private val phoneSwitches = LinkedHashMap<String, Switch>()

    val view: View

    init {
        val box = LinearLayout(activity).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(24), dp(24), dp(24), dp(32))
        }
        fun add(v: View, top: Int = 6, wrap: Boolean = false) =
            box.addView(v, LinearLayout.LayoutParams(if (wrap) -2 else -1, -2).apply { topMargin = dp(top) })
        fun heading(text: String) = add(label(text, 17f, ink), 26)

        add(Button(activity).apply { text = s(R.string.back); setOnClickListener { onClose() } }, 0, wrap = true)
        add(label(s(R.string.settings_title), 24f, ink), 8)
        add(label(s(R.string.settings_intro)))

        heading(s(R.string.battery_heading))
        add(batteryStatus)
        add(batteryButton.apply {
            text = s(R.string.battery_exempt_button)
            setOnClickListener { requestBatteryExemption() }
        }, wrap = true)

        heading(s(R.string.samsung_heading))
        add(label(s(R.string.samsung_text)))
        add(Button(activity).apply {
            text = s(R.string.open_app_settings)
            setOnClickListener { openAppSettings() }
        }, wrap = true)

        heading(s(R.string.startup_heading))
        add(bootSwitch.apply {
            text = s(R.string.boot_switch)
            setTextColor(ink)
            textSize = 15f
            setOnCheckedChangeListener { _, on -> Prefs.setBootStart(activity, on) }
        })
        add(label(s(R.string.boot_note)))

        heading(s(R.string.phone_heading))
        add(label(s(R.string.access_intro)))
        add(accessStatus, 10)
        add(accessButton.apply {
            text = s(R.string.access_open)
            setOnClickListener { openNotificationAccess(activity) }
        }, wrap = true)
        fun toggle(key: String, title: String, note: String) {
            @SuppressLint("UseSwitchCompatOrMaterialCode")
            val sw = Switch(activity).apply {
                text = title
                setTextColor(ink)
                textSize = 15f
                setOnCheckedChangeListener { _, on ->
                    if (PhonePrefs.get(activity, key) != on) {
                        PhonePrefs.set(activity, key, on)
                        if (key == PhonePrefs.MUSIC) NotifyService.refresh()
                    }
                }
            }
            phoneSwitches[key] = sw
            add(sw, 12)
            add(label(note), 0)
        }
        toggle(PhonePrefs.NOTIFICATIONS, s(R.string.toggle_notifications), s(R.string.toggle_notifications_note))
        add(appsButton.apply { setOnClickListener { onPickApps() } }, wrap = true)
        toggle(PhonePrefs.SCREEN, s(R.string.toggle_screen), s(R.string.toggle_screen_note))
        toggle(PhonePrefs.SILENT, s(R.string.toggle_silent), s(R.string.toggle_silent_note))
        toggle(PhonePrefs.DND, s(R.string.toggle_dnd), s(R.string.toggle_dnd_note))
        toggle(PhonePrefs.CALLS, s(R.string.toggle_calls), s(R.string.toggle_calls_note))
        toggle(PhonePrefs.MUSIC, s(R.string.toggle_music), s(R.string.toggle_music_note))
        add(playerButton.apply { setOnClickListener { onPickPlayer() } }, wrap = true)

        heading(s(R.string.permissions_heading))
        add(notifyStatus)
        add(notifyButton.apply { text = s(R.string.notify_permission_button); setOnClickListener { requestNotifications() } }, wrap = true)
        add(btStatus, 14)
        add(btButton.apply { text = s(R.string.bt_permission_button); setOnClickListener { requestBluetooth() } }, wrap = true)

        heading(s(R.string.stop))
        add(label(s(R.string.stop_text)))
        add(Button(activity).apply {
            text = s(R.string.stop_app)
            setOnClickListener { confirmStop() }
        }, wrap = true)

        view = ScrollView(activity).apply {
            setBackgroundColor(activity.getColor(R.color.bg))
            isClickable = true  // dokunuşlar alttaki sayfaya geçmesin
            addView(box)
        }
        refresh()
    }

    private fun granted(p: String) = activity.checkSelfPermission(p) == PackageManager.PERMISSION_GRANTED

    /** Durum satırlarını günceller; panel açılınca ve ayarlardan dönülünce çağrılır. */
    fun refresh() {
        val exempt = activity.getSystemService(PowerManager::class.java)
            .isIgnoringBatteryOptimizations(activity.packageName)
        batteryStatus.text = s(if (exempt) R.string.battery_exempt else R.string.battery_not_exempt)
        batteryStatus.setTextColor(if (exempt) good else warn)
        batteryButton.visibility = if (exempt) View.GONE else View.VISIBLE

        val notify = Build.VERSION.SDK_INT < 33 || granted(Manifest.permission.POST_NOTIFICATIONS)
        notifyStatus.text = s(if (notify) R.string.notify_permission_ok else R.string.notify_permission_missing)
        notifyStatus.setTextColor(if (notify) good else warn)
        notifyButton.visibility = if (notify) View.GONE else View.VISIBLE

        val bt = BandService.bluetoothGranted(activity)
        btStatus.text = s(if (bt) R.string.bt_permission_ok else R.string.bt_permission_missing)
        btStatus.setTextColor(if (bt) good else warn)
        btButton.visibility = if (bt) View.GONE else View.VISIBLE

        bootSwitch.isChecked = Prefs.bootStart(activity)

        val access = NotifyService.accessGranted(activity)
        accessStatus.text = s(if (access) R.string.access_granted else R.string.access_missing)
        accessStatus.setTextColor(if (access) good else warn)
        accessButton.text = s(if (access) R.string.access_manage else R.string.access_open)
        for ((key, sw) in phoneSwitches) sw.isChecked = PhonePrefs.get(activity, key)
        appsButton.text = s(R.string.apps_button, PhonePrefs.apps(activity).size)
        val player = PhonePrefs.player(activity)
        playerButton.text = s(
            R.string.player_button,
            if (player.isEmpty()) s(R.string.player_default) else PhonePrefs.label(activity, player)
        )
    }

    @SuppressLint("BatteryLife")  // sürekli bağlı kalan bir eşlik uygulaması: muafiyet işlevin kendisi için gerekli
    private fun requestBatteryExemption() {
        try {
            activity.startActivity(
                Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:${activity.packageName}"))
            )
        } catch (e: Exception) {
            Log.w(BandService.TAG, "muafiyet penceresi açılamadı: ${e.javaClass.simpleName}")
            runCatching { activity.startActivity(Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS)) }
        }
    }

    private fun openAppSettings() {
        runCatching {
            activity.startActivity(
                Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.fromParts("package", activity.packageName, null))
            )
        }
    }

    private fun requestNotifications() {
        if (Build.VERSION.SDK_INT >= 33 && activity.shouldShowRequestPermissionRationale(Manifest.permission.POST_NOTIFICATIONS)) {
            activity.requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), REQUEST)
        } else {  // daha önce reddedildiyse sistem penceresi açılmaz: bildirim ayarlarına götür
            runCatching {
                activity.startActivity(
                    Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS)
                        .putExtra(Settings.EXTRA_APP_PACKAGE, activity.packageName)
                )
            }
        }
    }

    private fun requestBluetooth() {
        if (Build.VERSION.SDK_INT >= 31 && activity.shouldShowRequestPermissionRationale(Manifest.permission.BLUETOOTH_CONNECT)) {
            activity.requestPermissions(arrayOf(Manifest.permission.BLUETOOTH_CONNECT), REQUEST)
        } else {
            openAppSettings()
        }
    }

    private fun confirmStop() {
        AlertDialog.Builder(activity)
            .setTitle(s(R.string.stop_app))
            .setMessage(s(R.string.stop_confirm_message))
            .setNegativeButton(s(R.string.cancel), null)
            .setPositiveButton(s(R.string.stop)) { _, _ ->
                activity.startService(Intent(activity, BandService::class.java).setAction(BandService.ACTION_STOP))
            }
            .show()
    }

    companion object {
        const val REQUEST = 2

        /** Sistemin "bildirim erişimi" ekranı: kullanıcı Smart Band'i orada açar ya da kapatır. */
        fun openNotificationAccess(activity: Activity) {
            runCatching { activity.startActivity(Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS)) }
        }
    }
}

package band.app

import android.Manifest
import android.app.AlarmManager
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothDevice
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import android.os.SystemClock
import android.util.Log
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import org.json.JSONObject
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.concurrent.CopyOnWriteArraySet

/** Uygulama tercihleri (saat anahtarı burada DURMAZ; o files/data/config.json içindedir). */
object Prefs {
    private fun sp(c: Context) = c.getSharedPreferences("ayar", Context.MODE_PRIVATE)

    /** "Telefon açılınca başlat" (varsayılan açık). */
    fun bootStart(c: Context) = sp(c).getBoolean("boot_start", true)
    fun setBootStart(c: Context, on: Boolean) = sp(c).edit().putBoolean("boot_start", on).apply()

    /** Kullanıcı "Durdur" dedi: uygulama yeniden açılana dek açılışta/güncellemede kendiliğinden başlamaz. */
    fun userStopped(c: Context) = sp(c).getBoolean("user_stopped", false)
    fun setUserStopped(c: Context, on: Boolean) = sp(c).edit().putBoolean("user_stopped", on).commit()
}

/** Telefon açılınca ve uygulama güncellenince servisi başlatır (ayar açıksa). */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val action = intent.action
        if (action != Intent.ACTION_BOOT_COMPLETED && action != Intent.ACTION_MY_PACKAGE_REPLACED) return
        val why = when {
            !Prefs.bootStart(context) -> "ayar kapalı"
            Prefs.userStopped(context) -> "kullanıcı durdurmuştu"
            !BandService.configFile(context).isFile -> "kurulum yapılmamış"
            !BandService.bluetoothGranted(context) -> "Bluetooth izni yok"
            else -> null
        }
        Log.i(BandService.TAG, "$action: " + (why?.let { "başlatılmadı ($it)" } ?: "servis başlatılıyor"))
        if (why != null) return
        try {
            context.startForegroundService(Intent(context, BandService::class.java))
        } catch (e: Exception) {
            Log.e(BandService.TAG, "açılışta başlatılamadı: ${e.javaClass.simpleName}: ${e.message}")
        }
    }
}

/**
 * Python yorumlayıcısını ve içindeki sunucuyu ayakta tutan ön plan servisi.
 *
 * - START_STICKY: sistem süreci öldürürse servis yeniden yaratılır, sunucu aynı veri klasörüyle başlar;
 *   yarım kalan antrenman ve bekleyen profil yazımı server.py'nin kendi mantığıyla geri gelir.
 * - Son uygulamalardan kaydırılıp atılınca çalışmayı sürdürür.
 * - Bluetooth açılıp kapanmasını, saatin bağlantı/eşleşme değişimlerini yayınlardan izler.
 * - Uyanık tutma kilidi yalnızca saatle etkin veri alışverişi sürerken tutulur (eşitleme, canlı ölçüm,
 *   uygulamadan başlatılan antrenman); 15 dakikada bir kısa bir uyanma, vadesi gelen eşitlemeye fırsat verir.
 * - Yorumlayıcı durdurulamaz; "Durdur" bağlantıyı kapatıp süreci sonlandırır.
 * - Konum: yalnızca uygulamadan başlatılan açık hava antrenmanı sürerken servis "location" türüne de girer
 *   ve [LocationFeed] çalışır. Android 14+ bu türe yalnızca uygulama görünürken girilmesine izin verir;
 *   antrenman zaten görünür ekrandan başlatılır. Tür antrenman bitene dek tutulur (duraklatmada yalnızca
 *   güncellemeler durur), böylece saatin düğmesiyle arka planda sürdürmek de çalışır.
 */
class BandService : Service() {

    private val ui = Handler(Looper.getMainLooper())
    private val onBtStatus: (String) -> Unit = { ui.post { refreshNotification() } }
    private val onLanguage: () -> Unit = {
        ui.post {  // kanalın adı ve bildirim yeni dilde yeniden yazılır
            createChannel()
            posted = null
            refreshNotification()
        }
    }
    private var wake: PowerManager.WakeLock? = null
    private var tickWake: PowerManager.WakeLock? = null
    private var posted: String? = null
    private var foregrounded = false
    private var locationType = false   // servis şu an "location" türünde mi
    private var feed: LocationFeed? = null
    private val scaleFeed by lazy { ScaleFeed(this) }

    private val btEvents = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            when (intent.action) {
                BluetoothAdapter.ACTION_STATE_CHANGED ->
                    when (intent.getIntExtra(BluetoothAdapter.EXTRA_STATE, -1)) {
                        BluetoothAdapter.STATE_TURNING_OFF, BluetoothAdapter.STATE_OFF -> BtBridge.adapterOff()
                        BluetoothAdapter.STATE_ON -> {
                            Log.i(TAG, "Bluetooth açıldı: saat yeniden aranıyor")
                            python("kick")
                        }
                    }
                BluetoothDevice.ACTION_ACL_DISCONNECTED ->
                    if (BtBridge.isOurDevice(device(intent))) BtBridge.closeActive("saatle ACL bağlantısı koptu")
                BluetoothDevice.ACTION_BOND_STATE_CHANGED ->
                    if (BtBridge.isOurDevice(device(intent))) {
                        when (intent.getIntExtra(BluetoothDevice.EXTRA_BOND_STATE, -1)) {
                            BluetoothDevice.BOND_BONDED -> {
                                Log.i(TAG, "eşleşme tamamlandı: bağlanılıyor")
                                python("kick")
                            }
                            BluetoothDevice.BOND_NONE -> {
                                Log.i(TAG, "eşleşme kalktı")
                                BtBridge.closeActive("eşleşme kalktı")
                            }
                        }
                    }
            }
        }
    }

    private fun device(intent: Intent): BluetoothDevice? =
        if (Build.VERSION.SDK_INT >= 33) {
            intent.getParcelableExtra(BluetoothDevice.EXTRA_DEVICE, BluetoothDevice::class.java)
        } else {
            @Suppress("DEPRECATION") intent.getParcelableExtra(BluetoothDevice.EXTRA_DEVICE)
        }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        BtBridge.init(this)
        createChannel()
        val pm = getSystemService(PowerManager::class.java)
        wake = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "bileklik:saat").apply { setReferenceCounted(false) }
        tickWake = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "bileklik:denetim").apply { setReferenceCounted(false) }
        val filter = IntentFilter().apply {
            addAction(BluetoothAdapter.ACTION_STATE_CHANGED)
            addAction(BluetoothDevice.ACTION_ACL_DISCONNECTED)
            addAction(BluetoothDevice.ACTION_BOND_STATE_CHANGED)
        }
        // Bu yayınları yalnızca sistem gönderebilir (korumalı); Bluetooth yığını ayrı bir uygulama olduğu
        // için alıcı "dışa açık" kaydedilir.
        if (Build.VERSION.SDK_INT >= 33) registerReceiver(btEvents, filter, RECEIVER_EXPORTED) else registerReceiver(btEvents, filter)
        BtBridge.listeners.add(onBtStatus)
        L10n.listeners.add(onLanguage)
        instance = this
    }

    /** Bildirim kanalı; var olan kanalda yalnızca adı ve açıklaması güncellenir (dil değişince). */
    private fun createChannel() {
        getSystemService(NotificationManager::class.java).createNotificationChannel(
            NotificationChannel(CHANNEL, L10n.str(this, R.string.channel_name), NotificationManager.IMPORTANCE_LOW).apply {
                description = L10n.str(this@BandService, R.string.channel_description)
                setShowBadge(false)
            }
        )
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            stopCleanly()
            return START_NOT_STICKY
        }
        // Periyodik denetim (TICK) startService ile gelir ve servis zaten ön plandadır: startForeground
        // yinelenmez, yoksa arka plandayken "location" türü yeniden istenmiş olur ve reddedilebilir.
        if (!(foregrounded && intent?.action == ACTION_TICK)) {
            try {
                enterForeground()
                foregrounded = true
            } catch (e: Exception) {  // örn. Bluetooth izni yokken connectedDevice türü reddedilir
                if (locationType) {   // konum türü arka planda yenilenemedi: konumsuz sürdür
                    Log.w(TAG, "konum türüyle ön plana geçilemedi (${e.javaClass.simpleName}); konumsuz sürülüyor")
                    locationType = false
                    feed?.stop()
                    runCatching { enterForeground(); foregrounded = true }
                }
                if (!foregrounded) {
                    Log.e(TAG, "ön plan servisi başlatılamadı: ${e.javaClass.simpleName}: ${e.message}")
                    lastError = L10n.str(this, R.string.error_service_start, e.message)
                    stopSelf()
                    return START_NOT_STICKY
                }
            }
        }
        if (intent == null) Log.i(TAG, "servis sistem tarafından yeniden başlatıldı")
        if (intent?.action == ACTION_TICK) {
            // kısa uyanma: Python döngüsü vadesi gelen işi (30 dk'lık eşitleme) başlatabilsin; iş başlarsa
            // asıl kilidi kendisi alır
            tickWake?.acquire(TICK_AWAKE_MS)
        }
        startPython(applicationContext)
        if (started) python("resync")  // servis yeniden yaratıldıysa bildirim ve kilit gerçek durumu yansıtsın
        scheduleTick()
        applyLocation()
        scaleFeed.refresh()   // tartı ayarlıysa yayını dinlenir; sunucu henüz açılmadıysa sonraki uyanmada
        return START_STICKY
    }

    override fun onTaskRemoved(rootIntent: Intent?) {
        Log.i(TAG, "uygulama son uygulamalardan atıldı; servis sürüyor")
        super.onTaskRemoved(rootIntent)
    }

    override fun onDestroy() {
        Log.i(TAG, "servis sonlanıyor")
        instance = null
        BtBridge.listeners.remove(onBtStatus)
        L10n.listeners.remove(onLanguage)
        runCatching { unregisterReceiver(btEvents) }
        feed?.stop()
        scaleFeed.stop()
        releaseWake()
        super.onDestroy()
    }

    private fun enterForeground() {
        if (Build.VERSION.SDK_INT >= 29) {
            val types = ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE or
                (if (locationType) ServiceInfo.FOREGROUND_SERVICE_TYPE_LOCATION else 0)
            startForeground(NOTIFICATION, buildNotification(), types)
        } else {
            startForeground(NOTIFICATION, buildNotification())
        }
    }

    // ------------------------------------------------------------ konum
    /**
     * İstenen kipi uygular (0 kapalı, 1 antrenman duraklatıldı, 2 açık). Ana iş parçacığında çalışır.
     * "location" türüne geçiş reddedilirse (uygulama görünür değilken, ör. süreç ölümünden sonra)
     * antrenman konumsuz sürer; uygulama açılınca yeniden denenir (MainActivity.onStart).
     */
    private fun applyLocation() {
        val mode = locationMode
        val f = feed ?: LocationFeed(this).also { feed = it }
        val want = mode >= 1
        if (want && !f.granted()) {
            f.stop()
            if (mode == 2) python("location_denied", L10n.str(this, R.string.location_no_permission))
            return
        }
        if (want != locationType && foregrounded) {
            locationType = want
            try {
                enterForeground()
                Log.i(TAG, if (want) "servis konum türüne girdi" else "servis konum türünden çıktı")
            } catch (e: Exception) {
                Log.w(TAG, "konum türüne geçilemedi: ${e.javaClass.simpleName}: ${e.message}")
                locationType = false
                f.stop()
                if (want) python("location_blocked", L10n.str(this, R.string.location_blocked))
                return
            }
        }
        if (mode == 2 && locationType) f.start() else f.stop()
    }

    // ------------------------------------------------------------ uyanık tutma
    private fun applyBusy() {
        val lock = wake ?: return
        if (busy && !lock.isHeld) {
            lock.acquire()  // süre sınırı yok: antrenman saatlerce sürebilir; meşguliyet bitince bırakılır
            Log.i(TAG, "uyanık tutma kilidi alındı ($busyWhy)")
        } else if (!busy && lock.isHeld) {
            lock.release()
            Log.i(TAG, "uyanık tutma kilidi bırakıldı")
        }
    }

    private fun releaseWake() {
        wake?.let { if (it.isHeld) it.release() }
        tickWake?.let { if (it.isHeld) it.release() }
    }

    private fun tickIntent(): PendingIntent = PendingIntent.getForegroundService(
        this, 2, Intent(this, BandService::class.java).setAction(ACTION_TICK),
        PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
    )

    /** Kesin olmayan, Doze'da da çalışan alarm; özel izin gerektirmez. */
    private fun scheduleTick() {
        getSystemService(AlarmManager::class.java).setAndAllowWhileIdle(
            AlarmManager.ELAPSED_REALTIME_WAKEUP, SystemClock.elapsedRealtime() + TICK_EVERY_MS, tickIntent()
        )
    }

    // ------------------------------------------------------------ durdurma
    private fun stopCleanly() {
        Log.i(TAG, "kullanıcı durdurdu: bağlantı kapatılıyor, süreç sonlanacak")
        Prefs.setUserStopped(this, true)
        getSystemService(AlarmManager::class.java).cancel(tickIntent())
        BtBridge.closeActive("kullanıcı durdurdu")  // saat serbest kalır: bilgisayar ya da Mi Fitness bağlanabilir
        feed?.stop()
        releaseWake()
        for (l in stopListeners) runCatching { l() }  // açık ekran kapanır
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
        // Python yorumlayıcısı durdurulamadığı için süreç kapatılır; SQLite her yazmada commit ediyor.
        ui.postDelayed({ android.os.Process.killProcess(android.os.Process.myPid()) }, 500)
    }

    // ------------------------------------------------------------ bildirim
    private fun refreshNotification() {
        val text = summary(this) + "\u0000" + (hostHint ?: "")
        if (text == posted) return  // yalnızca içerik değişince güncellenir
        runCatching {
            getSystemService(NotificationManager::class.java).notify(NOTIFICATION, buildNotification())
        }
    }

    private fun buildNotification(): Notification {
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        val stop = PendingIntent.getService(
            this, 1, Intent(this, BandService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        val text = summary(this)
        val hint = hostHint
        posted = text + "\u0000" + (hint ?: "")
        return Notification.Builder(this, CHANNEL)
            .setSmallIcon(R.drawable.ic_stat)
            .setContentTitle(L10n.str(this, if (hint != null) R.string.notification_title_hint else R.string.notification_title))
            .setContentText(text)
            .apply { if (hint != null) setStyle(Notification.BigTextStyle().bigText("$hint\n$text")) }
            .setContentIntent(open)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setShowWhen(false)
            .setCategory(Notification.CATEGORY_SERVICE)
            .addAction(Notification.Action.Builder(null, L10n.str(this, R.string.stop), stop).build())
            .build()
    }

    companion object {
        const val TAG = "Bileklik"
        const val PORT = 8377
        private const val CHANNEL = "durum"
        private const val NOTIFICATION = 1
        const val ACTION_STOP = "band.app.STOP"
        private const val ACTION_TICK = "band.app.TICK"
        private const val TICK_EVERY_MS = 15 * 60 * 1000L
        private const val TICK_AWAKE_MS = 20 * 1000L

        @Volatile
        private var instance: BandService? = null

        /** Python sunucusu bu süreçte başlatıldı mı. */
        @Volatile
        var started = false
            private set

        /** Başlatma sırasında oluşan son hata; arayüz "Başlatılıyor…" ekranında gösterir. */
        @Volatile
        var lastError: String? = null

        // --- Python'dan gelen durum (bileklik_android.py) ---
        @Volatile private var link: String? = null
        @Volatile private var battery: Int? = null
        @Volatile private var charging = false
        @Volatile private var synced: Long? = null

        /** Bağlantı art arda hemen kopuyorsa server.py'nin ürettiği uyarı (state.host_hint). */
        @Volatile
        var hostHint: String? = null
            private set
        @Volatile private var busy = false
        @Volatile private var locationMode = 0
        @Volatile private var busyWhy = ""

        /** Durum değişince çağrılır (herhangi bir iş parçacığından). */
        val stateListeners = CopyOnWriteArraySet<() -> Unit>()

        /** "Durdur" istenince çağrılır; açık ekran kendini kapatır. */
        val stopListeners = CopyOnWriteArraySet<() -> Unit>()

        fun dataDir(context: Context): File = File(context.filesDir, "data").apply { mkdirs() }
        fun configFile(context: Context): File = File(dataDir(context), "config.json")

        fun bluetoothGranted(context: Context) = Build.VERSION.SDK_INT < 31 ||
            context.checkSelfPermission(Manifest.permission.BLUETOOTH_CONNECT) == PackageManager.PERMISSION_GRANTED

        /** Python köprüsü: bağlantı, pil, son eşitleme ya da uyarı değişti. */
        @JvmStatic
        fun onState(json: String) {
            try {
                val o = JSONObject(json)
                link = if (o.isNull("link")) null else o.getString("link")
                battery = if (o.isNull("battery")) null else o.getInt("battery")
                charging = o.optBoolean("charging", false)
                synced = if (o.isNull("synced")) null else o.getLong("synced")
                hostHint = if (o.isNull("host_hint")) null else o.getString("host_hint")
                // sayfada seçilen dil (config["lang"]): yalnızca "tr"/"en" uygulanır; yokluğu seçimi silmez
                val lang = if (o.isNull("lang")) "" else o.optString("lang")
                if (lang == "tr" || lang == "en") instance?.let { L10n.setLanguage(it, lang) }
            } catch (e: Exception) {
                Log.w(TAG, "durum okunamadı: ${e.javaClass.simpleName}")
                return
            }
            instance?.let { s -> s.ui.post { s.refreshNotification() } }
            for (l in stateListeners) runCatching { l() }
        }

        /** Python köprüsü: saatle etkin veri alışverişi başladı ya da bitti. */
        @JvmStatic
        fun onBusy(on: Boolean, why: String) {
            busy = on
            busyWhy = why
            instance?.let { s -> s.ui.post { s.applyBusy() } }
        }

        /** Python köprüsü: konum güncellemeleri isteniyor mu (0 kapalı, 1 duraklatıldı, 2 açık). */
        @JvmStatic
        fun setLocation(mode: Int) {
            locationMode = mode
            instance?.let { s -> s.ui.post { s.applyLocation() } }
        }

        @Volatile var appShown = false   // uygulama ekranda mı (servis sonradan başlasa da bilinsin)

        /** Uygulama ekrana geldi / ekrandan çıktı: tartı taraması buna göre hızlanır ya da yavaşlar. */
        fun appVisible(on: Boolean) {
            appShown = on
            instance?.scaleFeed?.visible(on)
        }

        /** Uygulama görünür oldu: daha önce reddedilen konum türüne geçiş yeniden denenir. */
        fun retryLocation() {
            if (locationMode != 0) instance?.let { s -> s.ui.post { s.applyLocation() } }
        }

        /** Bildirimin tek satırı: bağlantı · pil · son eşitleme. */
        fun summary(context: Context): String {
            lastError?.let { return it }
            val bt = BtBridge.status
            val first = when {
                bt == BtBridge.NO_PERMISSION || bt == BtBridge.BT_OFF || bt == BtBridge.PAIRING -> BtBridge.describe(context, bt)
                link == "connected" -> L10n.str(context, R.string.link_connected)
                link == "connecting" -> L10n.str(context, R.string.link_connecting)
                link == "away" -> L10n.str(context, R.string.link_away)
                else -> L10n.str(context, R.string.link_starting)
            }
            val parts = ArrayList<String>()
            parts += first
            battery?.let {
                parts += L10n.str(context, if (charging) R.string.summary_battery_charging else R.string.summary_battery, it)
            }
            synced?.let {
                val day = SimpleDateFormat("yyyyMMdd", Locale.ROOT)
                val at = Date(it * 1000)
                val fmt = if (day.format(at) == day.format(Date())) "HH:mm" else "d MMM HH:mm"
                parts += L10n.str(context, R.string.summary_synced, SimpleDateFormat(fmt, L10n.locale(context)).format(at))
            }
            return parts.joinToString(" · ")
        }

        /** bileklik_android modülünde bir işlevi (çoğunlukla bağımsız değişkensiz) arka planda çağırır. */
        fun python(function: String, vararg args: Any?) {
            if (!started || !Python.isStarted()) return
            Thread({
                try {
                    Python.getInstance().getModule("bileklik_android").callAttr(function, *args)
                } catch (e: Throwable) {
                    Log.w(TAG, "Python çağrısı başarısız ($function): ${e.javaClass.simpleName}")
                }
            }, "bileklik-py").start()
        }

        private val phoneQueue = java.util.concurrent.Executors.newSingleThreadExecutor { r ->
            Thread(r, "bileklik-telefon").apply { isDaemon = true }
        }

        /**
         * bileklik_android modülünde bir işlevi bağımsız değişkenlerle, sırayı koruyarak arka planda çağırır
         * (bildirim ve müzik olayları). Sunucu çalışmıyorsa olay atılır; bekletilmez.
         */
        fun py(function: String, vararg args: Any?) {
            if (!started || !Python.isStarted()) return
            phoneQueue.execute {
                try {
                    Python.getInstance().getModule("bileklik_android").callAttr(function, *args)
                } catch (e: Throwable) {
                    Log.w(TAG, "Python çağrısı başarısız ($function): ${e.javaClass.simpleName}")
                }
            }
        }

        /** Python köprüsü: saatten gelen istek (bildirimi kapat, medya düğmesi, aramayı reddet). */
        @JvmStatic
        fun onBandEvent(event: String, json: String) = NotifyService.onBandEvent(event, json)

        /** Açık canlı bağlantı (sayfa) sayısı; tanılama için. Arka planda çağrılmalı. */
        fun viewers(): Int = try {
            if (started && Python.isStarted()) {
                Python.getInstance().getModule("bileklik_android").callAttr("viewers").toInt()
            } else -1
        } catch (e: Throwable) {
            -1
        }

        @Synchronized
        private fun startPython(context: Context) {
            if (started) return
            if (!configFile(context).isFile) {
                lastError = L10n.str(context, R.string.error_no_config)
                return
            }
            started = true
            lastError = null
            Thread({
                try {
                    val code = extractCode(context)
                    if (!Python.isStarted()) Python.start(AndroidPlatform(context))
                    // Veri klasörü her başlatmada aynıdır (BILEKLIK_DATA): süreç ölse de kayıtlar oradan sürer.
                    Python.getInstance().getModule("bileklik_android")
                        .callAttr("start", code.absolutePath, dataDir(context).absolutePath, PORT)
                    Log.i(TAG, "Python sunucusu başlatıldı (kapı $PORT)")
                } catch (e: Throwable) {
                    // İleti ayar dosyasının içeriğini taşımaz; yine de yalnızca ilk satırı tutulur.
                    val text = (e.message ?: e.javaClass.simpleName).lineSequence().firstOrNull().orEmpty()
                    lastError = L10n.str(context, R.string.error_server_start, text)
                    Log.e(TAG, "Python başlatılamadı: ${e.javaClass.simpleName}: $text")
                    started = false  // sonraki denetimde (15 dk) ya da uygulama açılınca yeniden denenir
                }
            }, "bileklik-start").start()
        }

        /**
         * APK'daki band/ varlıklarını (../band'den derlemede alınan Python kodu ve web/) özel klasöre açar.
         * Gerçek dosya yolu gerekir: server.py arayüzü Path(__file__).parent / "web" altından okur.
         * İçerik özeti (VERSION) değişmedikçe yeniden açılmaz.
         */
        private fun extractCode(context: Context): File {
            val assets = context.assets
            val version = assets.open("band/VERSION").use { it.readBytes().decodeToString().trim() }
            val target = File(context.filesDir, "code")
            val stamp = File(target, ".version")
            if (stamp.isFile && stamp.readText().trim() == version) return target
            target.deleteRecursively()
            val names = assets.open("band/INDEX").use { it.readBytes().decodeToString() }
                .lineSequence().map { it.trim() }.filter { it.isNotEmpty() }.toList()
            for (name in names) {
                val out = File(target, name)
                if (!out.canonicalPath.startsWith(target.canonicalPath + File.separator)) continue
                out.parentFile?.mkdirs()
                assets.open("band/$name").use { input -> out.outputStream().use { input.copyTo(it) } }
            }
            stamp.writeText(version)
            Log.i(TAG, "kod açıldı: ${names.size} dosya, sürüm ${version.take(12)}")
            return target
        }
    }
}

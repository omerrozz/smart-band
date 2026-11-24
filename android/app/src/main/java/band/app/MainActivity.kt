package band.app

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.app.AlertDialog
import android.bluetooth.BluetoothAdapter
import android.content.ContentValues
import android.content.Intent
import android.content.pm.ApplicationInfo
import android.content.pm.PackageManager
import android.graphics.Color
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.os.Handler
import android.os.Looper
import android.provider.MediaStore
import android.provider.Settings
import android.text.InputType
import android.util.Log
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.view.WindowInsets
import android.view.WindowInsetsController
import android.webkit.JavascriptInterface
import android.webkit.RenderProcessGoneDetail
import android.webkit.URLUtil
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import android.window.OnBackInvokedDispatcher
import org.json.JSONObject
import java.io.File
import java.net.HttpURLConnection
import java.net.URL

/**
 * Tek ekran: izinler -> (gerekirse) ilk kurulum -> "Başlatılıyor…" -> tam ekran WebView.
 * Arayüz, servis içindeki Python sunucusundan (http://127.0.0.1:8377/) yüklenir.
 */
class MainActivity : Activity() {

    private val ui = Handler(Looper.getMainLooper())
    private lateinit var root: LinearLayout
    private lateinit var banner: TextView
    private lateinit var content: FrameLayout
    private lateinit var statusView: LinearLayout
    private lateinit var statusText: TextView
    private lateinit var statusButton: Button
    private lateinit var resetButton: Button
    private var web: WebView? = null
    private var setupView: View? = null

    @Volatile
    private var polling = false
    private var pageShown = false
    private var askedPermissions = false
    private val onBtStatus: (String) -> Unit = { ui.post { updateBanner() } }
    private val onServiceState: () -> Unit = { ui.post { updateBanner() } }
    private val onServiceStop: () -> Unit = { ui.post { finishAndRemoveTask() } }
    private val onLanguage: () -> Unit = { ui.post { applyLanguage() } }
    private var settings: SettingsPanel? = null

    private val base get() = "http://127.0.0.1:${BandService.PORT}"

    // ---------------------------------------------------------------- yaşam döngüsü
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        BtBridge.init(this)
        if (applicationInfo.flags and ApplicationInfo.FLAG_DEBUGGABLE != 0) {
            WebView.setWebContentsDebuggingEnabled(true)  // debug derlemede chrome://inspect ile bakılabilir
        }
        buildLayout()
        if (Build.VERSION.SDK_INT >= 33) {
            onBackInvokedDispatcher.registerOnBackInvokedCallback(OnBackInvokedDispatcher.PRIORITY_DEFAULT) { handleBack() }
        }
        BtBridge.listeners.add(onBtStatus)
        BandService.stateListeners.add(onServiceState)
        BandService.stopListeners.add(onServiceStop)
        L10n.listeners.add(onLanguage)
        proceed()
    }

    override fun onDestroy() {
        polling = false
        BtBridge.listeners.remove(onBtStatus)
        BandService.stateListeners.remove(onServiceState)
        BandService.stopListeners.remove(onServiceStop)
        L10n.listeners.remove(onLanguage)
        web?.let { (it.parent as? ViewGroup)?.removeView(it); it.destroy() }
        web = null
        super.onDestroy()
    }

    /**
     * Arka planda "izleyen" yoktur: sayfa gizlenince canlı bağlantısını (EventSource) kapatır, böylece
     * sunucu saati dakikada bir yoklamayı ve 5 dakikada bir eşitlemeyi bırakıp seyrek düzene döner.
     * WebView.onPause() sayfaya visibilitychange yollar; yollamadığı durumlar için aynısı elle de yapılır.
     */
    override fun onStop() {
        BandService.appVisible(false)
        web?.let {
            it.evaluateJavascript(HIDE_SCRIPT, null)
            it.onPause()
        }
        Thread({  // tanılama: birkaç saniye sonra açık canlı bağlantı kalmamalı
            try {
                Thread.sleep(8000)  // sunucu kapanan bağlantıyı en geç ~2 sn'de fark eder
            } catch (_: InterruptedException) {
            }
            val n = BandService.viewers()
            if (n >= 0) Log.i(BandService.TAG, "arka planda açık canlı bağlantı: $n" + if (n > 0) " (0 olmalıydı)" else "")
        }, "bileklik-viewers").apply { isDaemon = true; start() }
        super.onStop()
    }

    override fun onStart() {
        super.onStart()
        web?.let {
            it.onResume()
            it.evaluateJavascript(SHOW_SCRIPT, null)
        }
        BandService.retryLocation()  // arka planda reddedilen konum türü, uygulama görünürken yeniden denenir
        BandService.appVisible(true)
    }

    override fun onResume() {
        super.onResume()
        updateBanner()
        settings?.refresh()
        web?.evaluateJavascript("window.phoneDraw&&phoneDraw()", null)  // erişim ayarlarından dönüldüyse durum yenilenir
        // izin ayarlardan verilip dönüldüyse kaldığı yerden sürer
        if (waitingPermission && Build.VERSION.SDK_INT >= 31 && granted(Manifest.permission.BLUETOOTH_CONNECT)) proceed()
    }

    @Deprecated("Android 13 öncesi için")
    override fun onBackPressed() = handleBack()

    // ---------------------------------------------------------------- yerleşim
    private fun dp(v: Int) = (v * resources.displayMetrics.density).toInt()

    /** Metin kaynağı (uygulamada seçilen dil varsa o dilde). */
    private fun s(id: Int, vararg args: Any?) = L10n.str(this, id, *args)

    /** Sayfada dil değişti: yerel taraftaki kalıcı metinler yenilenir. */
    private fun applyLanguage() {
        if (isDestroyed) return
        resetButton.text = s(R.string.reset_setup)
        updateBanner()
        settings?.let { old ->  // panel yeni dilde baştan kurulur
            val open = old.view.visibility == View.VISIBLE
            content.removeView(old.view)
            settings = null
            if (open) showSettings(true)
        }
        appPicker?.retitle()
        web?.evaluateJavascript(hostRowScript(), null)
    }

    /** Sayfaya eklenen "Telefon ayarları" satırının metinleri. */
    private fun hostRowScript(): String =
        "(function(){try{var r=document.getElementById('hostRow');if(!r)return;" +
            "r.querySelector('b').textContent=" + JSONObject.quote(s(R.string.settings_title)) + ";" +
            "r.querySelector('span>span').textContent=" + JSONObject.quote(s(R.string.host_row_summary)) + ";" +
            "r.querySelector('button').textContent=" + JSONObject.quote(s(R.string.host_row_open)) + ";" +
            "}catch(e){}})()"

    private fun buildLayout() {
        if (Build.VERSION.SDK_INT >= 30) window.setDecorFitsSystemWindows(false)
        root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(getColor(R.color.bg))
        }
        banner = TextView(this).apply {
            setBackgroundColor(Color.parseColor("#ffb740"))
            setTextColor(Color.parseColor("#14122b"))
            textSize = 14f
            setPadding(dp(16), dp(10), dp(16), dp(10))
            visibility = View.GONE
            setOnClickListener { bannerTapped() }
        }
        content = FrameLayout(this)
        statusText = TextView(this).apply {
            textSize = 16f
            gravity = Gravity.CENTER
            setTextColor(Color.parseColor("#f1efff"))
        }
        statusButton = Button(this).apply { visibility = View.GONE }
        resetButton = Button(this).apply {
            text = s(R.string.reset_setup)
            visibility = View.GONE
            setOnClickListener { confirmReset() }
        }
        statusView = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER
            setPadding(dp(28), dp(28), dp(28), dp(28))
            addView(ProgressBar(context))
            addView(statusText, LinearLayout.LayoutParams(-1, -2).apply { topMargin = dp(18) })
            addView(statusButton, LinearLayout.LayoutParams(-2, -2).apply { topMargin = dp(18) })
            addView(resetButton, LinearLayout.LayoutParams(-2, -2).apply { topMargin = dp(8) })
        }
        content.addView(statusView, FrameLayout.LayoutParams(-1, -1))
        root.addView(banner, LinearLayout.LayoutParams(-1, -2))
        root.addView(content, LinearLayout.LayoutParams(-1, 0, 1f))
        // Kenardan kenara çizim: durum çubuğu, hareket çubuğu, çentik ve klavye payı kökte bırakılır.
        // Böylece sayfa hiçbir çubuğun altında kalmaz; kök, sayfanın zemin rengine boyanır.
        root.setOnApplyWindowInsetsListener { v, insets ->
            if (Build.VERSION.SDK_INT >= 30) {
                val i = insets.getInsets(
                    WindowInsets.Type.systemBars() or WindowInsets.Type.displayCutout() or WindowInsets.Type.ime()
                )
                v.setPadding(i.left, i.top, i.right, i.bottom)
                WindowInsets.CONSUMED
            } else {
                @Suppress("DEPRECATION")
                v.setPadding(
                    insets.systemWindowInsetLeft, insets.systemWindowInsetTop,
                    insets.systemWindowInsetRight, insets.systemWindowInsetBottom
                )
                @Suppress("DEPRECATION")
                insets.consumeSystemWindowInsets()
            }
        }
        setContentView(root)
        applyBackground(getColor(R.color.bg))
    }

    /** Kökü ve sistem çubuğu simgelerini sayfanın zemin rengine uydurur (açık temalarda koyu simge). */
    private fun applyBackground(color: Int) {
        root.setBackgroundColor(color)
        val light = (0.299 * Color.red(color) + 0.587 * Color.green(color) + 0.114 * Color.blue(color)) > 150
        if (Build.VERSION.SDK_INT >= 30) {
            val mask = WindowInsetsController.APPEARANCE_LIGHT_STATUS_BARS or
                WindowInsetsController.APPEARANCE_LIGHT_NAVIGATION_BARS
            window.insetsController?.setSystemBarsAppearance(if (light) mask else 0, mask)
        } else {
            @Suppress("DEPRECATION")
            window.decorView.systemUiVisibility = if (light) View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR else 0
        }
    }

    private fun showStatus(text: String, button: String? = null, spinner: Boolean = true, action: (() -> Unit)? = null) {
        setupView?.visibility = View.GONE
        web?.visibility = View.GONE
        statusView.visibility = View.VISIBLE
        statusView.getChildAt(0).visibility = if (spinner) View.VISIBLE else View.GONE
        statusText.text = text
        statusButton.visibility = if (button != null) View.VISIBLE else View.GONE
        statusButton.text = button
        statusButton.setOnClickListener { action?.invoke() }
    }

    private fun updateBanner() {
        val st = BtBridge.status
        val bt = st == BtBridge.PAIRING || st == BtBridge.BT_OFF || st == BtBridge.NO_PERMISSION
        val hint = BandService.hostHint
        val show = pageShown && (bt || hint != null)
        banner.visibility = if (show) View.VISIBLE else View.GONE
        if (show) banner.text = if (bt) {
            val what = BtBridge.describe(this, st)
            if (st == BtBridge.PAIRING) what else s(R.string.banner_tap, what)
        } else hint
    }

    private fun bannerTapped() {
        when (BtBridge.status) {
            BtBridge.BT_OFF -> runCatching { startActivity(Intent(BluetoothAdapter.ACTION_REQUEST_ENABLE)) }
            BtBridge.NO_PERMISSION -> openAppSettings()
        }
    }

    private fun openAppSettings() {
        runCatching {
            startActivity(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.fromParts("package", packageName, null)))
        }
    }

    // ---------------------------------------------------------------- akış
    private fun granted(p: String) = checkSelfPermission(p) == PackageManager.PERMISSION_GRANTED

    /** Sıradaki adıma geçer: izin -> kurulum -> servis -> sayfa. */
    private fun proceed() {
        // 1) izinler. Bluetooth izni şart (servis türü de buna bağlı); bildirim izni reddedilse de devam edilir.
        val wanted = ArrayList<String>()
        if (Build.VERSION.SDK_INT >= 31 && !granted(Manifest.permission.BLUETOOTH_CONNECT)) {
            wanted += Manifest.permission.BLUETOOTH_CONNECT
        }
        if (Build.VERSION.SDK_INT >= 31 && !granted(Manifest.permission.BLUETOOTH_SCAN)) {
            wanted += Manifest.permission.BLUETOOTH_SCAN   // tartının yayınını dinlemek için (aynı izin grubu)
        }
        if (Build.VERSION.SDK_INT >= 33 && !granted(Manifest.permission.POST_NOTIFICATIONS)) {
            wanted += Manifest.permission.POST_NOTIFICATIONS
        }
        if (wanted.isNotEmpty() && !askedPermissions) {
            askedPermissions = true
            showStatus(s(R.string.status_waiting_permissions))
            requestPermissions(wanted.toTypedArray(), 1)
            return
        }
        waitingPermission = false
        if (Build.VERSION.SDK_INT >= 31 && !granted(Manifest.permission.BLUETOOTH_CONNECT)) {
            waitingPermission = true
            showStatus(
                s(R.string.bluetooth_rationale),
                s(R.string.grant), spinner = false
            ) {
                if (shouldShowRequestPermissionRationale(Manifest.permission.BLUETOOTH_CONNECT)) {
                    requestPermissions(arrayOf(Manifest.permission.BLUETOOTH_CONNECT), 1)
                } else {
                    openAppSettings()
                }
            }
            return
        }
        // 2) ayar yoksa ilk kurulum (bilgisayardan aktarım adb run-as ile yapılır; README'ye bakınız)
        // 3) ayar yoksa ilk kurulum
        if (!BandService.configFile(this).isFile) {
            showSetup()
            return
        }
        // 3) servis ve sayfa
        startServer()
    }

    private var waitingPermission = false

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        when (requestCode) {
            SettingsPanel.REQUEST -> settings?.refresh()
            // "yaklaşık konum" yetmez: GPS için kesin konum gerekir
            REQUEST_LOCATION -> locationResult(granted(Manifest.permission.ACCESS_FINE_LOCATION))
            else -> proceed()
        }
    }

    private fun locationResult(ok: Boolean) {
        Log.i(BandService.TAG, "konum izni: " + if (ok) "var" else "yok (antrenman adım tahminiyle sürer)")
        web?.evaluateJavascript("window.onHostLocation&&window.onHostLocation($ok)", null)
    }

    private fun restartApp() {
        stopService(Intent(this, BandService::class.java))
        val launch = packageManager.getLaunchIntentForPackage(packageName)!!
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK)
        startActivity(launch)
        finishAffinity()
        Runtime.getRuntime().exit(0)
    }

    private fun confirmReset() {
        AlertDialog.Builder(this)
            .setTitle(s(R.string.reset_setup))
            .setMessage(s(R.string.reset_setup_message))
            .setNegativeButton(s(R.string.cancel), null)
            .setPositiveButton(s(R.string.reset_setup_confirm)) { _, _ ->
                BandService.configFile(this).delete()
                restartApp()
            }
            .show()
    }

    // ---------------------------------------------------------------- ilk kurulum
    private fun showSetup() {
        statusView.visibility = View.GONE
        web?.visibility = View.GONE
        setupView?.let { it.visibility = View.VISIBLE; return }
        val ink = Color.parseColor("#f1efff")
        val mut = Color.parseColor("#b3afd8")
        fun label(text: String, size: Float = 14f, color: Int = mut) = TextView(this).apply {
            this.text = text; textSize = size; setTextColor(color)
        }
        fun field(hint: String) = EditText(this).apply {
            this.hint = hint
            setSingleLine()
            setTextColor(ink)
            setHintTextColor(mut)
            // klavye önerisi/öğrenmesi kapalı: anahtar klavyenin sözlüğüne girmesin
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD or
                InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS
            if (Build.VERSION.SDK_INT >= 26) importantForAutofill = View.IMPORTANT_FOR_AUTOFILL_NO
        }
        val mac = field("AA:BB:CC:DD:EE:FF")
        val key = field(s(R.string.setup_key_hint))
        val error = label("", color = Color.parseColor("#ff6f97"))
        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(24), dp(28), dp(24), dp(28))
            addView(label(s(R.string.setup_title), 24f, ink))
            addView(label(s(R.string.setup_intro, packageName)), LinearLayout.LayoutParams(-1, -2).apply { topMargin = dp(12) })
            addView(label(s(R.string.setup_mac_label)), LinearLayout.LayoutParams(-1, -2).apply { topMargin = dp(20) })
            addView(mac)
            addView(label(s(R.string.setup_key_label)), LinearLayout.LayoutParams(-1, -2).apply { topMargin = dp(12) })
            addView(key)
            addView(error, LinearLayout.LayoutParams(-1, -2).apply { topMargin = dp(8) })
            addView(Button(context).apply {
                text = s(R.string.setup_save)
                setOnClickListener {
                    val e = Importer.writeManual(context, mac.text.toString(), key.text.toString())
                    if (e != null) {
                        error.text = e
                    } else {
                        key.setText("")
                        proceed()
                    }
                }
            }, LinearLayout.LayoutParams(-2, -2).apply { topMargin = dp(8) })
        }
        setupView = ScrollView(this).apply { addView(box) }
        content.addView(setupView, FrameLayout.LayoutParams(-1, -1))
    }

    // ---------------------------------------------------------------- sunucu ve sayfa
    private fun startServer() {
        showStatus(s(R.string.status_starting))
        resetButton.visibility = View.GONE
        Prefs.setUserStopped(this, false)  // kullanıcı uygulamayı açtı: yeniden kendiliğinden başlayabilir
        try {
            startForegroundService(Intent(this, BandService::class.java))
        } catch (e: Exception) {
            Log.e(BandService.TAG, "servis başlatılamadı: ${e.javaClass.simpleName}")
            showStatus(s(R.string.error_service_start, e.message), s(R.string.retry), spinner = false) { proceed() }
            return
        }
        if (polling) return
        polling = true
        Thread({
            while (polling) {
                val ok = try {
                    val c = URL("$base/api/state").openConnection() as HttpURLConnection
                    c.connectTimeout = 1000
                    c.readTimeout = 3000
                    try {
                        c.responseCode == 200
                    } finally {
                        c.disconnect()
                    }
                } catch (e: Exception) {
                    false
                }
                if (ok) {
                    polling = false
                    ui.post { showPage() }
                    return@Thread
                }
                val err = BandService.lastError
                ui.post {
                    if (polling && statusView.visibility == View.VISIBLE) {
                        val starting = s(R.string.status_starting)
                        statusText.text = if (err != null) "$starting\n\n$err" else starting
                        resetButton.visibility = if (err != null) View.VISIBLE else View.GONE
                    }
                }
                try {
                    Thread.sleep(500)
                } catch (_: InterruptedException) {
                    return@Thread
                }
            }
        }, "bileklik-poll").apply { isDaemon = true; start() }
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun showPage() {
        if (isDestroyed) return
        val w = web ?: WebView(this).also { v ->
            v.settings.apply {
                javaScriptEnabled = true
                domStorageEnabled = true
                allowFileAccess = false
                allowContentAccess = false
                setSupportMultipleWindows(false)
                setGeolocationEnabled(false)
                // mixedContentMode varsayılanı (izin yok) korunur: sayfa http://127.0.0.1'den gelir,
                // yazı tipleri/harita https ile yüklenir; gevşetme gerekmez.
            }
            v.setBackgroundColor(getColor(R.color.bg))
            v.overScrollMode = View.OVER_SCROLL_NEVER
            v.webChromeClient = WebChromeClient()  // alert/confirm ve <input type="time"> pencereleri
            v.webViewClient = Client()
            v.addJavascriptInterface(Host(), "BileklikHost")
            v.setDownloadListener { url, _, disposition, mime, _ -> download(url, disposition, mime) }
            content.addView(v, 0, FrameLayout.LayoutParams(-1, -1))
            web = v
        }
        w.loadUrl("$base/")
    }

    private fun pageReady() {
        pageShown = true
        statusView.visibility = View.GONE
        setupView?.visibility = View.GONE
        web?.visibility = View.VISIBLE
        updateBanner()
    }

    private fun pageLost() {
        pageShown = false
        updateBanner()
        startServer()  // "Başlatılıyor…" ekranına dön, sunucu yanıt verince yeniden yükle
    }

    private fun isOurs(uri: Uri) = uri.scheme == "http" && uri.host == "127.0.0.1" && uri.port == BandService.PORT

    private inner class Client : WebViewClient() {
        override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
            if (isOurs(request.url)) return false
            // dış bağlantılar tarayıcıda açılır; WebView yalnızca kendi sunucumuzda gezinir
            if (request.url.scheme == "https" || request.url.scheme == "http") {
                runCatching { startActivity(Intent(Intent.ACTION_VIEW, request.url)) }
            }
            return true
        }

        override fun onPageFinished(view: WebView, url: String) {
            if (!isOurs(Uri.parse(url))) return
            view.evaluateJavascript(PAGE_HOOK, null)
            view.evaluateJavascript(hostRowScript(), null)
            if (!failed) pageReady()
        }

        private var failed = false

        override fun onPageStarted(view: WebView, url: String, favicon: android.graphics.Bitmap?) {
            failed = false
        }

        override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
            if (request.isForMainFrame) {
                failed = true
                Log.w(BandService.TAG, "sayfa yüklenemedi: ${error.description}")
                pageLost()
            }
        }

        override fun onRenderProcessGone(view: WebView, detail: RenderProcessGoneDetail): Boolean {
            Log.w(BandService.TAG, "WebView işlemi sonlandı, sayfa yeniden kuruluyor")
            (view.parent as? ViewGroup)?.removeView(view)
            view.destroy()
            if (web === view) web = null
            pageLost()
            return true
        }
    }

    /** Sayfanın zemin rengini (tema değişse de) yerel tarafa bildiren küçük kanca. Başka bir şey sunmaz. */
    private inner class Host {
        /**
         * Sayfada seçilen dili yerel tarafa bildirir: "tr" ya da "en"; boş metin (ya da başka her değer)
         * sistemin diline döner. Seçim kalıcıdır; bildirim, başlık şeridi ve yerel paneller bu dile geçer.
         */
        @JavascriptInterface
        fun setLanguage(lang: String?) {
            L10n.setLanguage(this@MainActivity, lang)
        }

        @JavascriptInterface
        fun theme(bg: String?) {
            val color = try {
                Color.parseColor(bg?.trim().orEmpty())
            } catch (e: Exception) {
                return
            }
            ui.post { applyBackground(color) }
        }

        /** Konum izni verilmiş mi (sayfa, antrenman penceresindeki açıklamayı buna göre yazar). */
        @JavascriptInterface
        fun locationGranted(): Boolean = granted(Manifest.permission.ACCESS_FINE_LOCATION)

        /** Sayfanın hazırladığı görseli (base64 JPEG) Resimler/Smart Band'e kaydeder ve paylaşma penceresini açar. */
        @JavascriptInterface
        fun shareImage(base64: String?, name: String?) {
            val data = try { android.util.Base64.decode(base64 ?: "", android.util.Base64.DEFAULT) } catch (e: Exception) { ByteArray(0) }
            val safe = (name ?: "bileklik.jpg").replace(Regex("[^A-Za-z0-9._-]"), "_")
            Thread({
                try {
                    if (data.size < 1000 || Build.VERSION.SDK_INT < 29) throw java.io.IOException(s(R.string.error_image_save))
                    val values = ContentValues().apply {
                        put(MediaStore.Images.Media.DISPLAY_NAME, safe)
                        put(MediaStore.Images.Media.MIME_TYPE, "image/jpeg")
                        put(MediaStore.Images.Media.RELATIVE_PATH, "Pictures/Smart Band")
                    }
                    val uri = contentResolver.insert(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, values)
                        ?: throw java.io.IOException(s(R.string.error_image_save))
                    contentResolver.openOutputStream(uri)?.use { it.write(data) } ?: throw java.io.IOException(s(R.string.error_image_write))
                    val send = Intent(Intent.ACTION_SEND).apply {
                        type = "image/jpeg"
                        putExtra(Intent.EXTRA_STREAM, uri)
                        addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                    }
                    ui.post { startActivity(Intent.createChooser(send, s(R.string.share_title))) }
                } catch (e: Exception) {
                    Log.w(BandService.TAG, "görsel paylaşılamadı: ${e.javaClass.simpleName}")
                    ui.post { Toast.makeText(this@MainActivity, s(R.string.share_failed, e.message), Toast.LENGTH_LONG).show() }
                }
            }, "bileklik-share").start()
        }

        /** Tam ekran haritada ekran yan çevrilir; kapatınca telefonun kendi ayarına döner. */
        @JavascriptInterface
        fun landscape(on: Boolean) {
            ui.post {
                requestedOrientation = if (on) android.content.pm.ActivityInfo.SCREEN_ORIENTATION_SENSOR_LANDSCAPE
                else android.content.pm.ActivityInfo.SCREEN_ORIENTATION_UNSPECIFIED
            }
        }

        /** Güç tasarrufu açık ve konumu ekran kapalıyken kısıtlıyorsa true: antrenmanda GPS kesilir. */
        @JavascriptInterface
        fun gpsLimited(): Boolean = try {
            val pm = getSystemService(android.content.Context.POWER_SERVICE) as android.os.PowerManager
            pm.isPowerSaveMode && (android.os.Build.VERSION.SDK_INT < 28 ||
                pm.locationPowerSaveMode != android.os.PowerManager.LOCATION_MODE_NO_CHANGE)
        } catch (e: Exception) { false }

        /**
         * Açık hava antrenmanı başlatılırken sayfa çağırır: konum izni yoksa sistem penceresi açılır.
         * Sonuç sayfaya window.onHostLocation(true|false) ile döner. Arka plan konum izni İSTENMEZ:
         * görünür ekrandan başlatılan ön plan servisi için gerekmez.
         */
        @JavascriptInterface
        fun requestLocation() {
            ui.post {
                if (granted(Manifest.permission.ACCESS_FINE_LOCATION)) {
                    locationResult(true)
                } else {
                    requestPermissions(
                        arrayOf(Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_COARSE_LOCATION),
                        REQUEST_LOCATION
                    )
                }
            }
        }

        /** Bildirim ve müzik durumu (sayfadaki "Bildirimler ve müzik" kutusu için). Metin içermez. */
        @JavascriptInterface
        fun phoneStatus(): String = JSONObject()
            .put("access", NotifyService.accessGranted(this@MainActivity))
            .put("notifications", PhonePrefs.get(this@MainActivity, PhonePrefs.NOTIFICATIONS))
            .put("screen", PhonePrefs.get(this@MainActivity, PhonePrefs.SCREEN))
            .put("music", PhonePrefs.get(this@MainActivity, PhonePrefs.MUSIC))
            .put("apps", PhonePrefs.apps(this@MainActivity).size)
            .put("player", PhonePrefs.player(this@MainActivity).let { if (it.isEmpty()) "" else PhonePrefs.label(this@MainActivity, it) })
            .toString()

        @JavascriptInterface
        fun phoneSet(key: String?, on: Boolean) {
            if (key != PhonePrefs.NOTIFICATIONS && key != PhonePrefs.SCREEN && key != PhonePrefs.MUSIC) return
            PhonePrefs.set(this@MainActivity, key, on)
            if (key == PhonePrefs.MUSIC) NotifyService.refresh()
        }

        @JavascriptInterface
        fun phoneApps() {
            ui.post { showApps(true) }
        }

        /** Tercih edilen oynatıcının seçimi (uygulama listesi, tek seçim). */
        @JavascriptInterface
        fun phonePlayer() {
            ui.post { showApps(true, player = true) }
        }

        @JavascriptInterface
        fun phoneAccess() {
            ui.post { SettingsPanel.openNotificationAccess(this@MainActivity) }
        }

        /** Sayfadaki "Telefon ayarları" satırı yerel paneli açar. */
        @JavascriptInterface
        fun settings() {
            ui.post { showSettings(true) }
        }
    }

    // ---------------------------------------------------------------- telefon ayarları
    private fun showSettings(show: Boolean) {
        if (!show) {
            settings?.view?.visibility = View.GONE
            return
        }
        val panel = settings ?: SettingsPanel(this, { showApps(true) }, { showApps(true, player = true) }) { showSettings(false) }.also {
            settings = it
            content.addView(it.view, FrameLayout.LayoutParams(-1, -1))
        }
        panel.refresh()
        panel.view.visibility = View.VISIBLE
        panel.view.bringToFront()
    }

    private var appPicker: AppPickerPanel? = null

    private fun showApps(show: Boolean, player: Boolean = false) {
        if (!show) {
            appPicker?.view?.visibility = View.GONE
            settings?.refresh()
            web?.evaluateJavascript("window.phoneDraw&&phoneDraw()", null)
            return
        }
        val panel = appPicker ?: AppPickerPanel(this) { showApps(false) }.also {
            appPicker = it
            content.addView(it.view, FrameLayout.LayoutParams(-1, -1))
        }
        if (player) {  // tek seçim: tercih edilen oynatıcı
            panel.reload({ pkg ->
                PhonePrefs.setPlayer(this, pkg)
                Log.i(BandService.TAG, "tercih edilen oynatıcı: " + pkg.ifEmpty { "son çalan" })
                NotifyService.refresh()
            }, PhonePrefs.player(this))
        } else {
            panel.reload()
        }
        panel.view.visibility = View.VISIBLE
        panel.view.bringToFront()
    }

    // ---------------------------------------------------------------- geri tuşu
    private fun handleBack() {
        if (appPicker?.view?.visibility == View.VISIBLE) {
            showApps(false)
            return
        }
        if (settings?.view?.visibility == View.VISIBLE) {
            showSettings(false)
            return
        }
        val w = web
        if (w == null || w.visibility != View.VISIBLE) {
            leave()
            return
        }
        w.evaluateJavascript(BACK_SCRIPT) { result ->
            when {
                result?.contains("handled") == true -> Unit
                w.canGoBack() -> w.goBack()
                else -> leave()
            }
        }
    }

    /** Etkinliği kapatmadan arka plana alır; servis ve sayfa durur, dönünce kaldığı yerden sürer. */
    private fun leave() {
        moveTaskToBack(true)
    }

    // ---------------------------------------------------------------- indirme (CSV, GPX)
    private fun download(url: String, disposition: String?, mime: String?) {
        if (!isOurs(Uri.parse(url))) return
        Thread({
            val message = try {
                val c = URL(url).openConnection() as HttpURLConnection
                c.connectTimeout = 5000
                c.readTimeout = 30000
                try {
                    if (c.responseCode != 200) throw java.io.IOException("HTTP ${c.responseCode}")
                    val type = (c.contentType ?: mime ?: "application/octet-stream").substringBefore(';').trim()
                    val name = URLUtil.guessFileName(url, c.getHeaderField("Content-Disposition") ?: disposition, type)
                    c.inputStream.use { save(name, type, it) }
                    s(R.string.download_saved, name)
                } finally {
                    c.disconnect()
                }
            } catch (e: Exception) {
                Log.w(BandService.TAG, "indirme başarısız: ${e.javaClass.simpleName}")
                s(R.string.download_failed, e.message)
            }
            ui.post { Toast.makeText(this, message, Toast.LENGTH_LONG).show() }
        }, "bileklik-download").start()
    }

    private fun save(name: String, type: String, input: java.io.InputStream) {
        if (Build.VERSION.SDK_INT >= 29) {  // ortak İndirilenler klasörü, izin gerekmez
            val values = ContentValues().apply {
                put(MediaStore.Downloads.DISPLAY_NAME, name)
                put(MediaStore.Downloads.MIME_TYPE, type)
                put(MediaStore.Downloads.IS_PENDING, 1)
            }
            val uri = contentResolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values)
                ?: throw java.io.IOException(s(R.string.error_download_create))
            contentResolver.openOutputStream(uri)!!.use { input.copyTo(it) }
            contentResolver.update(uri, ContentValues().apply { put(MediaStore.Downloads.IS_PENDING, 0) }, null, null)
        } else {  // eski sürümler: uygulamanın kendi dış klasörü
            val dir = getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS) ?: throw java.io.IOException(s(R.string.error_no_storage))
            File(dir, name).outputStream().use { input.copyTo(it) }
        }
    }

    private companion object {
        const val REQUEST_LOCATION = 3

        /** Açık pencereyi kapatır ya da özet dışındaki sekmeden özete döner; yapacak bir şey yoksa boş döner. */
        const val BACK_SCRIPT = """(function(){try{
            if(typeof window.appBack==='function')return window.appBack();
            var d=document.querySelectorAll('dialog[open]');
            if(d.length){d[d.length-1].close();return 'handled';}
            var h=location.hash.replace('#','');
            if(h&&h!=='ozet'){var b=document.querySelector('[data-tab="ozet"]');if(b){b.click();return 'handled';}}
            }catch(e){}return '';})()"""

        /**
         * Zemin rengini bildirir; "telefonda aç" satırını gizler, yerine "Telefon ayarları" satırını ekler.
         * Satırın metinleri hostRowScript() ile (seçili dilde) yazılır.
         */
        const val PAGE_HOOK = """(function(){if(window.__bileklikHost)return;window.__bileklikHost=1;
            var f=function(){try{BileklikHost.theme(getComputedStyle(document.documentElement).getPropertyValue('--bg'));}catch(e){}};
            try{new MutationObserver(f).observe(document.documentElement,{attributes:true});}catch(e){}
            f();var p=document.getElementById('phoneRow');if(p){p.hidden=true;
            if(!document.getElementById('hostRow')){var r=document.createElement('div');r.id='hostRow';
            var s=document.createElement('span'),b=document.createElement('b'),d=document.createElement('span');
            s.appendChild(b);s.appendChild(d);var k=document.createElement('button');k.className='btn ghost';
            k.onclick=function(){BileklikHost.settings();};r.appendChild(s);r.appendChild(k);
            p.parentNode.insertBefore(r,p);}}})()"""

        /** Etkinlik durunca: sayfanın canlı bağlantısını kapatır (sayfa bunu visibilitychange ile de yapar). */
        const val HIDE_SCRIPT = """(function(){try{if(typeof es!=='undefined'&&es){es.close();es=null;}}catch(e){}})()"""

        /** Etkinlik görününce: sayfa kendisi bağlanmadıysa canlı bağlantıyı yeniden kurar. */
        const val SHOW_SCRIPT = """(function(){try{if(typeof es!=='undefined'&&!es&&typeof connect==='function'&&!document.hidden)connect();}catch(e){}})()"""
    }
}

/**
 * İlk kurulum verisi: elle girilen adres ve anahtarın yazılması.
 * Anahtar hiçbir yerde günlüğe yazılmaz.
 */
object Importer {
    private val MAC = Regex("^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")
    private val KEY = Regex("^(0x)?[0-9A-Fa-f]{32}$")

    /** Elle girilen adres ve anahtarla ayar dosyası yazar; kalan varsayılanları server.py açılışta ekler. */
    fun writeManual(context: android.content.Context, mac: String, key: String): String? {
        val address = mac.trim().uppercase().replace('-', ':')
        val authKey = key.trim().replace(" ", "").lowercase()
        if (!MAC.matches(address)) return L10n.str(context, R.string.setup_error_mac)
        if (!KEY.matches(authKey)) return L10n.str(context, R.string.setup_error_key)
        return try {
            val json = JSONObject().put("address", address).put("auth_key", authKey)
            val target = BandService.configFile(context)
            val tmp = File(target.parentFile, "config.json.tmp")
            tmp.writeText(json.toString(2) + "\n")
            if (!tmp.renameTo(target)) throw java.io.IOException("yazılamadı")
            null
        } catch (e: Exception) {
            L10n.str(context, R.string.setup_error_write, e.javaClass.simpleName)
        }
    }
}

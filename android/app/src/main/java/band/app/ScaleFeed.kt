package band.app

import android.Manifest
import android.annotation.SuppressLint
import android.bluetooth.BluetoothManager
import android.bluetooth.le.BluetoothLeScanner
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanFilter
import android.bluetooth.le.ScanResult
import android.bluetooth.le.ScanSettings
import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import android.os.ParcelUuid
import android.util.Log
import com.chaquo.python.Python

/**
 * Tartının (Xiaomi S400) Bluetooth yayınını dinler ve FE95 servis verisini Python'a (bileklik_android.scale_frame)
 * aktarır. Tartıya bağlanılmaz; yalnızca ayarlı adresten gelen yayınlar süzülerek alınır, düşük güçlü taramayla.
 * Şifre çözme ve kayıt scale.py / server.py'dedir. Günlüğe ölçüm ya da adres yazılmaz.
 */
class ScaleFeed(private val context: Context) {
    private var scanner: BluetoothLeScanner? = null
    private var address = ""
    private var lastHex = ""
    private var frames = 0

    private val callback = object : ScanCallback() {
        override fun onScanResult(callbackType: Int, result: ScanResult) {
            val data = result.scanRecord?.getServiceData(XIAOMI) ?: return
            if (data.size != 24) return   // 11 baytlık kimlik paketi ölçüm taşımaz (süzgeç zaten elemeli)
            boost()                       // tartılma sürüyor: ardından gelen paket (250 kHz empedans) kaçmasın
            val hex = data.joinToString("") { "%02x".format(it) }
            if (hex == lastHex) return
            lastHex = hex
            if (frames++ == 0) Log.i(TAG, "tartıdan ilk ölçüm paketi alındı")
            BandService.py("scale_frame", hex)
        }

        override fun onScanFailed(errorCode: Int) {
            Log.w(TAG, "tartı taraması başlatılamadı: $errorCode")
            scanner = null
        }
    }

    private val ui = android.os.Handler(android.os.Looper.getMainLooper())
    private var fast = false
    private val slowDown = Runnable { synchronized(this) { if (fast) { fast = false; restart() } } }

    /**
     * Bir ölçüm paketi geldi: kısa süre kesintisiz taranır, sonra dengeli taramaya dönülür. Tartı boştayken de
     * sürekli kimlik paketi yayınladığı için hızlanma kimlik paketine bağlanmaz (aksi halde tarama hep hızlı kalır
     * ve pil tüketir; 21 Kasım'da böyleydi).
     */
    private fun boost() {
        ui.removeCallbacks(slowDown)
        ui.postDelayed(slowDown, FAST_MS)
        synchronized(this) {
            if (fast) return
            fast = true
            restart()
        }
    }

    @SuppressLint("MissingPermission")
    private fun restart() {
        val s = scanner ?: return
        runCatching { s.stopScan(callback) }
        try {
            s.startScan(listOf(filter(address)), settings(), callback)
            Log.i(TAG, if (fast) "ölçüm geldi: hızlı tarama" else if (visible) "uygulama ekranda: hızlı tarama" else "dengeli tarama")
        } catch (e: Exception) {
            scanner = null
            Log.w(TAG, "tarama yeniden başlatılamadı: ${e.javaClass.simpleName}")
        }
    }

    private fun settings() = ScanSettings.Builder()
        .setScanMode(if (fast || visible) ScanSettings.SCAN_MODE_LOW_LATENCY else ScanSettings.SCAN_MODE_BALANCED).build()

    @Volatile private var visible = false

    /**
     * Uygulama ekrandayken kesintisiz taranır: dengeli tarama tartının kısa süren ölçüm yayınını kaçırabiliyor
     * (21 Kasım gecesi iki tartılmadan biri kaçtı). Ekrandan çıkınca dengeli taramaya dönülür.
     */
    fun visible(on: Boolean) {
        synchronized(this) {
            if (visible == on) return
            visible = on
            if (!fast) restart()
        }
    }

    /** Yalnızca bu tartının ölçüm paketleri: servis verisinin ilk baytında "veri var" ve "şifreli" bitleri (0x48). */
    private fun filter(mac: String) = ScanFilter.Builder().setDeviceAddress(mac)
        .setServiceData(XIAOMI, byteArrayOf(0x48), byteArrayOf(0x48)).build()

    private fun granted() = Build.VERSION.SDK_INT < 31 ||
        context.checkSelfPermission(Manifest.permission.BLUETOOTH_SCAN) == PackageManager.PERMISSION_GRANTED

    /** Ayarlı tartı adresini Python'dan okur; tarama gerekiyorsa başlatır, gerekmiyorsa durdurur. */
    fun refresh() {
        Thread({
            var want = ""
            for (attempt in 0 until 8) {   // servis yeni başladıysa sunucunun açılması birkaç saniye sürer
                want = try {
                    if (Python.isStarted()) Python.getInstance().getModule("bileklik_android").callAttr("scale_address").toString() else ""
                } catch (e: Throwable) {
                    if (attempt == 7) Log.w(TAG, "tartı adresi okunamadı: ${e.javaClass.simpleName}: ${e.message?.take(120)}")
                    ""
                }
                if (want.isNotEmpty() || scanner != null) break
                Thread.sleep(4000)
            }
            if (want.isEmpty() && scanner == null) Log.i(TAG, "tartı ayarlı değil, dinlenmiyor")
            synchronized(this) { apply(want) }
        }, "bileklik-tarti").start()
    }

    @SuppressLint("MissingPermission")
    private fun apply(want: String) {
        if (want == address && (scanner != null || want.isEmpty())) return
        stop()
        if (want.isEmpty()) return
        if (!granted()) {
            Log.w(TAG, "tartı dinlenemiyor: yakındaki cihazlar (tarama) izni yok")
            return
        }
        val s = context.getSystemService(BluetoothManager::class.java)?.adapter?.takeIf { it.isEnabled }?.bluetoothLeScanner
        if (s == null) {
            Log.i(TAG, "tartı dinlenemiyor: Bluetooth kapalı")
            return
        }
        try {
            val filter = filter(want)
            fast = false
            visible = BandService.appShown
            s.startScan(listOf(filter), settings(), callback)
            scanner = s
            address = want
            Log.i(TAG, "tartı dinleniyor")
        } catch (e: Exception) {
            Log.w(TAG, "tartı taraması başlatılamadı: ${e.javaClass.simpleName}")
        }
    }

    @SuppressLint("MissingPermission")
    fun stop() {
        val s = scanner ?: return
        scanner = null
        address = ""
        fast = false
        ui.removeCallbacks(slowDown)
        runCatching { s.stopScan(callback) }
    }

    companion object {
        const val TAG = "BileklikTarti"
        private const val FAST_MS = 45_000L
        private val XIAOMI = ParcelUuid.fromString("0000fe95-0000-1000-8000-00805f9b34fb")
    }
}

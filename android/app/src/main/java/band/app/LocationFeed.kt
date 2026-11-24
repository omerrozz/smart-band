package band.app

import android.Manifest
import android.annotation.SuppressLint
import android.content.Context
import android.content.pm.PackageManager
import android.location.Location
import android.location.LocationListener
import android.location.LocationManager
import android.os.Build
import android.os.Bundle
import android.os.HandlerThread
import android.os.SystemClock
import android.util.Log
import com.chaquo.python.Python

/**
 * Telefonun konumunu Python tarafına (bileklik_android.on_location) aktarır.
 *
 * Google Play Hizmetleri kullanılmaz: platformun LocationManager'ı, GPS sağlayıcısı 1 saniye aralıkla;
 * Android 12+ cihazda varsa birleşik (fused) sağlayıcı da dinlenir ama yalnızca GPS 5 saniyedir nokta
 * vermiyorsa kullanılır. Yalnızca açık hava antrenmanı sürerken çalışır (BandService açıp kapatır).
 * Süzme, mesafe ve iz appworkout.py'dedir. Günlüğe koordinat yazılmaz.
 */
class LocationFeed(private val context: Context) {
    private val manager = context.getSystemService(LocationManager::class.java)
    private var thread: HandlerThread? = null
    private var lastGps = 0L
    private var count = 0

    val running get() = thread != null

    private val listener = object : LocationListener {
        override fun onLocationChanged(location: Location) = deliver(location)

        override fun onProviderDisabled(provider: String) {
            if (provider == LocationManager.GPS_PROVIDER) problem(L10n.str(context, R.string.location_off))
        }

        override fun onProviderEnabled(provider: String) {
            if (provider == LocationManager.GPS_PROVIDER) problem("")
        }

        @Deprecated("Android 10 ve öncesi için")
        override fun onStatusChanged(provider: String?, status: Int, extras: Bundle?) = Unit
    }

    fun granted() =
        context.checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED

    /** Güncellemeleri başlatır; başlatılamazsa nedenini Python'a bildirir ve false döner. */
    @SuppressLint("MissingPermission")
    fun start(): Boolean {
        if (running) return true
        if (manager == null || !granted()) {
            problem(L10n.str(context, R.string.location_no_permission))
            return false
        }
        val t = HandlerThread("bileklik-konum").apply { start() }
        try {
            manager.requestLocationUpdates(LocationManager.GPS_PROVIDER, 1000L, 0f, listener, t.looper)
            var fused = false
            if (Build.VERSION.SDK_INT >= 31 && manager.hasProvider(LocationManager.FUSED_PROVIDER)) {
                manager.requestLocationUpdates(LocationManager.FUSED_PROVIDER, 1000L, 0f, listener, t.looper)
                fused = true
            }
            thread = t
            count = 0
            lastGps = 0L
            Log.i(TAG, "konum güncellemeleri açıldı (gps" + (if (fused) " + fused" else "") + ", 1 sn)")
            if (!manager.isProviderEnabled(LocationManager.GPS_PROVIDER)) problem(L10n.str(context, R.string.location_off))
            return true
        } catch (e: Exception) {  // SecurityException, IllegalArgumentException (sağlayıcı yok)
            Log.w(TAG, "konum güncellemeleri açılamadı: ${e.javaClass.simpleName}: ${e.message}")
            runCatching { manager.removeUpdates(listener) }
            t.quitSafely()
            problem(L10n.str(context, R.string.location_unavailable))
            return false
        }
    }

    fun stop() {
        val t = thread ?: return
        thread = null
        runCatching { manager?.removeUpdates(listener) }
        t.quitSafely()
        Log.i(TAG, "konum güncellemeleri kapandı ($count nokta iletildi)")
    }

    private fun deliver(l: Location) {
        if (!running) return
        val now = SystemClock.elapsedRealtime()
        val gps = l.provider == LocationManager.GPS_PROVIDER
        if (gps) lastGps = now else if (now - lastGps < 5000) return  // GPS varken birleşik sağlayıcı kullanılmaz
        // nokta zamanı: sağlayıcının zamanı (toplu teslimde de doğru); saatten çok sapıyorsa şimdiki zaman
        val wall = System.currentTimeMillis()
        val ts = (if (kotlin.math.abs(l.time - wall) > 60_000) wall else l.time) / 1000.0
        val accepted = try {
            Python.getInstance().getModule("bileklik_android").callAttr(
                "on_location", ts, l.latitude, l.longitude,
                if (l.hasAccuracy()) l.accuracy.toDouble() else -1.0,
                if (l.hasSpeed()) l.speed.toDouble() else -1.0,
                if (l.hasAltitude()) l.altitude else Double.NaN
            ).toBoolean()
        } catch (e: Throwable) {
            Log.w(TAG, "konum Python'a iletilemedi: ${e.javaClass.simpleName}")
            false
        }
        count++
        if (count == 1 || count % 60 == 0) {
            val acc = if (l.hasAccuracy()) "±${l.accuracy.toInt()} m" else "doğruluk bilinmiyor"
            Log.i(TAG, "konum #$count: $acc, ${l.provider}, " + if (accepted) "kabul edildi" else "süzüldü")
        }
    }

    private fun problem(text: String) {
        Log.i(TAG, if (text.isEmpty()) "konum yeniden kullanılabilir" else "konum sorunu: $text")
        Thread({
            try {
                if (Python.isStarted()) Python.getInstance().getModule("bileklik_android").callAttr("location_problem", text)
            } catch (e: Throwable) {
                Log.w(TAG, "konum sorunu iletilemedi: ${e.javaClass.simpleName}")
            }
        }, "bileklik-py").start()
    }

    companion object {
        const val TAG = "BileklikKonum"
    }
}

package band.app

import android.Manifest
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothSocket
import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import android.util.Log
import java.io.IOException
import java.util.UUID
import java.util.concurrent.CopyOnWriteArraySet
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit

/**
 * Python tarafındaki Transport'un (bileklik_android.py) çağırdığı Bluetooth köprüsü.
 *
 * Saat klasik Bluetooth RFCOMM'u SABİT 5. kanalda dinler. Android'in açık API'si yalnızca UUID ile
 * (SDP sorgusuyla) soket açar; bu yüzden önce gizli createRfcommSocket(int) yansımayla denenir,
 * olmazsa SPP UUID'siyle açık API'ye düşülür. Hangi yolun kullanıldığı günlüğe yazılır.
 * Günlüğe adres ya da anahtar yazılmaz.
 */
object BtBridge {
    const val TAG = "BileklikBt"

    const val IDLE = "idle"
    const val NO_PERMISSION = "no_permission"
    const val BT_OFF = "bt_off"
    const val PAIRING = "pairing"
    const val CONNECTING = "connecting"
    const val CONNECTED = "connected"
    const val AWAY = "away"

    private val SPP: UUID = UUID.fromString("00001101-0000-1000-8000-00805F9B34FB")

    @Volatile
    var status: String = IDLE
        private set

    /** Son başarılı bağlantıda kullanılan yol (tanılama için). */
    @Volatile
    var lastPath: String = ""
        private set

    val listeners = CopyOnWriteArraySet<(String) -> Unit>()

    @Volatile
    private var app: Context? = null

    /** Ayardaki saat adresi (son bağlanma denemesinden); yayınları süzmek için. Günlüğe yazılmaz. */
    @Volatile
    var address: String? = null
        private set

    @Volatile
    private var active: BtLink? = null

    fun isOurDevice(device: BluetoothDevice?): Boolean =
        device != null && address != null && device.address.equals(address, ignoreCase = true)

    /** Açık bağlantıyı kapatır; Python tarafı bunu "saat bağlantıyı kapattı" olarak görür ve yeniden dener. */
    fun closeActive(reason: String) {
        val link = active ?: return
        Log.i(TAG, "bağlantı kapatılıyor: $reason")
        link.close()
    }

    /** Bluetooth kapandı: bağlantı düşer, durum buna göre gösterilir. */
    fun adapterOff() {
        closeActive("Bluetooth kapandı")
        set(BT_OFF)
    }

    internal fun linkOpened(link: BtLink) {
        active = link
    }

    fun init(context: Context) {
        app = context.applicationContext
    }

    internal fun set(s: String) {
        if (status == s) return
        status = s
        Log.i(TAG, "durum: $s")
        for (l in listeners) runCatching { l(s) }
    }

    fun describe(context: Context, s: String = status): String = L10n.str(
        context,
        when (s) {
            NO_PERMISSION -> R.string.bt_no_permission
            BT_OFF -> R.string.bt_off
            PAIRING -> R.string.bt_pairing
            CONNECTING -> R.string.link_connecting
            CONNECTED -> R.string.link_connected
            AWAY -> R.string.bt_away
            else -> R.string.bt_idle
        }
    )

    /**
     * Eşleşmiş saate bağlanır ve bağlı bir [BtLink] döndürür. Python iş parçacığından çağrılır, bloklar.
     * Saat eşleşmemişse eşleştirmeyi başlatır ve IOException atar; Python döngüsü 20 sn sonra yeniden dener.
     */
    @JvmStatic
    @Throws(IOException::class)
    fun open(address: String, channel: Int, timeoutMs: Int): BtLink {
        val ctx = app ?: throw IOException("Köprü hazır değil")
        if (Build.VERSION.SDK_INT >= 31 &&
            ctx.checkSelfPermission(Manifest.permission.BLUETOOTH_CONNECT) != PackageManager.PERMISSION_GRANTED
        ) {
            set(NO_PERMISSION)
            throw IOException("Bluetooth izni yok")
        }
        val adapter = ctx.getSystemService(BluetoothManager::class.java)?.adapter
            ?: throw IOException("Bu cihazda Bluetooth yok")
        try {
            if (!adapter.isEnabled) {
                set(BT_OFF)
                throw IOException("Bluetooth kapalı")
            }
            val device = try {
                adapter.getRemoteDevice(address.trim().uppercase()).also { this.address = it.address }
            } catch (e: IllegalArgumentException) {
                throw IOException("Ayardaki saat adresi geçersiz")
            }
            when (device.bondState) {
                BluetoothDevice.BOND_BONDED -> Unit
                BluetoothDevice.BOND_BONDING -> {
                    set(PAIRING)
                    throw IOException("Eşleştirme sürüyor")
                }
                else -> {
                    set(PAIRING)
                    Log.i(TAG, "saat eşleşmemiş, eşleştirme başlatılıyor: ${startBond(device)}")
                    throw IOException("Saat eşleşmemiş; eşleştirme başlatıldı")
                }
            }
            set(CONNECTING)
            var last: Throwable? = null
            for ((name, make) in paths(device, channel)) {
                val sock = try {
                    make()
                } catch (e: Throwable) {
                    val cause = (e as? java.lang.reflect.InvocationTargetException)?.targetException ?: e
                    Log.w(TAG, "yol kullanılamıyor: $name (${cause.javaClass.simpleName})")
                    last = cause
                    continue
                }
                try {
                    connect(sock, timeoutMs)
                    lastPath = name
                    Log.i(TAG, "RFCOMM bağlandı, kullanılan yol: $name")
                    set(CONNECTED)
                    return BtLink(sock).also { linkOpened(it) }
                } catch (e: Throwable) {
                    Log.w(TAG, "bağlanamadı, yol: $name (${e.javaClass.simpleName}: ${e.message})")
                    last = e
                    runCatching { sock.close() }
                }
            }
            set(AWAY)
            throw IOException("Saate bağlanılamadı: ${last?.message ?: "bilinmeyen hata"}")
        } catch (e: SecurityException) {
            set(NO_PERMISSION)
            throw IOException("Bluetooth izni yok")
        }
    }

    /** Sırayla denenecek soket yolları. Gizli API'ler yansımayla çağrılır; yoksa atlanır. */
    private fun paths(device: BluetoothDevice, channel: Int): List<Pair<String, () -> BluetoothSocket>> = listOf(
        "gizli createRfcommSocket($channel)" to {
            BluetoothDevice::class.java.getMethod("createRfcommSocket", Int::class.javaPrimitiveType)
                .invoke(device, channel) as BluetoothSocket
        },
        "gizli createInsecureRfcommSocket($channel)" to {
            BluetoothDevice::class.java.getMethod("createInsecureRfcommSocket", Int::class.javaPrimitiveType)
                .invoke(device, channel) as BluetoothSocket
        },
        "açık API createRfcommSocketToServiceRecord(SPP UUID)" to {
            device.createRfcommSocketToServiceRecord(SPP)
        },
    )

    /** BluetoothSocket.connect() süre sınırı tanımaz; süre dolunca soketi kapatan bir bekçi kullanılır. */
    private fun connect(sock: BluetoothSocket, timeoutMs: Int) {
        val done = java.util.concurrent.atomic.AtomicBoolean(false)
        val guard = Thread({
            try {
                Thread.sleep(timeoutMs.toLong().coerceAtLeast(1000))
                if (!done.get()) runCatching { sock.close() }
            } catch (_: InterruptedException) {
            }
        }, "bt-connect-guard").apply { isDaemon = true; start() }
        try {
            sock.connect()
        } finally {
            done.set(true)
            guard.interrupt()
        }
    }

    /** Saat çift modludur; mümkünse eşleştirme klasik Bluetooth (BR/EDR) üzerinden istenir. */
    private fun startBond(device: BluetoothDevice): String {
        try {
            val m = BluetoothDevice::class.java.getMethod("createBond", Int::class.javaPrimitiveType)
            if (m.invoke(device, 1 /* TRANSPORT_BREDR */) == true) return "createBond(BR/EDR)"
        } catch (e: Throwable) {
            Log.w(TAG, "createBond(BR/EDR) kullanılamıyor (${e.javaClass.simpleName})")
        }
        return if (device.createBond()) "createBond()" else "createBond() reddedildi"
    }

    internal fun linkClosed(link: BtLink) {
        if (active === link) active = null
        if (status == CONNECTED) set(AWAY)
    }
}

/** Açık bir RFCOMM bağlantısı. Okuma ayrı bir iş parçacığında yapılır ki süre sınırı konabilsin. */
class BtLink internal constructor(private val sock: BluetoothSocket) {
    private val queue = LinkedBlockingQueue<ByteArray>()
    private var pending: ByteArray? = null
    private var offset = 0
    private val out = sock.outputStream

    @Volatile
    private var closed = false

    init {
        Thread({
            val buf = ByteArray(4096)
            try {
                val input = sock.inputStream
                while (true) {
                    val n = input.read(buf)
                    if (n < 0) break
                    if (n > 0) queue.put(buf.copyOf(n))
                }
            } catch (e: IOException) {
                if (!closed) Log.i(BtBridge.TAG, "bağlantı koptu (${e.message})")
            } finally {
                queue.put(EOF)
                BtBridge.linkClosed(this)
            }
        }, "bt-reader").apply { isDaemon = true; start() }
    }

    /**
     * En çok [max] bayt döndürür. [timeoutMs] içinde veri gelmezse null (Python'da socket.timeout),
     * bağlantı kapandıysa boş dizi döner (Python'da "Saat bağlantıyı kapattı"). timeoutMs < 0: süresiz.
     */
    fun read(max: Int, timeoutMs: Int): ByteArray? {
        var cur = pending
        if (cur == null) {
            cur = if (timeoutMs < 0) queue.take() else queue.poll(timeoutMs.toLong(), TimeUnit.MILLISECONDS)
            if (cur == null) return null
            if (cur === EOF) {
                queue.put(EOF)  // sonraki okumalar da kapanmış görsün
                return EOF
            }
            offset = 0
        }
        val n = minOf(max.coerceAtLeast(1), cur.size - offset)
        val chunk = cur.copyOfRange(offset, offset + n)
        offset += n
        pending = if (offset >= cur.size) null else cur
        return chunk
    }

    @Synchronized
    @Throws(IOException::class)
    fun write(data: ByteArray) {
        out.write(data)
        out.flush()
    }

    fun close() {
        closed = true
        runCatching { sock.close() }
        BtBridge.linkClosed(this)
    }

    private companion object {
        val EOF = ByteArray(0)
    }
}

package band.app

import android.app.KeyguardManager
import android.app.Notification
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.media.AudioManager
import android.media.MediaMetadata
import android.media.browse.MediaBrowser
import android.media.session.MediaController
import android.media.session.MediaSessionManager
import android.media.session.PlaybackState
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.os.SystemClock
import android.provider.Settings
import android.service.media.MediaBrowserService
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import android.util.Log
import android.view.KeyEvent
import org.json.JSONObject

/** Bildirim ve müzik tercihleri. Hepsi varsayılan olarak kapalıdır; hiçbir uygulama seçili gelmez. */
object PhonePrefs {
    const val NOTIFICATIONS = "notifications"   // seçilen uygulamaların bildirimleri saate gider
    const val MUSIC = "music"                   // çalan parça saate gider, saatin düğmeleri telefonu yönetir
    const val CALLS = "calls"                   // gelen arama saatte gösterilir
    const val SILENT = "silent"                 // sessiz bildirimler de
    const val DND = "dnd"                       // Rahatsız etmeyin'de de gönder
    const val SCREEN = "screen"                 // ekran açıkken gönderme (varsayılan açık)

    private fun sp(c: Context) = c.getSharedPreferences("ayar", Context.MODE_PRIVATE)

    fun get(c: Context, key: String) = sp(c).getBoolean("phone_$key", key == SCREEN)
    fun set(c: Context, key: String, on: Boolean) = sp(c).edit().putBoolean("phone_$key", on).apply()

    /** Tercih edilen oynatıcının paketi; boş: son çalan. */
    fun player(c: Context): String = sp(c).getString("phone_player", "") ?: ""
    fun setPlayer(c: Context, pkg: String) = sp(c).edit().putString("phone_player", pkg).apply()

    /** Medya oturumu en son görülen uygulama: çalan yokken saatten başlatılacak olan. */
    fun lastPlayer(c: Context): String = sp(c).getString("phone_last_player", "") ?: ""
    fun setLastPlayer(c: Context, pkg: String) {
        if (lastPlayer(c) != pkg) sp(c).edit().putString("phone_last_player", pkg).apply()
    }

    fun label(c: Context, pkg: String): String = try {
        c.packageManager.getApplicationLabel(c.packageManager.getApplicationInfo(pkg, 0)).toString()
    } catch (e: Exception) {
        pkg
    }

    fun apps(c: Context): Set<String> = HashSet(sp(c).getStringSet("phone_apps", emptySet()) ?: emptySet())
    fun setApp(c: Context, pkg: String, on: Boolean) {
        val next = apps(c).toMutableSet()
        if (on) next.add(pkg) else next.remove(pkg)
        sp(c).edit().putStringSet("phone_apps", next).apply()
    }
}

/**
 * Telefonun bildirimlerini ve müzik oturumunu izler. Kullanıcı sistem ayarlarından "bildirim erişimi"
 * vermedikçe sistem bu servisi hiç bağlamaz.
 *
 * Yalnızca olayı toplar ve saatten gelen isteği uygular; saat protokolü Python'dadır (bandmsg.py,
 * server.phone_notify / music_state). Bildirim metni yalnızca saate Bluetooth üzerinden gider:
 * ağa gönderilmez, diske yazılmaz, günlüğe yalnızca paket adı ve uzunluklar yazılır.
 */
class NotifyService : NotificationListenerService() {

    private val ui = Handler(Looper.getMainLooper())
    private var controller: MediaController? = null
    private var waker: MediaBrowser? = null               // kapalı oynatıcıyı uyandıran geçici bağlantı
    private var lastController: MediaController? = null   // oturumu kapansa da düğme gönderilebilsin diye tutulur
    private var lastTrack = ""                            // çalan yokken saatte gösterilecek son parça (yalnızca bellekte)
    private var lastArtist = ""
    private var sessionsWatched = false
    private var callKey: String? = null
    private var decline: PendingIntent? = null

    private val sessions = MediaSessionManager.OnActiveSessionsChangedListener { list -> pick(list ?: emptyList()) }

    private val mediaEvents = object : MediaController.Callback() {
        override fun onPlaybackStateChanged(state: PlaybackState?) = pushMusic()
        override fun onMetadataChanged(metadata: MediaMetadata?) = pushMusic()
        override fun onAudioInfoChanged(info: MediaController.PlaybackInfo) = pushMusic()
        override fun onSessionDestroyed() = refreshMedia()
    }

    override fun onListenerConnected() {
        Log.i(TAG, "bildirim erişimi bağlandı")
        instance = this
        refreshMedia()
    }

    override fun onListenerDisconnected() {
        Log.i(TAG, "bildirim erişimi kesildi")
        stopMedia()
        instance = null
    }

    override fun onDestroy() {
        stopMedia()
        if (instance === this) instance = null
        super.onDestroy()
    }

    // ------------------------------------------------------------ bildirimler
    override fun onNotificationPosted(sbn: StatusBarNotification, rankingMap: RankingMap) {
        try {
            handlePosted(sbn, rankingMap)
        } catch (e: Exception) {
            Log.w(TAG, "bildirim işlenemedi (${sbn.packageName}): ${e.javaClass.simpleName}")
        }
    }

    private fun handlePosted(sbn: StatusBarNotification, rankingMap: RankingMap) {
        val n = sbn.notification ?: return
        val pkg = sbn.packageName
        if (pkg == packageName) return
        val extras = n.extras
        val title = extras.getCharSequence(Notification.EXTRA_TITLE)?.toString()?.trim().orEmpty()
        val text = (extras.getCharSequence(Notification.EXTRA_BIG_TEXT) ?: extras.getCharSequence(Notification.EXTRA_TEXT))
            ?.toString()?.trim().orEmpty()

        // gelen arama: arama uygulamasının bildirimi (ek izin gerekmez)
        if (n.category == Notification.CATEGORY_CALL) {
            if (!PhonePrefs.get(this, PhonePrefs.CALLS)) return
            val type = extras.getInt(EXTRA_CALL_TYPE, 0)
            val ringing = type == CALL_TYPE_INCOMING || (type == 0 && n.fullScreenIntent != null)
            if (ringing && callKey == null) {
                if (dndBlocks()) return
                callKey = sbn.key
                decline = if (Build.VERSION.SDK_INT >= 33) {
                    extras.getParcelable(EXTRA_DECLINE_INTENT, PendingIntent::class.java)
                } else {
                    @Suppress("DEPRECATION") extras.getParcelable(EXTRA_DECLINE_INTENT)
                }
                Log.i(TAG, "gelen arama: $pkg, ad ${title.length} karakter, reddetme düğmesi ${if (decline != null) "var" else "yok"}")
                BandService.py("phone_call", title, text)
            } else if (!ringing && sbn.key == callKey) {
                endCall("yanıtlandı")
            }
            return
        }

        if (!PhonePrefs.get(this, PhonePrefs.NOTIFICATIONS) || pkg !in PhonePrefs.apps(this)) return
        val skip = when {
            sbn.isOngoing || n.flags and Notification.FLAG_FOREGROUND_SERVICE != 0 -> "süren"
            n.flags and Notification.FLAG_GROUP_SUMMARY != 0 -> "grup özeti"
            extras.containsKey(Notification.EXTRA_MEDIA_SESSION) || n.category == Notification.CATEGORY_TRANSPORT -> "medya"
            title.isEmpty() && text.isEmpty() -> "boş"
            silent(sbn, rankingMap) && !PhonePrefs.get(this, PhonePrefs.SILENT) -> "sessiz"
            dndBlocks() -> "rahatsız etmeyin"
            PhonePrefs.get(this, PhonePrefs.SCREEN) && inUse() -> "ekran açık"
            else -> null
        }
        if (skip != null) {
            Log.d(TAG, "bildirim atlandı ($skip): $pkg")
            return
        }
        val app = try {
            packageManager.getApplicationLabel(packageManager.getApplicationInfo(pkg, 0)).toString()
        } catch (e: Exception) {
            pkg
        }
        // metin günlüğe yazılmaz: yalnızca paket ve uzunluklar
        Log.i(TAG, "bildirim: $pkg, başlık ${title.length}, metin ${text.length} karakter")
        BandService.py("phone_notify", sbn.key, pkg, app, title, text, sbn.postTime)
    }

    override fun onNotificationRemoved(sbn: StatusBarNotification) {
        if (sbn.key == callKey) {
            endCall("bitti")
            return
        }
        if (sbn.packageName in PhonePrefs.apps(this)) BandService.py("phone_remove", sbn.key)
    }

    private fun endCall(why: String) {
        Log.i(TAG, "arama $why")
        callKey = null
        decline = null
        BandService.py("phone_call_end")
    }

    private fun silent(sbn: StatusBarNotification, map: RankingMap): Boolean {
        val r = Ranking()
        return map.getRanking(sbn.key, r) && r.importance < NotificationManager.IMPORTANCE_DEFAULT
    }

    private fun dndBlocks() =
        currentInterruptionFilter != INTERRUPTION_FILTER_ALL && currentInterruptionFilter != INTERRUPTION_FILTER_UNKNOWN &&
            !PhonePrefs.get(this, PhonePrefs.DND)

    /** Kullanıcı telefonu o an kullanıyor mu: ekran açık ve kilit ekranında değil. */
    private fun inUse() = getSystemService(PowerManager::class.java).isInteractive &&
        !getSystemService(KeyguardManager::class.java).isKeyguardLocked

    // ------------------------------------------------------------ saatten gelen istekler
    private fun onBand(event: String, data: JSONObject) {
        when (event) {
            "notification_dismiss" -> {  // saatte kapatıldı: telefonda da kapanır
                Log.i(TAG, "saatte kapatılan bildirim telefonda kapatılıyor: ${data.optString("package")}")
                runCatching { cancelNotification(data.getString("key")) }
            }
            "call" -> if (data.optString("action") == "reject") {
                val p = decline
                Log.i(TAG, "saatten arama reddi: " + if (p != null) "uygulanıyor" else "arama bildiriminde reddetme düğmesi yok")
                runCatching { p?.send() }
            } else {
                Log.i(TAG, "saatten arama yok sayıldı")
            }
            "media" -> if (PhonePrefs.get(this, PhonePrefs.MUSIC)) mediaKey(data)
        }
    }

    // ------------------------------------------------------------ müzik
    /** Tercih ya da oturumlar değişince: izlemeyi kurar ya da kaldırır. */
    fun refreshMedia() {
        ui.post {
            if (!PhonePrefs.get(this, PhonePrefs.MUSIC)) {
                if (sessionsWatched) {
                    stopMedia()
                    BandService.py("music", "", volume())
                }
                return@post
            }
            try {
                val manager = getSystemService(MediaSessionManager::class.java)
                val me = ComponentName(this, NotifyService::class.java)
                if (!sessionsWatched) {
                    manager.addOnActiveSessionsChangedListener(sessions, me, ui)
                    sessionsWatched = true
                }
                pick(manager.getActiveSessions(me))
            } catch (e: SecurityException) {
                Log.w(TAG, "medya oturumları okunamadı: bildirim erişimi yok")
            }
        }
    }

    private fun stopMedia() {
        if (sessionsWatched) {
            runCatching { getSystemService(MediaSessionManager::class.java).removeOnActiveSessionsChangedListener(sessions) }
            sessionsWatched = false
        }
        controller?.let { c -> runCatching { c.unregisterCallback(mediaEvents) } }
        controller = null
    }

    /**
     * İzlenecek oturum: tercih edilen oynatıcının oturumu varsa o (başka bir uygulama sonradan etkin olsa da);
     * yoksa çalan oturum, o da yoksa listenin ilki (sistemin öncelik sırası).
     */
    private fun pick(list: List<MediaController>) {
        val preferred = PhonePrefs.player(this)
        val next = (if (preferred.isEmpty()) null else list.firstOrNull { it.packageName == preferred })
            ?: list.firstOrNull { it.playbackState?.state == PlaybackState.STATE_PLAYING } ?: list.firstOrNull()
        if (next?.sessionToken != controller?.sessionToken) {
            controller?.let { c -> runCatching { c.unregisterCallback(mediaEvents) } }
            controller = next
            next?.registerCallback(mediaEvents, ui)
            Log.i(TAG, "medya oturumu: " + (next?.packageName ?: "yok") +
                if (next != null && next.packageName == preferred) " (tercih edilen)" else "")
            if (next != null) {
                lastController = next
                PhonePrefs.setLastPlayer(this, next.packageName)
            }
        }
        pushMusic()
    }

    /** Çalan yokken saatten başlatılacak uygulama: tercih edilen, yoksa oturumu en son görülen. */
    private fun startTarget(): String = PhonePrefs.player(this).ifEmpty { PhonePrefs.lastPlayer(this) }

    private fun volume(): Int {
        val a = getSystemService(AudioManager::class.java)
        val max = a.getStreamMaxVolume(AudioManager.STREAM_MUSIC)
        return if (max > 0) a.getStreamVolume(AudioManager.STREAM_MUSIC) * 100 / max else 0
    }

    /** Çalanı Python'a verir; saate yalnızca değişince yazılır (karar server.music_state'te). */
    private fun pushMusic() {
        if (!PhonePrefs.get(this, PhonePrefs.MUSIC)) return
        val c = controller
        val state = c?.playbackState
        val meta = c?.metadata
        if (c == null || state == null || meta == null) {
            // Çalan yok. Kaynak (Gadgetbridge) bu durumda "yok" (0) yollar ve saat düğmelerini göstermeyebilir;
            // başlatılabilecek bir oynatıcı biliniyorsa saatin çal düğmesi kullanılabilsin diye son bilinen parça
            // (yoksa uygulamanın adı) "duraklatıldı" olarak gönderilir.
            val target = startTarget()
            if (target.isEmpty()) {
                BandService.py("music", "", volume())
            } else {
                val idle = JSONObject().put("state", "paused")
                    .put("track", lastTrack.ifEmpty { PhonePrefs.label(this, target) })
                    .put("artist", if (lastTrack.isEmpty()) "" else lastArtist)
                    .put("position", 0).put("duration", 0)
                BandService.py("music", idle.toString(), volume())
            }
            return
        }
        val playing = state.state == PlaybackState.STATE_PLAYING
        var position = state.position
        if (playing && state.lastPositionUpdateTime > 0) {
            position += ((SystemClock.elapsedRealtime() - state.lastPositionUpdateTime) * state.playbackSpeed).toLong()
        }
        lastTrack = meta.getString(MediaMetadata.METADATA_KEY_TITLE) ?: ""
        lastArtist = meta.getString(MediaMetadata.METADATA_KEY_ARTIST) ?: meta.getString(MediaMetadata.METADATA_KEY_ALBUM_ARTIST) ?: ""
        val json = JSONObject()
            .put("state", if (playing) "playing" else "paused")
            .put("track", lastTrack)
            .put("artist", lastArtist)
            .put("album", meta.getString(MediaMetadata.METADATA_KEY_ALBUM) ?: "")
            .put("position", maxOf(0L, position) / 1000)
            .put("duration", maxOf(0L, meta.getLong(MediaMetadata.METADATA_KEY_DURATION)) / 1000)
        BandService.py("music", json.toString(), volume())
    }

    private fun mediaKey(data: JSONObject) {
        val key = data.optString("key")
        Log.i(TAG, "saatten medya düğmesi: $key")
        val audio = getSystemService(AudioManager::class.java)
        if (key == "volume") {
            val dir = if (data.optString("direction") == "up") AudioManager.ADJUST_RAISE else AudioManager.ADJUST_LOWER
            audio.adjustStreamVolume(AudioManager.STREAM_MUSIC, dir, 0)
            ui.postDelayed({ pushMusic() }, 200)
            return
        }
        val t = controller?.transportControls
        if (t != null) {
            when (key) {
                "play" -> t.play()
                "pause" -> t.pause()
                "next" -> t.skipToNext()
                "previous" -> t.skipToPrevious()
            }
        } else {  // izlenen oturum yok: oynatıcı medya düğmesiyle başlatılır (etkinlik açılmaz)
            val code = when (key) {
                "play", "pause" -> KeyEvent.KEYCODE_MEDIA_PLAY
                "next" -> KeyEvent.KEYCODE_MEDIA_NEXT
                "previous" -> KeyEvent.KEYCODE_MEDIA_PREVIOUS
                else -> return
            }
            startPlayer(code)
            ui.postDelayed({ refreshMedia() }, 2500)  // oturum açıldıysa izlenmeye başlar
        }
    }

    /**
     * Çalan yokken oynatıcıyı başlatır. Sırayla: (1) hedef uygulamanın hâlâ açık olan oturumu, (2) uygulama
     * kapalıysa medya tarayıcı servisine bağlanıp (bu, uygulamayı arka planda uyandırır) oturumuna "çal" demek,
     * (3) medya düğmesi alıcısına yayın, (4) tercih yoksa sistemin medya tuşu. Etkinlik açılmaz.
     */
    private fun startPlayer(code: Int) {
        val target = startTarget()
        val down = KeyEvent(KeyEvent.ACTION_DOWN, code)
        val up = KeyEvent(KeyEvent.ACTION_UP, code)
        val old = lastController
        if (old != null && (target.isEmpty() || old.packageName == target) && liveSession(old.packageName) != null) {
            val ok = try {
                old.dispatchMediaButtonEvent(down) && old.dispatchMediaButtonEvent(up)
            } catch (e: Exception) {
                false
            }
            if (ok) {
                Log.i(TAG, "oynatıcı başlatılıyor: ${old.packageName} (açık oturumu üzerinden)")
                return
            }
        }
        if (target.isNotEmpty() && wake(target, code)) return
        startByKey(target, down, up)
    }

    /** Hedef uygulamanın şu an açık olan medya oturumu (yoksa null). */
    private fun liveSession(pkg: String): MediaController? = try {
        getSystemService(MediaSessionManager::class.java)
            .getActiveSessions(ComponentName(this, NotifyService::class.java)).firstOrNull { it.packageName == pkg }
    } catch (e: SecurityException) {
        null
    }

    private fun press(c: MediaController, code: Int) {
        val t = c.transportControls
        when (code) {
            KeyEvent.KEYCODE_MEDIA_NEXT -> t.skipToNext()
            KeyEvent.KEYCODE_MEDIA_PREVIOUS -> t.skipToPrevious()
            else -> t.play()
        }
    }

    /**
     * Kapalı oynatıcıyı medya tarayıcı servisine bağlanarak uyandırır. Bağlantı kabul edilirse verilen oturuma,
     * edilmezse (uygulama yabancı istemciyi reddedebilir) servis yine de ayağa kalktığı için açılan oturuma
     * "çal" denir. Servis yoksa false döner.
     */
    private fun wake(target: String, code: Int): Boolean {
        val service = try {
            packageManager.queryIntentServices(Intent(MediaBrowserService.SERVICE_INTERFACE).setPackage(target), 0)
                .firstOrNull()?.serviceInfo
        } catch (e: Exception) {
            null
        } ?: return false
        runCatching { waker?.disconnect() }
        var done = false
        val viaSessions = Runnable {
            if (!done) {
                done = true
                val c = liveSession(target)
                if (c != null) {
                    press(c, code)
                    Log.i(TAG, "oynatıcı başlatılıyor: $target (uyandırıldı, açılan oturum üzerinden)")
                } else {
                    Log.i(TAG, "oynatıcı uyandırılamadı: $target oturum açmadı; medya düğmesi deneniyor")
                    startByKey(target, KeyEvent(KeyEvent.ACTION_DOWN, code), KeyEvent(KeyEvent.ACTION_UP, code))
                }
                refreshMedia()
            }
        }
        val browser = try {
            MediaBrowser(this, ComponentName(service.packageName, service.name), object : MediaBrowser.ConnectionCallback() {
                override fun onConnected() {
                    if (done) return
                    val b = waker ?: return
                    done = true
                    ui.removeCallbacks(viaSessions)
                    runCatching { press(MediaController(this@NotifyService, b.sessionToken), code) }
                    Log.i(TAG, "oynatıcı başlatılıyor: $target (uyandırıldı, medya tarayıcı oturumu üzerinden)")
                    ui.postDelayed({ refreshMedia() }, 1500)
                }
                override fun onConnectionFailed() { ui.removeCallbacks(viaSessions); ui.postDelayed(viaSessions, 1200) }
                override fun onConnectionSuspended() {}
            }, null)
        } catch (e: Exception) {
            return false
        }
        waker = browser
        return try {
            browser.connect()
            ui.postDelayed(viaSessions, 4000)   // yanıt hiç gelmezse
            ui.postDelayed({ if (waker === browser) { runCatching { browser.disconnect() }; waker = null } }, 30000)
            true
        } catch (e: Exception) {
            waker = null
            false
        }
    }

    /**
     * Son çare: sistemin medya tuşu. Yalnızca sistem, kapalı bir oynatıcının arka planda çalmaya başlamasına
     * izin verebilir (uygulamadan gelen doğrudan yayını Android engeller). Sistem tuşu, medya düğmesini en son
     * kullanan oynatıcıya iletir; bu çoğunlukla hedefle aynıdır, değilse günlüğe yazılır.
     */
    private fun startByKey(target: String, down: KeyEvent, up: KeyEvent) {
        val audio = getSystemService(AudioManager::class.java)
        audio.dispatchMediaKeyEvent(down)
        audio.dispatchMediaKeyEvent(up)
        Log.i(TAG, "oynatıcı başlatılıyor: sistemin medya tuşuyla (hedef: ${target.ifEmpty { "bilinmiyor" }})")
        ui.postDelayed({
            val got = try {
                getSystemService(MediaSessionManager::class.java)
                    .getActiveSessions(ComponentName(this, NotifyService::class.java)).map { it.packageName }
            } catch (e: SecurityException) {
                emptyList()
            }
            Log.i(TAG, "sistem tuşundan sonra açık oturumlar: " + (if (got.isEmpty()) "yok" else got.joinToString()))
            refreshMedia()
        }, 3000)
    }

    companion object {
        const val TAG = "BileklikBildirim"
        private const val EXTRA_CALL_TYPE = "android.callType"          // Notification.EXTRA_CALL_TYPE
        private const val EXTRA_DECLINE_INTENT = "android.declineIntent" // Notification.EXTRA_DECLINE_INTENT
        private const val CALL_TYPE_INCOMING = 1

        @Volatile
        private var instance: NotifyService? = null

        /** Kullanıcı sistem ayarlarında bu uygulamaya bildirim erişimi vermiş mi. */
        fun accessGranted(context: Context): Boolean {
            val me = ComponentName(context, NotifyService::class.java)
            if (Build.VERSION.SDK_INT >= 27) {
                return context.getSystemService(NotificationManager::class.java).isNotificationListenerAccessGranted(me)
            }
            val on = Settings.Secure.getString(context.contentResolver, "enabled_notification_listeners") ?: ""
            return on.split(':').any { ComponentName.unflattenFromString(it) == me }
        }

        /** Müzik tercihi değişti. */
        fun refresh() {
            instance?.refreshMedia()
        }

        /** Python'dan (saatten) gelen istek; ana iş parçacığında uygulanır. */
        fun onBandEvent(event: String, json: String) {
            val s = instance
            if (s == null) {
                Log.i(TAG, "saatten istek geldi ama bildirim erişimi bağlı değil: $event")
                return
            }
            val data = try {
                JSONObject(json)
            } catch (e: Exception) {
                return
            }
            s.ui.post { runCatching { s.onBand(event, data) } }
        }
    }
}

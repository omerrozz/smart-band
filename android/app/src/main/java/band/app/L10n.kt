package band.app

import android.content.Context
import android.content.res.Configuration
import java.util.Locale
import java.util.concurrent.CopyOnWriteArraySet

/**
 * Yerel (WebView dışı) metinlerin dili. Varsayılan: sistemin dili (res/values = İngilizce, res/values-tr = Türkçe).
 * Sayfada bir dil seçildiyse (BileklikHost.setLanguage ya da sunucu durumundaki "lang") o dil kullanılır;
 * seçim SharedPreferences'ta durur. Bütün metin aramaları [str] üzerinden yapılır.
 */
object L10n {
    private const val KEY = "lang"

    @Volatile
    private var lang: String? = null   // null: henüz okunmadı; "": sistemin dili

    @Volatile
    private var wrapped: Pair<String, Context>? = null

    /** Dil değişince çağrılır (herhangi bir iş parçacığından). */
    val listeners = CopyOnWriteArraySet<() -> Unit>()

    private fun sp(c: Context) = c.getSharedPreferences("ayar", Context.MODE_PRIVATE)

    /** Uygulamada seçilen dil: "tr", "en" ya da "" (sistemin dili). */
    fun language(c: Context): String =
        lang ?: (sp(c).getString(KEY, "") ?: "").also { lang = it }

    /**
     * Uygulamada seçilen dili kaydeder. "tr" / "en" (bölge eki yok sayılır); başka her değer (boş, null, "auto")
     * sistemin diline döner. Değiştiyse true döner ve dinleyicileri çağırır.
     */
    fun setLanguage(c: Context, value: String?): Boolean {
        val tag = value.orEmpty().trim().lowercase(Locale.ROOT).substringBefore('-').substringBefore('_')
        val next = if (tag == "tr" || tag == "en") tag else ""
        synchronized(this) {
            if (next == language(c)) return false
            sp(c).edit().putString(KEY, next).apply()
            lang = next
            wrapped = null
        }
        for (l in listeners) runCatching { l() }
        return true
    }

    /** Metinlerin okunacağı bağlam: dil seçilmediyse [c]'nin kendisi, seçildiyse o dile çevrilmiş bir bağlam. */
    fun context(c: Context): Context {
        val want = language(c)
        if (want.isEmpty()) return c
        wrapped?.let { if (it.first == want) return it.second }
        val app = c.applicationContext ?: c
        val config = Configuration(app.resources.configuration).apply { setLocale(Locale.forLanguageTag(want)) }
        return app.createConfigurationContext(config).also { wrapped = want to it }
    }

    fun str(c: Context, id: Int, vararg args: Any?): String =
        if (args.isEmpty()) context(c).getString(id) else context(c).getString(id, *args)

    /** Gösterilen metinlerin dili (tarih biçimi, sıralama ve arama için): kaynaklardaki locale_tag. */
    fun locale(c: Context): Locale = Locale.forLanguageTag(str(c, R.string.locale_tag))
}

package band.app

import android.annotation.SuppressLint
import android.app.Activity
import android.content.Intent
import android.graphics.Color
import android.graphics.drawable.Drawable
import android.text.Editable
import android.text.InputType
import android.text.TextWatcher
import android.util.LruCache
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.BaseAdapter
import android.widget.Button
import android.widget.EditText
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.ListView
import android.widget.Switch
import android.widget.TextView
import java.text.Collator
import java.util.concurrent.Executors

/**
 * Bildirimi saate iletilecek uygulamaların seçimi. Başlatıcıda görünen uygulamalar listelenir
 * (manifestteki <queries> ile; tüm paketleri görme izni kullanılmaz). Liste geri dönüşümlüdür ve
 * simgeler arka planda yüklenir; birkaç yüz uygulamada da akıcıdır.
 */
class AppPickerPanel(private val activity: Activity, private val onClose: () -> Unit) {

    /** Tek seçim kipi (tercih edilen oynatıcı): doluysa satıra dokununca seçilir ve panel kapanır. */
    private var single: ((String) -> Unit)? = null
    private var current = ""
    private val titleView = TextView(activity).apply { textSize = 22f; setTextColor(Color.parseColor("#f1efff")) }

    private class App(val pkg: String, val label: String)

    private val ink = Color.parseColor("#f1efff")
    private val mut = Color.parseColor("#b3afd8")
    private fun dp(v: Int) = (v * activity.resources.displayMetrics.density).toInt()
    private fun s(id: Int, vararg args: Any?) = L10n.str(activity, id, *args)
    private val backButton = Button(activity)

    private var all: List<App> = emptyList()
    private var shown: List<App> = emptyList()
    private var chosen: MutableSet<String> = HashSet()
    private val icons = LruCache<String, Drawable>(300)
    private val loader = Executors.newFixedThreadPool(2) { r -> Thread(r, "bileklik-simge").apply { isDaemon = true } }
    private val count = TextView(activity).apply { textSize = 14f; setTextColor(mut) }
    private val search = EditText(activity)
    private val list = ListView(activity)
    val view: View

    private val adapter = object : BaseAdapter() {
        override fun getCount() = shown.size
        override fun getItem(position: Int): Any = shown[position]
        override fun getItemId(position: Int) = position.toLong()

        @SuppressLint("UseSwitchCompatOrMaterialCode")
        override fun getView(position: Int, convertView: View?, parent: ViewGroup): View {
            val row = (convertView as? LinearLayout) ?: LinearLayout(activity).apply {
                orientation = LinearLayout.HORIZONTAL
                gravity = Gravity.CENTER_VERTICAL
                setPadding(dp(20), dp(8), dp(20), dp(8))
                addView(ImageView(activity), LinearLayout.LayoutParams(dp(40), dp(40)))
                addView(TextView(activity).apply { textSize = 16f; setTextColor(ink); maxLines = 2 },
                    LinearLayout.LayoutParams(0, -2, 1f).apply { marginStart = dp(14); marginEnd = dp(10) })
                addView(Switch(activity).apply { isClickable = false; isFocusable = false })
            }
            val app = shown[position]
            val icon = row.getChildAt(0) as ImageView
            (row.getChildAt(1) as TextView).text = app.label
            (row.getChildAt(2) as Switch).isChecked = if (single != null) app.pkg == current else app.pkg in chosen
            row.tag = app.pkg
            val cached = icons.get(app.pkg)
            icon.setImageDrawable(cached)
            if (cached == null) {
                loader.execute {
                    val d = try {
                        activity.packageManager.getApplicationIcon(app.pkg)
                    } catch (e: Exception) {
                        return@execute
                    }
                    icons.put(app.pkg, d)
                    activity.runOnUiThread { if (row.tag == app.pkg) icon.setImageDrawable(d) }
                }
            }
            return row
        }
    }

    init {
        val box = LinearLayout(activity).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(activity.getColor(R.color.bg))
            isClickable = true
        }
        val head = LinearLayout(activity).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(20), dp(16), dp(20), dp(8))
            addView(backButton.apply { setOnClickListener { onClose() } }, LinearLayout.LayoutParams(-2, -2))
            addView(titleView,
                LinearLayout.LayoutParams(-1, -2).apply { topMargin = dp(6) })
            addView(count, LinearLayout.LayoutParams(-1, -2).apply { topMargin = dp(4) })
            addView(search.apply {
                setSingleLine()
                setTextColor(ink)
                setHintTextColor(mut)
                inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS
                addTextChangedListener(object : TextWatcher {
                    override fun afterTextChanged(s: Editable?) = filter()
                    override fun beforeTextChanged(s: CharSequence?, a: Int, b: Int, c: Int) = Unit
                    override fun onTextChanged(s: CharSequence?, a: Int, b: Int, c: Int) = Unit
                })
            }, LinearLayout.LayoutParams(-1, -2).apply { topMargin = dp(6) })
        }
        list.adapter = adapter
        list.divider = null
        list.setOnItemClickListener { _, row, position, _ ->
            val app = shown.getOrNull(position) ?: return@setOnItemClickListener
            single?.let { done ->
                done(app.pkg)
                onClose()
                return@setOnItemClickListener
            }
            val on = app.pkg !in chosen
            if (on) chosen.add(app.pkg) else chosen.remove(app.pkg)
            PhonePrefs.setApp(activity, app.pkg, on)
            ((row as LinearLayout).getChildAt(2) as Switch).isChecked = on
            showCount()
        }
        box.addView(head, LinearLayout.LayoutParams(-1, -2))
        box.addView(list, LinearLayout.LayoutParams(-1, 0, 1f))
        view = box
        retitle()
    }

    /** Dile bağlı kalıcı metinler; panel kurulurken, her açılışta ve dil değişince yazılır. */
    fun retitle() {
        backButton.text = s(R.string.back)
        search.hint = s(R.string.picker_search_hint)
        titleView.text = s(if (single != null) R.string.picker_title_player else R.string.picker_title_apps)
        showCount()
    }

    private fun showCount() {
        count.text = when {
            all.isEmpty() -> s(R.string.picker_loading)
            single != null -> s(R.string.picker_player_note)
            else -> s(R.string.picker_count, chosen.size, all.size)
        }
    }

    private fun filter() {
        val locale = L10n.locale(activity)
        val q = search.text.toString().trim().lowercase(locale)
        shown = if (q.isEmpty()) all else all.filter { it.label.lowercase(locale).contains(q) || it.pkg.contains(q) }
        adapter.notifyDataSetChanged()
    }

    /** Panel açılırken: seçimleri ve uygulama listesini yeniler (seçililer üstte, sonra ada göre). */
    fun reload(pick: ((String) -> Unit)? = null, selected: String = "") {
        single = pick
        current = selected
        retitle()
        search.setText("")
        all = emptyList()
        shown = emptyList()
        adapter.notifyDataSetChanged()
        chosen = if (pick != null) hashSetOf(selected) else PhonePrefs.apps(activity).toMutableSet()
        showCount()
        val defaultPlayer = s(R.string.player_default)
        val locale = L10n.locale(activity)
        Thread({
            val pm = activity.packageManager
            val launch = Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER)
            val collator = Collator.getInstance(locale)
            val picked = HashSet(chosen)
            val apps = pm.queryIntentActivities(launch, 0)
                .mapNotNull { it.activityInfo?.applicationInfo }
                .filter { it.packageName != activity.packageName }
                .distinctBy { it.packageName }
                .map { App(it.packageName, pm.getApplicationLabel(it).toString()) }
                .sortedWith { a, b ->
                    val pa = a.pkg in picked
                    val pb = b.pkg in picked
                    if (pa != pb) (if (pa) -1 else 1) else collator.compare(a.label, b.label)
                }
            activity.runOnUiThread {
                // tek seçim kipinde ilk satır: tercih yok (boş paket adı)
                all = if (single != null) listOf(App("", defaultPlayer)) + apps else apps
                showCount()
                filter()
            }
        }, "bileklik-uygulamalar").start()
    }
}

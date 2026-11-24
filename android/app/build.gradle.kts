import java.security.MessageDigest

plugins {
    id("com.android.application")
    id("com.chaquo.python")
}

// Python kaynakları elle kopyalanmaz: her derlemede ../band klasöründen alınır.
// Liste "izin verilenler" mantığıyla çalışır; config.json, band.db, raw/ gibi dosyalar hiçbir koşulda girmez.
val bandDir = rootProject.file("../band")
val bandAssets = layout.buildDirectory.dir("generated/bandAssets")

val syncBand = tasks.register<Sync>("syncBandSources") {
    from(bandDir) {
        include("*.py")
        include("pb/**/*.py")
        include("web/index.html", "web/icon.svg", "web/app.webmanifest")
        exclude(
            "test_*.py", "testfiles.py", "fetch.py", "cmdtest.py", "probe.py", "onizleme.py",
            "import_mifitness.py", "**/__pycache__/**", "**/*.bak*", "config.json*", "band.db*",
            "raw/**", "designs/**", "proto/**",
        )
    }
    into(bandAssets.map { it.dir("band") })
    includeEmptyDirs = false
    doLast {
        // Uygulama bu dizini ilk açılışta (ve içerik değişince) kendi özel klasörüne açar.
        val root = destinationDir
        val files = root.walkTopDown().filter { it.isFile && it.name != "INDEX" && it.name != "VERSION" }
            .map { it.relativeTo(root).invariantSeparatorsPath }.sorted().toList()
        val md = MessageDigest.getInstance("SHA-256")
        files.forEach { md.update(it.toByteArray()); md.update(root.resolve(it).readBytes()) }
        root.resolve("INDEX").writeText(files.joinToString("\n") + "\n")
        root.resolve("VERSION").writeText(md.digest().joinToString("") { "%02x".format(it) } + "\n")
    }
}

android {
    namespace = "band.app"
    compileSdk = 37

    defaultConfig {
        applicationId = "band.app"
        minSdk = 26
        targetSdk = 37
        versionCode = 1
        versionName = "0.1"
        ndk { abiFilters += "arm64-v8a" }
    }
    buildTypes {
        debug { }  // varsayılan debug anahtarıyla imzalanır
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    sourceSets["main"].assets.srcDir(bandAssets.get().asFile)
}

tasks.named("preBuild") { dependsOn(syncBand) }

chaquopy {
    defaultConfig {
        // cryptography'nin Chaquopy deposundaki en yeni tekeri cp313 içindir; 3.14/3.15 için yok.
        version = "3.13"
        buildPython((findProperty("bileklik.buildPython") as String?) ?: "/usr/bin/python3.13")
        pip {
            install("protobuf==7.36.2")      // pb/ altındaki üretilmiş kod 7.35.1 ister; saf Python tekeri
            install("cryptography==42.0.8")  // AES-CTR ve AES-CCM (client.py)
        }
    }
}

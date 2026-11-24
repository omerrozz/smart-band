#!/usr/bin/env bash
# Bileklik Android uygulamasını (debug APK) derler. Araçlar $HOME/android-tools altındadır;
# sistemde hiçbir şey kurulu olması gerekmez (yalnızca Chaquopy'nin pip adımı için /usr/bin/python3.13).
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

TOOLS="${BILEKLIK_TOOLS:-$HOME/android-tools}"
export JAVA_HOME="$(ls -d "$TOOLS"/jdk-17* | sort | tail -1)"
export ANDROID_HOME="$TOOLS/sdk"
export ANDROID_SDK_ROOT="$ANDROID_HOME"
export PATH="$JAVA_HOME/bin:$PATH"
# Gradle önbelleği ve debug imza anahtarı da araç klasöründe durur; ev dizinine bir şey yazılmaz.
export GRADLE_USER_HOME="$TOOLS/gradle-home"
export ANDROID_USER_HOME="$TOOLS/android-home"

./gradlew --no-daemon assembleDebug "$@"

APK="$PWD/app/build/outputs/apk/debug/app-debug.apk"
echo
echo "APK: $APK"
echo "Boyut: $(du -h "$APK" | cut -f1) ($(stat -c %s "$APK") bayt)"

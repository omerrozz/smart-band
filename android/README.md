# Smart Band (Android)

A shell that runs the Python application from `../band` (server.py + web UI) on the phone.
The Python code is not copied by hand: every build takes it from `../band`, so the PC and the phone run
the same code.

- Package: `band.app`, minSdk 26, target/compile SDK 37 (Android 17), `arm64-v8a` only
- Chaquopy 17.2.0, Python 3.13, `protobuf 7.36.2` (pure Python), `cryptography 42.0.8`
- AGP 9.5.2, Gradle 9.9.2 (wrapper), JDK 17

## Build

```
./build.sh
```

Tools are looked up under `~/android-tools` by default (JDK 17 as `jdk-17*`, the Android SDK as `sdk`,
the Gradle cache, the debug signing key); use `BILEKLIK_TOOLS=/path ./build.sh` for another folder.
Chaquopy's pip step uses `/usr/bin/python3.13` (override: `./build.sh -Pbileklik.buildPython=/path/python3.13`).
Output: `app/build/outputs/apk/debug/app-debug.apk`

## Install

```
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

## Getting your settings and data onto the phone

`config.json` (band address + key) is **not** inside the APK. There are two ways:

1. **Copy from the PC (`run-as`)** — works with the debug build. Open the app once and stop it ("Durdur"
   in the notification), then:

   ```
   sqlite3 ../band/band.db ".backup /tmp/band-copy.db"      # consistent copy of a live database (read-only)
   adb exec-in run-as band.app sh -c 'mkdir -p files/data && cat > files/data/config.json' < ../band/config.json
   adb exec-in run-as band.app sh -c 'cat > files/data/band.db' < /tmp/band-copy.db
   adb exec-in run-as band.app sh -c 'rm -f files/data/band.db-wal files/data/band.db-shm'
   ```

   Then open the app. Dropping files under `/sdcard/Android/data/band.app/files/…` with `adb push`
   does **not** work: they end up owned by the `shell` user and the app cannot see them.
2. **Manual entry** — type the MAC address and the 32-digit key on the setup screen; server.py fills in
   the remaining defaults.

On the phone the data lives under `files/data/` (`config.json`, `band.db`, `raw/`) and is excluded from
backups.

## How it works

- `BandService` (foreground service, `connectedDevice`, `START_STICKY`) starts Python; `bileklik_android.py`
  sets `BILEKLIK_DATA`, fills in `server.transport_factory` and supervises server.py's three loops (band,
  app workout, HTTP): a loop that dies is restarted after 5 s. The server listens on `127.0.0.1:8377` only.
- `BtBridge` connects to the band over classic Bluetooth RFCOMM **channel 5**: first the hidden
  `createRfcommSocket(5)`, then `createInsecureRfcommSocket(5)`, finally the public API with the SPP UUID.
  The path that worked is logged.
- The service hears about Bluetooth being toggled, pairing completing and the ACL link to the band
  dropping, and reconnects without waiting.
- **When another app holds the band** (Mi Fitness): after three connections in a row that last less than
  5 seconds, `state.host_hint` is set, a warning appears in the notification and in the banner at the top
  of the page, and the retry interval widens 20 → 60 → 120 seconds. Once a connection survives 30 seconds
  the warning clears and the interval returns to 20 seconds.
- The **wake lock** is held only while data is being exchanged with the band: file sync, live stream
  (spot measurement, zone training, a workout running on the band) and a workout started from the app.
  Otherwise the phone may sleep; a short (20 s) wake-up every 15 minutes gives a due sync its chance.
- When the screen turns off or the app goes to the background the page's live connection is closed and
  the server falls back to its "nobody is watching" schedule (sync every 30 minutes, never the sleep file
  while asleep).
- `BootReceiver` starts the service when the phone boots and when the app is updated (if "start at boot"
  is on, setup is done and the user has not pressed "Durdur").
- The **phone settings** row under Saat › Uygulama (visible only inside the app) opens a native panel:
  battery-optimisation exemption, a Samsung note, start at boot, permission status, "stop the app".
- `MainActivity` opens the UI in a full-screen WebView once the server answers.
- **Phone GPS**: for a run, walk or ride started from the app, `LocationFeed` passes fixes from the
  platform `LocationManager` (GPS, fused as a fallback when available; 1 s) to Python
  (`server.location_fix`). Filtering and distance live in `appworkout.py`: fixes with accuracy worse than
  30 m and jumps above 12 m/s on foot (25 on a bike) are dropped; the first fix must be accurate to 20 m;
  distance is summed in 6 m steps of a 5 s mean position (haversine); when GPS is lost for more than 20 s
  that stretch is estimated from steps and flagged. The same distance is what the band's screen shows.
  Location is on only while a workout is running (updates stop while paused); the service also enters the
  `location` type for that time. Permission is requested the first time an outdoor workout is started; if
  it is refused the workout starts with step-based estimation. Background location is never requested.
  The live card's map shows the path so far (`state.app_workout.track_len` +
  `/api/workout/live-track?from=N`); a finished workout is saved with its route and GPX.
- **Notifications and music** (all off by default): `NotifyService` (the user grants notification access
  in system settings) reads only the notifications of the selected apps and forwards them to the band via
  `server.phone_notify` (7/0; 7/1 when dismissed on the phone; dismissing on the band dismisses on the
  phone). Ongoing, group-summary, media and — unless enabled — silent notifications are skipped, as are
  those arriving in Do Not Disturb or while the phone is in use. At most one notification per app every
  2 seconds (the ones in between are merged into the last); identical content is not repeated within
  5 seconds. Text goes to the band over Bluetooth only: it is not sent to the network or written to disk
  or the log; it stays in memory for at most 2 s (20 s when queued), after which only the id, package
  name and a CRC of the text are kept (last 128 notifications). Incoming calls are shown from the dialer's
  notification (no extra permission). Music: the active media session's track, state and volume are
  written to the band as they change (18/1); the band's buttons (18/2) are applied as
  play/pause/next/previous/volume. With a "preferred player" selected, the band shows and controls that
  app whenever it has an active session; otherwise it follows the last one playing. When nothing plays
  the last known track (or the player's name) is written as "paused" so the play button stays usable.
  The app picker lists launcher apps (`<queries>`; no `QUERY_ALL_PACKAGES`).

Permissions: `INTERNET`, `BLUETOOTH_CONNECT`, `POST_NOTIFICATIONS`, `FOREGROUND_SERVICE`,
`FOREGROUND_SERVICE_CONNECTED_DEVICE`, `RECEIVE_BOOT_COMPLETED`, `REQUEST_IGNORE_BATTERY_OPTIMIZATIONS`,
`WAKE_LOCK`, `ACCESS_FINE_LOCATION`, `ACCESS_COARSE_LOCATION`, `FOREGROUND_SERVICE_LOCATION`.
`NotifyService` is declared as a service only the system can bind (`BIND_NOTIFICATION_LISTENER_SERVICE`).

Logs:

```
adb logcat -s Bileklik:V BileklikBt:V BileklikKonum:V BileklikBildirim:V python.stdout:V python.stderr:V chromium:W AndroidRuntime:E
```

## Manual steps on Samsung phones

1. Exempt the app from battery optimisation from the phone settings panel. That is enough: exempt apps
   are not put into "Sleeping apps" and need not be added to "Never sleeping apps".
2. If Mi Fitness is installed, force-stop it; while it runs it holds the band.
3. For outdoor workouts choose "Precise" and "While using the app" in the location prompt.

## Known limits

- The band talks to one app at a time: for the phone to connect, the PC service and Mi Fitness must be
  closed.
- The Python interpreter cannot be stopped in place, so "Durdur" ends the process; it does not start by
  itself (not even at boot) until the app is opened again.
- Timers stop while the phone is in deep sleep; sync depends on the 15-minute wake-ups, so night-time
  intervals can be somewhat longer than 30 minutes.
- No QR sharing with the phone (`/api/phone`).
- Notifications: no "open on phone", canned replies or app-icon transfer; rejecting a call from the band
  works only when the call notification carries a decline action.
- The album name is not sent to the band (the protocol has no field for it).
- If the process dies during a workout, location does not resume until the app is reopened (Android does
  not allow entering the location type from the background); distance is estimated from steps meanwhile.
- Map tiles and the map library come from the internet; offline, the route is drawn on a blank ground.
- Bluetooth does not work on Android 11 and earlier (the legacy `BLUETOOTH` permission is deliberately
  not declared).
- The hidden RFCOMM channel API sometimes fails with "read failed"; the SPP UUID path is used then.
- Downloaded CSV/GPX files are saved to the shared "Downloads" folder.

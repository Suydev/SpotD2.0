# SpotDL — Android APK

A native Android wrapper around the SpotDL web app. Ships a single
`MainActivity` hosting a hardware-accelerated `WebView` that loads the deployed
Flask server, plus an Android `DownloadManager` integration so finished ZIPs
land in `/Music/SpotDL/` (or `/Movies/SpotDL/` for video) on the device.

## Configure the server URL

Open `app/src/main/res/values/strings.xml` and set:

```xml
<string name="server_url">https://your-deployment.replit.app</string>
```

The default value is a placeholder — the app will not connect to anything until
you change this.

## Build locally

Requires JDK 17 and the Android SDK (cmdline-tools + platform 34).

```bash
cd android
gradle wrapper --gradle-version 8.5 --distribution-type bin
chmod +x gradlew
./gradlew assembleDebug          # → app/build/outputs/apk/debug/app-debug.apk
./gradlew assembleRelease        # → app/build/outputs/apk/release/app-release.apk
```

Both build types are debug-signed by default so the APKs are sideloadable
without setting up a keystore. For Play Store distribution, plug a real
`signingConfig` into `app/build.gradle.kts`.

## Build via GitHub Actions

Every push to `main` that touches `android/**` triggers
`.github/workflows/android.yml`, which builds both APKs and uploads them as
workflow artifacts. Tag a commit (e.g. `v1.0.0`) to also attach the APKs to a
GitHub Release.

## Sideload to a phone

```bash
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

Or transfer the `.apk` to the device, open it from the file manager, and
allow installs from unknown sources.

## Hidden console

The web app exposes an admin console reached by tapping the `♪ SpotDL` logo
in the top-left **7 times within 3 seconds**, then entering the access code
configured on the server (`SPOTDL_ADMIN_PASSWORD` env var). The console is
not advertised anywhere in the UI; if the access code is unset on the server,
the URL returns 404.

## Permissions

| Permission | Why |
|---|---|
| `INTERNET` / `ACCESS_NETWORK_STATE` | reach the Flask server |
| `WAKE_LOCK` | keep CPU alive during long downloads |
| `FOREGROUND_SERVICE` | reserved for future on-device download worker |
| `POST_NOTIFICATIONS` (13+) | `DownloadManager` completion notification |
| `WRITE_EXTERNAL_STORAGE` (≤28) | legacy storage write |
| `READ_MEDIA_AUDIO` / `READ_MEDIA_VIDEO` (13+) | scoped storage media access |
| `REQUEST_IGNORE_BATTERY_OPTIMIZATIONS` | so background downloads survive Doze |

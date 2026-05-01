# SpotDL

A Flask-based web app for downloading Spotify playlists and YouTube videos.

## Tech Stack
- **Language**: Python 3.12
- **Framework**: Flask (server-rendered Jinja2 templates)
- **Key deps**: `flask`, `yt-dlp`, `mutagen`, `requests`, `pillow`, `gunicorn` (see `requirements.txt`)
- **Frontend**: Static assets in `src/static`, Jinja2 templates in `src/templates`

## Project Layout
- `src/web_app.py` — main Flask server (entry point)
- `src/main.py` — alternate/legacy entry
- `src/templates/` — Jinja2 HTML templates
- `src/static/` — PWA manifest, icons, service worker
- `src/utils/` — helper modules
- `data/` — runtime config & session state (gitignored)
- `downloads/` — generated ZIP files (gitignored)

## Replit Setup
- **Workflow**: `Start application` runs `python3 src/web_app.py` and binds to `0.0.0.0:5000` (webview).
- **Deployment**: Configured as `vm` target running `python3 src/web_app.py`. VM was chosen because the app keeps in-memory download state and runs background threads that must not be killed mid-download.

## Environment Variables (optional)
- `SESSION_SECRET` — Flask session secret (defaults to a placeholder).
- `PORT` — overrides the default `5000`.
- `FLASK_DEBUG` — `true` to enable debug mode.

## Heavy Android APK (`android/`)
Self-contained on-device build — no server needed. Output: `android/build-output/SpotDL-heavy-debug.apk` (~45 MB).

- **Runtime**: Chaquopy 16.1.0 embeds CPython 3.12 in the APK (`lib/arm64-v8a/libpython3.12.so`).
- **Bundled deps** (12 from `requirements.txt`): flask 3.0.3, flask-sqlalchemy 3.1.1, spotipy 2.24.0, yt-dlp 2024.10.7, python-dotenv 1.0.1, mutagen 1.47.0, tqdm 4.66.5, colorama 0.4.6, requests 2.32.3, gunicorn 23.0.0, **psutil 7.1.3** (bumped from 6.0.0 — only version with a chaquopy arm64-v8a wheel), **Pillow 10.1.0** (bumped from 10.4.0 — same reason).
- **App code** (`android/app/src/main/python/`): `web_app.py` (with the `Response` import fix and env-driven `_FFMPEG_LOCATION` + `SPOTDL_DATA_DIR`), `spotdl_main.py` (werkzeug bg-thread launcher), `main.py`, `utils/`, `templates/`, `static/`.
- **ffmpeg**: prebuilt `libffmpeg.so` at `lib/arm64-v8a/`, plus 7 SONAME-renamed shared libs in `assets/ffmpeg-libs/` (extracted to `filesDir` on first launch).
- **Activity**: `MainActivity.kt` shows a splash, extracts ffmpeg libs, sets env vars, starts Python + Flask in a background thread, then loads `http://127.0.0.1:5000` in a WebView.

### Building the APK
```bash
bash /tmp/build-apk.sh   # writes log to /tmp/gradle-build.log, exit code to /tmp/gradle-build.done
```
Toolchain: JDK 17 + Gradle 8.5 + Android SDK (platform-34, build-tools-34.0.0) at `$HOME/android-sdk`.

**Critical build-env detail**: Replit's nix Python ranks its system pip 25.0.1 ahead of any venv pip in `sys.path`, but Chaquopy's `pip_install` module imports `pip._vendor.retrying` (removed in pip 24.2+). The build script exports `PYTHONPATH=$venv/lib/python3.12/site-packages` so the chaquopy-bundled pip 20.1 wins. Without this, `generateDebugPythonRequirements` fails with `ModuleNotFoundError: No module named 'pip._vendor.retrying'`.

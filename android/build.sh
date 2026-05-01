#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# android/build.sh  —  Persistent APK build script
#
# All heavy downloads (Android SDK, Gradle) go to android/.build-cache/
# which lives on the project's persistent 256 GB nbd filesystem, so they
# survive container restarts.  Only /tmp gets wiped between sessions.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"   # android/
CACHE_DIR="$SCRIPT_DIR/.build-cache"
SDK_DIR="$CACHE_DIR/android-sdk"
GRADLE_HOME="$CACHE_DIR/gradle-8.5"
GRADLE_USER_HOME_DIR="$CACHE_DIR/gradle-home"
LOG="/tmp/gradle-build.log"

mkdir -p "$SDK_DIR" "$GRADLE_USER_HOME_DIR"

# ── 1. Gradle 8.5 ────────────────────────────────────────────────────────────
if [[ ! -f "$GRADLE_HOME/bin/gradle" ]]; then
    echo "[setup] Downloading Gradle 8.5…"
    GRADLE_ZIP="/tmp/gradle-8.5-bin.zip"
    curl -fsSL -o "$GRADLE_ZIP" \
        "https://services.gradle.org/distributions/gradle-8.5-bin.zip"
    unzip -q "$GRADLE_ZIP" -d "$CACHE_DIR"
    mv "$CACHE_DIR/gradle-8.5" "$GRADLE_HOME" 2>/dev/null || true
    rm -f "$GRADLE_ZIP"
    echo "[setup] Gradle 8.5 installed at $GRADLE_HOME"
fi
export PATH="$GRADLE_HOME/bin:$PATH"
echo "[info] Gradle: $(gradle --version 2>&1 | head -1)"

# ── 2. Android command-line tools ────────────────────────────────────────────
SDKMANAGER="$SDK_DIR/cmdline-tools/latest/bin/sdkmanager"
if [[ ! -f "$SDKMANAGER" ]]; then
    echo "[setup] Downloading Android command-line tools…"
    CMDTOOLS_ZIP="/tmp/cmdline-tools.zip"
    curl -fsSL -o "$CMDTOOLS_ZIP" \
        "https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip"
    mkdir -p "$SDK_DIR/cmdline-tools"
    unzip -q "$CMDTOOLS_ZIP" -d "$SDK_DIR/cmdline-tools"
    mv "$SDK_DIR/cmdline-tools/cmdline-tools" "$SDK_DIR/cmdline-tools/latest"
    rm -f "$CMDTOOLS_ZIP"
    echo "[setup] cmdline-tools installed"
fi
export ANDROID_HOME="$SDK_DIR"
export PATH="$SDKMANAGER:$SDK_DIR/cmdline-tools/latest/bin:$SDK_DIR/platform-tools:$PATH"

# ── 3. SDK components (platform 34 + build-tools 34.0.0) ─────────────────────
if [[ ! -d "$SDK_DIR/platforms/android-34" ]]; then
    echo "[setup] Installing Android platform 34 + build-tools 34.0.0…"
    yes | sdkmanager --sdk_root="$SDK_DIR" \
        "platform-tools" \
        "platforms;android-34" \
        "build-tools;34.0.0" \
        2>&1 | grep -v "^\[=" || true
    echo "[setup] SDK components installed"
fi
echo "[info] ANDROID_HOME=$ANDROID_HOME"

# ── 4. local.properties ───────────────────────────────────────────────────────
cat > "$SCRIPT_DIR/local.properties" <<EOF
sdk.dir=$SDK_DIR
EOF

# ── 5. JAVA_HOME ──────────────────────────────────────────────────────────────
JAVA_BIN="$(which java)"
export JAVA_HOME="$(dirname "$(dirname "$(readlink -f "$JAVA_BIN")")")"
echo "[info] JAVA_HOME=$JAVA_HOME"

# ── 6. PYTHONPATH fix for Chaquopy's bundled pip ──────────────────────────────
# Replit sets PYTHONPATH to include Nix pip 25.0.1 which overrides the venv's
# pip 20.1.  Chaquopy's pip_install.py does `from pip._vendor.retrying import
# retry` which pip 25.x removed.  Fix: put the Chaquopy build venv's
# site-packages (pip 20.1) FIRST in PYTHONPATH.  Chaquopy creates this venv
# during the extractDebugPythonBuildPackages task, which runs BEFORE
# generateDebugPythonRequirements (the task that calls pip_install.py), so the
# path exists by the time that subprocess starts.
CHAQUOPY_VENV="$SCRIPT_DIR/app/build/python/env/debug/lib/python3.12/site-packages"
export PYTHONPATH="$CHAQUOPY_VENV${PYTHONPATH:+:$PYTHONPATH}"
echo "[info] PYTHONPATH=$PYTHONPATH"

# ── 7. Build ──────────────────────────────────────────────────────────────────
echo "[build] Starting Gradle assembleDebug…"
mkdir -p "$SCRIPT_DIR/build-output"

cd "$SCRIPT_DIR"
export GRADLE_USER_HOME="$GRADLE_USER_HOME_DIR"
gradle assembleDebug \
    --no-daemon \
    --stacktrace \
    --info \
    2>&1 | tee "$LOG"

APK_SRC="$SCRIPT_DIR/app/build/outputs/apk/debug/app-debug.apk"
APK_DST="$SCRIPT_DIR/build-output/SpotDL-heavy-debug.apk"
if [[ -f "$APK_SRC" ]]; then
    cp "$APK_SRC" "$APK_DST"
    SIZE=$(du -sh "$APK_DST" | cut -f1)
    echo ""
    echo "╔══════════════════════════════════════════════════╗"
    echo "║  BUILD SUCCESS                                   ║"
    echo "║  $APK_DST"
    echo "║  Size: $SIZE"
    echo "╚══════════════════════════════════════════════════╝"
else
    echo "[ERROR] APK not found at $APK_SRC"
    tail -50 "$LOG"
    exit 1
fi

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

android {
    namespace  = "com.suydev.spotdl"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.suydev.spotdl"
        minSdk        = 24
        targetSdk     = 34
        versionCode   = 1
        versionName   = "1.0"
        resourceConfigurations += listOf("en")

        // Only ship the 64-bit ARM slice — covers every Android phone made
        // since ~2018 and matches the architecture of the bundled ffmpeg.
        ndk { abiFilters += listOf("arm64-v8a") }
    }

    buildTypes {
        debug {
            isMinifyEnabled = false
            applicationIdSuffix = ".debug"
            versionNameSuffix   = "-debug"
        }
        release {
            isMinifyEnabled    = false   // Keep Python reflection paths intact
            isShrinkResources  = false
            // Debug-signed by default so the APK is sideloadable from CI without a keystore.
            signingConfig = signingConfigs.getByName("debug")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }

    buildFeatures { viewBinding = true }

    packaging {
        // Don't compress already-compressed Python bytecode/wheel files
        // (Chaquopy needs random-access reads at runtime).
        resources.excludes += setOf(
            "/META-INF/{AL2.0,LGPL2.1}",
            "/META-INF/DEPENDENCIES",
            "/META-INF/LICENSE*",
            "/META-INF/NOTICE*",
        )
        jniLibs {
            useLegacyPackaging = true   // Required so libffmpeg.so is extracted to nativeLibraryDir
        }
    }
}

chaquopy {
    defaultConfig {
        version = "3.12"
        // Replit's Python 3.12 is on PATH and matches the target version.
        buildPython("python3")

        pip {
            // Every package from the project's requirements.txt — the user
            // wants the full toolset shipped, not a subset.
            install("flask==3.0.3")
            install("flask-sqlalchemy==3.1.1")
            install("spotipy==2.24.0")
            install("yt-dlp==2024.10.7")
            install("python-dotenv==1.0.1")
            install("mutagen==1.47.0")
            install("tqdm==4.66.5")
            install("colorama==0.4.6")
            install("psutil==7.1.3")
            install("Pillow==10.1.0")
            install("requests==2.32.3")
            install("gunicorn==23.0.0")
            install("qrcode==7.4.2")
        }
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.12.0")
    implementation("androidx.appcompat:appcompat:1.6.1")
    implementation("com.google.android.material:material:1.11.0")
    implementation("androidx.activity:activity-ktx:1.8.2")
    implementation("androidx.constraintlayout:constraintlayout:2.1.4")
    implementation("androidx.swiperefreshlayout:swiperefreshlayout:1.1.0")
    implementation("androidx.webkit:webkit:1.10.0")
}

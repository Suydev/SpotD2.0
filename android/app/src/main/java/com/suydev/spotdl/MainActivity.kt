package com.suydev.spotdl

import android.Manifest
import android.app.DownloadManager
import android.content.Context
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.WindowManager
import android.webkit.CookieManager
import android.webkit.URLUtil
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Toast
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.swiperefreshlayout.widget.SwipeRefreshLayout
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import com.suydev.spotdl.databinding.ActivityMainBinding
import java.io.File

class MainActivity : AppCompatActivity() {

    companion object {
        private const val TAG = "SpotDL"
        private const val SERVER_URL = "http://127.0.0.1:5000"

        // Splash shown immediately while the embedded server is booting.
        private const val SPLASH_HTML = """
            <!doctype html><html><head><meta name="viewport"
              content="width=device-width,initial-scale=1"><style>
              html,body{margin:0;height:100%;background:#0d1117;color:#cdffd0;
                font-family:-apple-system,system-ui,sans-serif;display:flex;
                align-items:center;justify-content:center;text-align:center}
              .b{font-size:42px;margin-bottom:14px}
              .t{font-size:16px;opacity:.85}
              .s{margin-top:22px;font-size:13px;opacity:.55}
            </style></head><body><div>
              <div class="b">♪ SpotDL</div>
              <div class="t">Starting embedded server…</div>
              <div class="s">First launch unpacks Python &amp; ffmpeg<br>
                — this only takes a few seconds</div>
            </div></body></html>
        """
    }

    private lateinit var binding: ActivityMainBinding
    private lateinit var web:     WebView
    private lateinit var refresh: SwipeRefreshLayout

    private val notifPermLauncher =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { /* no-op */ }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        window.setBackgroundDrawable(null)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)

        web     = binding.web
        refresh = binding.refresh

        setupWebView()
        registerBackHandler()
        requestNotificationsIfNeeded()

        // Show splash immediately so the WebView isn't blank.
        web.loadDataWithBaseURL(null, SPLASH_HTML, "text/html", "utf-8", null)

        // Boot Python + Flask off the UI thread, then load the local URL.
        Thread({ bootPythonAndLoad() }, "spotdl-boot").start()
    }

    // ──────────────────────────────────────────────────────────────────────
    // Python / Flask bootstrap
    // ──────────────────────────────────────────────────────────────────────

    private fun bootPythonAndLoad() {
        try {
            // 1) Extract bundled ffmpeg shared libs (assets/ffmpeg-libs/*) → filesDir
            val libsDir = File(filesDir, "ffmpeg-libs")
            extractFfmpegLibs(libsDir)

            // 2) Writable data dir for config.json etc.
            val dataDir      = File(filesDir, "spotdl-data").apply { mkdirs() }
            val downloadsDir = File(filesDir, "spotdl-downloads").apply { mkdirs() }

            // 3) ffmpeg binary path — Android extracts libffmpeg.so to nativeLibraryDir.
            val ffmpegBin = File(applicationInfo.nativeLibraryDir, "libffmpeg.so")
            try { ffmpegBin.setExecutable(true, false) } catch (_: Exception) {}

            // 4) Initialise Python (idempotent).
            if (!Python.isStarted()) Python.start(AndroidPlatform(this))
            val py     = Python.getInstance()
            val osMod  = py.getModule("os")
            val envMod = osMod.get("environ")!!

            // 5) Wire env vars the Python side reads.
            envMod.callAttr("__setitem__", "SPOTDL_FFMPEG",        ffmpegBin.absolutePath)
            envMod.callAttr("__setitem__", "LD_LIBRARY_PATH",      libsDir.absolutePath)
            envMod.callAttr("__setitem__", "SPOTDL_DATA_DIR",      dataDir.absolutePath)
            envMod.callAttr("__setitem__", "SPOTDL_DOWNLOADS_DIR", downloadsDir.absolutePath)
            envMod.callAttr("__setitem__", "TMPDIR",               cacheDir.absolutePath)

            // 6) Start the Flask server (blocks until the socket is accepting).
            val ok = py.getModule("spotdl_main").callAttr("start").toBoolean()
            if (!ok) throw RuntimeException("Embedded server did not come up in time")

            Log.i(TAG, "Server is up — loading $SERVER_URL")
            Handler(Looper.getMainLooper()).post { web.loadUrl(SERVER_URL) }
        } catch (e: Throwable) {
            Log.e(TAG, "Boot failed", e)
            Handler(Looper.getMainLooper()).post {
                Toast.makeText(this,
                    "SpotDL failed to start: ${e.message}",
                    Toast.LENGTH_LONG).show()
                val msg = (e.message ?: "unknown error")
                    .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                web.loadDataWithBaseURL(null,
                    "<html><body style='background:#0d1117;color:#ff8a8a;" +
                    "font-family:monospace;padding:20px'>" +
                    "<h2>Server boot failed</h2><pre>$msg</pre></body></html>",
                    "text/html", "utf-8", null)
            }
        }
    }

    // Copy every file under assets/ffmpeg-libs/ into [destDir] on first launch (idempotent).
    private fun extractFfmpegLibs(destDir: File) {
        destDir.mkdirs()
        val names = assets.list("ffmpeg-libs") ?: return
        for (name in names) {
            val out = File(destDir, name)
            if (out.exists() && out.length() > 0) continue   // already extracted
            assets.open("ffmpeg-libs/$name").use { input ->
                out.outputStream().use { output -> input.copyTo(output) }
            }
        }
        Log.i(TAG, "ffmpeg libs extracted to ${destDir.absolutePath}: ${names.toList()}")
    }

    // ──────────────────────────────────────────────────────────────────────
    // WebView setup (unchanged behaviour from the original wrapper)
    // ──────────────────────────────────────────────────────────────────────

    private fun setupWebView() {
        with(web.settings) {
            javaScriptEnabled                     = true
            domStorageEnabled                     = true
            databaseEnabled                       = true
            loadsImagesAutomatically              = true
            mediaPlaybackRequiresUserGesture      = false
            cacheMode                             = WebSettings.LOAD_DEFAULT
            useWideViewPort                       = true
            loadWithOverviewMode                  = true
            mixedContentMode                      = WebSettings.MIXED_CONTENT_COMPATIBILITY_MODE
            userAgentString                       = "$userAgentString SpotDL-Android/1.0"
            setSupportMultipleWindows(false)
            javaScriptCanOpenWindowsAutomatically = true
            allowFileAccess                       = true
            allowContentAccess                    = true
        }

        CookieManager.getInstance().setAcceptCookie(true)
        CookieManager.getInstance().setAcceptThirdPartyCookies(web, true)

        web.webViewClient = object : WebViewClient() {
            override fun onPageFinished(view: WebView?, url: String?) {
                refresh.isRefreshing = false
            }
            override fun onReceivedError(
                view: WebView?, request: WebResourceRequest?, error: WebResourceError?
            ) {
                refresh.isRefreshing = false
                if (request?.isForMainFrame == true) {
                    Toast.makeText(
                        this@MainActivity,
                        "Connection failed. Pull to retry.",
                        Toast.LENGTH_SHORT
                    ).show()
                }
            }
            override fun shouldOverrideUrlLoading(
                view: WebView?, request: WebResourceRequest?
            ): Boolean {
                val url = request?.url?.toString() ?: return false
                if (!url.startsWith("http")) {
                    return try {
                        startActivity(android.content.Intent(
                            android.content.Intent.ACTION_VIEW, Uri.parse(url)
                        )); true
                    } catch (e: Exception) { false }
                }
                return false
            }
        }
        web.webChromeClient = WebChromeClient()

        // File downloads → Android DownloadManager → /Music/SpotDL/ or /Movies/SpotDL/
        web.setDownloadListener { url, userAgent, contentDisposition, mimeType, _ ->
            try {
                val fileName = URLUtil.guessFileName(url, contentDisposition, mimeType)
                val req = DownloadManager.Request(Uri.parse(url)).apply {
                    setMimeType(mimeType)
                    addRequestHeader("User-Agent", userAgent)
                    addRequestHeader("Cookie", CookieManager.getInstance().getCookie(url) ?: "")
                    setTitle(fileName)
                    setDescription("Saving from SpotDL")
                    setNotificationVisibility(
                        DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED
                    )
                    setAllowedOverMetered(true)
                    setAllowedOverRoaming(true)
                    val subdir = if (fileName.endsWith(".mp4", true)) "Movies/SpotDL"
                                 else "Music/SpotDL"
                    setDestinationInExternalPublicDir(subdir, fileName)
                }
                val dm = getSystemService(Context.DOWNLOAD_SERVICE) as DownloadManager
                dm.enqueue(req)
                Toast.makeText(this, "Downloading $fileName…", Toast.LENGTH_SHORT).show()
            } catch (e: Exception) {
                Toast.makeText(this, "Download failed: ${e.message}", Toast.LENGTH_LONG).show()
            }
        }

        refresh.setOnRefreshListener { web.reload() }
        refresh.setColorSchemeColors(0xFF66E0CF.toInt(), 0xFFCDFFD0.toInt())
    }

    private fun registerBackHandler() {
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (web.canGoBack()) web.goBack()
                else { isEnabled = false; onBackPressedDispatcher.onBackPressed() }
            }
        })
    }

    private fun requestNotificationsIfNeeded() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            val granted = ContextCompat.checkSelfPermission(
                this, Manifest.permission.POST_NOTIFICATIONS
            ) == PackageManager.PERMISSION_GRANTED
            if (!granted) notifPermLauncher.launch(Manifest.permission.POST_NOTIFICATIONS)
        }
    }
}

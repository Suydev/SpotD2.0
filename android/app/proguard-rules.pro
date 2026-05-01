# Keep WebView JS interface methods if any are added.
-keepclassmembers class * {
    @android.webkit.JavascriptInterface <methods>;
}
-keep class com.suydev.spotdl.** { *; }

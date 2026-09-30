# The JS bridge is called reflectively from page JS — R8 must not strip/rename it.
-keepclassmembers class biz.boskovic.dida.NativeBridge {
    public *;
}
-keepattributes JavascriptInterface

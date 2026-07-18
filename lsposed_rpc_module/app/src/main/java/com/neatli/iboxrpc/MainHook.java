package com.neatli.iboxrpc;

import android.app.Application;
import android.content.Context;
import android.os.PowerManager;

import de.robv.android.xposed.IXposedHookLoadPackage;
import de.robv.android.xposed.XC_MethodHook;
import de.robv.android.xposed.XposedBridge;
import de.robv.android.xposed.XposedHelpers;
import de.robv.android.xposed.callbacks.XC_LoadPackage;

public class MainHook implements IXposedHookLoadPackage {

    private static RpcServer server;
    public static Object encryptInstance = null;
    public static Object decryptInstance = null;

    private static final int PORT = 27042;

    // ── GeeTest V4 captcha result storage ─────────────────────────────────
    // Populated by the WebView @JavascriptInterface hook when the user solves
    // a slider captcha inside the iBox app.  Python reads via "captcha-latest"
    // RPC command and clears via "captcha-clear".
    public static volatile String lastCaptchaLotNumber    = null;
    public static volatile String lastCaptchaPassToken    = null;
    public static volatile String lastCaptchaGenTime      = null;
    public static volatile String lastCaptchaOutput       = null;
    public static volatile String lastCaptchaId           = null;
    public static volatile long   lastCaptchaTimestampMs  = 0;
    public static volatile Context appContext               = null;

    @Override
    public void handleLoadPackage(XC_LoadPackage.LoadPackageParam lpparam) throws Throwable {
        if (!lpparam.packageName.equals("com.box.art")) return;

        XposedBridge.log("[iBoxRPC] Entered target package.");

        // Hook Application onCreate to start our RPC Server
        XposedHelpers.findAndHookMethod(Application.class, "onCreate", new XC_MethodHook() {
            @Override
            protected void afterHookedMethod(MethodHookParam param) throws Throwable {
                if (server != null) return;
                
                Context appContext = (Context) param.thisObject;
                MainHook.appContext = appContext.getApplicationContext();
                XposedBridge.log("[iBoxRPC] Application Context acquired. Starting TCP Server...");

                try {
                    server = new RpcServer(PORT, lpparam.classLoader);
                    new Thread(server).start();
                    XposedBridge.log("[iBoxRPC] Server started on port " + PORT);
                    
                    acquireWakeLock(appContext);
                } catch (Exception e) {
                    XposedBridge.log("[iBoxRPC] Server start failed: " + e.getMessage());
                }
            }
        });

        // Capture EncryptDataImpl instance
        try {
            Class<?> encryptClass = XposedHelpers.findClass("com.basetools.encrypt.EncryptDataImpl", lpparam.classLoader);
            XposedHelpers.findAndHookMethod(encryptClass, "b", String.class, String.class, new XC_MethodHook() {
                @Override
                protected void afterHookedMethod(MethodHookParam param) throws Throwable {
                    if (encryptInstance == null) {
                        encryptInstance = param.thisObject;
                        XposedBridge.log("[iBoxRPC] Captured EncryptDataImpl instance!");
                    }
                }
            });
        } catch (Throwable t) {
            XposedBridge.log("[iBoxRPC] Failed to hook EncryptDataImpl: " + t.getMessage());
        }

        // Capture DecryptInterceptor instance
        try {
            Class<?> decryptClass = XposedHelpers.findClass("com.basenetwork.interceptor.DecryptInterceptor", lpparam.classLoader);
            XposedHelpers.findAndHookMethod(decryptClass, "a", String.class, new XC_MethodHook() {
                @Override
                protected void afterHookedMethod(MethodHookParam param) throws Throwable {
                    if (decryptInstance == null) {
                        decryptInstance = param.thisObject;
                        XposedBridge.log("[iBoxRPC] Captured DecryptInterceptor instance!");
                    }
                }
            });
        } catch (Throwable t) {
            XposedBridge.log("[iBoxRPC] Failed to hook DecryptInterceptor: " + t.getMessage());
        }

        // ── Hook WebView.addJavascriptInterface to intercept GeeTest V4 result ──
        // When iBox shows the synthesis slider captcha (WebView-based GeeTest H5),
        // GeeTest's JS calls a @JavascriptInterface method on the object the app
        // registered.  We hook every such method and extract captcha fields when
        // the argument JSON contains "lot_number" + "pass_token".
        try {
            XposedHelpers.findAndHookMethod(
                android.webkit.WebView.class,
                "addJavascriptInterface",
                Object.class,
                String.class,
                new XC_MethodHook() {
                    @Override
                    protected void afterHookedMethod(MethodHookParam param) throws Throwable {
                        Object jsObj      = param.args[0];
                        String ifaceName  = (String) param.args[1];
                        if (jsObj == null) return;

                        for (java.lang.reflect.Method m : jsObj.getClass().getMethods()) {
                            boolean isJsIface = false;
                            for (java.lang.annotation.Annotation a : m.getAnnotations()) {
                                if (a.annotationType().getName()
                                        .equals("android.webkit.JavascriptInterface")) {
                                    isJsIface = true;
                                    break;
                                }
                            }
                            if (!isJsIface) continue;

                            XposedBridge.log("[iBoxRPC] Hooking @JavascriptInterface "
                                + ifaceName + "." + m.getName());
                            try {
                                XposedBridge.hookMethod(m, new XC_MethodHook() {
                                    @Override
                                    protected void afterHookedMethod(MethodHookParam p)
                                            throws Throwable {
                                        _tryExtractCaptcha(p.args);
                                    }
                                });
                            } catch (Throwable hookErr) {
                                XposedBridge.log("[iBoxRPC] Could not hook method "
                                    + m.getName() + ": " + hookErr.getMessage());
                            }
                        }
                    }
                }
            );
            XposedBridge.log("[iBoxRPC] WebView.addJavascriptInterface hook installed.");
        } catch (Throwable t) {
            XposedBridge.log("[iBoxRPC] Failed to hook WebView.addJavascriptInterface: "
                + t.getMessage());
        }
    }

    /**
     * Inspect method call arguments: if any String arg looks like a GeeTest V4
     * seccode JSON (contains both "lot_number" and "pass_token"), store the
     * captcha fields so Python can read them via RPC.
     */
    private static void _tryExtractCaptcha(Object[] args) {
        if (args == null) return;
        for (Object arg : args) {
            if (!(arg instanceof String)) continue;
            String s = (String) arg;
            if (!s.contains("lot_number") || !s.contains("pass_token")) continue;
            try {
                org.json.JSONObject j = new org.json.JSONObject(s);
                // GeeTest result may be flat or nested under "seccode"
                org.json.JSONObject src = j;
                if (j.has("seccode") && j.getJSONObject("seccode").has("lot_number")) {
                    src = j.getJSONObject("seccode");
                }
                String lotNum  = src.optString("lot_number",    "");
                String passTok = src.optString("pass_token",    "");
                String genTime = src.optString("gen_time",      "");
                String capOut  = src.optString("captcha_output","");
                String capId   = src.optString("captcha_id",    "");
                if (!lotNum.isEmpty() && !passTok.isEmpty()) {
                    lastCaptchaLotNumber   = lotNum;
                    lastCaptchaPassToken   = passTok;
                    lastCaptchaGenTime     = genTime;
                    lastCaptchaOutput      = capOut;
                    lastCaptchaId          = capId;
                    lastCaptchaTimestampMs = System.currentTimeMillis();
                    XposedBridge.log("[iBoxRPC] GeeTest captcha captured! lot_number="
                        + lotNum.substring(0, Math.min(8, lotNum.length())) + "…");
                }
            } catch (Exception ignored) {}
        }
    }

    private void acquireWakeLock(Context context) {
        try {
            PowerManager pm = (PowerManager) context.getSystemService(Context.POWER_SERVICE);
            PowerManager.WakeLock wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "iBoxRPC::WakeLock");
            wakeLock.setReferenceCounted(false);
            wakeLock.acquire(); 
            XposedBridge.log("[iBoxRPC] WakeLock acquired successfully. CPU should stay awake.");
        } catch (Exception e) {
            XposedBridge.log("[iBoxRPC] Failed to acquire WakeLock: " + e.getMessage());
        }
    }
}

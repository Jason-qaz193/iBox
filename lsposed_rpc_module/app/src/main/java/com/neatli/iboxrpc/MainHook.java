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

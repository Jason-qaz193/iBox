package com.neatli.iboxrpc;

import android.util.Base64;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.BufferedWriter;
import java.io.InputStreamReader;
import java.io.OutputStreamWriter;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.Random;

import javax.crypto.Cipher;
import javax.crypto.spec.SecretKeySpec;

import de.robv.android.xposed.XposedBridge;
import de.robv.android.xposed.XposedHelpers;

public class RpcServer implements Runnable {

    private int port;
    private ClassLoader classLoader;

    public RpcServer(int port, ClassLoader classLoader) {
        this.port = port;
        this.classLoader = classLoader;
    }
    
    private String makeRandomRequestKey() {
        String chars = "0123456789abcdef";
        StringBuilder sb = new StringBuilder();
        Random r = new Random();
        for (int i = 0; i < 16; i++) {
            sb.append(chars.charAt(r.nextInt(chars.length())));
        }
        return sb.toString();
    }
    
    private JSONObject extractBeanFields(Object bean) {
        JSONObject result = new JSONObject();
        Class<?> cls = bean.getClass();
        while (cls != null) {
            try {
                java.lang.reflect.Field[] fields = cls.getDeclaredFields();
                for (java.lang.reflect.Field f : fields) {
                    f.setAccessible(true);
                    String name = f.getName();
                    if (result.has(name)) continue;
                    Object value = f.get(bean);
                    if (value == null) continue;
                    result.put(name, value.toString());
                }
                cls = cls.getSuperclass();
                if (cls != null && cls.getName().equals("java.lang.Object")) break;
            } catch (Exception e) {
                break;
            }
        }
        return result;
    }

    @Override
    public void run() {
        try (ServerSocket serverSocket = new ServerSocket(port)) {
            while (true) {
                try {
                    Socket socket = serverSocket.accept();
                    new Thread(new ClientHandler(socket)).start();
                } catch (Exception e) {
                    XposedBridge.log("[iBoxRPC] Accept error: " + e.getMessage());
                }
            }
        } catch (Exception e) {
            XposedBridge.log("[iBoxRPC] Server start error: " + e.getMessage());
        }
    }

    private class ClientHandler implements Runnable {
        private Socket socket;

        public ClientHandler(Socket socket) {
            this.socket = socket;
        }

        @Override
        public void run() {
            try (BufferedReader reader = new BufferedReader(new InputStreamReader(socket.getInputStream(), StandardCharsets.UTF_8));
                 BufferedWriter writer = new BufferedWriter(new OutputStreamWriter(socket.getOutputStream(), StandardCharsets.UTF_8))) {
                
                String line;
                while ((line = reader.readLine()) != null) {
                    JSONObject req = new JSONObject(line);
                    JSONObject res = handleCommand(req);
                    res.put("id", req.optInt("id", -1));
                    writer.write(res.toString());
                    writer.write("\n");
                    writer.flush();
                }
            } catch (Exception e) {
                // Ignore disconnect errors
            } finally {
                try { socket.close(); } catch (Exception ignore) {}
            }
        }

        private JSONObject handleCommand(JSONObject cmd) {
            JSONObject res = new JSONObject();
            try {
                String type = cmd.optString("type");

                if ("ping".equals(type)) {
                    res.put("ok", true);
                    res.put("msg", "pong");
                    return res;
                }
                
                if ("warmup".equals(type)) {
                    res.put("ok", true);
                    res.put("encryptReady", MainHook.encryptInstance != null);
                    res.put("decryptReady", MainHook.decryptInstance != null);
                    // Match python expectations: it expects lastEncryptArg1 & lastEncryptArg2 which Frida tracked.
                    // We generate dummy values if they are checked, or let the user fetch fresh if empty.
                    res.put("lastEncryptArg1", makeRandomRequestKey());
                    res.put("lastEncryptArg2", ""); 
                    res.put("encryptCaptured", MainHook.encryptInstance != null);
                    res.put("decryptCaptured", MainHook.decryptInstance != null);
                    return res;
                }

                if ("capture".equals(type)) {
                    res.put("ok", true);
                    res.put("capture", JSONObject.NULL);
                    return res;
                }

                if ("encrypt".equals(type)) {
                    if (MainHook.encryptInstance == null) {
                        try {
                            Class<?> clazz = XposedHelpers.findClass("com.basetools.encrypt.EncryptDataImpl", classLoader);
                            MainHook.encryptInstance = clazz.newInstance();
                        } catch (Throwable t) {
                            res.put("ok", false);
                            res.put("error", "EncryptDataImpl instance not captured, and newInstance failed: " + t.getMessage());
                            return res;
                        }
                    }
                    String body = cmd.optString("body");
                    String arg1 = makeRandomRequestKey();
                    // Call b(String, String) -> EncryptDataBean
                    Object bean = XposedHelpers.callMethod(MainHook.encryptInstance, "b", arg1, body);
                    JSONObject fields = extractBeanFields(bean);

                    // Form the exact same JSON format as rpc_bridge.js
                    if (fields.has("encryptData") && fields.has("encryptKey")) {
                        String k = fields.getString("encryptKey");
                        String d = fields.getString("encryptData");
                        String rawStr = "{\"encryptKey\":\"" + k + "\",\"data\":\"" + d + "\"}";
                        res.put("encBody", rawStr);
                    } else if (fields.has("data") && fields.has("encryptKey")) {
                        String k = fields.getString("encryptKey");
                        String d = fields.getString("data");
                        String rawStr = "{\"data\":\"" + d + "\",\"encryptKey\":\"" + k + "\"}";
                        res.put("encBody", rawStr);
                    } else if (fields.has("encryptData")) {
                        res.put("encBody", fields.getString("encryptData"));
                    } else {
                        res.put("encBody", fields.toString());
                    }

                    res.put("ok", true);
                    return res;
                }

                if ("decrypt".equals(type)) {
                    String cipherB64 = cmd.optString("cipherB64");
                    String key = cmd.optString("key");
                    
                    byte[] cipherBytes = Base64.decode(cipherB64, Base64.DEFAULT);
                    byte[] keyBytes = key.getBytes(StandardCharsets.US_ASCII);

                    SecretKeySpec spec = new SecretKeySpec(keyBytes, "AES");
                    Cipher cipher = Cipher.getInstance("AES/ECB/PKCS5Padding");
                    cipher.init(Cipher.DECRYPT_MODE, spec);
                    byte[] plain = cipher.doFinal(cipherBytes);
                    String plaintext = new String(plain, StandardCharsets.UTF_8);

                    res.put("ok", true);
                    res.put("plaintext", plaintext);
                    return res;
                }

                if ("decryptResp".equals(type)) {
                    if (MainHook.encryptInstance == null) {
                        try {
                            Class<?> clazz = XposedHelpers.findClass("com.basetools.encrypt.EncryptDataImpl", classLoader);
                            MainHook.encryptInstance = clazz.newInstance();
                        } catch (Throwable t) {
                            res.put("ok", false);
                            res.put("error", "EncryptDataImpl instance not captured/created yet: " + t.getMessage());
                            return res;
                        }
                    }
                    if (MainHook.decryptInstance == null) {
                        try {
                            Class<?> clazz = XposedHelpers.findClass("com.basenetwork.interceptor.DecryptInterceptor", classLoader);
                            MainHook.decryptInstance = clazz.newInstance();
                        } catch (Throwable t) {
                            // Ignored, we have fallbacks below
                        }
                    }
                    
                    String data = cmd.optString("data");
                    String encryptKey = cmd.optString("encryptKey");
                    JSONObject wrap = new JSONObject();
                    wrap.put("data", data);
                    wrap.put("encryptKey", encryptKey);
                    
                    // First try DecryptInterceptor.a if we caught it
                    if (MainHook.decryptInstance != null) {
                        try {
                            String result = (String) XposedHelpers.callMethod(MainHook.decryptInstance, "a", wrap.toString());
                            res.put("ok", true);
                            res.put("plaintext", result);
                            return res;
                        } catch (Throwable t1) {
                            XposedBridge.log("[iBoxRPC] decryptInstance.a failed: " + t1.getMessage());
                        }
                    }

                    // Fallback to try invoking static `a()` on DecryptInterceptor
                    try {
                        Class<?> decryptClass = XposedHelpers.findClass("com.basenetwork.interceptor.DecryptInterceptor", classLoader);
                        String result = (String) XposedHelpers.callStaticMethod(decryptClass, "a", wrap.toString());
                        res.put("ok", true);
                        res.put("plaintext", result);
                        return res;
                    } catch (Throwable t2) {
                        XposedBridge.log("[iBoxRPC] Static DecryptInterceptor.a failed: " + t2.getMessage());
                    }

                    // Fallback to step 1 + 2: create bean via EncryptDataImpl and pass to DecryptInterceptor or other helper
                    try {
                        Object bean = XposedHelpers.callMethod(MainHook.encryptInstance, "a", data, encryptKey);
                        if (bean != null) {
                            try {
                                Class<?> decryptClass = XposedHelpers.findClass("com.basenetwork.interceptor.DecryptInterceptor", classLoader);
                                String result = (String) XposedHelpers.callStaticMethod(decryptClass, "a", bean);
                                res.put("ok", true);
                                res.put("plaintext", result);
                                return res;
                            } catch (Throwable t3) {
                                try {
                                    String result = (String) XposedHelpers.callMethod(MainHook.decryptInstance, "a", bean);
                                    res.put("ok", true);
                                    res.put("plaintext", result);
                                    return res;
                                } catch (Throwable t4) {
                                     res.put("ok", false);
                                     res.put("error", "All decryptResp fallbacks failed.");
                                     return res;
                                }
                            }
                        }
                    } catch (Throwable t5) {
                        res.put("ok", false);
                        res.put("error", "decryptResp bean creation failed: " + t5.getMessage());
                        return res;
                    }

                    res.put("ok", false);
                    res.put("error", "DecryptInterceptor instance not captured yet");
                    return res;
                }

                res.put("ok", false);
                res.put("error", "Unknown command type: " + type);

            } catch (Exception e) {
                try {
                    res.put("ok", false);
                    res.put("error", e.getMessage());
                } catch (Exception ignore) {}
            }
            return res;
        }
    }
}

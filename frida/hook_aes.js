/**
 * iBox HTTP request/response format inspector — DEBUG USE ONLY.
 *
 * WARNING: iBox has Frida detection. DO NOT inject via frida-server / frida CLI.
 *   frida -U com.box.art ...  ← This WILL abort the app.
 * Load this script through 算法助手 (LSPosed) only, same as rpc_bridge.js.
 *
 * Background (from log analysis):
 *   - iBox uses AES/ECB/PKCS5Padding with a per-request random 16-char hex key.
 *   - The AES key is RSA/ECB/PKCS1Padding encrypted with a fixed server public key.
 *   - Request body and RSA-encrypted key are sent together (format TBD).
 *   - Response is also AES/ECB encrypted; the decryption key location is TBD.
 *
 * Goal: hook okhttp3 to log the ACTUAL encrypted HTTP request/response bodies
 *       and all headers, so we can confirm the exact wire format.
 *       After capturing, check logcat: adb logcat | grep "\[HTTP\]"
 */

Java.perform(function () {

    // ── 1. Hook okhttp3 to dump full request + response ──────────────────────

    var OkHttpClient = Java.use("okhttp3.OkHttpClient");
    var Request = Java.use("okhttp3.Request");
    var Response = Java.use("okhttp3.Response");
    var ResponseBody = Java.use("okhttp3.ResponseBody");
    var Buffer = Java.use("okio.Buffer");
    var Charset = Java.use("java.nio.charset.Charset");
    var UTF8 = Charset.forName("UTF-8");

    try {
        var RealCall = Java.use("okhttp3.internal.connection.RealCall");
        RealCall.getResponseWithInterceptorChain.implementation = function () {
            var resp = this.getResponseWithInterceptorChain();
            try {
                var req = this.request.value;
                var url = req.url().toString();
                if (url.indexOf("ibox") !== -1 || url.indexOf("box.art") !== -1) {
                    console.log("\n[HTTP] ============================");
                    console.log("[HTTP] URL: " + url);
                    console.log("[HTTP] Method: " + req.method());

                    // Request headers
                    var reqHeaders = req.headers();
                    for (var i = 0; i < reqHeaders.size(); i++) {
                        console.log("[HTTP] Req-Header: " + reqHeaders.name(i) + ": " + reqHeaders.value(i));
                    }

                    // Request body
                    var reqBody = req.body();
                    if (reqBody !== null) {
                        var buf = Buffer.$new();
                        reqBody.writeTo(buf);
                        console.log("[HTTP] Req-Body: " + buf.readString(UTF8));
                    }

                    // Response headers
                    var respHeaders = resp.headers();
                    console.log("[HTTP] Resp-Code: " + resp.code());
                    for (var j = 0; j < respHeaders.size(); j++) {
                        console.log("[HTTP] Resp-Header: " + respHeaders.name(j) + ": " + respHeaders.value(j));
                    }

                    // Response body (peek — must not consume)
                    var respBody = resp.peekBody(1024 * 1024);
                    if (respBody !== null) {
                        console.log("[HTTP] Resp-Body: " + respBody.string());
                    }
                }
            } catch (e) {
                console.log("[HTTP] capture error: " + e);
            }
            return resp;
        };
        console.log("[ibox] okhttp3.RealCall hooked");
    } catch (e) {
        console.log("[ibox] RealCall hook failed: " + e);
    }

    // ── 2. Hook AES doFinal to log key + plaintext/ciphertext ────────────────
    // Keep this as a secondary reference to cross-check with HTTP body.

    var Cipher = Java.use("javax.crypto.Cipher");

    function bytesToHex(javaBytes) {
        if (!javaBytes) return "(null)";
        var arr = Java.array("byte", javaBytes);
        var hex = "";
        for (var i = 0; i < arr.length; i++) {
            hex += ("0" + (arr[i] & 0xff).toString(16)).slice(-2);
        }
        return hex;
    }

    function bytesToAscii(javaBytes) {
        if (!javaBytes) return "(null)";
        var arr = Java.array("byte", javaBytes);
        var s = "";
        for (var i = 0; i < arr.length; i++) {
            var c = arr[i] & 0xff;
            s += (c >= 32 && c < 127) ? String.fromCharCode(c) : ".";
        }
        return s;
    }

    Cipher.doFinal.overload("[B").implementation = function (input) {
        var result = this.doFinal(input);
        var algo = this.getAlgorithm();
        if (algo.indexOf("AES/ECB") !== -1) {
            // Determine mode: encryption → key is the request AES key
            //                 decryption → key is the response AES key
            try {
                var keyField = this.class.getDeclaredField("key");
                keyField.setAccessible(true);
            } catch (e) { /* ignore */ }

            // Log via stack to distinguish encrypt vs decrypt
            var stack = Java.use("android.util.Log").getStackTraceString(
                Java.use("java.lang.Exception").$new()
            );
            var isIBox = stack.indexOf("basetools") !== -1 || stack.indexOf("basenetwork") !== -1;
            if (isIBox) {
                console.log("\n[AES] algo=" + algo + " inputLen=" + (input ? input.length : 0));
                console.log("[AES] input-ascii: " + bytesToAscii(input));
                if (result) {
                    console.log("[AES] output-hex: " + bytesToHex(result));
                    console.log("[AES] output-b64: " + Java.use("android.util.Base64").encodeToString(result, 0));
                }
            }
        }
        return result;
    };

    console.log("[ibox] Hooks installed. Trigger login in app to capture HTTP format.");
    console.log("[ibox] Look for [HTTP] lines to see exact request/response body structure.");
});

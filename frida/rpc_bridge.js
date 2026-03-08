/**
 * iBox RPC Bridge — TCP socket server, injected via 算法助手 (LSPosed)
 *
 * iBox has Frida detection; frida-server will abort the app.
 * This script is loaded through 算法助手's JS engine (LSPosed-based),
 * which iBox cannot detect.
 *
 * How to load (算法助手 only — do NOT use frida CLI):
 *   1. 算法助手 → 目标 App: iBox (com.box.art) → 新建脚本 → 粘贴本文件
 *   2. 保存启用 → 重启 iBox
 *
 * Connecting from Python:
 *   WiFi mode (no USB): Python connects to phone_ip:27042 directly
 *   USB  mode:          adb forward tcp:27042 tcp:27042  →  connect localhost:27042
 *
 * Protocol: newline-delimited JSON over TCP
 *   Request:  {"id":1,"type":"encrypt","body":"{...}"}\n
 *   Response: {"id":1,"ok":true,"encBody":"..."}\n
 *
 * Supported commands:
 *   ping                           → {"ok":true,"msg":"pong"}
 *   encrypt  {body: jsonStr}       → {encBody: "..."}  (calls EncryptDataImpl.b)
 *   decrypt  {cipherB64, key}      → {plaintext: "..."}
 *   capture                        → last HTTP exchange (url, headers, body)
 */

var PORT = 27042;
var TARGET_PKG = "com.box.art";

// ── Utilities ─────────────────────────────────────────────────────────────────

function bodyToString(okBody) {
    var Buffer = Java.use("okio.Buffer");
    var Charset = Java.use("java.nio.charset.Charset");
    var buf = Buffer.$new();
    okBody.writeTo(buf);
    return buf.readString(Charset.forName("UTF-8"));
}

// ── Last HTTP capture (updated by hook) ──────────────────────────────────────

var _lastCapture = null;
var _captureSeq = 0;

// Cached EncryptDataImpl instance, populated via Java.choose on first use
var _encryptInstance = null;
// Cached DecryptInterceptor instance, populated via choose or intercept hook
var _decryptInstance = null;
// Captured args from a real b(String,String) call by the app itself.
// Logs show the real signature is b(requestKey16, bodyJson).
var _lastEncryptArg1 = null;
var _lastEncryptArg2 = null;

function makeRandomRequestKey() {
    var chars = "0123456789abcdef";
    var out = "";
    for (var i = 0; i < 16; i++) {
        out += chars.charAt(Math.floor(Math.random() * chars.length));
    }
    return out;
}

function extractBeanFields(bean) {
    var result = {};
    var cls = bean.getClass();
    while (cls !== null) {
        try {
            var fields = cls.getDeclaredFields();
            for (var i = 0; i < fields.length; i++) {
                try {
                    fields[i].setAccessible(true);
                    var name = fields[i].getName();
                    if (Object.prototype.hasOwnProperty.call(result, name)) continue;
                    var value = fields[i].get(bean);
                    if (value === null || value === undefined) continue;
                    var typeName = fields[i].getType().getName();
                    if (typeName === "java.lang.String"
                            || typeName === "java.lang.Integer"
                            || typeName === "java.lang.Long"
                            || typeName === "java.lang.Boolean"
                            || typeName === "int"
                            || typeName === "long"
                            || typeName === "boolean") {
                        result[name] = value.toString();
                    }
                } catch (_) {}
            }
            cls = cls.getSuperclass();
            if (cls !== null && cls.getName() === "java.lang.Object") break;
        } catch (_) {
            break;
        }
    }
    return result;
}

function getEncryptInstance() {
    if (_encryptInstance !== null) return _encryptInstance;
    Java.choose("com.basetools.encrypt.EncryptDataImpl", {
        onMatch: function (inst) {
            if (!_encryptInstance) {
                _encryptInstance = inst;
                console.log("[rpc] cached EncryptDataImpl instance");
            }
        },
        onComplete: function () {}
    });
    return _encryptInstance;
}

function getDecryptInstance() {
    if (_decryptInstance !== null) return _decryptInstance;
    try {
        Java.choose("com.basenetwork.interceptor.DecryptInterceptor", {
            onMatch: function (inst) {
                if (!_decryptInstance) {
                    _decryptInstance = inst;
                    console.log("[rpc] cached DecryptInterceptor instance via choose");
                }
            },
            onComplete: function () {}
        });
    } catch (e) {
        console.log("[rpc] getDecryptInstance failed: " + e);
    }
    return _decryptInstance;
}

// ── Thread factory ─────────────────────────────────────────────────────────
// Java.registerClass throws IOException; Java.$extend is unavailable;
// $new.overload() crashes with $borrowClassHandle error in this engine.
//
// Strategy: hook java.lang.Thread.run() (only needs .implementation =).
// Create threads via reflection — Thread(String name) constructor.
//
// Forwarding non-ibox threads back to their original behavior:
//   Path 1: access Thread's private 'target' Runnable field directly (fast).
//   Path 2: set implementation=null (restores original), call this.run(),
//           then re-hook. Android 9+ blocks hidden-API reflection so Path 1
//           may be unavailable — Path 2 is always safe.

var _threadFns   = {};    // uniqueName → function
var _threadSeq   = 0;
var _runHooked   = false;
var _ThreadRef   = null;  // cached Java.use("java.lang.Thread")
var _targetField = null;  // Thread#target Runnable field, null if inaccessible
var _RpcRunnableClass = undefined; // undefined=untried, null=failed, class=ready

function _ensureThreadRef() {
    if (_ThreadRef !== null) return _ThreadRef;
    _ThreadRef = Java.use("java.lang.Thread");
    return _ThreadRef;
}

function _ensureRunnableClass() {
    if (_RpcRunnableClass !== undefined) return _RpcRunnableClass;
    try {
        var Runnable = Java.use("java.lang.Runnable");
        _RpcRunnableClass = Java.registerClass({
            name: "com.cursor.ibox.RpcRunnable",
            implements: [Runnable],
            fields: {
                taskName: "java.lang.String"
            },
            methods: {
                $init: [{
                    argumentTypes: ["java.lang.String"],
                    implementation: function (taskName) {
                        this.taskName.value = taskName;
                    }
                }],
                run: function () {
                    var n = this.taskName.value ? this.taskName.value.toString() : "";
                    var fn = _threadFns[n];
                    if (fn) delete _threadFns[n];
                    console.log("[rpc] runnable thread enter: " + n);
                    if (!fn) return;
                    try {
                        fn();
                    } catch (e) {
                        console.log("[rpc] runnable thread '" + n + "' error: " + e);
                    }
                }
            }
        });
        console.log("[rpc] Runnable helper registered");
    } catch (e) {
        _RpcRunnableClass = null;
        console.log("[rpc] Runnable helper unavailable, fallback to run hook: " + e);
    }
    return _RpcRunnableClass;
}

function _hookImpl() {
    var n = this.getName();
    if (n !== null && Object.prototype.hasOwnProperty.call(_threadFns, n)) {
        var fn = _threadFns[n];
        delete _threadFns[n];
        console.log("[rpc] hook thread enter: " + n);
        try { fn(); } catch (e) {
            console.log("[rpc] thread '" + n + "' error: " + e);
        }
        return;
    }
    // Non-ibox thread — restore original Thread.run behavior.
    // Path 1: call Runnable target directly (no race window).
    if (_targetField !== null) {
        try {
            var tgt = _targetField.get(this);
            if (tgt !== null) tgt.run();
            return;
        } catch (_) {
            _targetField = null;  // field became inaccessible; switch to Path 2
        }
    }
    // Path 2: unhook → call original → rehook.
    _ThreadRef.run.implementation = null;
    try { this.run(); } catch (_) {}
    _ThreadRef.run.implementation = _hookImpl;
}

function _ensureRunHook() {
    if (_runHooked) return true;
    try {
        _ThreadRef = _ensureThreadRef();
        // Try to cache Thread#target for Path 1 forwarding.
        try {
            var tf = _ThreadRef.class.getDeclaredField("target");
            tf.setAccessible(true);
            _targetField = tf;
        } catch (_) {}
        _ThreadRef.run.implementation = _hookImpl;
        _runHooked = true;
        console.log("[rpc] Thread.run hook installed (targetField=" + (_targetField !== null) + ")");
        return true;
    } catch (e) {
        console.log("[rpc] Thread.run hook failed: " + e);
        return false;
    }
}

function startThread(fn, name) {
    _threadSeq++;
    var uname = "ibox." + _threadSeq + "." + name;
    _threadFns[uname] = fn;

    // Preferred path: create a real Runnable and start a normal Java thread.
    try {
        var RunnableImpl = _ensureRunnableClass();
        if (RunnableImpl !== null) {
            var Thread0 = _ensureThreadRef();
            var r0 = RunnableImpl.$new(uname);
            var t0 = null;
            try { t0 = Thread0.$new(r0, uname); }
            catch (e0) { console.log("[rpc] ctor(runnable,str) failed: " + e0); }
            if (!t0) {
                try { t0 = Thread0.$new(null, r0, uname); }
                catch (e00) { console.log("[rpc] ctor(null,runnable,str) failed: " + e00); }
            }
            if (!t0) {
                try {
                    var grp0 = Thread0.currentThread().getThreadGroup();
                    t0 = Thread0.$new(grp0, r0, uname);
                } catch (e000) { console.log("[rpc] ctor(grp,runnable,str) failed: " + e000); }
            }
            if (t0) {
                try {
                    t0.setDaemon(true);
                    t0.start();
                    console.log("[rpc] thread started (runnable): " + uname);
                    return t0;
                } catch (te0) {
                    console.log("[rpc] runnable thread start failed for '" + name + "': " + te0);
                }
            }
        }
    } catch (rte) {
        console.log("[rpc] runnable thread path failed for '" + name + "': " + rte);
    }

    if (!_ensureRunHook()) {
        delete _threadFns[uname];
        console.log("[rpc] no threading mechanism — cannot start '" + name + "'");
        return null;
    }
    var t = null;
    // Reuse the cached _ThreadRef — avoids calling Java.use("java.lang.Thread") again
    // after classFactory.loader was switched to the app classloader, which can break
    // system-class lookups and cause $new to appear as "not a function".
    var Thread = _ThreadRef;

    // Each attempt logged individually to diagnose which constructor works.
    // Thread(ThreadGroup, Runnable, String) is the only unambiguous 3-arg ctor.
    try { t = Thread.$new(null, null, uname); }
    catch (e1) { console.log("[rpc] ctor(null,null,str) failed: " + e1); }

    if (!t) {
        try { t = Thread.$new(null, uname); }
        catch (e2) { console.log("[rpc] ctor(null,str) failed: " + e2); }
    }

    if (!t) {
        try {
            var grp = Thread.currentThread().getThreadGroup();
            t = Thread.$new(grp, uname);
        } catch (e3) { console.log("[rpc] ctor(grp,str) failed: " + e3); }
    }

    if (!t) {
        delete _threadFns[uname];
        console.log("[rpc] all thread ctor attempts failed for '" + name + "'");
        return null;
    }

    try {
        t.setDaemon(true);
        t.start();
        console.log("[rpc] thread started: " + uname);
        return t;
    } catch (e) {
        delete _threadFns[uname];
        console.log("[rpc] thread start/daemon failed for '" + name + "': " + e);
        return null;
    }
}

// ── Command handlers ──────────────────────────────────────────────────────────

function handleCmd(cmd) {
    var type = cmd.type;

    if (type === "ping") {
        return { ok: true, msg: "pong" };
    }

    // Pre-warm EncryptDataImpl instance cache.
    // DecryptInterceptor is no longer cached globally — Step 0 of decryptResp
    // acquires a fresh instance via Java.choose() each time to avoid stale GC'd refs.
    if (type === "warmup") {
        var encOk = getEncryptInstance() !== null;
        // Probe whether DecryptInterceptor is accessible (don't cache the result).
        var decOk = false;
        try {
            Java.choose("com.basenetwork.interceptor.DecryptInterceptor", {
                onMatch: function () { decOk = true; },
                onComplete: function () {}
            });
        } catch (we) {
            console.log("[rpc] warmup: DecryptInterceptor probe failed: " + we);
        }
        console.log("[rpc] warmup: encryptReady=" + encOk + " decryptReady=" + decOk
            + " lastArg1=" + _lastEncryptArg1 + " lastArg2=" + _lastEncryptArg2);
        return {
            ok: true,
            encryptReady: encOk,
            decryptReady: decOk,
            lastEncryptArg1: _lastEncryptArg1,
            lastEncryptArg2: _lastEncryptArg2
        };
    }

    if (type === "capture") {
        // Also include a sanitised header summary for quick diagnosis
        var hdrSummary = null;
        if (_lastCapture && _lastCapture.reqHeaders) {
            hdrSummary = {};
            var hh = _lastCapture.reqHeaders;
            for (var hk in hh) {
                if (Object.prototype.hasOwnProperty.call(hh, hk)) {
                    hdrSummary[hk] = hh[hk];
                }
            }
        }
        return { ok: true, capture: _lastCapture, reqHeaders: hdrSummary };
    }

    if (type === "encrypt") {
        try {
            var inst = getEncryptInstance();
            if (!inst) {
                return { ok: false, error: "EncryptDataImpl instance not yet available; trigger a request in iBox first" };
            }

            // Get the EncryptDataImpl class via Frida for direct method calls
            var EncryptDataImpl = Java.use("com.basetools.encrypt.EncryptDataImpl");

            // Discover all b() overloads via reflection for logging/diagnosis
            var bMethods = inst.class.getDeclaredMethods();
            var byBSig = {};
            var allBSigs = [];
            for (var mi = 0; mi < bMethods.length; mi++) {
                var bm = bMethods[mi];
                if (bm.getName() !== "b") continue;
                bm.setAccessible(true);
                var pts = bm.getParameterTypes();
                var sig = [];
                for (var pi = 0; pi < pts.length; pi++) sig.push(pts[pi].getName());
                var key = sig.join(",");
                byBSig[key] = true;
                allBSigs.push("b(" + key + ")");
            }

            // Variant 1: b(RequestBody) → RequestBody  (original assumption)
            var rbKey = "okhttp3.RequestBody";
            if (byBSig[rbKey]) {
                try {
                    var MediaType = Java.use("okhttp3.MediaType");
                    var RequestBody = Java.use("okhttp3.RequestBody");
                    var ct = MediaType.parse("application/json; charset=utf-8");
                    var originalBody = RequestBody.create(ct, cmd.body);
                    var encBody = inst.b(originalBody);
                    return { ok: true, encBody: bodyToString(encBody) };
                } catch (e1) {
                    console.log("[rpc] b(RequestBody) failed: " + e1);
                }
            }

            // Variant 2: b(String, String) → EncryptDataBean (observed in app)
            // Real app calls look like: b(requestKey16, bodyJson).
            var ssKey2 = "java.lang.String,java.lang.String";
            if (byBSig[ssKey2]) {
                try {
                    var arg1 = (cmd.bodyArg1 !== undefined && cmd.bodyArg1 !== null) ? cmd.bodyArg1
                             : (_lastEncryptArg1 !== null) ? _lastEncryptArg1
                             : makeRandomRequestKey();
                    console.log("[rpc] encrypt: calling b(String,String), capturedArg1="
                        + JSON.stringify(_lastEncryptArg1) + ", usingArg1=" + JSON.stringify(arg1)
                        + ", bodyLen=" + cmd.body.length);

                    // Get the method via reflection for reliable invocation
                    var targetMethod = null;
                    var methods = inst.class.getDeclaredMethods();
                    for (var mi = 0; mi < methods.length; mi++) {
                        var m = methods[mi];
                        if (m.getName() === "b") {
                            var pts = m.getParameterTypes();
                            if (pts.length === 2 &&
                                pts[0].getName() === "java.lang.String" &&
                                pts[1].getName() === "java.lang.String") {
                                m.setAccessible(true);
                                targetMethod = m;
                                break;
                            }
                        }
                    }

                    if (!targetMethod) {
                        return { ok: false, error: "Could not find b(String,String) method via reflection" };
                    }

                    // Create Java strings explicitly
                    var JavaString = Java.use("java.lang.String");
                    var jArg1 = JavaString.$new(arg1);
                    var jBody = JavaString.$new(cmd.body);

                    // Invoke via reflection with proper Java objects
                    // Method.invoke(Object obj, Object... args) — args must be passed as array
                    var argsArray = Java.array("java.lang.Object", [jArg1, jBody]);
                    var encResult = targetMethod.invoke(inst, argsArray);
                    console.log("[rpc] encrypt: b(String,String) returned " + (encResult !== null ? encResult.getClass().getName() : "null"));

                    if (encResult !== null) {
                        var resultClass = encResult.getClass();
                        var resultClassName = resultClass.getName();
                        console.log("[rpc] encrypt: result class=" + resultClassName);

                        var beanMap = extractBeanFields(encResult);
                        var beanKeys = [];
                        for (var k in beanMap) {
                            if (Object.prototype.hasOwnProperty.call(beanMap, k)) beanKeys.push(k);
                        }
                        console.log("[rpc] encrypt: bean fields=" + beanKeys.join(","));

                        // The app returns an EncryptDataBean, and the real HTTP body is the
                        // JSON serialization of that bean, not just bean.encryptData.
                        if (beanMap.encryptData && beanMap.encryptKey) {
                            var reqJson = JSON.stringify({
                                encryptKey: beanMap.encryptKey,
                                data: beanMap.encryptData
                            });
                            console.log("[rpc] encrypt: final encBody wrapper len=" + reqJson.length);
                            return { ok: true, encBody: reqJson, arg1Used: arg1 };
                        }

                        // Some builds may use data/encryptKey naming.
                        if (beanMap.data && beanMap.encryptKey) {
                            var reqJson2 = JSON.stringify({
                                data: beanMap.data,
                                encryptKey: beanMap.encryptKey
                            });
                            console.log("[rpc] encrypt: final encBody wrapper len=" + reqJson2.length);
                            return { ok: true, encBody: reqJson2, arg1Used: arg1 };
                        }

                        // Fallback for diagnosis if the bean shape is unexpected.
                        if (beanMap.encryptData) {
                            console.log("[rpc] encrypt: WARNING - bean has encryptData but no encryptKey");
                            return { ok: true, encBody: beanMap.encryptData, arg1Used: arg1 };
                        }

                        console.log("[rpc] encrypt: WARNING - using bean.toString() fallback");
                        return { ok: true, encBody: encResult.toString(), arg1Used: arg1 };
                    }
                } catch (e2) {
                    console.log("[rpc] b(String,String) failed: " + e2);
                    if (e2.stack) {
                        console.log("[rpc] b(String,String) stack: " + e2.stack);
                    }
                    // Return specific error for this variant
                    return { ok: false, error: "b(String,String) invocation failed: " + e2.toString() };
                }
            }

            // Variant 3: b(String) → String
            var sKey = "java.lang.String";
            if (byBSig[sKey]) {
                try {
                    // Find b(String) method via reflection
                    var targetMethod3 = null;
                    var methods3 = inst.class.getDeclaredMethods();
                    for (var mi3 = 0; mi3 < methods3.length; mi3++) {
                        var m3 = methods3[mi3];
                        if (m3.getName() === "b") {
                            var pts3 = m3.getParameterTypes();
                            if (pts3.length === 1 && pts3[0].getName() === "java.lang.String") {
                                m3.setAccessible(true);
                                targetMethod3 = m3;
                                break;
                            }
                        }
                    }
                    if (targetMethod3) {
                        var JavaString3 = Java.use("java.lang.String");
                        var jBody3 = JavaString3.$new(cmd.body);
                        var argsArray3 = Java.array("java.lang.Object", [jBody3]);
                        var encBody2 = targetMethod3.invoke(inst, argsArray3);
                        if (encBody2 !== null) {
                            return { ok: true, encBody: encBody2.toString() };
                        }
                    }
                } catch (e3) {
                    console.log("[rpc] b(String) failed: " + e3);
                }
            }

            return { ok: false, error: "No working b() overload found. Available: " + allBSigs.join(", ") };
        } catch (e) {
            return { ok: false, error: e.toString() };
        }
    }

    if (type === "decrypt") {
        try {
            var Base64 = Java.use("android.util.Base64");
            var cipherBytes = Base64.decode(cmd.cipherB64, 0);
            var keyBytes = Java.use("java.lang.String").$new(cmd.key).getBytes("ASCII");

            var Cipher = Java.use("javax.crypto.Cipher");
            var SecretKeySpec = Java.use("javax.crypto.spec.SecretKeySpec");
            var spec = SecretKeySpec.$new(keyBytes, "AES");
            var cipher = Cipher.getInstance("AES/ECB/PKCS5Padding");
            cipher.init(2, spec);
            var plain = cipher.doFinal(cipherBytes);
            var plaintext = Java.use("java.lang.String").$new(plain, "UTF-8");
            return { ok: true, plaintext: plaintext };
        } catch (e) {
            return { ok: false, error: e.toString() };
        }
    }

    // Call EncryptDataImpl.a() — the app's own response decryption function.
    // Mirrors the "encrypt" command which calls EncryptDataImpl.b().
    // Tries every overload of method "a" and picks the one that works.
    if (type === "decryptResp") {
        console.log("[rpc] decryptResp: enter");
        try {
            var inst = getEncryptInstance();
            if (!inst) {
                return { ok: false, error: "EncryptDataImpl not cached — trigger a request in iBox first" };
            }
            console.log("[rpc] decryptResp: inst OK");

            // ── Step 0: call DecryptInterceptor.a(String) to fully decrypt the response.
            //
            // Prefer the cached _decryptInstance (set by the intercept hook on real requests)
            // to avoid Java.choose() which triggers a heap scan and causes GC conflicts in
            // 算法助手 that crash the app ~5s after the call.
            // Fall back to Java.choose() only when the cached instance is unavailable.
            console.log("[rpc] step0: acquiring DecryptInterceptor instance");
            var freshDI = null;

            // Try cached instance first (no heap scan, no GC conflict).
            // IMPORTANT: also validate the class name.  算法助手's JS engine may store
            // the captured `this` as a WeakReference internally; after GC the referent
            // becomes null and getClass() returns WeakReference instead of DecryptInterceptor.
            // In that case we must invalidate and fall back to Java.choose().
            if (_decryptInstance !== null) {
                try {
                    var _diCachedCls = _decryptInstance.getClass().getName();
                    if (_diCachedCls.indexOf("DecryptInterceptor") >= 0) {
                        freshDI = _decryptInstance;
                        console.log("[rpc] step0: using cached _decryptInstance (" + _diCachedCls + ")");
                    } else {
                        // WeakReference or wrong object — invalidate
                        console.log("[rpc] step0: cached instance is " + _diCachedCls + " (not DI), invalidating");
                        _decryptInstance = null;
                    }
                } catch (_stale) {
                    _decryptInstance = null;
                    console.log("[rpc] step0: cached instance stale, falling back to choose");
                }
            }

            // Fallback: heap scan (safe here — no bean created yet).
            if (freshDI === null) {
                try {
                    Java.choose("com.basenetwork.interceptor.DecryptInterceptor", {
                        onMatch: function (di) { if (!freshDI) { freshDI = di; } },
                        onComplete: function () {}
                    });
                } catch (chooseErr) {
                    console.log("[rpc] step0: choose failed: " + chooseErr);
                }
            }
            console.log("[rpc] step0: freshDI=" + (freshDI !== null));

            if (freshDI !== null) {
                try {
                    // ── Try 1: call DecryptInterceptor.a(String) via the static class definition.
                    //
                    // Previously we looked up a(String) via freshDI.getClass().getDeclaredMethods().
                    // That broke when freshDI's runtime class is a subclass (or a WeakReference)
                    // that doesn't declare a(String) itself — getDeclaredMethods() won't find
                    // inherited methods.  Using Java.use() to get the method directly avoids this.
                    var bodyJson0 = JSON.stringify({ data: cmd.data, encryptKey: cmd.encryptKey });
                    var diAm0 = null, diRetType0 = null, diBm0 = null;

                    // Resolve a(String) from the concrete class definition (walks parent chain).
                    try {
                        var DIClass0 = Java.use("com.basenetwork.interceptor.DecryptInterceptor");
                        // Walk declared methods of every class in the hierarchy to find a(String).
                        var _cls0 = DIClass0.class;
                        while (_cls0 !== null) {
                            var _ms0 = _cls0.getDeclaredMethods();
                            for (var _mi0 = 0; _mi0 < _ms0.length; _mi0++) {
                                var _dpts0 = _ms0[_mi0].getParameterTypes();
                                var _ret0  = _ms0[_mi0].getReturnType().getName();
                                if (_dpts0.length === 1 && _dpts0[0].getName() === "java.lang.String"
                                        && _ms0[_mi0].getName() === "a") {
                                    _ms0[_mi0].setAccessible(true);
                                    diAm0 = _ms0[_mi0]; diRetType0 = _ret0;
                                }
                                if (_dpts0.length === 1 && _dpts0[0].getName() === "okhttp3.Response") {
                                    _ms0[_mi0].setAccessible(true);
                                    diBm0 = _ms0[_mi0];
                                }
                            }
                            try {
                                _cls0 = _cls0.getSuperclass();
                                if (_cls0 !== null && _cls0.getName() === "java.lang.Object") break;
                            } catch (_) { break; }
                        }
                    } catch (_e0) {
                        console.log("[rpc] step0: method lookup err: " + _e0);
                    }

                    if (diAm0 !== null) {
                        // Also include the full server body text if the caller supplied it,
                        // so that a(String) receives the exact JSON the server sent (incl.
                        // "code"/"message" fields we don't forward separately).
                        var variants = [
                            cmd.bodyJson || bodyJson0,  // full server response body text (preferred)
                            bodyJson0,                   // {"data":"...","encryptKey":"..."}
                            cmd.data,                    // just the AES-encrypted data field
                            cmd.encryptKey               // just the RSA-encrypted key field
                        ];
                        for (var vi = 0; vi < variants.length; vi++) {
                            if (!variants[vi]) continue;
                            console.log("[rpc] step0: trying a(variant[" + vi + "])");
                            try {
                                var jArg = Java.use("java.lang.String").$new(variants[vi]);
                                var r0   = diAm0.invoke(freshDI, [jArg]);
                                console.log("[rpc] step0: a(v" + vi + ") = " + (r0 !== null ? r0.toString().substring(0, 40) : "null"));
                                if (r0 !== null) {
                                    if (diRetType0 === "[B") {
                                        // byte[] return → it's a raw AES key, decrypt data with it
                                        try {
                                            var B64a = Java.use("android.util.Base64");
                                            var Cia  = Java.use("javax.crypto.Cipher");
                                            var SKSa = Java.use("javax.crypto.spec.SecretKeySpec");
                                            var cia  = Cia.getInstance("AES/ECB/PKCS5Padding");
                                            cia.init(2, SKSa.$new(r0, "AES"));
                                            var pta  = Java.use("java.lang.String").$new(
                                                cia.doFinal(B64a.decode(cmd.data, 0)), "UTF-8");
                                            console.log("[rpc] step0: AES([B key) OK, pt=" + pta.toString().substring(0, 40));
                                            return { ok: true, plaintext: pta.toString() };
                                        } catch (veB) {
                                            console.log("[rpc] step0: AES([B key) err: " + veB);
                                        }
                                    } else {
                                        // String return → a(String) already returns the fully decrypted JSON
                                        var r0Str = r0.toString();
                                        console.log("[rpc] step0: a(v" + vi + ") is plaintext, pt=" + r0Str.substring(0, 40));
                                        return { ok: true, plaintext: r0Str };
                                    }
                                }
                            } catch (ve) {
                                console.log("[rpc] step0: a(v" + vi + ") err: " + ve);
                            }
                        }
                    }

                    // ── Try 2: call b(Response) with a synthesized OkHttp Response.
                    if (diBm0 !== null) {
                        console.log("[rpc] step0: trying b(Response)");
                        try {
                            var RB0 = Java.use("okhttp3.ResponseBody");
                            var MT0 = Java.use("okhttp3.MediaType");
                            var rb0 = RB0.create(MT0.parse("application/json; charset=utf-8"),
                                                  Java.use("java.lang.String").$new(bodyJson0));
                            var Req0 = Java.use("okhttp3.Request$Builder").$new();
                            Req0 = Req0.url("https://sail-api.ibox.art/login");
                            var req0 = Req0.build();
                            var Proto0 = Java.use("okhttp3.Protocol");
                            var resp0  = Java.use("okhttp3.Response$Builder").$new()
                                .request(req0)
                                .protocol(Proto0.HTTP_1_1.value)
                                .code(200)
                                .message("OK")
                                .body(rb0)
                                .build();
                            var pt0b = diBm0.invoke(freshDI, [resp0]);
                            console.log("[rpc] step0: b(Response) result=" + (pt0b !== null ? pt0b.toString().substring(0, 40) : "null"));
                            if (pt0b !== null) {
                                var pt0bStr = pt0b.toString();
                                // b() may just serialize the body — only treat as plaintext if no encryptKey field
                                if (pt0bStr.indexOf('"encryptKey"') < 0) {
                                    return { ok: true, plaintext: pt0bStr };
                                }
                                console.log("[rpc] step0: b(Response) echoed encrypted body — continuing to RSA scan");
                            }
                        } catch (be) {
                            console.log("[rpc] step0: b(Response) err: " + be);
                        }
                    }

                    // ── Try 3a: scan DecryptInterceptor (freshDI) fields for RSA private key.
                    try {
                        var diCls3 = freshDI.getClass();
                        while (diCls3 !== null) {
                            var diF3 = diCls3.getDeclaredFields();
                            for (var dfi3 = 0; dfi3 < diF3.length; dfi3++) {
                                diF3[dfi3].setAccessible(true);
                                var dfv3 = diF3[dfi3].get(freshDI);
                                var dft3 = diF3[dfi3].getType().getName();
                                console.log("[rpc] step0: di." + diF3[dfi3].getName()
                                    + "(" + dft3 + ")="
                                    + (dfv3 !== null ? dfv3.toString().substring(0, 80) : "null"));
                                if (dfv3 !== null && (dft3 === "java.security.PrivateKey"
                                        || dfv3.getClass().getName().indexOf("RSA") >= 0
                                        || dfv3.getClass().getName().indexOf("Private") >= 0)) {
                                    try {
                                        var CipherR3 = Java.use("javax.crypto.Cipher");
                                        var cr3 = CipherR3.getInstance("RSA/ECB/PKCS1Padding");
                                        cr3.init(2, dfv3);
                                        var B64r3 = Java.use("android.util.Base64");
                                        var aesKey3 = cr3.doFinal(B64r3.decode(cmd.encryptKey, 0));
                                        console.log("[rpc] step0: di RSA OK, keyLen=" + aesKey3.length);
                                        var c3 = CipherR3.getInstance("AES/ECB/PKCS5Padding");
                                        c3.init(2, Java.use("javax.crypto.spec.SecretKeySpec").$new(aesKey3, "AES"));
                                        var pt3 = Java.use("java.lang.String").$new(
                                            c3.doFinal(B64r3.decode(cmd.data, 0)), "UTF-8");
                                        console.log("[rpc] step0: di RSA+AES OK, pt=" + pt3.toString().substring(0, 40));
                                        return { ok: true, plaintext: pt3.toString() };
                                    } catch (re3) {
                                        console.log("[rpc] step0: di RSA err: " + re3);
                                    }
                                }
                            }
                            try {
                                diCls3 = diCls3.getSuperclass();
                                if (diCls3 !== null && diCls3.getName() === "java.lang.Object") break;
                            } catch (_) { break; }
                        }
                    } catch (e3a) {
                        console.log("[rpc] step0: di field scan err: " + e3a);
                    }

                    // ── Try 3b: scan EncryptDataImpl fields for RSA private key.
                    console.log("[rpc] step0: scanning EncryptDataImpl fields for RSA key");
                    var encImplCls = inst.getClass();
                    while (encImplCls !== null) {
                        var encF = encImplCls.getDeclaredFields();
                        for (var efi = 0; efi < encF.length; efi++) {
                            encF[efi].setAccessible(true);
                            var efv = encF[efi].get(inst);
                            var eft = encF[efi].getType().getName();
                            console.log("[rpc] step0: encImpl." + encF[efi].getName()
                                + "(" + eft + ")="
                                + (efv !== null ? efv.toString().substring(0, 60) : "null"));

                            // If it's a PrivateKey, try RSA decrypt directly.
                            if (efv !== null && (eft === "java.security.PrivateKey"
                                    || efv.getClass().getName().indexOf("RSA") >= 0
                                    || efv.getClass().getName().indexOf("Private") >= 0)) {
                                try {
                                    var CipherR = Java.use("javax.crypto.Cipher");
                                    var cr = CipherR.getInstance("RSA/ECB/PKCS1Padding");
                                    cr.init(2, efv);  // Cipher.DECRYPT_MODE = 2
                                    var B64r = Java.use("android.util.Base64");
                                    var encKeyR = B64r.decode(cmd.encryptKey, 0);
                                    var aesKeyR = cr.doFinal(encKeyR);
                                    console.log("[rpc] step0: RSA decrypt OK, keyLen=" + aesKeyR.length);
                                    var cipherR = CipherR.getInstance("AES/ECB/PKCS5Padding");
                                    var SKSr = Java.use("javax.crypto.spec.SecretKeySpec");
                                    cipherR.init(2, SKSr.$new(aesKeyR, "AES"));
                                    var dataR = B64r.decode(cmd.data, 0);
                                    var ptR   = Java.use("java.lang.String").$new(cipherR.doFinal(dataR), "UTF-8");
                                    console.log("[rpc] step0: full RSA+AES decrypt OK!");
                                    return { ok: true, plaintext: ptR.toString() };
                                } catch (re) {
                                    console.log("[rpc] step0: RSA decrypt err: " + re);
                                }
                            }
                        }
                        try {
                            encImplCls = encImplCls.getSuperclass();
                            if (encImplCls !== null && encImplCls.getName() === "java.lang.Object") break;
                        } catch (_) { break; }
                    }
                } catch (step0err) {
                    console.log("[rpc] step0 err: " + step0err);
                }
            }

            // Discover all overloads of method "a" via reflection.
            var methods = inst.class.getDeclaredMethods();
            var overloads = [];
            for (var mi = 0; mi < methods.length; mi++) {
                var m = methods[mi];
                if (m.getName() === "a") {
                    m.setAccessible(true);
                    var pts = m.getParameterTypes();
                    var sig = [];
                    for (var pi = 0; pi < pts.length; pi++) sig.push(pts[pi].getName());
                    overloads.push({ m: m, sig: sig });
                }
            }
            if (overloads.length === 0) {
                return { ok: false, error: "No method 'a' found on EncryptDataImpl" };
            }

            var sigList = overloads.map(function (o) { return "a(" + o.sig.join(",") + ")"; }).join(" | ");

            // Index overloads by signature for easy lookup.
            var byKey = {};
            for (var i = 0; i < overloads.length; i++) {
                byKey[overloads[i].sig.join(",")] = overloads[i].m;
            }

            // Step 1: a(String, String) → EncryptDataBean
            //   bean.encryptData  = cmd.data        (AES-encrypted response body)
            //   bean.encryptKey   = cmd.encryptKey  (RSA-encrypted AES key)
            console.log("[rpc] step1: creating bean");
            var ssKey = "java.lang.String,java.lang.String";
            if (!byKey[ssKey]) {
                return { ok: false, error: "a(String,String) not found. Available: " + sigList };
            }
            var bean = byKey[ssKey].invoke(inst, [cmd.data, cmd.encryptKey]);
            if (bean === null) {
                return { ok: false, error: "a(String,String) returned null" };
            }
            console.log("[rpc] step1: bean created, class=" + bean.getClass().getName());

            // Step 2: find ANY method (any name) that takes a single EncryptDataBean.
            // Search EncryptDataImpl first, then candidate helper/interceptor classes.
            // IMPORTANT: never call Java.choose() here — heap scans from the TCP handler
            // thread conflict with GC and crash the app. Use static invocation instead.
            var realBeanClass = bean.getClass().getName();
            var allSigs2 = [];
            var decryptMethod = null;
            var decryptTarget = null; // null = static call; inst = instance call

            // 2a: scan EncryptDataImpl
            var allDeclared = inst.class.getDeclaredMethods();
            for (var di = 0; di < allDeclared.length; di++) {
                var dm = allDeclared[di];
                var dpts = dm.getParameterTypes();
                var dsig = dm.getName() + "(";
                for (var dpi = 0; dpi < dpts.length; dpi++) {
                    dsig += (dpi > 0 ? "," : "") + dpts[dpi].getName();
                }
                dsig += ")";
                allSigs2.push("EncryptDataImpl." + dsig);
                if (dpts.length === 1 && dpts[0].getName() === realBeanClass) {
                    dm.setAccessible(true);
                    decryptMethod = dm;
                    decryptTarget = inst;
                }
            }

            // 2b: scan candidate classes (static-only, no heap scan)
            if (!decryptMethod) {
                var candidateNames = [
                    "com.basenetwork.interceptor.DecryptInterceptor",
                    "com.basetools.encrypt.EncryptHelper",
                    "com.basetools.encrypt.EncryptUtil",
                    "com.basetools.encrypt.RSAHelper",
                    "com.basetools.encrypt.AESHelper",
                ];
                for (var ci = 0; ci < candidateNames.length; ci++) {
                    try {
                        var candClass = Java.use(candidateNames[ci]).class;
                        var candMethods = candClass.getDeclaredMethods();
                        for (var cmi = 0; cmi < candMethods.length; cmi++) {
                            var cm = candMethods[cmi];
                            var cmpts = cm.getParameterTypes();
                            var cmsig = cm.getName() + "(";
                            for (var cmpi = 0; cmpi < cmpts.length; cmpi++) {
                                cmsig += (cmpi > 0 ? "," : "") + cmpts[cmpi].getName();
                            }
                            cmsig += ")";
                            allSigs2.push(candidateNames[ci].split(".").pop() + "." + cmsig);
                            if (cmpts.length === 1 && cmpts[0].getName() === realBeanClass) {
                                cm.setAccessible(true);
                                decryptMethod = cm;
                                // Use live freshDI instance for DecryptInterceptor; static for others
                                decryptTarget = (candidateNames[ci] === "com.basenetwork.interceptor.DecryptInterceptor" && freshDI !== null)
                                    ? freshDI : null;
                            }
                        }
                    } catch (_) {}
                }
            }

            if (decryptMethod) {
                var target = decryptTarget !== null ? decryptTarget : null;
                var plainObj = decryptMethod.invoke(target, [bean]);
                if (plainObj !== null) return { ok: true, plaintext: plainObj.toString() };
                return { ok: false, error: decryptMethod.getName() + "(EncryptDataBean) returned null" };
            }

            // Step 3 intentionally omitted.
            // Calling any Java method while an EncryptDataBean is live on the heap
            // triggers a GC conflict in 算法助手 that crashes the app.
            // The decrypt path is now handled in Step 0 (before bean creation).

            // Nothing found — dump bean fields + all scanned method sigs for diagnosis.
            var fields = bean.getClass().getDeclaredFields();
            var info = {};
            for (var fi = 0; fi < fields.length; fi++) {
                fields[fi].setAccessible(true);
                var fv = fields[fi].get(bean);
                info[fields[fi].getName()] = fv ? fv.toString().substring(0, 80) : null;
            }
            return {
                ok: false,
                error: "No decrypt method found. Bean class: " + realBeanClass +
                       " | Bean fields: " + JSON.stringify(info) +
                       " | Scanned: " + allSigs2.join(", ")
            };
        } catch (e) {
            return { ok: false, error: e.toString() };
        }
    }

    return { ok: false, error: "unknown command type: " + type };
}

// ── TCP client handler ────────────────────────────────────────────────────────

function handleClient(socket) {
    try {
        var BufferedReader = Java.use("java.io.BufferedReader");
        var InputStreamReader = Java.use("java.io.InputStreamReader");
        var PrintWriter = Java.use("java.io.PrintWriter");

        // Drop idle/stale PC connections so a dead client cannot block the RPC server forever.
        try { socket.setSoTimeout(15000); } catch (_) {}
        try {
            var peer0 = socket.getInetAddress().getHostAddress() + ":" + socket.getPort();
            console.log("[rpc] handleClient enter: " + peer0);
        } catch (_) {
            console.log("[rpc] handleClient enter");
        }

        var reader = BufferedReader.$new(InputStreamReader.$new(socket.getInputStream(), "UTF-8"));
        var writer = PrintWriter.$new(socket.getOutputStream(), true);

        var line;
        while ((line = reader.readLine()) !== null) {
            line = line.trim();
            if (!line) continue;
            console.log("[rpc] recv line: " + line.substring(0, 120));

            var resp;
            try {
                var cmd = JSON.parse(line);
                var result = handleCmd(cmd);
                if (cmd.id !== undefined) result.id = cmd.id;
                resp = JSON.stringify(result);
            } catch (e) {
                resp = JSON.stringify({ ok: false, error: "parse error: " + e.toString() });
            }
            console.log("[rpc] send line: " + resp.substring(0, 120));
            writer.println(resp);
        }
    } catch (e) {
        // Idle timeout / client disconnect are both expected in normal use.
        var emsg = String(e);
        if (emsg.indexOf("SocketTimeoutException") >= 0) {
            console.log("[rpc] client idle timeout, closing stale connection");
        }
    } finally {
        try { socket.close(); } catch (e) {}
    }
}

// ── Main ──────────────────────────────────────────────────────────────────────

Java.perform(function () {

    // ── Anti-detection hooks: run in ALL processes (main + every subprocess). ──
    //
    // These MUST execute BEFORE the process-name guard so that subprocesses are
    // also protected.  They only use standard java.* / android.os.* classes and
    // do NOT need the app classloader to be set up first.
    //
    // The app spawns several processes (main, :push, …) each running an
    // anti-tamper Thread-3 that:
    //   (a) reads /proc/net/tcp every ~5 s to detect port 27042, and
    //   (b) calls System.exit() / Process.killProcess() on detection.
    // Previously these hooks only ran in the main process, leaving every
    // subprocess free to trigger the kill — causing the crash loop.

    // (a) Redirect /proc/net/tcp reads to /dev/null so the port scan sees nothing.
    (function installProcNetHidingHook() {
        try {
            var FileInputStream = Java.use("java.io.FileInputStream");
            var FISFile = Java.use("java.io.File");

            function _isTcpPath(p) {
                // Match /proc/net/tcp[6] and /proc/self/net/tcp[6]
                return p && (p.indexOf("/proc/net/tcp") !== -1
                          || p.indexOf("/proc/self/net/tcp") !== -1);
            }

            FileInputStream.$init.overload("java.lang.String").implementation = function (path) {
                if (_isTcpPath(path)) {
                    return this.$init("/dev/null");
                }
                return this.$init(path);
            };

            FileInputStream.$init.overload("java.io.File").implementation = function (file) {
                try {
                    if (file && _isTcpPath(file.getAbsolutePath())) {
                        return this.$init(FISFile.$new("/dev/null"));
                    }
                } catch (_) {}
                return this.$init(file);
            };

            console.log("[rpc] proc/net/tcp hiding hook installed");
        } catch (e) {
            console.log("[rpc] proc/net/tcp hiding hook failed: " + e);
        }
    }());

    // (b) Block self-destruct calls triggered by tamper detection (Java layer).
    (function installAntiKillHooks() {
        try {
            var JavaSystem = Java.use("java.lang.System");
            JavaSystem.exit.implementation = function (code) {
                console.log("[rpc] System.exit(" + code + ") suppressed by anti-detection hook");
            };
            console.log("[rpc] System.exit hook installed");
        } catch (e) {
            console.log("[rpc] System.exit hook failed: " + e);
        }
        try {
            var OsProcess = Java.use("android.os.Process");
            OsProcess.killProcess.implementation = function (pid) {
                console.log("[rpc] Process.killProcess(" + pid + ") suppressed by anti-detection hook");
            };
            console.log("[rpc] Process.killProcess hook installed");
        } catch (e) {
            console.log("[rpc] Process.killProcess hook failed: " + e);
        }
    }());

    // (c) Hook native open/openat to redirect /proc/net/tcp at the C level.
    //
    // Thread-3 uses libc open() directly — it completely bypasses the Java
    // FileInputStream hook above.  We swap the path to a pre-written fake
    // TCP table that looks realistic (a few normal entries, NO port 27042).
    // Redirecting to /dev/null (empty file) can itself look suspicious because
    // a real Android device always has at least a few TCP connections.
    //
    // The fake file is written once at hook-install time; the pre-allocated
    // path pointer outlives every onEnter callback.
    (function installNativeOpenHook() {
        try {
            // Write a fake /proc/net/tcp file with plausible-looking entries.
            // Format: "  sl  local_address rem_address   st ..." header + rows.
            // All local ports shown are common system ports — port 27042 (0x699A)
            // is deliberately absent.
            var fakeTcpContent =
                "  sl  local_address rem_address   st tx_queue rx_queue " +
                "tr tm->when retrnsmt   uid  timeout inode\n" +
                "   0: 00000000:006F 00000000:0000 0A 00000000:00000000 " +
                "00:00000000 00000000   1000        0 12301 1 0000000000000000 100 0 0 10 0\n" +
                "   1: 0F02A8C0:E3F2 5F52344E:01BB 01 00000000:00000000 " +
                "02:000906A5 00000000 10226        0 45678 4 0000000000000000 20 4 24 10 -1\n" +
                "   2: 0F02A8C0:C3D1 2B32344E:01BB 01 00000000:00000000 " +
                "02:000926F1 00000000 10226        0 45700 4 0000000000000000 20 4 24 10 -1\n";

            var fakeTcpPath = null;
            try {
                var _fCtx = Java.use("android.app.ActivityThread").currentApplication();
                if (_fCtx) {
                    var _fDir = null;
                    try { _fDir = _fCtx.getCacheDir().getAbsolutePath(); } catch (_) {}
                    if (!_fDir) { try { _fDir = _fCtx.getFilesDir().getAbsolutePath(); } catch (_) {} }
                    if (_fDir) {
                        fakeTcpPath = _fDir + "/.net_tcp_fake";
                        var _fos = Java.use("java.io.FileOutputStream").$new(fakeTcpPath);
                        var _fb  = Java.use("java.lang.String").$new(fakeTcpContent).getBytes("UTF-8");
                        _fos.write(_fb);
                        _fos.close();
                        console.log("[rpc] fake /proc/net/tcp written to " + fakeTcpPath);
                    }
                }
            } catch (_fe) {
                console.log("[rpc] fake tcp file write failed: " + _fe);
            }

            // Fall back to /dev/null if file creation failed.
            var redirectPath = Memory.allocUtf8String(fakeTcpPath || "/dev/null");

            var devNullPath = Memory.allocUtf8String("/dev/null");

            function patchPath(args, idx) {
                try {
                    var path = args[idx].readCString();
                    if (!path) return;
                    // Redirect TCP proc table reads to the fake file.
                    if (path.indexOf("/proc/net/tcp") !== -1
                     || path.indexOf("/proc/self/net/tcp") !== -1) {
                        args[idx] = redirectPath;
                        return;
                    }
                    // Redirect /proc/self/maps to /dev/null so hook-framework
                    // library names (算法助手 / NativeHook) are invisible.
                    if (path === "/proc/self/maps"
                     || path === "/proc/net/unix"
                     || path.indexOf("/proc/self/net/") !== -1) {
                        args[idx] = devNullPath;
                    }
                    // No diagnostic logging here — /proc/self/stat is read
                    // ~10x/second by the runtime, flooding the JS event loop
                    // and causing the RPC bridge ping to time out.
                } catch (_) {}
            }

            var openPtr = Module.findExportByName("libc.so", "open");
            if (openPtr) {
                Interceptor.attach(openPtr, { onEnter: function(args) { patchPath(args, 0); } });
            }

            var openatPtr = Module.findExportByName("libc.so", "openat");
            if (openatPtr) {
                Interceptor.attach(openatPtr, { onEnter: function(args) { patchPath(args, 1); } });
            }

            console.log("[rpc] native open/openat hook installed");
        } catch (e) {
            console.log("[rpc] native open/openat hook failed: " + e);
        }
    }());

    // (d) Intercept native abort().
    //
    // Strategy: call pthread_exit(0) to cleanly terminate the detection thread
    // without hitting the compiler-generated trap (brk #0) that appears after
    // every noreturn call site.  Also hook pthread_kill so that any watchdog
    // checking the dead thread's handle via kill(tid,0) gets a fake "alive".
    //
    // Diagnostic: capture a fuzzy backtrace each time abort() is called so
    // we can see EXACTLY what detection logic triggered it.
    (function installAbortHook() {
        try {
            var abortPtr = Module.findExportByName("libc.so", "abort");
            if (!abortPtr) throw new Error("abort not found in libc.so");

            var pthreadExitPtr = Module.findExportByName("libc.so", "pthread_exit");
            var pthreadExitFn  = pthreadExitPtr
                ? new NativeFunction(pthreadExitPtr, 'void', ['pointer'])
                : null;

            var usleepPtr = Module.findExportByName("libc.so", "usleep");
            var usleepFn  = usleepPtr ? new NativeFunction(usleepPtr, 'int', ['uint']) : null;

            Interceptor.replace(abortPtr, new NativeCallback(function () {
                console.log("[rpc] native abort() intercepted — pthread_exit tid="
                          + Process.getCurrentThreadId());
                if (pthreadExitFn) {
                    pthreadExitFn(ptr(0));
                    // pthread_exit never returns.
                }
                // Fallback: spin so we never hit the compiler trap after abort.
                if (usleepFn) { while (true) { usleepFn(3600000); } }
            }, 'void', []));

            console.log("[rpc] native abort() hook installed");
            // Note: pthread_kill hook was removed — it was intercepting JVM's
            // own thread-signal calls and adding overhead that delayed the RPC
            // bridge's Java thread, causing ping timeouts.  abort() is now
            // rarely called (detection is fully bypassed by the maps/tcp
            // hooks), so the watchdog bypass is not needed in practice.
        } catch (e) {
            console.log("[rpc] native abort() hook failed: " + e);
        }
    }());

    // (e) Block connect()-based port detection.
    //
    // Thread-3 probes port 27042 via connect(127.0.0.1:27042).
    // If the bridge is running (listening on 27042), the probe succeeds and
    // abort() is triggered.
    //
    // Fix: intercept connect() calls to port 27042 inside THIS process,
    // replace sockfd with -1 (→ EBADF), then fake errno=ECONNREFUSED.
    //
    // Safe because:
    //   • The bridge is a SERVER — it never calls connect() to its own port.
    //   • adb forward runs as adbd (a separate process) — our hook in
    //     com.box.art does NOT intercept adbd's forwarding calls.
    //
    // bind() hook has been intentionally removed: it was intercepting the
    // bridge's OWN ServerSocket bind, causing it to land on a random port.
    (function installConnectHook() {
        try {
            var _setErrno = null;
            try {
                var _errnoSym = Module.findExportByName("libc.so", "__errno")
                             || Module.findExportByName("libc.so", "__errno_location");
                if (_errnoSym) {
                    var _getErrnoPtr = new NativeFunction(_errnoSym, 'pointer', []);
                    _setErrno = function(code) {
                        try { _getErrnoPtr().writeS32(code); } catch (_) {}
                    };
                }
            } catch (_) {}

            // sin_port is at offset +2, network byte order (big-endian).
            function _isPort27042(addrPtr) {
                try {
                    if (addrPtr.isNull()) return false;
                    return (((addrPtr.add(2).readU8() << 8) | addrPtr.add(3).readU8()) === 27042);
                } catch (_) { return false; }
            }

            var connectPtr = Module.findExportByName("libc.so", "connect");
            if (connectPtr) {
                Interceptor.attach(connectPtr, {
                    onEnter: function(args) {
                        this._block = false;
                        if (_isPort27042(args[1])) {
                            this._block = true;
                            args[0] = ptr(-1);   // invalid fd → EBADF, no packet sent
                        }
                    },
                    onLeave: function(retval) {
                        if (this._block) {
                            if (_setErrno) _setErrno(111);  // ECONNREFUSED
                            retval.replace(ptr(-1));
                        }
                    }
                });
                console.log("[rpc] connect() anti-detection hook installed");
            }
        } catch (e) {
            console.log("[rpc] connect() hook failed: " + e);
        }
    }());

    // ── Guard: only run the heavy hooks inside the iBox MAIN process. ─────────
    // LSPosed injects into every process; iBox also has subprocesses
    // (e.g. com.box.art:push) that share the same package name.
    // currentProcessName() returns the full name including ":<suffix>".
    try {
        var ActivityThread = Java.use("android.app.ActivityThread");
        var app = ActivityThread.currentApplication();
        var pkg = app ? app.getPackageName() : "unknown";
        if (pkg !== TARGET_PKG) return;

        var processName = pkg;
        try { processName = ActivityThread.currentProcessName(); } catch (e) {}
        if (processName !== TARGET_PKG) {
            // Subprocess — skip silently
            return;
        }
        console.log("[rpc] running in " + processName);
    } catch (e) {
        console.log("[rpc] pkg guard error: " + e);
        return;
    }

    // ── Fix classFactory: point it at the app's own classloader + code-cache dir.
    //
    // 算法助手 initialises the Java bridge with a system-level classloader, so
    // Java.use() can't find app-bundled classes (OkHttp, etc.) by default.
    // Switching to the app's ClassLoader fixes both the ClassNotFoundException for
    // OkHttp AND the java.io.IOException from registerClass (which needs a writable
    // cacheDir to write DEX; getCodeCacheDir() is guaranteed writable by the app).
    try {
        var _appCtx = Java.use("android.app.ActivityThread").currentApplication();
        if (_appCtx) {
            // Switch to app classloader so Java.use finds OkHttp, etc.
            try { Java.classFactory.loader = _appCtx.getClassLoader(); } catch (_) {}
            // Provide a writable dir for registerClass to write DEX files.
            try {
                var File = Java.use("java.io.File");
                var baseDir = null;
                try { baseDir = _appCtx.getCodeCacheDir().getAbsolutePath(); } catch (_) {}
                if (!baseDir) {
                    try { baseDir = _appCtx.getCacheDir().getAbsolutePath(); } catch (_) {}
                }
                if (!baseDir) {
                    try { baseDir = _appCtx.getFilesDir().getAbsolutePath(); } catch (_) {}
                }
                if (baseDir) {
                    var rpcDexDir = File.$new(baseDir, "cursor_rpc_dex");
                    if (!rpcDexDir.exists()) {
                        rpcDexDir.mkdirs();
                    }
                    Java.classFactory.cacheDir = rpcDexDir.getAbsolutePath();
                    console.log("[rpc] classFactory cacheDir=" + Java.classFactory.cacheDir);
                }
            } catch (_) {}
        }
    } catch (e) {
        console.log("[rpc] classFactory setup failed: " + e);
    }

    // ── Hook EncryptDataImpl.b(String,String) to capture the second encryption key.
    // This arg2 is required for calling b() via RPC — the app uses it as an internal key/IV.
    (function installEncryptHook() {
        try {
            var EncryptDataImpl = Java.use("com.basetools.encrypt.EncryptDataImpl");
            // Hook all overloads of b() — we specifically need b(String,String)
            var bMethods = EncryptDataImpl.class.getDeclaredMethods();
            for (var mi = 0; mi < bMethods.length; mi++) {
                var m = bMethods[mi];
                if (m.getName() !== "b") continue;
                var pts = m.getParameterTypes();
                if (pts.length === 2 && pts[0].getName() === "java.lang.String" && pts[1].getName() === "java.lang.String") {
                    // Found b(String,String) — install hook via Frida's method replacement
                    var bSS = EncryptDataImpl.b.overload('java.lang.String', 'java.lang.String');
                    bSS.implementation = function(arg1, arg2) {
                        if (arg1 !== null && arg1 !== undefined) _lastEncryptArg1 = arg1.toString();
                        if (arg2 !== null && arg2 !== undefined) _lastEncryptArg2 = arg2.toString();
                        var arg1Str = (arg1 === null || arg1 === undefined) ? "null" : arg1.toString();
                        var arg2Str = (arg2 === null || arg2 === undefined) ? "null" : arg2.toString();
                        console.log("[rpc] b(String,String): arg1(len=" + arg1Str.length + ")="
                            + arg1Str.substring(0, 32) + ", arg2(len=" + arg2Str.length + ")="
                            + arg2Str.substring(0, 60));
                        // Call the original overload explicitly to avoid recursion.
                        var result = bSS.call(this, arg1, arg2);
                        var resultStr = result ? result.toString().substring(0, 60) : "null";
                        console.log("[rpc] b(String,String) result=" + resultStr);
                        return result;
                    };
                    console.log("[rpc] EncryptDataImpl.b(String,String) hook installed");
                    break;
                }
            }
        } catch (e) {
            console.log("[rpc] EncryptDataImpl.b hook failed: " + e);
        }
    }());

    // ── Hook HTTP to capture request/response.
    // Hook RealCall.execute() — compatible across OkHttp 3.x/4.x.
    // If RealCall is not found with the current loader, enumerate all loaders.
    (function installHttpHook() {
        function applyHook() {
            var RealCall = Java.use("okhttp3.internal.connection.RealCall");
            RealCall.execute.implementation = function () {
                var resp = this.execute();
                try {
                    var req = null;
                    try { req = this.originalRequest.value; } catch (e) {}
                    if (!req) { try { req = this.request.value; } catch (e) {} }

                    if (req) {
                        var url = req.url().toString();
                        if (url.indexOf("sail-api") !== -1) {
                            var reqHeaders = {};
                            var rh = req.headers();
                            for (var i = 0; i < rh.size(); i++) reqHeaders[rh.name(i)] = rh.value(i);

                            var encBody = "";
                            try { encBody = bodyToString(req.body()); } catch (e) {}

                            var respHeaders = {};
                            var sh = resp.headers();
                            for (var j = 0; j < sh.size(); j++) respHeaders[sh.name(j)] = sh.value(j);

                            var respBody = "";
                            try {
                                var peeked = resp.peekBody(512 * 1024);
                                if (peeked) respBody = peeked.string();
                            } catch (e) {}

                            _captureSeq += 1;
                            _lastCapture = {
                                seq: _captureSeq,
                                url: url,
                                method: req.method(),
                                reqHeaders: reqHeaders,
                                encBody: encBody,
                                respCode: resp.code(),
                                respHeaders: respHeaders,
                                respBody: respBody
                            };
                        }
                    }
                } catch (e) {
                    console.log("[rpc] capture error: " + e);
                }
                return resp;
            };
            console.log("[rpc] HTTP capture hook installed");
        }

        // Also hook DecryptInterceptor to capture the fully-decrypted response body.
        // This populates _lastCapture.respDecrypted so Python can read plaintext directly.
        try {
            var DI = Java.use("com.basenetwork.interceptor.DecryptInterceptor");
            DI.intercept.implementation = function (chain) {
                // Cache the live instance so decryptResp can call a(String) on it.
                if (!_decryptInstance) {
                    _decryptInstance = this;
                    console.log("[rpc] cached DecryptInterceptor instance");
                }
                var resp = this.intercept(chain);
                try {
                    var url = chain.request().url().toString();
                    if (url.indexOf("sail-api") !== -1 && _lastCapture) {
                        var peeked = resp.peekBody(512 * 1024);
                        if (peeked) _lastCapture.respDecrypted = peeked.string();
                    }
                } catch (_) {}
                return resp;
            };
            console.log("[rpc] DecryptInterceptor hook installed");
        } catch (_) {}

        // Try with the already-configured classloader first.
        try {
            applyHook();
            return;
        } catch (e) {
            console.log("[rpc] HTTP hook first attempt failed (" + e + "), enumerating classloaders…");
        }

        // Fallback: walk every classloader until we find one that has RealCall.
        var hooked = false;
        try {
            Java.enumerateClassLoaders({
                onMatch: function (loader) {
                    if (hooked) return;
                    try {
                        loader.loadClass("okhttp3.internal.connection.RealCall");
                        Java.classFactory.loader = loader;
                        applyHook();
                        hooked = true;
                    } catch (_) {}
                },
                onComplete: function () {
                    if (!hooked) console.log("[rpc] HTTP hook failed: RealCall not found in any classloader");
                }
            });
        } catch (e2) {
            console.log("[rpc] HTTP hook failed: " + e2);
        }
    }());

    // Start TCP server.
    // Handle one client at a time in the server thread, but use a socket read timeout
    // so stale PC connections cannot block new RPC calls forever.
    startThread(function () {
        try {
            var ServerSocket = Java.use("java.net.ServerSocket");
            var server;
            try {
                server = ServerSocket.$new(PORT);
            } catch (bindErr) {
                if (String(bindErr).indexOf("EADDRINUSE") >= 0) {
                    console.log("[rpc] port " + PORT + " already bound by another instance");
                    return;
                }
                throw bindErr;
            }
            console.log("[rpc] TCP server listening on port " + PORT);

            while (true) {
                try {
                    var client = server.accept();
                    var peer = "unknown";
                    try { peer = client.getInetAddress().getHostAddress() + ":" + client.getPort(); } catch (_) {}
                    console.log("[rpc] client accepted: " + peer);
                    handleClient(client);
                } catch (e) {
                    console.log("[rpc] accept error: " + e);
                }
            }
        } catch (e) {
            console.log("[rpc] TCP server FATAL: " + e);
        }
    }, "ibox-rpc-server");

    console.log("[rpc] Bridge ready. Run on PC: adb forward tcp:" + PORT + " tcp:" + PORT);
});

# frida/ 目录说明

| 文件 | 用途 | 是否需要 Frida server |
|------|------|----------------------|
| `rpc_bridge.js` | **核心：注入 iBox（通过算法助手），建 TCP RPC server** | 否（LSPosed 注入） |
| `hook_aes.js` | 调试：捕获 HTTP 请求/响应格式（**无法直接 attach iBox**，见下） | 是（有检测风险） |
| `dump_chucker.sh` | 调试：从设备拉取 Chucker SQLite，确认 HTTP 格式 | 否（adb + root） |

---

## rpc_bridge.js — 主脚本

### 原理

iBox 有 Frida 检测，frida-server 运行时 App 会 abort。  
**算法助手**（LSPosed 模块）的 JS 引擎以系统级方式注入代码，iBox 无法检测。

`rpc_bridge.js` 在 iBox 进程内启动一个 **TCP ServerSocket（端口 27042）**，
PC 上的 Python 通过 TCP 连接后发送 JSON 命令，调用 iBox 内部的加密/解密函数。

### 加载步骤

1. 打开**算法助手** App
2. 目标 App → **iBox（com.box.art）** → 新建脚本
3. 粘贴 `rpc_bridge.js` 全部内容，保存并**启用**
4. 在算法助手中**重启 iBox**（或手动杀进程重开）

iBox 启动后脚本自动运行，logcat 可见：
```
[rpc] HTTP capture hook installed
[rpc] TCP server listening on port 27042
[rpc] Bridge ready. ...
```

### 连接方式

**WiFi 模式（推荐，无需 USB）**
```
手机和 PC 在同一 WiFi 下
Python 直连手机 IP：IBoxRPCClient(device_host="192.168.x.x")
```

**USB 模式（需要有线）**
```bash
adb forward tcp:27042 tcp:27042
# Python 连 localhost: IBoxRPCClient()  (默认 127.0.0.1)
```

### 通信协议

换行符分隔的 JSON（每行一个请求/响应）：

```
请求: {"id":1,"type":"encrypt","body":"{\"mobile\":\"...\"}"}
响应: {"id":1,"ok":true,"encBody":"..."}
```

支持的命令：

| type | 参数 | 返回 | 说明 |
|------|------|------|------|
| `ping` | — | `{ok:true,msg:"pong"}` | 连通性测试 |
| `encrypt` | `body: string` | `{encBody: string}` | 调用 `EncryptDataImpl.b()` 加密请求体 |
| `decrypt` | `cipherB64, key` | `{plaintext: string}` | AES/ECB 解密响应体 |
| `capture` | — | `{capture: {...}}` | 最近一次 HTTP 交换（调试用） |

---

## hook_aes.js — 调试脚本

> ⚠️ **iBox 有 Frida 检测**，不能直接用 frida-server attach。  
> 此脚本**只能通过算法助手注入**，不可用 `frida -U` 命令行方式。

通过算法助手注入到 iBox 后，会在 logcat 中打印完整的 HTTP 请求/响应（含加密 body 和所有 header），用于一次性确认 HTTP 格式：

```bash
adb logcat | grep "\[HTTP\]"
```

确认完 HTTP 格式后，更新 `src/api_client.py` 中的请求构造逻辑即可。

---

## dump_chucker.sh — 一次性调试

iBox 集成了 Chucker（HTTP 调试库），会把所有 HTTP 流量写入设备本地 SQLite。  
运行此脚本可直接拿到加密 body 和响应头，无需注入脚本：

```bash
bash frida/dump_chucker.sh
```

**前提**：adb 连接（USB 或 WiFi adb）+ root（`su` 可用）+ 本地有 `sqlite3`。

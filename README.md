# iBox 自动登录与购买

## 背景与约束

iBox 有 Frida 检测——frida-server 运行时 App 会自动 abort。  
因此采用 **算法助手（LSPosed 模块）** 作为注入载体，iBox 检测不到。

---

## 架构

```
PC (Python)
    │
    │  连接方式 A（推荐，无需 USB）：直连手机 IP
    │  TCP  192.168.x.x:27042
    │
    │  连接方式 B（需 USB）：adb forward tcp:27042 tcp:27042
    │  TCP  127.0.0.1:27042
    ▼
iBox 进程内 TCP ServerSocket:27042（监听所有网卡）
（rpc_bridge.js 通过算法助手 LSPosed 注入，iBox 检测不到）
    │  EncryptDataImpl.b()  — 加密请求体
    │  AES/ECB decrypt      — 解密响应
    ▼
Python 拿到加密 body → requests.post() → 拿到响应 → 解密
```

通信方式：**TCP socket（换行符分隔 JSON）**，速度快、无延迟。

---

## 环境准备

### PC 端

```bash
pip install -r requirements.txt
```

### 设备端（Android，需 root + LSPosed）

| 组件 | 说明 |
|------|------|
| Magisk | root 环境 |
| LSPosed | Xposed 框架实现 |
| **算法助手** | JS 注入引擎，本项目的注入载体 |

---

## 完整运行步骤

### 第一步：在算法助手中注入脚本

1. 打开**算法助手** App
2. 目标 App → **iBox（com.box.art）** → 新建脚本
3. 粘贴 `frida/rpc_bridge.js` 全部内容，保存并**启用**
4. 在算法助手中**重启 iBox**（或手动杀进程重开）

脚本加载后 logcat 可见：
```
[rpc] TCP server listening on port 27042
[rpc] Bridge ready. ...
```

### 第二步：配置连接方式

```bash
cp config/config.example.yaml config/config.yaml
```

编辑 `config/config.yaml`，选择连接方式：

**WiFi 模式（推荐，无需 USB 线）**  
手机和 PC 连同一 WiFi，在手机"设置 → WLAN → 当前网络"查看 IP：
```yaml
device_host: "192.168.1.88"   # 替换为手机实际 IP
```

**USB 模式（需要有线连接）**  
USB 连接后执行一次：
```bash
adb forward tcp:27042 tcp:27042
```
config.yaml 保持默认（或显式写）：
```yaml
device_host: "127.0.0.1"
```

### 第三步：运行

```bash
# 登录（默认走 RPC，可省略 --rpc）
python run.py login <手机号> <验证码> [cId] [邀请码]

# 示例
python run.py login 15300668769 236241 1a0018970b89c8c7072

# 登录 + 加购 + 下单
python run.py purchase 15300668769 236241 <cId> <商品ID>

# 若 cId 已写在 config.yaml，可显式传商品 ID，避免位置参数歧义
python run.py purchase 15300668769 236241 --product-id <商品ID>

# 临时指定手机 IP（不改 config）
python run.py --host 192.168.1.88 login 15300668769 236241 1a0018970b89c8c7072

# 仅在需要验证纯 Python fallback 时才使用
python run.py --python login 15300668769 236241 1a0018970b89c8c7072
```

---

## 各文件说明

| 文件 | 用途 |
|------|------|
| `frida/rpc_bridge.js` | **注入 iBox（通过算法助手）** — TCP server + 加密/解密 RPC + HTTP 捕获 |
| `frida/hook_aes.js` | 调试：通过算法助手注入，打印 HTTP 请求/响应格式到 logcat |
| `frida/dump_chucker.sh` | 调试：拉取 Chucker SQLite 确认 HTTP 格式（需 USB + root） |
| `src/frida_client.py` | Python RPC 客户端（TCP socket 连接 rpc_bridge.js） |
| `src/api_client.py` | 备用：纯 Python 加密实现（HTTP 格式确认后可启用） |
| `src/crypto_utils.py` | 纯 Python AES/RSA 实现（api_client.py 使用） |

---

## 待确认：响应解密 key 的位置

请求的 AES key 已知（每次随机生成，RSA 加密后附在请求里）。  
响应体的解密 key 来源尚未确认（header？body 某字段？）。

确认方法：触发一次登录后，在 Python 中调用：

```python
from src.frida_client import print_capture
print_capture()
# 查看输出的 respHeaders 和 respBody，找那个 16 位 ASCII hex 字符串
```

找到后更新 `src/frida_client.py` 里的 `_extract_resp_key()` 函数。

---

## 已逆向确认的信息

| 项目 | 值 |
|------|-----|
| 加密算法 | AES/ECB/PKCS5Padding |
| AES key 格式 | 每请求随机 16 位小写 hex ASCII 字符串 |
| key 传输方式 | RSA/ECB/PKCS1Padding，服务器固定公钥（已硬编码） |
| 加密入口 | `com.basetools.encrypt.EncryptDataImpl.b()` |
| 发短信接口 | `POST /personal-center-service/login/sendSms` |
| 登录接口 | `POST /personal-center-service/login/mobile` |
| 登录字段 | `mobile` `verificationCode` `invitationCode` `cId` `enable:1` |

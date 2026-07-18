# 腾讯云 CVM 部署 iBox（方案 A）

在腾讯云轻量/云服务器上运行 `qq_bot.py` + `run.py`；手机留在家中，通过 **Tailscale**（推荐）或 **frp** 连通。

## 架构

```text
腾讯云 CVM                          家里
┌────────────────────────┐         ┌─────────────────────┐
│ NapCat (QQ 机器人)      │         │ Android 手机         │
│ qq_bot.py              │         │ iBox + LSPosed :27042│
│ run.py                 │ ◄────── │ adb :5555            │
│ Tailscale / frps       │  VPN/frp│ Tailscale / frpc     │
└────────────────────────┘         └─────────────────────┘
```

## 一、购买与初始化 CVM

1. 腾讯云控制台创建 **Ubuntu 22.04/24.04** 实例（建议 2核2G+，带宽 3M+）
2. 安全组放行：**22**（SSH）；若用 frp 再放行 **7000、27042、15555**（Tailscale 方案可不放行业务端口）
3. SSH 登录后上传/克隆代码：

```bash
git clone <你的仓库> ibox && cd ibox
bash scripts/deploy_tencent_cvm.sh
```

## 二、手机侧（家里，一次性）

1. LSPosed RPC 模块正常，iBox 可监听 **27042**
2. USB 执行一次：`adb tcpip 5555`
3. 手机：**常亮、不休眠、固定 WiFi**

### 推荐：Tailscale

- CVM：`curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up`
- 手机：安装 Tailscale App 并登录同一账号
- 记下手机 Tailscale IP（如 `100.64.0.5`）

### 备选：frp

- CVM 运行 `frps`：见 [frps.toml](frps.toml)
- 家里常开设备运行 `frpc`：见 [frpc.home.toml](frpc.home.toml)（`localIP` 填手机局域网 IP）

## 三、CVM 配置

```bash
cp config/config.tencent.example.yaml config/config.yaml
cp config/qq_bot.tencent.example.yaml config/qq_bot.yaml
```

编辑 `config/config.yaml`：

```yaml
device_host: "100.64.0.5"   # 手机 Tailscale IP

adb:
  host: "100.64.0.5"
  port: 5555

login:
  c_id: "你的cId"
```

`cId` 获取：手机操作一次 iBox 后执行 `python run.py capture`（或从旧环境复制）。

编辑 `config/qq_bot.yaml`：`bot_qq`、OneBot 地址、默认手机号等。

## 四、NapCat（同一台 CVM）

1. 在 CVM 安装 [NapCat](https://napcat.napneko.cn/)（Docker 或二进制均可）
2. 配置 OneBot HTTP `3000`、WS `3001`，与 `qq_bot.yaml` 一致
3. 扫码登录机器人 QQ

## 五、验证与启动

```bash
source .venv/bin/activate
python run.py bridge-check
```

`"ready": true` 后：

```bash
# 前台
python qq_bot.py

# 或 systemd 常驻
INSTALL_SYSTEMD=1 bash scripts/deploy_tencent_cvm.sh
sudo systemctl start ibox-qqbot
sudo systemctl status ibox-qqbot
```

## 六、腾讯云注意点

| 项目 | 说明 |
|------|------|
| 安全组 | Tailscale 方案只需 22；勿将 27042/5555 对 0.0.0.0/0 长期开放 |
| 手机离线 | CVM 任务会失败；可用 cron 定时 `bridge-check` 告警 |
| 会话文件 | `config/session.json` 在 CVM 上，注意备份 |
| 日志 | `logs/run-*.log`、`logs/qq_bot.log` |

## 七、常用命令

```bash
source .venv/bin/activate
python run.py bridge-check
python run.py login 13800138000 123456
python qq_bot.py
tail -f logs/qq_bot.log
```

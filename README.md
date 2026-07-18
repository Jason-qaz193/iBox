# iBox 自动登录与二级市场操作

## 现状

这套脚本当前的主路径是 `RPC 模式`：

1. 通过 `算法助手 + frida/rpc_bridge.js` 注入 iBox 进程
2. Python 不自己实现线上加解密，而是调用 App 内部加密/解密逻辑
3. `run.py` 登录成功后会把会话保存到 `config/session.json`
4. `session.json` 按手机号区分账号，各账号单独保存自己的 `token/uid`
5. 后续命令可以复用该手机号的会话，避免每次都重新短信登录

也就是说，现在的使用方式是：

- 你执行一次 `python run.py ...`
- 第一次传短信验证码登录
- 脚本把该手机号对应的会话保存到 `config/session.json`
- 后续同手机号命令可以传 `-` 代替验证码，直接复用已保存会话
- 不同手机号会分别保存，不会混在同一个账号会话里
- 如果检测到 token 已失效，会自动删除旧 session；本次命令若带了验证码，还会自动重新登录并重试一次

## 架构

```text
PC (Python)
    |
    |  WiFi: 直连手机 IP:27042
    |  USB : adb forward tcp:27042 tcp:27042
    v
iBox 进程内 TCP ServerSocket:27042
    |
    |  EncryptDataImpl.b()   -> 加密请求
    |  DecryptInterceptor.a() -> 解密响应
    v
Python 发 HTTP -> 收到密文响应 -> 再交回 App 解密
```

通信方式是换行分隔的 JSON over TCP。

## 环境准备

### PC

```bash
pip install -r requirements.txt
```

至少需要：

- `PyYAML`
- `requests`

### Android

需要：

- root
- Magisk
- LSPosed
- 算法助手

## 第一步：在手机上注入 `rpc_bridge.js`

1. 打开算法助手
2. 目标 App 选择 `iBox（com.box.art）`
3. 新建脚本
4. 把 [frida/rpc_bridge.js](/Users/neatli/code/ibox/frida/rpc_bridge.js) 全部粘进去
5. 启用脚本
6. 重启 iBox

成功后理论上会监听 `27042` 端口。

## 第二步：配置 `config.yaml`

先复制模板：

```bash
cp config/config.example.yaml config/config.yaml
```

重点配置这几个字段：

```yaml
base_url: "https://sail-api.ibox.art"

device_host: "192.168.1.88"

login:
  path: "/personal-center-service/login/mobile"
  c_id: "你的cId"
```

说明：

- `device_host` 是手机 IP，WiFi 模式下填手机局域网 IP
- 如果走 USB，可以用默认 `127.0.0.1`，然后执行 `adb forward tcp:27042 tcp:27042`
- `login.c_id` 是登录时需要的 `cId`

### `cId` 怎么拿

最稳的是先让手机里正常操作一次 iBox，然后执行：

```bash
python run.py capture
```

看输出里的加密请求体，找到对应的 `cId`，再填回 `config/config.yaml`。

## 第三步：先验证登录链路

先发验证码：

```bash
python run.py sms 13800138000
```

再登录：

```bash
python run.py login 13800138000 123456
```

如果 `config.yaml` 里没填 `login.c_id`，就显式传：

```bash
python run.py login 13800138000 123456 your_cid
```

## 当前命令如何工作

除 `sms` 和 `capture` 之外，其他二级市场命令当前都是：

1. 如果你传了验证码，就先登录并刷新该手机号的本地 session
2. 如果你传 `-`，就读取该手机号在 `config/session.json` 里的 session
3. 从登录返回或已保存 session 里取 `token/uid`
4. 继续执行目标接口

如果本地 session 对应的 token 已过期：

- 本次命令传了验证码：脚本会自动重新登录，并重试一次目标接口
- 本次命令传的是 `-`：脚本会删除失效 session，并提示你重新带验证码执行一次

所以你现在可以二选一：

- 首次登录时：手机号、短信验证码、`cId`
- 复用会话时：手机号、`-`

`config/session.json` 会按手机号保存会话。

## 关于 `cId` / `--cid`

`cId` 主要是登录接口使用的设备/渠道标识，用来完成登录或在 session 失效时补登录。

它不是下面每一条业务接口本身都要单独依赖的参数。之所以很多命令都支持 `--cid`，是因为这些命令在执行目标接口前，可能会先自动登录，或者在发现本地 session 失效后自动重新登录一次。

推荐把它写到 `config/config.yaml`：

```yaml
login:
  c_id: "your_cid"
```

这样平时执行命令时通常不用每次都显式传 `--cid`。只有在下面这些情况才需要额外关心它：

- 你还没在 `config.yaml` 里配置 `login.c_id`
- 你想临时覆盖配置里的 `cId`
- 本地没有可用 session，或者 session 已过期，需要重新登录

如果该手机号已经有可用 session，很多命令在传 `-` 复用 session 时，实际上不会再次用到 `cId`；但为了避免 session 失效后补登录失败，最省事的做法仍然是把 `login.c_id` 固定写进配置。

## 已接入的命令

这些命令对应的接口路径和默认查询参数现在也可以放在 `config/config.yaml` 的 `commands` 节点里维护。

比如：

```yaml
commands:
  market-info:
    path: "/public-market-service/digital-collection-groups/{group_id}/purchase-consignment-info?configType={config_type}"
    defaults:
      config_type: "0"

  market-list:
    path: "/public-market-service/digital-collection-groups/{group_id}/consignment-orders?pageNo={page_no}&pageSize={page_size}&sortType={sort_type}&sortField={sort_field}&uid={uid}"
    defaults:
      page_no: "1"
      page_size: "20"
      sort_type: "1"
      sort_field: "1"
```

命令行参数优先级更高，所以你仍然可以临时用 `--page-no`、`--sort-type` 之类覆盖 YAML 里的默认值。

### 1. 查看寄售购买信息

```bash
python run.py market-info 13800138000 123456 19649
```

如果你没有在 `config.yaml` 里配置 `login.c_id`，再补上 `--cid your_cid`。

如果该手机号已经登录过并保存了 session，也可以直接复用：

```bash
python run.py market-info 13800138000 - 19649
```

对应接口：

```text
GET /public-market-service/digital-collection-groups/{group_id}/purchase-consignment-info?configType=0
```

### 2. 查看某个藏品组的挂单列表

```bash
python run.py market-list 13800138000 123456 19649
```

可选参数：

```bash
--page-no 1 --page-size 20 --sort-type 1 --sort-field 1
```

### 3. 查看某个藏品组的求购单列表

```bash
python run.py purchase-orders 13800138000 123456 20254
```

### 4. 批量买二级市场挂单

```bash
python run.py market-buy 13800138000 123456 --payload @payload.json
```

对应接口：

```text
POST /order-create-service/batch-purchase-consignment-orders?uid=...
```

### 4.1 点对点直购（路径一：B 寄售 → A 购买）

**流程：** B 先用 `consign-create` 挂单，A 再用 `market-purchase` 按**精确价格** + **寄售信息（orderId|藏品ID）** 购买。

```bash
# B 挂单（成功后 QQ 会返回 寄售ID|藏品ID）
python run.py consign-create 19965260715 - --支付密码 071599 --藏品名字 "2026大镖客" --出售价格 3 --出售数量 10

# A 购买（整段复制 B 返回的 orderId|digitalCollectionId）
python run.py market-purchase 13800138000 - --支付密码 123456 --collection-name "2026大镖客" --price 3 --quantity 1 --consign-order-id "80f6109727304eb297dd4cd359a4fd7d|414688282"
```

**批量从 B 扫货（更简单）：** 若 B 独占某价位（如只有 B 挂 3 元），A 可直接用「捡漏」/ `market-buy` 按价格+数量批量买，无需逐个寄售ID：

```bash
python run.py market-buy 13800138000 - --支付密码 123456 --collection-name "2026大镖客" --price 3 --quantity 10
```

QQ：`捡漏-A手机号-验证码-支付密码-2026大镖客-3-10`

可选参数：

- `--consign-order-id`：格式 `orderId|digitalCollectionId`（点对点直购）
- `--digital-collection-id`：也可与 `--consign-order-id` 分开传
- `--list-pages 10`：扫描市场挂单页数（默认 10）

对应接口：

```text
GET  /public-market-service/.../consignment-orders?...
POST /order-create-service/purchase-consignment-orders?uid=...
```

QQ 指令：

```text
寄售-B手机号-验证码-寄售密码-藏品名-价格-数量
直购-A手机号-验证码-支付密码-藏品名-价格-数量-寄售ID|藏品ID
```

### 5. 创建挂单

```bash
python run.py consign-create 13800138000 123456 --支付密码 123456 --藏品名字 "藏品名" --出售价格 99 --出售数量 1
```

脚本会按 `--藏品名字` 匹配「我的藏品」分组，自动选取未锁定的藏品并逐个提交寄售。

对应接口：

```text
POST /order-create-service/consignment-orders
```

### 6. 取消挂单

```bash
python run.py consign-cancel 13800138000 123456 --支付密码 123456 --藏品名字 "藏品名" --下架价格 99 --下架数量 1
```

脚本会按 `--藏品名字` 匹配分组，查找指定 `--下架价格` 的寄售单，并按 `--下架数量` 逐个取消。

对应接口：

```text
POST /order-service/consign-orders/{consign_order_id}/cancel
```

### 7. 查看自己买到的二级订单详情

```bash
python run.py purchase-detail 13800138000 123456 2344b2aeb2a34f24a99756c8c209335a
```

### 8. 自动把当前能合成的都合成掉

```bash
python run.py synthesis-auto 13800138000 123456
```

这个命令会：

- 循环拉取最新的 `synthesis-activity-list`
- 并发请求每个活动的 `synthesis-activity-detail`（`--scan-concurrency`）
- 从活动详情里找出所有当前可参与的 `synthetic_id`
- 并发请求 `synthesis-center/{synthetic_id}` 校验材料是否满足
- 材料够就提交合成；不传 `--target-count` 时默认按当前库存全部合成
- 每轮提交成功后立即重新扫描最新活动，直到材料用尽或达到 `--target-count`
- `submit` 成功后还会调用 `confirm` 才会真正消耗材料；`needSlider=0` 的活动无需滑块，直接 confirm

如果你想先只看脚本识别出来的方案，不真正提交：

```bash
python run.py synthesis-auto 13800138000 - --dry-run
```

如果你希望提交失败后每隔 1 分钟自动重试：

```bash
python run.py synthesis-auto 13800138000 - --submit-window 60 --submit-concurrency 3 --retry-interval 0.3
```

如果你只希望本次最多合成指定个数：

```bash
python run.py synthesis-auto 13800138000 - --target-count 3
```

常用参数：

- `--scan-concurrency 4`：并发拉取活动详情和 synthesis-center（默认 4）
- `--loop-interval 2`：活动尚未开始或暂时无法提交时，隔多少秒重新扫描（默认 2）
- `--max-rounds 0`：扫描轮数上限，0 表示不限（默认 0，直到合成结束或达到 `--target-count`）
- `--target-count` / `--expected-count`：限制本次最多提交的 `syntheticNum` 总数，不传则尽量全部合成

注意：

- 当前默认就是按活动列表接口返回的“当前活动”去扫，不额外暴露翻页参数
- 这个实现依赖 `synthesis-activity-detail` 里能稳定找到 `synthetic_id`
- 也依赖 `synthesis-center` 返回里确实带有材料清单和当前持有数量
- 不同活动的字段名如果差异很大，仍然可能需要再补字段别名
- 单次提交失败后会在 `--submit-window` 指定的时间窗口内持续重试
- `--retry-interval` 是这个窗口内两次尝试之间的短间隔，默认 0.3 秒
- `--submit-concurrency` 可以让同一个合成项在窗口内用小并发持续抢，任一成功就会收敛停止

### 9. 查看公开求购详情

```bash
python run.py wanted-detail 13800138000 123456 112333982
```

### 10. 成交一个求购单

**方式一 — 按藏品名称自动查找（推荐）：**

```bash
python run.py wanted-deal 13800138000 - \
  --collection-name "星空机器人" \
  --quantity 1 \
  --min-price 99.0 \
  --consignment-password 123456
```

自动流程：
1. 查用户藏品列表，按名称（子串匹配）找到对应 `group_id`
2. 查该 group 的求购单列表，过滤掉出价低于 `--min-price` 或数量不足 `--quantity` 的
3. 取出价最高的求购单，调 `wanted-detail` 获取 `relation_id`
4. 发起成交请求，body 自动附带寄售密码

可选：`--dry-run` 只打印匹配到的求购单，不真正成交。

可选：`--po-page-size 50` 控制求购单翻页大小（默认 50）。

**方式二 — 直接传 ID：**

```bash
python run.py wanted-deal 13800138000 - 112333982 72906965 \
  --consignment-password 123456
```

也可以用 `--payload @payload.json` 传自定义额外字段（会和 `--consignment-password` 合并）。

对应接口：

```text
POST /order-create-service/advance-orders/{purchase_order_id}/relation/{relation_id}/deal?uid=...
```

### 11. 调任意已登录接口

```bash
python run.py api 13800138000 123456 GET /public-service/app/url-configs
python run.py api 13800138000 123456 POST /order-service/xxx --payload '{"a":1}'
```

这个命令适合继续试接口，不用每次改代码。

## `--payload` 怎么传

写操作命令目前基本都需要 `--payload`，因为抓包文件里保存的是加密后的请求体，没法从 `.har` 里直接还原出完整明文字段结构。

支持两种方式。

创建挂单示例：

```bash
python run.py consign-create 13800138000 123456 --支付密码 123456 --藏品名字 "藏品名" --出售价格 99 --出售数量 1
```

如需额外字段，仍可用 `--payload @payload.json` 合并进请求体。

例如：

```json
{
  "foo": "bar"
}
```

## 为什么现在还要你自己准备 payload

因为当前抓包里能稳定确认的是：

- 真实接口路径
- 请求方法
- 哪些接口需要 `uid`

但写操作的请求 body 在 `.har` 里看到的是：

```json
{
  "encryptKey": "...",
  "data": "..."
}
```

这是加密后的内容，不是业务明文。所以现在脚本只能先把“加密发送能力”和“真实路径”接起来，业务字段还需要你后续从 App 明文日志、hook、或更细的捕获里补全。

## 推荐的实际使用顺序

建议按这个顺序操作：

1. `python run.py capture`
2. 确认 `cId`
3. `python run.py sms <手机号>`
4. `python run.py login <手机号> <验证码>`
5. `python run.py market-list ...`
6. `python run.py purchase-orders ...`
7. 准备某个写操作的 `payload.json`
8. `python run.py consign-create ... --支付密码 ... --藏品名字 "..." --出售价格 ... --出售数量 ...`
9. `python run.py consign-cancel ... --支付密码 ... --藏品名字 "..." --下架价格 ... --下架数量 ...`
10. `python run.py wanted-deal ... --collection-name "藏品名" --min-price 99 --consignment-password 123456`

## 目前和“token 持久化复用”相比的差异

当前实现：

- 优点：逻辑简单，和 App 当前会话保持一致，不需要自己维护 token 过期
- 缺点：每个命令都要重新输入验证码并重新登录

如果你想改成“登录一次后保存 token”，通常会变成：

1. `login` 命令把登录返回保存到 `session.json`
2. 后续命令优先读取 `session.json`
3. 若 token 失效，再重新登录刷新

这个改造不复杂，但要先确认：

- 登录返回里 token 的稳定字段名
- 是否还依赖其他登录态字段
- token 过期后的错误码

## QQ 机器人（OneBot）

可通过 QQ 私聊/群聊发送指令，间接调用 `run.py`。

### 1. 准备 OneBot

推荐使用 [NapCat](https://napcat.napneko.icu/) 或 Lagrange.OneBot，开启 OneBot v11 的 HTTP + 正向 WebSocket。

默认地址一般是：

- HTTP: `http://127.0.0.1:3000`
- WS: `ws://127.0.0.1:3001`

**NapCat WebUI → OneBot11 → 网络配置（与 `qq_bot.py` 配套）：**

| 类型 | 建议 |
|------|------|
| HTTP 服务器 | ✅ 开启，端口 `3000` |
| 正向 WebSocket 服务器 | ✅ 开启，端口 `3001` |
| 反向 WebSocket 客户端 | ❌ **关闭**（除非你自己写了 OneBot 反向 WS 服务端） |

`qq_bot.py` 的工作方式是：**HTTP 发消息 + 正向 WS 收事件**。不要同时开启「反向 WebSocket」，也不要把反向 WS 地址填成 `ws://127.0.0.1:3001`（会和正向 WS 冲突，触发 `不支持的Api undefined`）。

若登录 NapCat 后出现：

```text
[OneBot] [WebSocket Client] 发生错误 不支持的Api undefined
```

说明 NapCat 作为 WS 客户端收到了没有 `action` 字段的 JSON，常见原因：

1. **反向 WebSocket 已开启但 URL 为空或错误**
2. 反向 WS 地址误填为 `ws://127.0.0.1:3001`（指回自己）
3. 有其他程序向 NapCat 的 WS 端口发送了非 OneBot 格式数据

处理：在 NapCat WebUI 关闭所有「反向 WebSocket」连接，只保留 HTTP + 正向 WS，然后重启 NapCat 再运行 `python qq_bot.py`。

### 2. 配置机器人

```bash
cp config/qq_bot.example.yaml config/qq_bot.yaml
pip install websockets
```

编辑 `config/qq_bot.yaml`：

- `bot_qq`：NapCat 登录的机器人 QQ 号（用户发消息给这个号）
- `allow_all_senders: true`：任何用户私聊 `bot_qq` 都可执行指令
- `allowed_users`：仅当 `allow_all_senders: false` 时，限制可发指令的 QQ 号
- `access_token`：与 OneBot 配置一致（如有）
- `default_mobile` / `default_pay_password`：可省略命令里的手机号/密码
- `run_args`：传给 `run.py` 的额外参数，例如 `["--usb"]`

### 3. 启动

```bash
python qq_bot.py
```

### 4. 支持的 QQ 指令

```text
帮助
登录 13800138000 123456
寄售 13800138000 - 123456 藏品名 99 1
下架 13800138000 - 123456 藏品名 99 1
求购 13800138000 - 123456 藏品名 88 1
捡漏 13800138000 - 123456 藏品名 5000 1
合成 13800138000 - 3
合成 -
```

若已在 `qq_bot.yaml` 配置默认手机号和支付密码，可简写：

```text
寄售 - 藏品名 99 1
下架 - 藏品名 99 1
合成
合成 5
```

说明：

- `-` 表示复用 `config/session.json` 里该手机号的 session
- `合成` 省略数量时会尽量全部合成；指定数量则等价于 `--target-count`
- 寄售/下架/求购/捡漏/合成 执行时会先回复「任务已开始」，完成后回复「任务已完成」
- 藏品名含空格时用引号：`寄售 - 123456 "2026喜糖熊猫" 199 1`
- 支付密码会出现在 QQ 聊天记录中；若 `allow_all_senders: true`，任何私聊机器人的人都能操作，请谨慎使用

## 云端混合部署（方案 A）

目标：**云端 VPS 跑 `qq_bot.py` + `run.py`，手机留在家中**，通过 VPN/内网穿透让云端访问手机的 RPC（27042）和无线 adb（5555）。

```text
云端 VPS                         家里（Tailscale / frp）
┌─────────────────┐               ┌────────────────────────┐
│ python qq_bot.py│  ──VPN/frp──► │ 手机 iBox + LSPosed     │
│ config.yaml     │               │ :27042 RPC  :5555 adb   │
└─────────────────┘               └────────────────────────┘
```

### 1. 手机侧（一次性）

1. 安装 **LSPosed RPC 模块**（或 `rpc_bridge.js`），重启 iBox，确认本机可连 `27042`
2. 开启 **无线 adb**（USB 连电脑时执行一次）：

```bash
adb tcpip 5555
```

3. 手机设置：**常亮、勿休眠、固定 WiFi**
4. 安装 **Tailscale**（推荐）或配置 **frp** 暴露 `27042` 与 `5555`

Tailscale 示例：手机和 VPS 都登录同一账号，记下手机 Tailscale IP（如 `100.64.0.5`）。

### 2. 云端 VPS

```bash
git clone <repo> && cd ibox
pip install -r requirements.txt
apt install adb   # 或 Android platform-tools

cp config/config.example.yaml config/config.yaml
cp config/qq_bot.example.yaml config/qq_bot.yaml
```

编辑 `config/config.yaml`：

```yaml
device_host: "100.64.0.5"   # 手机 Tailscale IP，或 frp 映射的 RPC 地址

adb:
  host: "100.64.0.5"
  port: 5555
```

编辑 `config/qq_bot.yaml`（**不要**用 `--usb`）：

```yaml
onebot_http_url: "http://127.0.0.1:3000"   # NapCat 与 bot 同机或可达
onebot_ws_url: "ws://127.0.0.1:3001"
run_args: []   # device_host / adb 已在 config.yaml
```

NapCat 可装在云端同一台 VPS，QQ 指令 → OneBot → `qq_bot.py` → `run.py`。

### 3. 验证连通性

在云端执行：

```bash
python run.py bridge-check
# 或
python scripts/cloud_bridge_check.py
```

期望输出 `"ready": true`，且 `rpc.ok` 与 `adb.ok` 均为 true。

### 4. 启动

```bash
# 前台
python qq_bot.py

# 后台（示例）
nohup python qq_bot.py >> logs/qq_bot.log 2>&1 &
```

### frp 参考（不用 Tailscale 时）

在 VPS 的 `frpc.toml` 中映射手机端口（手机跑 frpc，或网关转发）：

```toml
[[proxies]]
name = "ibox-rpc"
type = "tcp"
localIP = "127.0.0.1"
localPort = 27042
remotePort = 27042

[[proxies]]
name = "ibox-adb"
type = "tcp"
localIP = "127.0.0.1"
localPort = 5555
remotePort = 15555
```

则 `config.yaml` 填 VPS 公网 IP，`device_host` 用 RPC 端口，`adb.port` 用 `15555`。

### 注意

- **首发抢购**同时需要 RPC + adb；寄售/合成等仅需 RPC
- 不要把 27042/5555 裸奔公网，优先 Tailscale 或带认证的隧道
- 手机断电、App 被杀、RPC 断开时，云端任务会失败；可用 `bridge-check` 定期巡检

## 关键文件

| 文件 | 作用 |
|------|------|
| [run.py](/Users/neatli/code/ibox/run.py) | 命令行入口 |
| [src/frida_client.py](/Users/neatli/code/ibox/src/frida_client.py) | RPC 客户端，负责登录后携带 token 调接口 |
| [frida/rpc_bridge.js](/Users/neatli/code/ibox/frida/rpc_bridge.js) | 注入 App 内部执行加解密 |
| [config/config.example.yaml](/Users/neatli/code/ibox/config/config.example.yaml) | iBox 配置模板（含云端 device_host / adb） |
| [deploy/tencent-cvm/README.md](/Users/neatli/code/ibox/deploy/tencent-cvm/README.md) | 腾讯云 CVM 部署指南 |
| [scripts/deploy_tencent_cvm.sh](/Users/neatli/code/ibox/scripts/deploy_tencent_cvm.sh) | CVM 一键安装脚本 |
| [qq_bot.py](/Users/neatli/code/ibox/qq_bot.py) | QQ OneBot 指令桥接 |
| [config/qq_bot.example.yaml](/Users/neatli/code/ibox/config/qq_bot.example.yaml) | QQ 机器人配置模板 |

## 当前限制

- `--python` 纯 Python 模式仍然只是实验性质
- README 里列出的二级市场接口路径是已从抓包确认的，但部分写操作请求体字段还没完全还原
- `wanted-deal --collection-name` 模式下 `relation_id` 从 `wanted-detail` 响应自动提取，若 API 返回结构变化可能需要补充字段别名

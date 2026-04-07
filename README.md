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

### 5. 创建挂单

```bash
python run.py consign-create 13800138000 123456 --payload @payload.json
```

对应接口：

```text
POST /order-create-service/consignment-orders
```

### 6. 取消挂单

```bash
python run.py consign-cancel 13800138000 123456 112333806
```

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

- 先请求 `synthesis-activity-list`
- 再请求每个活动的 `synthesis-activity-detail`
- 从活动详情里找出所有 `synthetic_id`
- 逐个请求 `synthesis-center/{synthetic_id}`
- 计算当前库存下每个配方最多能合成多少次
- 把当前能合成的都提交掉
- 每轮提交后再重新扫一遍，直到没有任何一项还能继续合成

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

注意：

- 当前默认就是按活动列表接口返回的“当前活动”去扫，不额外暴露翻页参数
- 这个实现依赖 `synthesis-activity-detail` 里能稳定找到 `synthetic_id`
- 也依赖 `synthesis-center` 返回里确实带有材料清单和当前持有数量
- 不同活动的字段名如果差异很大，仍然可能需要再补字段别名
- 单次提交失败后会在 `--submit-window` 指定的时间窗口内持续重试
- `--retry-interval` 是这个窗口内两次尝试之间的短间隔，默认 0.3 秒
- `--submit-concurrency` 可以让同一个合成项在窗口内用小并发持续抢，任一成功就会收敛停止
- `--target-count` / `--expected-count` 可以限制本次最多提交的 `syntheticNum` 总数，不传则保持原来的“能合成多少就提交多少”
- 当前命令只负责 `submit`，如果该活动后面还需要验证码确认，仍然继续用 `synthesis-confirm`

### 9. 查看公开求购详情

```bash
python run.py wanted-detail 13800138000 123456 112333982
```

### 10. 成交一个求购单

```bash
python run.py wanted-deal 13800138000 123456 112333982 72906965 --payload @payload.json
```

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

直接传 JSON 字符串：

```bash
python run.py consign-create 13800138000 123456 --cid your_cid --payload '{"foo":"bar"}'
```

或者传 JSON 文件：

```bash
python run.py consign-create 13800138000 123456 --cid your_cid --payload @payload.json
```

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
8. `python run.py consign-create ... --payload @payload.json`
9. `python run.py consign-cancel ...`
10. `python run.py wanted-deal ... --payload @payload.json`

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

## 关键文件

| 文件 | 作用 |
|------|------|
| [run.py](/Users/neatli/code/ibox/run.py) | 命令行入口 |
| [src/frida_client.py](/Users/neatli/code/ibox/src/frida_client.py) | RPC 客户端，负责登录后携带 token 调接口 |
| [frida/rpc_bridge.js](/Users/neatli/code/ibox/frida/rpc_bridge.js) | 注入 App 内部执行加解密 |
| [config/config.example.yaml](/Users/neatli/code/ibox/config/config.example.yaml) | 配置模板 |

## 当前限制

- `--python` 纯 Python 模式仍然只是实验性质
- README 里列出的二级市场接口路径是已从抓包确认的，但部分写操作请求体字段还没完全还原
- 当前没有 token 持久化
- 当前没有自动刷新验证码/自动重试机制

如果你下一步要做“登录一次保存 token 到本地 json 后复用”，我可以直接继续把这块补到 `run.py` 和 `src/frida_client.py`。 

# Polymarket 世界杯冷门大额下注实时监测与提醒系统

> An explainable, read-only alerting system for large underdog trades in Polymarket World Cup prediction markets.
>
> 仅用于行情监测与提醒，**不含任何下单/交易功能**，不登录、不签名、不需要私钥。
> 数据全部来自 Polymarket 官方公开只读接口。
>
> English documentation: [README_EN.md](README_EN.md),
> [docs/ALGORITHM_EN.md](docs/ALGORITHM_EN.md).

---

## 一、总体架构

```
 ┌─────────────────────────── Ubuntu 服务器 ───────────────────────────┐
 │                                                                     │
 │  Polymarket 公开接口（只读）          本系统                         │
 │  ┌───────────────┐                 ┌──────────────────────────┐    │
 │  │ Gamma REST    │──市场发现──────▶│ MonitoringEngine         │    │
 │  │ CLOB REST     │──盘口/价格─────▶│  ├─ discovery_loop       │    │
 │  │ 官方实时 WS    │──成交/异动─────▶│  ├─ ws_loop              │    │
 │  └───────────────┘                 │  ├─ window_sweep_loop    │    │
 │                                    │  └─ fallback_poll_loop   │    │
 │                                    │         │                │    │
 │                                    │         ▼                │    │
 │                                    │  Detector(underdog_score)│    │
 │                                    │         │ alert          │    │
 │                                    │         ▼                │    │
 │                                    │  Notifier ──▶ SQLite 持久化   │
 │                                    │     │                     │    │
 │                                    │     │ WS(8765, token鉴权)  │    │
 │                                    │     │   + Telegram/邮件   │    │
 │                                    └─────┼─────────────────────┘    │
 └──────────────────────────────────────────┼──────────────────────────┘
                                            │ WS push (JSON)
                                            ▼
 ┌────────────────────── Windows 客户端 (tkinter) ────────────────────┐
 │  WSClient(token) ──▶ 收到 alert ──▶ 桌面通知 + 声音 + 历史列表      │
 │                      断线自动重连 / 状态栏 / 双击查看详情            │
 └────────────────────────────────────────────────────────────────────┘
```

**数据流**：Polymarket 公开接口 → 服务器发现市场 + 实时订阅 → Detector 算冷门分 →
满足条件生成 Alert → 写 SQLite + 推 WS → Windows 客户端弹窗提醒。

---

## 二、冷门大额下注识别逻辑

### 2.1 成交额估算（口径）
Polymarket 为二元结果市场，每份份额结算为 1 USDC：

```
estimated_usd(单笔) = size(份额) × price(单价 ∈ [0,1])
```
当 WS 推送缺少 `size`（仅推 `last_trade_price`）时，回退到 CLOB `/trades` REST
按时间戳补全；若仍无法补全，在 `reason` 中标注 `size_missing，估算不确定`。

### 2.2 触发条件（全部满足）
| 条件 | 默认值 | 配置项 |
|------|--------|--------|
| 估算成交额 ≥ 大额阈值 | $5,000 | `detector.large_trade_usd` |
| 冷门评分 ≥ 阈值 | 70 | `detector.underdog_score_threshold` |
| outcome 隐含概率 ≤ 上限 | 0.35 | `detector.underdog_max_implied_prob` |
| 盘口深度 ≥ 最低值 | $500 | `detector.min_book_depth_usd` |

> 短窗口累计模式：5 分钟内同 outcome 累计成交 ≥ `short_window_large_usd` 也视为一次
> 大额事件，防止被拆单绕过。

### 2.3 underdog_score（0–100，加权求和）

```
S = 0.30·A + 0.25·B + 0.20·C + 0.15·D + 0.10·E
```

| 子项 | 含义 | 计算 |
|------|------|------|
| **A** 隐含概率冷度 | 价格越低越冷 | `p≤0.05→100`；`p≥0.35→0`；中间线性 |
| **B** 相对弱势 | 相对同市场最热 outcome | `(1 - p_under/p_fav)×100` |
| **C** 短时异动 | 短窗涨幅 | `(涨幅 / 0.05)×100`，clip[0,100] |
| **D** 成交额异常 | 相对近1h中位数倍数 | `(单笔/中位数 / 5)×100` |
| **E** 流动性匹配 | 盘口深度越足越可信 | `(深度 / (10·单笔))×100` |

### 2.4 alert_level
```
estimated_usd ≥ severe_trade_usd(10万)       → SEVERE
estimated_usd ≥ important_trade_usd(2万)     → IMPORTANT
否则                                          → INFO
```

---

## 三、项目目录结构

```
polymarket_wc_alert/
├── server/                         # Ubuntu 服务器端
│   ├── main.py                     # 编排入口
│   ├── config.py                   # 配置加载 + pydantic 校验
│   ├── config.yaml.example         # 配置示例
│   ├── models.py                   # 数据模型 (Market/Outcome/Alert)
│   ├── polymarket_client.py        # 公开接口封装 (REST+WS)
│   ├── detector.py                 # 冷门评分 + 提醒判定
│   ├── notifier.py                 # WS推送服务 + 二级通知
│   ├── db.py                       # SQLite 持久化
│   └── requirements.txt
├── client/                         # Windows 客户端
│   ├── windows_client.py           # tkinter 主程序
│   ├── config.json.example
│   └── requirements.txt
├── tests/
│   ├── test_detector.py            # 模拟成交单元测试
│   ├── test_pipeline.py            # 端到端推送测试
│   └── test_connectivity.py        # 真实接口连通性测试
└── deploy/
    ├── polymarket-alert.service    # systemd
    └── build_exe.bat               # Windows PyInstaller 打包
```

---

## 四、Ubuntu 服务器端部署

### 4.1 环境
- **Python 3.11+**（推荐 3.12）
- Ubuntu 22.04 / 24.04 LTS

### 4.2 安装
```bash
# 1. 上传项目到 /opt/polymarket-wc-alert
sudo mkdir -p /opt/polymarket-wc-alert
sudo chown $USER:$USER /opt/polymarket-wc-alert
# （把项目文件拷进去）

cd /opt/polymarket-wc-alert/server

# 2. 建虚拟环境
python3.12 -m venv ../venv
source ../venv/bin/activate
pip install -U pip
pip install -r requirements.txt

# 3. 生成配置
cp config.yaml.example config.yaml
# 编辑 config.yaml，至少改 server.auth_token、detector 阈值
nano config.yaml

# 4. 建数据/日志目录
mkdir -p data logs
```

### 4.3 启动（前台测试）
```bash
source ../venv/bin/activate
python main.py --config ./config.yaml
```

### 4.4 systemd 后台运行
```bash
# 复制 service 文件
sudo cp /opt/polymarket-wc-alert/deploy/polymarket-alert.service /etc/systemd/system/
# 按你的实际路径/用户检查 User / WorkingDirectory / ExecStart
sudo nano /etc/systemd/system/polymarket-alert.service

sudo systemctl daemon-reload
sudo systemctl enable --now polymarket-alert
```

### 4.5 日志与状态
```bash
systemctl status polymarket-alert
# 实时日志
journalctl -u polymarket-alert -f
# 文件日志
tail -f /opt/polymarket-wc-alert/server/logs/server.log
```

### 4.6 修改配置
改 `config.yaml` 后重启服务：`sudo systemctl restart polymarket-alert`。
敏感字段（token、SMTP 密码）建议用环境变量覆盖，service 文件里加
`Environment=POLYALERT_SERVER__AUTH_TOKEN=xxx`。

---

## 五、Windows 客户端

### 5.1 直接运行
```bat
cd client
copy config.json.example config.json
notepad config.json   :: 填服务器地址和 token
pip install -r requirements.txt
python windows_client.py
```

### 5.2 打包成 exe
```bat
cd C:\path\to\polymarket_wc_alert
deploy\build_exe.bat
:: 产物：dist\PolymarketAlert.exe
:: 把 config.json 放到 exe 同目录运行
```

---

## 六、连接安全与 TLS 部署（强烈建议公网部署阅读）

本系统客户端↔服务端默认是**明文 ws://**。若服务器对公网开放，token 与提醒内容在传输中可被嗅探。请按下述启用 TLS（`wss://`）。

### 6.TLS.1 三种部署场景对照

| 场景 | 是否需要 TLS | 推荐做法 |
|------|------------|---------|
| 仅本机自测（`127.0.0.1`） | 否 | `host: 127.0.0.1`，客户端填 `ws://127.0.0.1:8765` |
| 内网/SSH 隧道访问 | 可选 | 用 SSH 端口转发，token 不出公网 |
| **公网开放（推荐做法）** | **是** | 申请域名 + Let's Encrypt 证书，启用 `wss://` |

### 6.TLS.2 申请免费 TLS 证书（Let's Encrypt / Certbot）

**前提**：你有一个域名（如 `alert.example.com`）已解析到服务器公网 IP。

```bash
# 1. 安装 certbot
sudo apt update
sudo apt install -y certbot

# 2. 申请证书（standalone 模式，需保证 80 端口空闲且防火墙放行）
sudo certbot certonly --standalone -d alert.example.com

# 成功后证书位于：
#   /etc/letsencrypt/live/alert.example.com/fullchain.pem   ← ssl_cert
#   /etc/letsencrypt/live/alert.example.com/privkey.pem     ← ssl_key
```

> 如果 80 端口被占用（如已装 Nginx），改用 webroot 或 Nginx 插件：
> `sudo certbot certonly --webroot -w /var/www/html -d alert.example.com`

**让运行程序的用户能读私钥**（默认 root:root，Python 进程读不到会启动失败）：
```bash
# 假设 systemd 用 ubuntu 用户运行
sudo chown -R root:ubuntu /etc/letsencrypt/live/ /etc/letsencrypt/archive/
sudo chmod -R 750 /etc/letsencrypt/live/ /etc/letsencrypt/archive/
```

### 6.TLS.3 在 config.yaml 启用 TLS

```yaml
server:
  host: "0.0.0.0"
  port: 8765
  auth_token: "你生成的强随机token"     # openssl rand -hex 24
  ssl_cert: "/etc/letsencrypt/live/alert.example.com/fullchain.pem"
  ssl_key:  "/etc/letsencrypt/live/alert.example.com/privkey.pem"
```

改完重启服务：
```bash
sudo systemctl restart polymarket-alert
journalctl -u polymarket-alert -f
# 应看到：client WS server on wss://0.0.0.0:8765 (TLS enabled)
```

放行防火墙：
```bash
sudo ufw allow 8765/tcp
```

### 6.TLS.4 Windows 客户端连接 wss

编辑 `client/config.json`：
```json
{
  "server_url": "wss://alert.example.com:8765",
  "token": "与服务端相同的强随机token",
  "sound_enabled": true,
  "popup_enabled": true,
  "insecure_skip_verify": false
}
```
> `insecure_skip_verify` 仅在你用**自签证书**测试时才设 `true`；Let's Encrypt 等受信证书务必保持 `false`。

### 6.TLS.5 证书自动续期

Let's Encrypt 证书 90 天过期。加一条 cron 自动续期 + 重启服务：
```bash
sudo crontab -e
# 每月 1 号凌晨 3 点检查并续期，成功后重启服务
0 3 1 * * certbot renew --quiet --deploy-hook "systemctl restart polymarket-alert"
```
也可用 systemd timer，效果相同。

### 6.TLS.6 没有域名怎么办（自签证书，仅自测）

```bash
# 生成自签证书（IP 直连用，浏览器/客户端会提示不受信）
openssl req -x509 -newkey rsa:4096 -nodes -keyout selfkey.pem \
    -out selfcert.pem -days 365 -subj "/CN=localhost" \
    -addext "subjectAltName=IP:你的服务器IP"
```
config.yaml 照填路径，客户端 `config.json` 设 `"insecure_skip_verify": true`。
> 自签证书不提供真正的身份验证，仅适合内网自测，不要用于公网生产。

---

### 6.SEC 安全机制总览（已实现）

| 威胁 | 防护措施 | 代码位置 |
|------|---------|---------|
| Token 明文泄露（URL/日志） | token 走 `X-Auth-Token` 请求头，不入 URL | `notifier._extract_token` / `windows_client._main` |
| Token 时序攻击 | `hmac.compare_digest` 常量时间比较 | `notifier.client_handler` |
| 弱/默认 token 对外暴露 | 启动时 pydantic 校验，公网绑定拒绝弱 token | `config.Settings._check_token_strength` |
| 单 IP 大量连接 DoS | 每 IP 最多 8 个并发连接 | `notifier.ClientHub.add` |
| 传输内容被嗅探 | 支持 TLS（`wss://`） | `main._build_ssl_context` |
| 恶意客户端拉超大历史 | `recent` 请求 limit 强制 clip 到 [1,200] | `notifier.client_handler` |
| 未授权连接信息泄露 | 鉴权失败只回 `unauthorized`，不区分 token 错/缺失 | `notifier.client_handler` |

> 兼容性：服务端仍保留 `?token=` URL 方式作为兜底，旧客户端无需改动即可继续工作；但建议尽快切到请求头方式。

---

## 七、测试方案

```bash
cd server && source ../venv/bin/activate

# 1) 单元测试：模拟成交验证冷门评分（无需网络）
python ../tests/test_detector.py

# 2) 端到端：启动内存服务器 + 客户端验证推送链路
python ../tests/test_pipeline.py

# 3) 真实连通性：只读访问 Polymarket 公开接口
python ../tests/test_connectivity.py
```

> 客户端连通性：服务器跑起来后，Windows 客户端填入地址+token，状态栏应显示
> `connected`；运行 `test_pipeline.py` 时打开客户端可同步看到测试提醒。

---

## 八、风险与限制

1. **数据延迟**：官方 WS 推送存在网络与服务端处理延迟，提醒非“零延迟”。
2. **接口变更**：Polymarket 官方接口字段/地址可能调整；本系统已把数据源
   收拢在 `polymarket_client.py`，字段变化只需改这一处。
3. **成交额估算误差**：当 WS 缺 `size` 时回退 REST 补全，存在时间戳对齐误差；
   提醒中会标注 `size_missing`。
4. **低流动性误报**：已用 `min_book_depth_usd` 过滤薄盘口，但极端行情下仍可能误报。
5. **合规声明**：本系统仅做行情监测与提醒，**不构成任何投注或投资建议**，
   不含任何自动交易功能，不要求用户提供 Polymarket 私钥。

---

## 九、数据源参考（公开官方）

- Polymarket API 文档：https://docs-polymarket-us.mintlify.app/
- Gamma API（市场发现）：`https://gamma-api.polymarket.com`
- CLOB API（行情/成交）：`https://clob.polymarket.com`
- 官方实时 WS：`wss://ws-subscriptions-clob.polymarket.com/ws/market`

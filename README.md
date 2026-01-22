# OP/PM 套利通知 TG 机器人（Opinion × Polymarket）

用于监控 Opinion 与 Polymarket 同一事件的盘口价差，发现套利空间后通过 Telegram 发送提醒。

---

## 套利监控 4 个脚本

| 文件 | 作用 | 你通常怎么用 |
|------|------|--------------|
| `token_registry.py` | 维护你要监控的市场 URL 列表（`URL_PAIRS_FOR_DEBUG`） | **编辑配置**（一般不直接跑） |
| `token_registry_core.py` | 生成 token 映射的核心逻辑（供 registry 调用） | 一般不直接跑 |
| `run_token_registry.py` | 生成/刷新 `market_token_pairs.json` | **经常跑**：新增市场/更新映射时 |
| `run_arb_monitor.py` | 读取 `market_token_pairs.json` 并开始监控 + TG 提醒 | **常驻跑**：服务器后台运行 |

---

## 其它脚本/工具（README 之前未覆盖）

| 文件 | 作用 | 你通常怎么用 |
|------|------|--------------|
| `run_profit_monitor_env.py` | 结合持仓与盘口，满足阈值时发送获利提醒 | **可选跑**：对持仓做套利提醒 |
| `run_position_reconcile.py` | Opinion vs Polymarket 仓位对账，输出差异 JSON | **按需跑**：核对仓位 |
| `opinion_frontend_fetch.py` | 拉取 Opinion 前端话题列表/深度数据 | **做市/选市场前跑** |
| `opinion_depth_tui.py` | 读取深度缓存并做 TUI 可视化 | **排查流动性时跑** |
| `scripts/run_market_selector.py` | 根据话题列表生成 `selected_markets.json` | **做市前/轮换时跑** |
| `scripts/run_mm.py` | 做市主循环（挂单/撤单） | **常驻跑** |
| `scripts/account_monitor_tui.py` | 账户/订单/持仓/余额 TUI 监控 | **排障/观察时跑** |
| `scripts/cancel_orders_from_state.py` | 从 `mm_state.json`/接口拉单并一键撤单 | **紧急处理时跑** |

---

## 环境要求

- Python 3.9+（推荐 3.10+）
- 服务器可访问外网（Opinion / Polymarket / Telegram）

---

## 快速开始（服务器）

### 1) 拉取代码 & 安装依赖

```bash
git clone https://github.com/peabodyrainert93/op-pm-arb-monitor.git
cd op-pm-arb-monitor

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2) 在服务器上创建并填写 `.env`（不会上传 GitHub）

`.env` 只存在于服务器本地，用来存放密钥与 TG 配置，不要上传到仓库。

在仓库根目录创建 `.env`：

```bash
cp .env.example .env
nano .env
```

`.env` 示例（把值换成你自己的）：

```env
OPINION_API_KEYS=KEY1,KEY2
OPINION_API_KEY=KEY1
TELEGRAM_BOT_TOKEN=123456:ABCDEF
TELEGRAM_CHAT_ID=-100xxxxxxxxxx
```

字段说明：

- `OPINION_API_KEYS`：多 key（逗号分隔），主要供 **生成映射**（`run_token_registry.py`）使用
- `OPINION_API_KEY`：单 key，供 **监控脚本**（`run_arb_monitor.py`）使用（建议填其中一个 key 即可）
- `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`：发送 Telegram 提醒所需
- `TELEGRAM_MESSAGE_THREAD_ID`：可选，论坛群的 topic id

补充字段（按需）：

- `OP_WALLET_ADDRESS` / `PM_WALLET_ADDRESS`：获利提醒/仓位对账的钱包地址
- `WALLET_ADDRESS`：兼容旧用法，同一个地址同时作为 OP/PM
- `PROFIT_OPINION_API_KEYS` / `PROFIT_OPINION_API_KEY`：获利提醒专用 key（不配则回退到 OPINION_API_KEYS）
- `OPINION_RPC_URL` / `OPINION_PRIVATE_KEY` / `OPINION_MULTI_SIG`：做市下单相关
- `OPINION_FRONTEND_AUTH` / `OPINION_DEVICE_FINGERPRINT` / `OPINION_WAF_TOKEN` / `OPINION_USER_AGENT`：前端抓取/做市数据源

---

## （可选）前端 Bearer 自动登录

如果你需要调用 Opinion 前端接口（例如 `opinion_frontend_fetch.py`），可以用脚本自动登录并定时刷新 Bearer。

准备一个 **小额热钱包**，私钥只放在本机 `.env`（不要上传 GitHub）。

`.env` 里添加（示例）：

```env
OPINION_PRIVATE_KEY=YOUR_PRIVATE_KEY
# 可选：多私钥（逗号分隔）
OPINION_PRIVATE_KEYS=KEY1,KEY2
OPINION_WALLET_ADDRESS=0xYourWallet  # 可选：不填则自动从私钥推导
OPINION_DEVICE_FINGERPRINT=YOUR_DEVICE_FP  # 可选：不填会自动生成稳定指纹
# 可选：多指纹（逗号分隔；只有多私钥时才需要）
OPINION_DEVICE_FINGERPRINTS=FP1,FP2
```

单次获取并写入 `.env`：

```bash
python opinion_frontend_auth.py --once
```

常驻刷新（自动写入 `OPINION_FRONTEND_AUTH` / `OPINION_FRONTEND_TOKEN_EXPIRE`）：

```bash
nohup python opinion_frontend_auth.py >> auth.log 2>&1 &
```

多私钥时，脚本会额外写入（并把 `OPINION_FRONTEND_AUTH` / `OPINION_FRONTEND_TOKEN_EXPIRE` 写成逗号分隔列表）：

- `OPINION_FRONTEND_ADDRESSES`：地址列表
- `OPINION_FRONTEND_AUTH_<ADDR>` / `OPINION_FRONTEND_TOKEN_EXPIRE_<ADDR>` / `OPINION_DEVICE_FINGERPRINT_<ADDR>`：按地址存储

---

## 使用流程（推荐顺序）

### 第一步：配置你要监控的市场（`token_registry.py`）

打开 `token_registry.py`，维护 `URL_PAIRS_FOR_DEBUG` 列表。每个条目需要：

- `name`：你自定义的市场名称
- `type`：`binary` 或 `categorical`
- `opinion_url`：Opinion 市场链接（带 `topicId`）
- `polymarket_url`：Polymarket event 链接（slug）

保存后进入下一步生成映射文件。

### 第二步：生成 / 刷新映射文件（`run_token_registry.py`）

生成的文件：`market_token_pairs.json`  
监控脚本会读取它，所以 **必须先生成**。

常规生成（默认增量更新）：

```bash
python run_token_registry.py
```

强制全量刷新（推荐：新增市场 / 映射不对 / 想彻底重建）：

```bash
python run_token_registry.py --refresh
```

常用可选参数（按需）：

- `--workers`：线程数（默认使用 core 的配置；监控脚本默认 8）
- `--opinion-interval`：Opinion 每次请求最小间隔（秒）
- `--gamma-interval`：Polymarket Gamma 每次请求最小间隔（秒）
- `--retries`：HTTP 最大重试次数
- `--backoff`：退避基数秒（越大越保守）
- `--refresh`：忽略缓存强制重抓
- `--keep-expired`：不删除已过期市场（默认会清理）
- `--expiry-grace-hours`：过期宽限期（小时，默认 12）

示例（更快一点）：

```bash
python run_token_registry.py --refresh --workers 6 --opinion-interval 0.35 --gamma-interval 0.35 --retries 4 --backoff 0.6
```

### 第三步：启动监控与提醒（`run_arb_monitor.py`）

运行前确保仓库根目录存在 `market_token_pairs.json`。如果没有，先执行上一步生成。

先单次自检（推荐）：

```bash
python run_arb_monitor.py --once
```

常驻运行（后台）：

```bash
nohup python run_arb_monitor.py >> monitor.log 2>&1 &
```

查看日志：

```bash
tail -f monitor.log
```

停止进程（示例）：

```bash
pkill -f run_arb_monitor.py
```

---

## `run_arb_monitor.py` 常用参数

- `--json`：指定 `market_token_pairs.json` 路径（默认：仓库根目录）
- `--interval`：轮询间隔秒（默认 1.0）
- `--delta-cents`：fallback 阈值(%)，仅 endDate/days 缺失时使用（默认 1.4）
- `--cooldown`：同一条机会最短提醒间隔（秒，默认 180）
- `--once`：只跑一轮就退出（用于自检）
- `--short-days`：剩余天数 < short-days 走短期阈值（默认 30）
- `--delta-short`：短期阈值(%)（默认 1.40）
- `--delta-mid`：中期阈值(%)（默认 2.50）
- `--workers`：Opinion 并发线程数（默认 300）
- `--op-qps`：Opinion 限速 QPS（默认 13）
- `--pm-qps`：Polymarket 限速 QPS（默认 8）
- `--gamma-qps`：Gamma 限速 QPS（默认 2）
- `--pm-batch / --no-pm-batch`：是否使用 `/books` 批量（默认开启，推荐）
- `--min-deploy-usd`：可套利资金低于该值不提醒（默认 20）
- `--max-days-to-expiry`：距离 Polymarket `endDate` 超过该天数不提醒（默认 90）

示例（更稳一点、提醒更少）：

```bash
python run_arb_monitor.py --interval 3 --delta-short 1.6 --delta-mid 2.8 --cooldown 300 --min-deploy-usd 30 --max-days-to-expiry 60
```

---

## `token_registry.py`（可选：直接运行）

一般推荐用 `run_token_registry.py`。如果你想直接跑 `token_registry.py` 也可以：

默认增量：

```bash
python token_registry.py
```

强制全量刷新（Linux/macOS）：

```bash
FORCE_REFRESH=1 python token_registry.py
```

---

## `run_profit_monitor_env.py`（持仓获利提醒）

基于 `market_token_pairs.json`，拉取 OP/PM 持仓 + 盘口 best bid，满足阈值时发送 TG 提醒。

依赖 `.env`：

- `OP_WALLET_ADDRESS` / `PM_WALLET_ADDRESS`（或 `WALLET_ADDRESS`）
- `PROFIT_OPINION_API_KEYS` / `PROFIT_OPINION_API_KEY`（不配则回退到 OPINION_API_KEYS）
- `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`（不发 TG 时可不配）

常用参数（默认值见脚本）：

- `--json`：`market_token_pairs.json` 路径
- `--interval`：扫描间隔秒
- `--min-shares`：过滤小仓位
- `--min-bid-size`：过滤小挂单
- `--threshold`：PM+OP best bid 相加阈值
- `--cooldown`：提醒冷却
- `--once` / `--dry-run`
- `--op-workers` / `--op-rps`

示例（只跑一轮、不发 TG）：

```bash
python run_profit_monitor_env.py --once --dry-run
```

---

## `run_position_reconcile.py`（仓位对账）

对账 Opinion vs Polymarket 仓位（OP NO 对 PM YES / OP YES 对 PM NO），输出差异 JSON。

常用参数：

- `--market-json`：`market_token_pairs.json` 路径
- `--op-wallet` / `--pm-wallet`
- `--threshold`：abs(op_shares - pm_shares) > threshold 才输出
- `--min-shares-fetch`：过滤小仓位（提高性能）
- `--include-unmapped`：额外输出未映射但持仓超过阈值的 token
- `--out`：输出文件（默认 `position_diff.json`）

示例：

```bash
python run_position_reconcile.py --threshold 5 --out position_diff.json
```

---

## Opinion 前端话题/深度抓取

### `opinion_frontend_fetch.py`

需要 `OPINION_FRONTEND_AUTH`（可用 `opinion_frontend_auth.py` 自动刷新）。

常见输出：

- `opinion_topics_cache.json`：分页缓存
- `opinion_topics_merged.json`：合并后的话题列表（`market_selector` 默认输入）
- `opinion_depth_cache.json` / `opinion_depth_ui.json`：深度缓存与 TUI 输入

示例（拉取话题并合并）：

```bash
python opinion_frontend_fetch.py --max-pages 200 --merge-cached --merged-output opinion_topics_merged.json
```

示例（抓深度用于 TUI）：

```bash
python opinion_frontend_fetch.py --fetch-depth --topics-input opinion_topics_merged.json --depth-ui-output opinion_depth_ui.json
```

示例（刷新话题并顺便抓深度）：

```bash
python opinion_frontend_fetch.py --refresh --fetch-depth --depth-output opinion_depth_cache.json
```

### `opinion_depth_tui.py`

```bash
python opinion_depth_tui.py --input opinion_depth_ui.json
```

---

## 做市（mm）模块

做市主流程（推荐顺序）：

1) 更新话题数据：`opinion_frontend_fetch.py` 生成 `opinion_topics_merged.json`
2) 选市场：`scripts/run_market_selector.py` 生成 `selected_markets.json`
3) 启动做市：`scripts/run_mm.py`

`mm_flow.canvas` 是流程图文件（Obsidian Canvas），用于梳理逻辑。

### `scripts/run_market_selector.py`

读取 `mm_config.json` 的 `market_selector`，根据话题列表筛选市场并写入 `selected_markets.json`。
如需按深度筛选，先用 `opinion_frontend_fetch.py --fetch-depth` 生成 `opinion_depth_cache.json`，再配置 `depth_min_*` 参数。

```bash
python scripts/run_market_selector.py --config mm_config.json
```

### `scripts/run_mm.py`

读取 `mm_config.json` 运行做市主循环。

```bash
python scripts/run_mm.py --config mm_config.json
```

---

## `mm_config.json` 配置说明（做市）

- `market_selector`：`source_file`/`output_file`/`market_pairs_file`/`require_market_pairs`/`price_target`/`price_tolerance`/`volume_percentile_min`/`volume_percentile_max`/`depth_source_file`/`depth_levels`/`depth_min_notional`/`depth_min_size`/`depth_token_side`/`max_markets`/`auto_run_on_missing`
- `market_switch`：`enabled`/`interval_seconds`/`cancel_all_on_switch`/`run_selector_on_switch`
- `quote`：`price_source`/`orderbook_level`/`token_side`/`size_per_side`/`min_size`/`min_depth_usd`/`replace_bps`
- `risk`：`cancel_on_price_proximity`/`proximity_bps`/`reference_price`
- `order_sync`：`enabled`/`interval_loops`/`status`/`limit`/`max_pages`（可额外传 `min_interval_loops` / `backoff_factor`）
- `logging`：`level`/`console_level`/`file_level`/`file`/`file_enabled`/`log_orderbook`/`log_state_changes`/`log_order_params`/`log_decisions`
- `opinion`：`host`/`chain_id`/`api_key_env`/`rpc_url_env`/`private_key_env`/`multi_sig_addr_env`/`http_min_interval`/`http_timeout_seconds`
- `opinion_data`：`source`(`openapi`/`frontend`)/`frontend_auth_env`/`frontend_device_fp_env`/`frontend_waf_env`/`frontend_auth_refresh`
- `state`：`file`（默认 `mm_state.json`）
- `runtime`：`loop_interval_seconds`

---

## 做市运维/排障脚本

### `scripts/account_monitor_tui.py`

账户/订单/持仓/余额/市场 TUI：

```bash
python scripts/account_monitor_tui.py --config mm_config.json --view orders
```

### `scripts/cancel_orders_from_state.py`

从 `mm_state.json` / 接口拉单并撤单：

```bash
python scripts/cancel_orders_from_state.py --config mm_config.json --state mm_state.json --dry-run
```

---

## 更新流程（本地 → GitHub → 服务器）

### A) 本地更新并推送到 GitHub

```bash
git status
git add .
git commit -m "your message"
git push origin main
```

### B) 服务器拉取最新代码并重启监控

```bash
cd op-pm-arb-monitor
git pull

source .venv/bin/activate
pip install -r requirements.txt
```

如果你更新了市场列表或怀疑映射过期，记得刷新：

```bash
python run_token_registry.py --refresh
```

重启监控（示例）：

```bash
pkill -f run_arb_monitor.py || true
nohup python run_arb_monitor.py >> monitor.log 2>&1 &
```

---

## 常见问题

### 1) 没有 Telegram 提醒，只在终端打印

请检查服务器 `.env` 是否配置了：

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

另外确认 Telegram Bot 已加入对应群组/频道，并且你填的 `TELEGRAM_CHAT_ID` 正确（群组一般是 `-100...` 这种格式）。

### 2) 找不到 `market_token_pairs.json`

先生成：

```bash
python run_token_registry.py --refresh
```

### 3) Opinion Key 报错 / 缺少 Key

确保 `.env` 至少包含：

```env
OPINION_API_KEY=KEY1
```

如果你打算用多 key 轮换，也可以填：

```env
OPINION_API_KEYS=KEY1,KEY2
```

---

## 安全提醒

- 不要把 `.env` 上传到 GitHub（里面有 API Key / Telegram Token）
- 如果不小心泄露过 Token / Key，请立刻在对应平台撤销并重新生成

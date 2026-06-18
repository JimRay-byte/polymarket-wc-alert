# 冷门大额提醒算法说明

本文解释服务端如何判断一笔成交是否值得提醒。系统只读取公开行情数据，不下单、不签名、不需要私钥。

## 核心数据口径

Polymarket 二元结果市场中，每份份额按 1 USDC 结算。服务端把成交金额估算为：

```text
estimated_usd = size * price
```

其中：

- `size`：成交份额数量。
- `price`：成交价格，也近似代表该 outcome 的隐含概率，范围通常是 `0-1`。
- `estimated_usd`：估算成交额，单位 `USDC`。

如果实时 WS 没有携带 `size`，服务端会尝试用 REST 最近成交补齐；仍无法补齐时，提醒原因里会标注 `size_missing`，表示金额可靠性较低。

## 先做硬过滤

算法先排除明显不应该提醒的事件。只有通过下面条件，才会继续算冷门分：

| 条件 | 配置项 | 单位 | 默认值 | 作用 |
|---|---:|---:|---:|---|
| 单笔成交额足够大 | `detector.large_trade_usd` | USDC | 5000 | 过滤普通小额成交 |
| 短窗口累计成交足够大 | `detector.short_window_large_usd` | USDC | 5000 | 识别拆单成交 |
| outcome 足够冷 | `detector.underdog_max_implied_prob` | 0-1 概率 | 0.35 | 价格高于该值不算冷门 |
| 盘口有基本深度 | `detector.min_book_depth_usd` | USDC | 500 | 过滤极薄盘口噪声 |

单笔模式要求 `estimated_usd >= large_trade_usd`。短窗口模式要求同一 outcome 在 `short_window_sec` 秒内累计成交额达到 `short_window_large_usd`。

## 冷门评分 underdog_score

冷门分是 `0-100` 的加权分：

```text
S = 0.30*A + 0.25*B + 0.20*C + 0.15*D + 0.10*E
```

分数越高，表示“低概率方向突然出现较大成交”的信号越强。最终分数必须达到：

```text
S >= detector.underdog_score_threshold
```

默认阈值是 `70` 分。

## A：隐含概率冷度

`A` 衡量 outcome 本身有多冷。价格越低，分越高。

```text
p <= 0.05 -> A = 100
p >= underdog_max_implied_prob -> A = 0
中间线性插值
```

默认 `underdog_max_implied_prob = 0.35`，所以：

- `p = 0.05`，极冷，A 为 100。
- `p = 0.20`，中等冷门，A 约为 50。
- `p >= 0.35`，不再视为冷门方向。

## B：相对弱势

`B` 衡量该 outcome 相对同市场最热门 outcome 有多弱。

```text
ratio = p_underdog / p_favorite
B = (1 - ratio) * 100
```

例子：

- 冷门 outcome 价格 `0.08`，热门 outcome 价格 `0.80`。
- `ratio = 0.08 / 0.80 = 0.10`。
- `B = 90`。

## C：短时价格异动

`C` 衡量短窗口内冷门方向是否快速上涨。

```text
change_ratio = (current_price - window_start_price) / window_start_price
C = clip((change_ratio / price_spike_ratio) * 100, 0, 100)
```

默认：

- `short_window_sec = 300` 秒。
- `price_spike_ratio = 0.05`，即 5%。

如果 5 分钟内上涨 5% 或更多，`C = 100`；上涨 2.5%，`C = 50`；下跌不加分。

## D：成交额异常度

`D` 衡量当前成交额相对近 1 小时历史成交中位数是否异常放大。

```text
D = min(100, (estimated_usd / median_1h_trade_usd) / volume_anomaly_multiplier * 100)
```

默认 `volume_anomaly_multiplier = 5`。也就是说，当前成交额达到近 1 小时中位数的 5 倍时，`D = 100`。

如果近 1 小时没有历史成交样本，系统使用兜底分：

- 成交额达到 `important_trade_usd`：D = 80。
- 否则：D = 40。

## E：流动性匹配度

`E` 衡量盘口深度是否足以支撑这条信号。深度越足，信号越可信。

```text
E = min(100, depth_usd / estimated_usd / 10 * 100)
```

含义：

- 盘口深度达到成交额的 10 倍或以上，`E = 100`。
- 如果盘口很薄，即使成交看起来大，也会降低最终分。
- 低于 `min_book_depth_usd` 会在硬过滤阶段直接忽略。

## 提醒级别

提醒级别只由成交额决定：

| 级别 | 条件 | 默认阈值 |
|---|---:|---:|
| `SEVERE` | `estimated_usd >= severe_trade_usd` | 100000 USDC |
| `IMPORTANT` | `estimated_usd >= important_trade_usd` | 20000 USDC |
| `INFO` | 达到普通提醒条件但低于重要阈值 | 5000 USDC 起 |

同一 outcome 在 30 秒内出现同级别事件时会去重，避免重复刷屏。

## 算分示例

假设一笔成交：

- 冷门 outcome 当前价格 `p = 0.08`。
- 同市场热门 outcome 价格 `0.80`。
- 5 分钟内价格上涨 `4%`。
- 本笔成交额 `12000 USDC`。
- 近 1 小时成交中位数 `2000 USDC`。
- 盘口深度 `100000 USDC`。

按默认参数：

```text
A = (0.35 - 0.08) / (0.35 - 0.05) * 100 = 90
B = (1 - 0.08 / 0.80) * 100 = 90
C = 0.04 / 0.05 * 100 = 80
D = min(100, (12000 / 2000) / 5 * 100) = 100
E = min(100, 100000 / 12000 / 10 * 100) = 83.3

S = 0.30*90 + 0.25*90 + 0.20*80 + 0.15*100 + 0.10*83.3
  = 88.8
```

如果阈值是 `70`，这笔会触发提醒；成交额低于 `important_trade_usd`，因此级别是 `INFO`。

## 参数调节建议

| 想要的效果 | 建议调整 |
|---|---|
| 更积极提醒 | 降低 `large_trade_usd`、`underdog_score_threshold`、`min_liquidity_usd` |
| 更少误报 | 提高 `underdog_score_threshold`、`min_book_depth_usd`、`large_trade_usd` |
| 更关注拆单 | 降低 `short_window_large_usd` 或延长 `short_window_sec` |
| 更关注短时异动 | 降低 `price_spike_ratio` |
| 更关注异常大单 | 降低 `volume_anomaly_multiplier` |

降低阈值会提高覆盖率，也会增加噪声。公开市场数据可能延迟或字段变化，本系统的提醒不构成投注或投资建议。

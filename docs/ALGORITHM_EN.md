# Underdog Large Trade Alert Algorithm

This document explains how the server decides whether a Polymarket World Cup trade should trigger an alert.

The system is read-only. It monitors public market data, calculates an explainable score, and sends notifications. It does not trade.

## Trade Value

Polymarket binary outcome shares settle to 1 USDC. The estimated trade value is:

```text
estimated_usd = size * price
```

Fields:

- `size`: number of outcome shares traded.
- `price`: trade price, usually between `0` and `1`.
- `estimated_usd`: estimated trade value, in USDC.

If the live WebSocket event does not include `size`, the server tries to recover it from recent REST trade data. If the size still cannot be recovered reliably, the alert reason includes `size_missing`.

## Hard Filters

The detector first removes events that are too small or too noisy.

| Condition | Config key | Unit | Default | Purpose |
|---|---:|---:|---:|---|
| Single trade value is large enough | `detector.large_trade_usd` | USDC | 5000 | Ignore ordinary small trades |
| Short-window cumulative value is large enough | `detector.short_window_large_usd` | USDC | 5000 | Detect split orders |
| Outcome is still an underdog | `detector.underdog_max_implied_prob` | probability, 0-1 | 0.35 | Ignore outcomes that are no longer underdogs |
| Order book has enough depth | `detector.min_book_depth_usd` | USDC | 500 | Filter extremely thin books |

Single-trade mode requires:

```text
estimated_usd >= large_trade_usd
```

Short-window mode requires the same outcome to accumulate:

```text
sum(trade_usd within short_window_sec) >= short_window_large_usd
```

The default short window is 300 seconds.

## Underdog Score

The final score is a weighted 0-100 value:

```text
S = 0.30*A + 0.25*B + 0.20*C + 0.15*D + 0.10*E
```

An alert requires:

```text
S >= detector.underdog_score_threshold
```

The default threshold is `70`.

## A: Implied Probability Coldness

`A` measures how low the outcome's implied probability is.

```text
p <= 0.05 -> A = 100
p >= underdog_max_implied_prob -> A = 0
otherwise -> linear interpolation
```

With the default `underdog_max_implied_prob = 0.35`:

- `p = 0.05` gets 100.
- `p = 0.20` gets about 50.
- `p >= 0.35` is not treated as an underdog.

## B: Relative Weakness

`B` compares the underdog outcome against the favorite outcome in the same market.

```text
ratio = p_underdog / p_favorite
B = (1 - ratio) * 100
```

Example:

```text
p_underdog = 0.08
p_favorite = 0.80
ratio = 0.10
B = 90
```

## C: Short-Window Price Movement

`C` rewards short-term upward movement on the underdog side.

```text
change_ratio = (current_price - window_start_price) / window_start_price
C = clip((change_ratio / price_spike_ratio) * 100, 0, 100)
```

Defaults:

- `short_window_sec = 300`.
- `price_spike_ratio = 0.05`.

If the outcome rises 5% or more within the short window, `C = 100`. A 2.5% rise gives `C = 50`. A price drop gives no positive score.

## D: Volume Anomaly

`D` checks whether the current value is unusually large compared with recent history.

```text
D = min(100, (estimated_usd / median_1h_trade_usd) / volume_anomaly_multiplier * 100)
```

Default:

```text
volume_anomaly_multiplier = 5
```

If the current trade value is 5 times the recent 1-hour median, `D = 100`.

If there is no recent 1-hour trade sample:

- `D = 80` when `estimated_usd >= important_trade_usd`.
- `D = 40` otherwise.

## E: Liquidity Support

`E` measures whether order book depth is large enough to make the signal credible.

```text
E = min(100, depth_usd / estimated_usd / 10 * 100)
```

Interpretation:

- Depth at least 10 times the trade value gives `E = 100`.
- Thin books reduce the final score.
- If depth is below `min_book_depth_usd`, the event is removed by the hard filter before scoring.

## Alert Level

The alert level depends on trade value:

| Level | Condition | Default threshold |
|---|---:|---:|
| `SEVERE` | `estimated_usd >= severe_trade_usd` | 100000 USDC |
| `IMPORTANT` | `estimated_usd >= important_trade_usd` | 20000 USDC |
| `INFO` | Passed alert conditions but below important threshold | 5000 USDC and above |

The same outcome and same level are deduplicated for 30 seconds to avoid repeated alerts.

## Worked Example

Assume:

- Underdog price: `p = 0.08`.
- Favorite price in the same market: `0.80`.
- Price rose `4%` during the 5-minute window.
- Trade value: `12000 USDC`.
- Recent 1-hour median trade value: `2000 USDC`.
- Order book depth: `100000 USDC`.

Using default parameters:

```text
A = (0.35 - 0.08) / (0.35 - 0.05) * 100 = 90
B = (1 - 0.08 / 0.80) * 100 = 90
C = 0.04 / 0.05 * 100 = 80
D = min(100, (12000 / 2000) / 5 * 100) = 100
E = min(100, 100000 / 12000 / 10 * 100) = 83.3

S = 0.30*90 + 0.25*90 + 0.20*80 + 0.15*100 + 0.10*83.3
  = 88.8
```

If the threshold is `70`, this trade triggers an alert. Because `12000 USDC` is below the default `important_trade_usd`, the level is `INFO`.

## Parameter Tuning

| Goal | Suggested changes |
|---|---|
| More aggressive alerts | Lower `large_trade_usd`, `underdog_score_threshold`, or `min_liquidity_usd` |
| Fewer false positives | Raise `underdog_score_threshold`, `min_book_depth_usd`, or `large_trade_usd` |
| Better split-order coverage | Lower `short_window_large_usd` or increase `short_window_sec` |
| More focus on price spikes | Lower `price_spike_ratio` |
| More focus on unusually large trades | Lower `volume_anomaly_multiplier` |

Lower thresholds increase coverage and noise. Alerts are only informational and do not constitute betting or investment advice.

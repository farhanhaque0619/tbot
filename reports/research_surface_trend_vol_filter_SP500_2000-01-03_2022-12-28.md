# Parameter surface · trend_vol_filter · SP500

| fast | slow | vol_window | vol_cap | sharpe | cagr | max_drawdown | trades | profit_factor | exposure | killed |
|---|---|---|---|---|---|---|---|---|---|---|
| 50 | 200 | 21 | 0.150 | 0.269 | 0.010 | -0.112 | 87 | 1.536 | 0.491 | False |
| 50 | 200 | 21 | 0.200 | 0.423 | 0.021 | -0.118 | 53 | 2.425 | 0.606 | False |
| 50 | 200 | 21 | 0.250 | 0.512 | 0.029 | -0.102 | 48 | 3.259 | 0.658 | False |
| 50 | 200 | 21 | 0.350 | 0.540 | 0.030 | -0.109 | 21 | 7.629 | 0.647 | False |
| 50 | 200 | 21 | 10.000 | 0.507 | 0.030 | -0.149 | 26 | 6.301 | 0.650 | False |

best: {'fast': 50, 'slow': 200, 'vol_window': 21, 'vol_cap': 0.35} · neighbour/best sharpe ratio: 0.94 · stable neighbourhood

_grid of 5 points; best chosen by sharpe among points with >= 5 trades_
_positive-sharpe share of the grid: 100%_
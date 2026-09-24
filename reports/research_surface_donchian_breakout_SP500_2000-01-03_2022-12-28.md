# Parameter surface · donchian_breakout · SP500

| entry_n | exit_n | sharpe | cagr | max_drawdown | trades | profit_factor | exposure | killed |
|---|---|---|---|---|---|---|---|---|
| 20 | 10 | 0.233 | 0.008 | -0.138 | 149 | 1.330 | 0.465 | False |
| 55 | 10 | 0.049 | 0.001 | -0.086 | 112 | 1.051 | 0.333 | False |
| 55 | 20 | 0.168 | 0.006 | -0.103 | 81 | 1.280 | 0.401 | False |
| 55 | 50 | 0.256 | 0.010 | -0.114 | 53 | 1.553 | 0.507 | False |
| 100 | 10 | 0.148 | 0.004 | -0.074 | 91 | 1.249 | 0.285 | False |
| 100 | 20 | 0.257 | 0.008 | -0.084 | 62 | 1.589 | 0.342 | False |
| 100 | 50 | 0.389 | 0.015 | -0.092 | 36 | 2.334 | 0.439 | False |

best: {'entry_n': 100, 'exit_n': 50} · neighbour/best sharpe ratio: 0.66 · stable neighbourhood

_grid of 7 points; best chosen by sharpe among points with >= 5 trades_
_positive-sharpe share of the grid: 100%_
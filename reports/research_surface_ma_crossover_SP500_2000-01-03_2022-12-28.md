# Parameter surface · ma_crossover · SP500

| fast | slow | sharpe | cagr | max_drawdown | trades | profit_factor | exposure | killed |
|---|---|---|---|---|---|---|---|---|
| 10 | 50 | 0.258 | 0.012 | -0.144 | 127 | 1.415 | 0.624 | False |
| 10 | 100 | 0.347 | 0.017 | -0.173 | 75 | 1.840 | 0.659 | False |
| 10 | 200 | 0.444 | 0.024 | -0.104 | 39 | 3.257 | 0.682 | False |
| 20 | 50 | 0.279 | 0.013 | -0.139 | 126 | 1.425 | 0.620 | False |
| 20 | 100 | 0.227 | 0.011 | -0.152 | 78 | 1.412 | 0.660 | False |
| 20 | 200 | 0.470 | 0.026 | -0.149 | 26 | 4.062 | 0.653 | False |
| 50 | 100 | 0.386 | 0.021 | -0.149 | 73 | 2.203 | 0.650 | False |
| 50 | 200 | 0.507 | 0.030 | -0.149 | 26 | 6.301 | 0.650 | False |

best: {'fast': 50, 'slow': 200} · neighbour/best sharpe ratio: 0.84 · stable neighbourhood

_grid of 8 points; best chosen by sharpe among points with >= 5 trades_
_positive-sharpe share of the grid: 100%_
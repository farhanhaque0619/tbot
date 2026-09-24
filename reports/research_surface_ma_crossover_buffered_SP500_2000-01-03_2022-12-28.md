# Parameter surface · ma_crossover_buffered · SP500

| fast | slow | band | sharpe | cagr | max_drawdown | trades | profit_factor | exposure | killed |
|---|---|---|---|---|---|---|---|---|---|
| 20 | 100 | 0.000 | 0.227 | 0.011 | -0.152 | 78 | 1.412 | 0.660 | False |
| 20 | 100 | 0.005 | 0.135 | 0.006 | -0.157 | 75 | 1.225 | 0.654 | False |
| 20 | 100 | 0.010 | 0.176 | 0.008 | -0.175 | 53 | 1.354 | 0.659 | False |
| 20 | 100 | 0.020 | 0.401 | 0.020 | -0.149 | 42 | 2.893 | 0.599 | False |
| 50 | 200 | 0.000 | 0.507 | 0.030 | -0.149 | 26 | 6.301 | 0.650 | False |
| 50 | 200 | 0.005 | 0.526 | 0.031 | -0.149 | 27 | 7.280 | 0.646 | False |
| 50 | 200 | 0.010 | 0.508 | 0.030 | -0.152 | 23 | 7.994 | 0.648 | False |
| 50 | 200 | 0.020 | 0.470 | 0.029 | -0.181 | 21 | 7.803 | 0.664 | False |

best: {'fast': 50, 'slow': 200, 'band': 0.005} · neighbour/best sharpe ratio: 0.96 · stable neighbourhood

_grid of 8 points; best chosen by sharpe among points with >= 5 trades_
_positive-sharpe share of the grid: 100%_
"""V1.5 strategy modules (spec §5.3–5.5). Research candidates until research/PROMOTIONS.md says otherwise.

Modules express opinions as TradeIntents; they never see equity, cash or other modules' positions beyond their own slice
(PositionView). They import nothing from bot.execution (enforced by tests/test_architecture.py).
"""
from bot.strategies.v15.m1_vol_trend import M1VolTrend
from bot.strategies.v15.m2_intraday_momentum import M2IntradayMomentum
from bot.strategies.v15.m3_residual_reversal import M3ResidualReversal

MODULES = {"M1": M1VolTrend, "M2": M2IntradayMomentum, "M3": M3ResidualReversal}

__all__ = ["MODULES", "M1VolTrend", "M2IntradayMomentum", "M3ResidualReversal"]

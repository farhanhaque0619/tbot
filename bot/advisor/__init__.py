"""Advisor layer (Phase 10): structured MarketState in, structured classification out. Shadow mode only.

Consumers may LOG advice; nothing here can reach a broker, the risk manager, or configuration.
"""
from bot.advisor.base import Advisor, MarketAdvice, NullAdvisor, RuleAdvisor, build_advisor
from bot.advisor.shadow import ShadowRecorder, calibration

__all__ = ["Advisor", "MarketAdvice", "NullAdvisor", "RuleAdvisor", "build_advisor", "ShadowRecorder", "calibration"]

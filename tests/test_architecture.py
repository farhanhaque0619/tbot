"""Phase 9: brain / reflex separation, enforced by import scanning."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "bot"


def imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
    return out


def modules(pkg: str):
    return list((ROOT / pkg).rglob("*.py"))


def test_research_layer_cannot_reach_a_broker():
    forbidden = ("bot.execution.broker", "bot.execution.paper_loop", "bot.execution.interlock", "alpaca")
    for f in modules("research") + modules("advisor"):
        imps = imports_of(f)
        bad = [i for i in imps if any(i == x or i.startswith(x + ".") for x in forbidden)]
        assert not bad, f"{f.relative_to(ROOT)} imports {bad}"


def test_execution_and_risk_never_import_research_or_kelly():
    for f in modules("execution") + modules("risk") + modules("backtest"):
        imps = imports_of(f)
        bad = [i for i in imps if i.startswith("bot.research") or i == "bot.advisor.jev"]
        assert not bad, f"{f.relative_to(ROOT)} imports {bad}"
    # no identifier (function, name, attribute) mentioning kelly anywhere in execution/risk - comments may mention it
    for f in modules("execution") + modules("risk"):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        names |= {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
        assert not [n for n in names if "kelly" in n.lower()], f


def test_risk_layer_sits_below_everything():
    for f in modules("risk"):
        imps = imports_of(f)
        bad = [i for i in imps if i.startswith(("bot.execution", "bot.research", "bot.advisor", "bot.strategies"))]
        assert not bad, f"{f.relative_to(ROOT)} imports {bad}"


def test_execution_registry_only_contains_baselines():
    from bot.research.candidates import RESEARCH_STRATEGIES
    from bot.strategies import STRATEGIES
    assert set(STRATEGIES) == {"ma_crossover", "mean_reversion"}
    assert not set(RESEARCH_STRATEGIES) & set(STRATEGIES)


# ---------------------------------------------------------------- V1.5 (Phase 2) layering
BROKER_PATHS = ("bot.execution.broker", "bot.execution.oms", "bot.execution.paper_loop", "bot.execution.interlock", "bot.execution.smoke", "alpaca")


def test_strategy_feature_portfolio_layers_cannot_reach_order_submission():
    """Strategies express opinions; the allocator sizes; neither may import anything that can submit an order."""
    for f in modules("strategies") + modules("portfolio") + modules("features") + modules("core") + modules("advisor"):
        imps = imports_of(f)
        bad = [i for i in imps if any(i == x or i.startswith(x + ".") for x in BROKER_PATHS)]
        assert not bad, f"{f.relative_to(ROOT)} imports {bad}"


def test_core_and_risk_sit_below_strategy_portfolio_and_execution():
    for f in modules("core") + modules("risk"):
        imps = imports_of(f)
        bad = [i for i in imps if i.startswith(("bot.execution", "bot.strategies", "bot.portfolio", "bot.features", "bot.backtest", "bot.research", "bot.advisor"))]
        assert not bad, f"{f.relative_to(ROOT)} imports {bad}"


def test_nothing_in_bot_mutates_a_risk_policy():
    """RiskPolicy is frozen; the only way to get a different policy is a new file + fingerprint. No runtime copies with edits."""
    for pkg in ("strategies", "portfolio", "features", "execution", "backtest", "risk", "core", "research", "advisor", "monitoring"):
        for f in modules(pkg):
            src = f.read_text(encoding="utf-8")
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "model_copy":
                    raise AssertionError(f"{f.relative_to(ROOT)} copies a pydantic model with edits (policy mutation path)")
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "__setattr__":
                    raise AssertionError(f"{f.relative_to(ROOT)} uses __setattr__")


def test_policy_files_are_frozen_and_live_is_stricter_than_paper():
    from bot.core.policy import RiskPolicy
    paper, live = RiskPolicy.load("config/policy.paper.yaml"), RiskPolicy.load("config/policy.live.yaml")
    assert live.max_gross_pct <= paper.max_gross_pct and live.max_drawdown_pct <= paper.max_drawdown_pct
    assert live.max_daily_loss_pct <= paper.max_daily_loss_pct and not live.allow_short and not live.allow_margin
    assert set(live.allowed_modules) <= set(paper.allowed_modules) and live.allowed_symbols
    assert paper.model_config.get("frozen") is True


def test_watchdog_and_streams_cannot_import_strategies_or_research():
    for f in modules("runtime") + modules("stream"):
        imps = imports_of(f)
        if f.name == "daemon.py":
            bad = [i for i in imps if i.startswith(("bot.research", "bot.advisor"))]
        else:
            bad = [i for i in imps if i.startswith(("bot.strategies", "bot.research", "bot.advisor"))]
        assert not bad, f"{f.relative_to(ROOT)} imports {bad}"
    wd = imports_of(ROOT / "runtime" / "watchdog.py")
    assert not [i for i in wd if i.startswith(("bot.strategies", "bot.portfolio", "bot.execution.oms", "bot.features"))]
    for f in modules("stream"):
        assert not [i for i in imports_of(f) if i.startswith("bot.execution")], f"{f.relative_to(ROOT)} must not reach the order path"

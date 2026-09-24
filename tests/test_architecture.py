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

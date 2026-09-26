"""Real Alpaca PAPER smoke test. Requires RUN_ALPACA_INTEGRATION=1 and RUN_ALPACA_INTEGRATION_ORDERS=1.
Submits one ~$2 fractional SPY order and closes it (market open) or acknowledges + cancels it (market closed)."""

from bot.execution.smoke import FAIL, PaperSmokeTest


def test_paper_smoke_cycle(broker, orders_enabled, settings, tmp_path):
    t = PaperSmokeTest(settings=settings, broker=broker, symbol="SPY", notional=2.0, wait_seconds=180, report_dir=tmp_path)
    rep = t.run()
    for s in rep.steps:
        print(f"{s.name:34s} {s.status:5s} {s.detail}  order={s.order_id} req={s.request_id}")
    failed = [s for s in rep.steps if s.status == FAIL]
    assert not failed, failed

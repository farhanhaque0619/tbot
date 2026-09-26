# PROMOTIONS — modules allowed to trade live

A module appears here only after it passed cuts B and C of the research protocol and, once, the sealed cut D
(see PROTOCOL.md and UNSEAL_LOG.md). The format is machine-read by `bot/core/policy.py::promoted_modules`:

    ## <MODULE> promoted <YYYY-MM-DD> <free text: who, RESULTS_V1_5.md section, code hash>

No module has been promoted. `config/policy.live.yaml` lists M1 as the intended first live module; loading that policy
with `require_promotions=True` (the live path) fails until a line for M1 exists here.

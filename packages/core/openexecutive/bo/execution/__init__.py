"""Delegated execution, checkpoints and the effect ledger (VAL4-01).

The execution slice is deliberately separate from the observe-mode router:
a recommendation never becomes an authorization, and an observation never
becomes an effect. Authority flows only through a mandate; effects flow
only through the ledger.
"""

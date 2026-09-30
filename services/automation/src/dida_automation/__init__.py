"""DIDA automation engine — Layer 1 (typed Trigger -> Condition -> Action).

A separate, isolated process (PROPOSAL §3/§6): it consumes the validated engine
events stream, matches each event against enabled rules, checks conditions
against current state, and publishes the rules' actions as commands. Running it
apart from the state engine means a misbehaving rule (or, later, a Starlark
snippet) can't stall state projection — and a per-rule circuit breaker disables
a rule that keeps erroring instead of retrying into the void.
"""

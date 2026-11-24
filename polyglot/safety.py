"""The "controlled" part of Track 1, kept in plain code (never in a prompt).

1. Rolling-code refusal: a button is captured twice; if the payload changes, it is a rolling
   code (car keys, gates) and is refused.
2. Taught codes only: we transmit only codes captured in this home. The model passes a *name*;
   it can never pass raw bits.
3. Human approval: rules are drafts until a person approves them on the Polyglot page.
   Rules that move something physical (servo) carry a flag the page shows in red.
"""
from __future__ import annotations

from dataclasses import dataclass

from .protocol import Code

PHYSICAL_ACTIONS = {"light_on", "light_off"}  # built-in servo actions


@dataclass
class LearnVerdict:
    ok: bool
    reason: str
    code: Code | None = None


def judge_captures(first: Code, second: Code) -> LearnVerdict:
    if first.src != second.src:
        return LearnVerdict(False, "the two presses came from different radios; press the same button twice")
    if first == second:
        return LearnVerdict(True, "fixed code: both presses decoded identically", first)
    if first.proto == second.proto and first.addr == second.addr and first.cmd != second.cmd:
        return LearnVerdict(False, "two different buttons on the same remote were pressed; press one button twice")
    return LearnVerdict(
        False,
        "REFUSED: rolling code (the payload changed between presses). Car keys, gates and garage remotes "
        "are deliberately unsupported.",
    )


def is_physical(action_name: str) -> bool:
    return action_name in PHYSICAL_ACTIONS

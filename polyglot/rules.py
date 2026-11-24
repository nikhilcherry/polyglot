"""The rule engine that runs ON THE HUB (simulator here, firmware/polyglot_hub for the ESP32).

Rules are stored compiled: the trigger is a decoded Code and the actions are raw hub ops,
so the hub needs no names, no network and no model to run them.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .protocol import Code

DEBOUNCE_S = 1.0  # RF remotes repeat a frame many times per press


@dataclass
class CompiledRule:
    id: int
    when: Code
    then: list[dict[str, Any]]  # [{"tx": {...code...}}, {"servo": 90}, {"clip": 2}]

    def to_wire(self) -> dict[str, Any]:
        return {"op": "rule_put", "id": self.id, "when": self.when.to_dict(), "then": self.then}

    @classmethod
    def from_wire(cls, m: dict[str, Any]) -> "CompiledRule":
        return cls(int(m["id"]), Code(**m["when"]), list(m["then"]))


@dataclass
class RuleEngine:
    rules: dict[int, CompiledRule] = field(default_factory=dict)
    _last: dict[str, float] = field(default_factory=dict)
    max_rules: int = 32  # same limit as the firmware's NVS table

    def put(self, rule: CompiledRule) -> None:
        if rule.id not in self.rules and len(self.rules) >= self.max_rules:
            raise ValueError("rule table full")
        self.rules[rule.id] = rule

    def delete(self, rule_id: int) -> bool:
        return self.rules.pop(rule_id, None) is not None

    def match(self, code: Code, now: float | None = None) -> list[CompiledRule]:
        """Rules to fire for one received code (debounced per code)."""
        now = time.monotonic() if now is None else now
        k = code.key()
        if now - self._last.get(k, -1e9) < DEBOUNCE_S:
            return []
        self._last[k] = now
        return [r for r in self.rules.values() if r.when == code]

"""Offline fallback: turn one English sentence into a rule draft, using only taught names.

The main path is Claude (or any MCP client) calling `propose_rule` with structured fields.
This parser lets the visualiser's "Say it" box (typed, or browser speech-to-text) work with no
model at all, e.g.:
  "When the doorbell rings, mute the TV, switch on the hall light and tell Amma someone is at the door"
"""
from __future__ import annotations

import re

from .protocol import Signal

SAY_RE = re.compile(r"\b(?:say|tell(?:\s+\w+)?|announce)\b\s*(?:that\s+)?[\"“]?(.+?)[\"”]?$", re.I)
LIGHT_RE = re.compile(r"\b(?:switch|turn)\s+(on|off)\b.*\blight|\blight\b.*\b(on|off)\b", re.I)


def _tokens(name: str) -> set[str]:
    return {t for t in re.split(r"[_\W]+", name.lower()) if t}


def _mentions(chunk: str, sig: Signal) -> bool:
    words = set(re.findall(r"[a-z0-9]+", chunk.lower()))
    names = [sig.name, *sig.aliases]
    return any(_tokens(n) and _tokens(n) <= words for n in names)


def parse(sentence: str, signals: list[Signal]) -> dict:
    s = sentence.strip().rstrip(".")
    m = re.match(r"^\s*(?:when(?:ever)?|if)\s+(.+?)(?:,|\bthen\b)\s*(.+)$", s, re.I)
    if not m:
        raise ValueError('start with "When <something happens>, <do this>"')
    cond, rest = m.group(1), m.group(2)
    triggers = [g for g in signals if _mentions(cond, g)]
    if not triggers:
        raise ValueError(f"I don't know a taught signal in “{cond}”. Teach it first.")
    when = max(triggers, key=lambda g: len(_tokens(g.name))).name

    then: list[str] = []
    say = None
    chunks = [c.strip() for c in re.split(r",|\band then\b|\band\b", rest) if c.strip()]
    for i, c in enumerate(chunks):
        sm = SAY_RE.search(c)
        if sm and re.search(r"\b(say|tell|announce)\b", c, re.I):
            # the spoken text may itself contain "and" -> keep the remainder of the sentence
            tail = ", ".join([sm.group(1)] + chunks[i + 1:]) if i + 1 < len(chunks) else sm.group(1)
            say = tail.strip().strip("\"“”")
            say = say[0].upper() + say[1:] if say else say
            break
        lm = LIGHT_RE.search(c)
        if lm:
            then.append("light_on" if (lm.group(1) or lm.group(2)).lower() == "on" else "light_off")
            continue
        hits = [g for g in signals if g.name != when and _mentions(c, g)]
        if not hits:
            raise ValueError(f"I don't know how to “{c}”. Teach that button first.")
        then.append(max(hits, key=lambda g: len(_tokens(g.name))).name)
    if not then and not say:
        raise ValueError("no actions found")
    return {"when": when, "then": then, "say": say}

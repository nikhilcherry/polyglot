# Safety model ("reliable and controlled")

1. **Taught codes only.** `send_signal` transmits only codes captured in this home. The AI cannot invent or brute-force a code, and there is no built-in code database for RF.
2. **Rolling-code refusal.** `learn_signal` captures the same button twice. If the payload changes between presses, it is a rolling code (car keys, gates, most garage remotes), and Polyglot marks it **REFUSED**. Only fixed-code devices (e.g. EV1527/PT2262 doorbells, IR remotes) are supported.
3. **Read-back before save.** Every rule is read back in plain language (ElevenLabs TTS) and saved only after the user confirms it.
4. **Second confirmation for physical actions.** Anything that moves a servo or switches mains-adjacent devices needs a second "yes". No pumps, geysers or locks.
5. **Full log.** Every learn, send, rule change and rule firing is logged with a timestamp.
6. **AI off the trigger path.** Rules execute on the hub; a model cannot fire anything at run time.

What this does *not* do: it can't stop someone with physical access to a fixed-code remote from teaching it. Anyone can already clone such a remote with a cheap copy remote. The allowlist stops the AI from inventing codes; it does not prove ownership.

Regulatory notes: 433.05–434.79 MHz low-power devices are licence-exempt in India (DoT rules, 2022); LoRa uses 865–867 MHz (licence-exempt, 2021 rules). IR needs no licence.

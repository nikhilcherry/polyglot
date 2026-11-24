# Architecture (design, not yet built)

Two planes:

- **A · Authoring plane (online, once per rule):** user voice/text → ElevenLabs Scribe (speech → text) → Claude or any MCP client → **Polyglot MCP server** (FastMCP, safety checks) → USB serial → hub. Optional AWS (codebook, family alerts, backup).
- **B · Run plane (on the hub, offline, every time):** 433 MHz / IR signal → CC1101 / TSOP receiver → decoder → rule engine (rules in NVS flash) → IR LED · servo · I2S speaker · (stretch) LoRa packet.

The LLM never sits on the trigger path. Triggers match **decoded** codes (protocol + address + command), never raw waveforms. Raw pulse replay is used only as a transmit fallback for unknown IR protocols.

## MCP surface (planned)

| Tool | Purpose |
|---|---|
| `learn_signal(name, kind)` | Capture for ~5 s, decode, capture again, compare. Saves fixed codes; refuses rolling codes. |
| `list_signals()` | Learned devices with type (trigger / action), protocol and when they were taught. |
| `send_signal(name)` | Transmit a taught code. Fails if the name was not taught in this home. |
| `create_rule(when, then[])` | Validate → read back in plain language → save only after confirmation. Physical actions (servo) need a second confirmation. |
| `test_rule(id)` | Fire a rule's actions once, logged. |
| `delete_rule(id)` | Remove a rule from the hub. |

Resources: `signals://library`, `rules://active`, `log://recent`.
When a device is learned, the server emits `notifications/tools/list_changed` so clients that support it see a per-device tool. Clients that don't still have `send_signal(name)`.

## Serial contract hub ⇄ server (draft, JSON lines)

```
hub → server  {"ev":"rx","src":"rf433","proto":"EV1527","addr":"0x5A3C1","cmd":"0x8","rssi":-54}
hub → server  {"ev":"rx","src":"ir","proto":"NEC","addr":"0x04","cmd":"0x09"}
server → hub  {"op":"tx","src":"ir","proto":"NEC","addr":"0x04","cmd":"0x09"}
server → hub  {"op":"rule_put","id":3,"when":{"src":"rf433","addr":"0x5A3C1","cmd":"0x8"},"then":[{"tx":"tv_mute"},{"servo":90},{"clip":2}]}
hub → server  {"ev":"fired","rule":3,"ms":<measured>}
```

Values above are illustrative.

## Bill of materials (approximate, unverified retail INR)

| Part | ₹ |
|---|---|
| ESP32 DevKit | ~350 |
| CC1101 433 MHz module | ~300 |
| TSOP38238 + 940 nm IR LED + transistor | ~90 |
| MG90S servo | ~250 |
| MAX98357A I2S amp + speaker | ~250 |
| **Hub core (ESP32 + CC1101 + IR + servo)** | **≈ 900–1,000** |
| Stretch: 2 × LoRa 865–868 MHz modules | ~1,600 |

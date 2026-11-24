// Polyglot hub firmware (ESP32). Speaks the same JSON-lines protocol as polyglot/hub.py's SimHub.
//
//  hub -> server : {"ev":"rx","src":"ir|rf433","proto":"NEC","addr":"b32","cmd":"0x20DF10EF"}
//                  {"ev":"fired","rule":3,"us":812,"actions":[...]}
//  server -> hub : {"op":"tx",...code...} {"op":"servo","angle":90} {"op":"play","clip":2}
//                  {"op":"rule_put","id":3,"when":{...code...},"then":[{"tx":{...}},{"servo":90},{"clip":2}]}
//                  {"op":"rule_del","id":3}
//
// Code fields on real hardware: proto = decoder protocol name, addr = "b<bit length>",
// cmd = hex value (or hex state bytes for long AC frames). The server only compares codes, so this
// is opaque to it. Rules live in NVS and run here with no laptop, network or model.
//
// Wiring (3.3 V logic only):
//   CC1101  SCK 18 · MISO 19 · MOSI 23 · CSN 5 · GDO0 25 (TX data) · GDO2 26 (RX data)
//   TSOP38238 out -> 14 · IR LED via NPN transistor -> 12 · servo signal -> 13 (own 5 V supply, common GND)
//   Piezo/I2S speaker -> 27 (chime for now; clip playback is a hack-day task)
//
// Libraries: ArduinoJson 7, IRremoteESP8266, rc-switch, SmartRC-CC1101-Driver-Lib, ESP32Servo.
// Status: compiles for esp32:esp32:esp32; not yet run on hardware (parts arrive for hack day).

#include <Arduino.h>
#include <ArduinoJson.h>
#include <Preferences.h>
#include <ESP32Servo.h>
#include <ELECHOUSE_CC1101_SRC_DRV.h>
#include <RCSwitch.h>
#include <IRremoteESP8266.h>
#include <IRrecv.h>
#include <IRsend.h>
#include <IRutils.h>

constexpr int PIN_SCK = 18, PIN_MISO = 19, PIN_MOSI = 23, PIN_CSN = 5, PIN_GDO0 = 25, PIN_GDO2 = 26;
constexpr int PIN_IR_RX = 14, PIN_IR_TX = 12, PIN_SERVO = 13, PIN_CHIME = 27;
constexpr int MAX_RULES = 32;
constexpr uint32_t DEBOUNCE_MS = 1000;

RCSwitch rf;
IRrecv irrecv(PIN_IR_RX, 1024, 50, true);  // big buffer + 50 ms timeout: long AC frames fit
IRsend irsend(PIN_IR_TX);
decode_results ir;
Servo servo;
Preferences nvs;

struct Rule {
  bool used = false;
  int id = 0;
  String whenKey;  // "src|proto|addr|cmd"
  String json;     // the full rule_put message, re-parsed when it fires
};
Rule rules[MAX_RULES];
String lastKey;
uint32_t lastAt = 0;

String keyOf(JsonVariantConst c) {
  return String((const char*)c["src"]) + "|" + (const char*)c["proto"] + "|" + (const char*)c["addr"] + "|" + (const char*)c["cmd"];
}

void emit(JsonDocument& d) {
  serializeJson(d, Serial);
  Serial.println();
}

// ---------------------------------------------------------------- transmit
bool sendCode(JsonVariantConst c) {
  String src = c["src"], proto = c["proto"], addr = c["addr"], cmd = c["cmd"];
  uint16_t bits = addr.substring(1).toInt();
  if (src == "ir") {
    decode_type_t t = strToDecodeType(proto.c_str());
    if (t == decode_type_t::UNKNOWN) return false;
    if (hasACState(t)) {  // cmd = hex bytes of the full AC state
      uint8_t state[kStateSizeMax];
      uint16_t n = (cmd.length() - 2) / 2;
      for (uint16_t i = 0; i < n && i < kStateSizeMax; i++) state[i] = strtoul(cmd.substring(2 + 2 * i, 4 + 2 * i).c_str(), nullptr, 16);
      return irsend.send(t, state, n);
    }
    return irsend.send(t, strtoull(cmd.c_str(), nullptr, 16), bits);
  }
  if (src == "rf433") {
    ELECHOUSE_cc1101.SetTx();
    rf.disableReceive();
    rf.enableTransmit(PIN_GDO0);
    rf.setProtocol(proto.substring(3).toInt());  // "RCS1" -> rc-switch protocol 1
    rf.setRepeatTransmit(8);
    rf.send(strtoul(cmd.c_str(), nullptr, 16), bits);
    rf.disableTransmit();
    ELECHOUSE_cc1101.SetRx();
    rf.enableReceive(PIN_GDO2);
    return true;
  }
  return false;
}

void chime() {  // placeholder for the pre-rendered voice clip
  for (int f : {880, 660}) { tone(PIN_CHIME, f, 180); delay(200); }
}

// ---------------------------------------------------------------- rules
void fire(Rule& r, uint32_t t0) {
  JsonDocument d;
  if (deserializeJson(d, r.json)) return;
  JsonDocument out;
  out["ev"] = "fired";
  out["rule"] = r.id;
  JsonArray acts = out["actions"].to<JsonArray>();
  for (JsonVariantConst a : d["then"].as<JsonArrayConst>()) {
    if (a["tx"].is<JsonObjectConst>()) acts.add(sendCode(a["tx"]) ? "tx ok" : "tx failed");
    else if (a["servo"].is<int>()) { servo.write(a["servo"].as<int>()); acts.add("servo " + String(a["servo"].as<int>())); }
    else if (a["clip"].is<int>()) { chime(); acts.add("clip " + String(a["clip"].as<int>())); }
  }
  out["us"] = micros() - t0;
  emit(out);
}

void onCode(const String& src, const String& proto, const String& addr, const String& cmd, uint32_t t0) {
  String key = src + "|" + proto + "|" + addr + "|" + cmd;
  if (key == lastKey && millis() - lastAt < DEBOUNCE_MS) return;  // remotes repeat frames
  lastKey = key;
  lastAt = millis();
  JsonDocument e;
  e["ev"] = "rx"; e["src"] = src; e["proto"] = proto; e["addr"] = addr; e["cmd"] = cmd;
  emit(e);
  for (auto& r : rules)
    if (r.used && r.whenKey == key) fire(r, t0);
}

void saveRule(Rule& r) { nvs.putString(("r" + String(r.id)).c_str(), r.json); }

void putRule(const String& json, int id, const String& whenKey) {
  Rule* slot = nullptr;
  for (auto& r : rules) if (r.used && r.id == id) slot = &r;
  for (auto& r : rules) if (!slot && !r.used) slot = &r;
  if (!slot) return;
  slot->used = true; slot->id = id; slot->whenKey = whenKey; slot->json = json;
  saveRule(*slot);
}

void loadRules() {
  for (int id = 1; id <= 255; id++) {
    String j = nvs.getString(("r" + String(id)).c_str(), "");
    if (!j.length()) continue;
    JsonDocument d;
    if (!deserializeJson(d, j)) putRule(j, id, keyOf(d["when"]));
  }
}

// ---------------------------------------------------------------- serial ops
void handle(const String& line) {
  JsonDocument d;
  if (deserializeJson(d, line)) return;
  String op = d["op"] | "";
  JsonDocument ack;
  ack["ev"] = "ack"; ack["op"] = op;
  if (op == "rule_put") putRule(line, d["id"], keyOf(d["when"]));
  else if (op == "rule_del") {
    for (auto& r : rules) if (r.used && r.id == d["id"].as<int>()) { r.used = false; nvs.remove(("r" + String(r.id)).c_str()); }
  } else if (op == "tx") sendCode(d.as<JsonVariantConst>());
  else if (op == "servo") servo.write(d["angle"].as<int>());
  else if (op == "play") chime();
  else if (op == "clip_put") { /* clips are rendered server-side; hack day: store audio in flash */ }
  else return;
  int n = 0;
  for (auto& r : rules) n += r.used;
  ack["rules"] = n;
  emit(ack);
}

void setup() {
  Serial.begin(115200);
  nvs.begin("polyglot", false);
  ELECHOUSE_cc1101.setSpiPin(PIN_SCK, PIN_MISO, PIN_MOSI, PIN_CSN);
  ELECHOUSE_cc1101.setGDO(PIN_GDO0, PIN_GDO2);
  ELECHOUSE_cc1101.Init();
  ELECHOUSE_cc1101.setMHZ(433.92);
  ELECHOUSE_cc1101.SetRx();
  rf.enableReceive(PIN_GDO2);
  irrecv.enableIRIn();
  irsend.begin();
  servo.attach(PIN_SERVO);
  loadRules();
  JsonDocument h;
  h["ev"] = "hello"; h["fw"] = "polyglot-hub 0.1";
  int n = 0;
  for (auto& r : rules) n += r.used;
  h["rules"] = n;
  emit(h);
}

void loop() {
  static String buf;
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n') { handle(buf); buf = ""; }
    else if (buf.length() < 2048) buf += c;
  }
  if (rf.available()) {
    uint32_t t0 = micros();
    char cmd[20];
    snprintf(cmd, sizeof cmd, "0x%lX", (unsigned long)rf.getReceivedValue());
    onCode("rf433", "RCS" + String(rf.getReceivedProtocol()), "b" + String(rf.getReceivedBitlength()), cmd, t0);
    rf.resetAvailable();
  }
  if (irrecv.decode(&ir)) {
    uint32_t t0 = micros();
    if (ir.decode_type != decode_type_t::UNKNOWN && !ir.repeat) {
      String cmd = "0x";
      if (hasACState(ir.decode_type)) {
        for (uint16_t i = 0; i < ir.bits / 8; i++) { char b[3]; snprintf(b, 3, "%02X", ir.state[i]); cmd += b; }
      } else cmd += uint64ToString(ir.value, 16);
      onCode("ir", typeToString(ir.decode_type), "b" + String(ir.bits), cmd, t0);
    }
    irrecv.resume();
  }
}

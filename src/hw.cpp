#include "hw.h"
#include <time.h>

namespace hw {

static bool     _edge[K_COUNT];
static bool     _held[K_COUNT];
static bool     _any = false;
static uint8_t  _bright = 4;

// ---------------------------------------------------------------- clock

static uint32_t _epochAtSet = 0;
static uint32_t _millisAtSet = 0;
static bool     _clockValid = false;

void clockSet(uint32_t epochLocal) {
  _epochAtSet  = epochLocal;
  _millisAtSet = millis();
  _clockValid  = true;
}

bool clockValid() { return _clockValid; }

Clock clockNow() {
  Clock c = {};
  time_t t = (time_t)(_epochAtSet + (millis() - _millisAtSet) / 1000);
  struct tm lt;
  gmtime_r(&t, &lt);   // epoch is already local-adjusted by the caller
  c.hour = lt.tm_hour; c.minute = lt.tm_min; c.second = lt.tm_sec;
  c.day = lt.tm_mday;  c.month = lt.tm_mon + 1;
  c.weekday = lt.tm_wday;
  c.year = lt.tm_year + 1900;
  return c;
}

// ---------------------------------------------------------------- input

// The keyboard only reports on change, so a state read gives the full set
// of keys currently down. Rising edges are that set minus the last one.
static void latchKeys() {
  for (int i = 0; i < K_COUNT; i++) _edge[i] = false;

  if (!M5Cardputer.Keyboard.isChange()) return;

  bool now[K_COUNT] = {false};
  if (M5Cardputer.Keyboard.isPressed()) {
    auto st = M5Cardputer.Keyboard.keysState();
    now[K_ENTER] = st.enter;
    now[K_ESC]   = st.esc;
    now[K_TAB]   = st.tab;
    now[K_SPACE] = st.space;
    now[K_UP]    = st.up;
    now[K_DOWN]  = st.down;
    for (char ch : st.word) {
      switch (ch) {
        case 'y': now[K_APPROVE] = true; break;
        case 'n': now[K_DENY]    = true; break;
        case 'm': now[K_MENU]    = true; break;
        case 'z': now[K_DIZZY]   = true; break;
        default: break;
      }
    }
    // Backspace is a second, more reachable deny.
    if (st.backspace || st.del) now[K_DENY] = true;
  }

  for (int i = 0; i < K_COUNT; i++) {
    _edge[i] = now[i] && !_held[i];
    _held[i] = now[i];
    if (_edge[i]) _any = true;
  }
}

void update() {
  M5Cardputer.update();
  _any = false;
  latchKeys();
  if (M5Cardputer.BtnA.wasPressed()) _any = true;
}

bool pressed(Key k) { return _edge[k]; }
bool anyPressed()   { return _any; }

bool buttonShort() { return M5Cardputer.BtnA.wasReleased() && !M5Cardputer.BtnA.pressedFor(600); }
bool buttonHeld(uint32_t ms) { return M5Cardputer.BtnA.pressedFor(ms); }

// ---------------------------------------------------------------- output

void beep(uint16_t freq, uint16_t durMs) {
  M5Cardputer.Speaker.tone(freq, durMs);
}

void setBrightness(uint8_t level) {
  _bright = level;
  M5Cardputer.Display.setBrightness(35 + level * 55);   // 35..255
}

void screenOff() { M5Cardputer.Display.setBrightness(0); }
void screenOn(uint8_t level) { setBrightness(level); }

void powerOff() {
  M5Cardputer.Power.powerOff();
  // Cardputer has no PMIC latch to cut its own rail, so if powerOff()
  // returns we are still on. Deep sleep is the closest thing to off.
  M5Cardputer.Power.deepSleep();
}

int  batteryPct()        { return (int)M5Cardputer.Power.getBatteryLevel(); }
int  batteryMilliVolts() { return (int)M5Cardputer.Power.getBatteryVoltage(); }
bool charging()          { return M5Cardputer.Power.isCharging() == m5::Power_Class::is_charging; }

// ---------------------------------------------------------------- setup

void begin() {
  auto cfg = M5.config();
  M5Cardputer.begin(cfg, true);
  M5Cardputer.Display.setRotation(1);        // 240x135 landscape
  M5Cardputer.Display.setColorDepth(16);
  // The 6x8 GLCD font all the layout arithmetic assumes.
  M5Cardputer.Display.setFont(&fonts::Font0);
  M5Cardputer.Display.setTextSize(1);
  M5Cardputer.Speaker.setVolume(120);
  setBrightness(_bright);
}

}  // namespace hw

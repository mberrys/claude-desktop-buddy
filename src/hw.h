#pragma once
#include <M5Cardputer.h>
#include <stdint.h>

// Cardputer hardware layer. Everything board-specific lives behind this
// header: the display surface types, the keyboard, power, and the software
// clock that stands in for the RTC the Cardputer doesn't have.

// Both M5GFX (the panel) and M5Canvas (an off-screen sprite) derive from
// LovyanGFX, so drawing code can target either through one pointer type.
using Gfx    = LovyanGFX;
using Sprite = M5Canvas;

// Landscape. The pet lives in the left column, UI in the panel to its right.
constexpr int SCREEN_W = 240;
constexpr int SCREEN_H = 135;
constexpr int PET_W    = 112;          // left column reserved for the pet
constexpr int PANEL_X  = PET_W + 4;
constexpr int PANEL_W  = SCREEN_W - PANEL_X;
constexpr int PANEL_COLS = PANEL_W / 6;   // 6px per glyph in the 6x8 font

// The 6x8 font this UI is laid out around, matched to what the StickC build
// used, so all the pixel arithmetic in the draw code still holds.
constexpr int CHAR_W = 6;
constexpr int CHAR_H = 8;

// TFT_eSPI's named colors are gone with the StickC library; the few this
// project used are just RGB565 literals.
constexpr uint16_t COL_GREEN = 0x07E0;
constexpr uint16_t COL_RED   = 0xF800;

namespace hw {

// Every key the UI reacts to. Edges only — a held key fires once.
enum Key : uint8_t {
  K_ENTER,      // select / approve
  K_ESC,        // back / close
  K_TAB,        // next screen
  K_SPACE,      // next page
  K_UP,
  K_DOWN,
  K_APPROVE,    // 'y'
  K_DENY,       // 'n'
  K_MENU,       // 'm'
  K_DIZZY,      // 'z' — the Cardputer has no IMU, so shake-to-dizzy is a key
  K_COUNT
};

void begin();

// Call once per loop. Latches key edges and updates the button.
void update();

// True once per physical press. Consuming is non-destructive: several
// callers may test the same key within one loop iteration.
bool pressed(Key k);

// Any key or button went down this tick — used to wake the screen.
bool anyPressed();

// The top button on the StampS3 (the one poking through the case).
bool buttonShort();
bool buttonHeld(uint32_t ms);

void beep(uint16_t freq, uint16_t durMs);

// 0..4, matching the brightness setting the menu exposes.
void setBrightness(uint8_t level);
void screenOff();
void screenOn(uint8_t level);

void powerOff();

int  batteryPct();
int  batteryMilliVolts();
bool charging();

// ---- software clock -------------------------------------------------
// The Cardputer has no RTC, so the bridge's time sync is held in RAM and
// advanced from millis(). It does not survive a reboot, which is why
// clockValid() gates every clock UI.
struct Clock {
  uint8_t  hour, minute, second;
  uint8_t  day, month, weekday;   // weekday 0 = Sunday
  uint16_t year;
};

void  clockSet(uint32_t epochLocal);
bool  clockValid();
Clock clockNow();

}  // namespace hw

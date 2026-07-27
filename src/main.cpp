#include "hw.h"
#include <LittleFS.h>
#include <stdarg.h>
#include "ble_bridge.h"
#include "data.h"
#include "buddy.h"

Sprite spr(&M5Cardputer.Display);

// Advertise as "Claude-XXXX" (last two MAC bytes) so multiple devices in
// one room are distinguishable in the host's picker. Name persists in
// btName for the BLUETOOTH info page.
static char btName[16] = "Claude";
static void startBt() {
  uint8_t mac[6] = {0};
  if (esp_read_mac(mac, ESP_MAC_BT) != ESP_OK) esp_read_mac(mac, ESP_MAC_WIFI_STA);
  snprintf(btName, sizeof(btName), "Claude-%02X%02X", mac[4], mac[5]);
  bleInit(btName);
}

#include "character.h"
#include "stats.h"

const int W = SCREEN_W, H = SCREEN_H;

// Colors used across multiple UI surfaces
const uint16_t HOT   = 0xFA20;   // red-orange: warnings, impatience, deny
const uint16_t PANEL = 0x2104;   // overlay panel background

enum PersonaState { P_SLEEP, P_IDLE, P_BUSY, P_ATTENTION, P_CELEBRATE, P_DIZZY, P_HEART };
const char* stateNames[] = { "sleep", "idle", "busy", "attention", "celebrate", "dizzy", "heart" };

TamaState    tama;
PersonaState baseState   = P_SLEEP;
PersonaState activeState = P_SLEEP;
uint32_t     oneShotUntil = 0;
unsigned long t = 0;

// Menu
bool    menuOpen    = false;
uint8_t menuSel     = 0;
uint8_t brightLevel = 4;           // 0..4

enum DisplayMode { DISP_NORMAL, DISP_PET, DISP_INFO, DISP_CLOCK, DISP_COUNT };
uint8_t displayMode = DISP_NORMAL;
uint8_t infoPage = 0;
uint8_t petPage = 0;
const uint8_t PET_PAGES = 2;
uint8_t msgScroll = 0;
uint16_t lastLineGen = 0;
char     lastPromptId[40] = "";
uint32_t lastInteractMs = 0;
bool     screenOff = false;
bool     buddyMode = false;
bool     gifAvailable = false;
const uint8_t SPECIES_GIF = 0xFF;   // species NVS sentinel: use the installed GIF

// Cycle GIF (if installed) → ASCII species 0..N-1 → GIF. Persisted to the
// existing "species" NVS key; 0xFF means GIF mode.
static void nextPet() {
  uint8_t n = buddySpeciesCount();
  if (!buddyMode) {                          // GIF → species 0
    buddyMode = true;
    buddySetSpeciesIdx(0);
    speciesIdxSave(0);
  } else if (buddySpeciesIdx() + 1 >= n && gifAvailable) {  // last species → GIF
    buddyMode = false;
    speciesIdxSave(SPECIES_GIF);
  } else {                                   // species i → species i+1
    buddyNextSpecies();
  }
  characterInvalidate();
  if (buddyMode) buddyInvalidate();
}
uint32_t wakeTransitionUntil = 0;
const uint32_t SCREEN_OFF_MS = 30000;

// The Cardputer has no IMU, so there is no face-down nap. The pet naps
// while the screen is off instead, which is the same idea: energy refills
// when you are not looking at it.
bool     napping = false;
uint32_t napStartMs = 0;
uint32_t promptArrivedMs = 0;
bool     responseSent = false;

static void applyBrightness() { hw::setBrightness(brightLevel); }

static void wake() {
  lastInteractMs = millis();
  if (screenOff) {
    hw::screenOn(brightLevel);
    screenOff = false;
    wakeTransitionUntil = millis() + 12000;
    if (napping) {
      napping = false;
      statsOnNapEnd((millis() - napStartMs) / 1000);
      statsOnWake();
    }
  }
}

static void sleepScreen() {
  hw::screenOff();
  screenOff = true;
  napping = true;
  napStartMs = millis();
}

static void beep(uint16_t freq, uint16_t dur) {
  if (settings().sound) hw::beep(freq, dur);
}

static void sendCmd(const char* json) {
  Serial.println(json);
  size_t n = strlen(json);
  bleWrite((const uint8_t*)json, n);
  bleWrite((const uint8_t*)"\n", 1);
}

const uint8_t INFO_PAGES = 6;
const uint8_t INFO_PG_KEYS    = 1;
const uint8_t INFO_PG_CREDITS = 5;

void applyDisplayMode() {
  // Clear the whole sprite on mode switch. The per-screen draws clear
  // their own regions, but switching away from one leaves its stale
  // pixels behind. A full clear is cheap and guarantees no leftovers.
  spr.fillSprite(0x0000);
  characterInvalidate();
  if (buddyMode) buddyInvalidate();
}

const char* menuItems[] = { "settings", "turn off", "help", "about", "demo", "close" };
const uint8_t MENU_N = 6;

bool    settingsOpen = false;
uint8_t settingsSel  = 0;
const char* settingsItems[] = { "brightness", "sound", "bluetooth", "wifi", "transcript", "ascii pet", "reset", "back" };
const uint8_t SETTINGS_N = 8;

bool    resetOpen = false;
uint8_t resetSel  = 0;
const char* resetItems[] = { "delete char", "factory reset", "back" };
const uint8_t RESET_N = 3;
static uint32_t resetConfirmUntil = 0;
static uint8_t  resetConfirmIdx = 0xFF;

static void applySetting(uint8_t idx) {
  Settings& s = settings();
  switch (idx) {
    case 0:
      brightLevel = (brightLevel + 1) % 5;
      applyBrightness();
      return;
    case 1: s.sound = !s.sound; break;
    case 2:
      // BT toggle is a stored preference only — BLE stays live. Turning
      // BLE off cleanly would require tearing down the BLE stack which
      // the Arduino BLE library doesn't do reliably.
      s.bt = !s.bt;
      break;
    case 3: s.wifi = !s.wifi; break;   // stored only — no WiFi stack linked
    case 4: s.hud = !s.hud; break;
    case 5: nextPet(); return;
    case 6: resetOpen = true; resetSel = 0; resetConfirmIdx = 0xFF; return;
    case 7: settingsOpen = false; characterInvalidate(); return;
  }
  settingsSave();
}

// Press-twice confirm: first press arms (label flips to "really?"), second
// within 3s executes. Moving the selection clears the arm.
static void applyReset(uint8_t idx) {
  uint32_t now = millis();
  bool armed = (resetConfirmIdx == idx) && (int32_t)(now - resetConfirmUntil) < 0;

  if (idx == 2) { resetOpen = false; return; }

  if (!armed) {
    resetConfirmIdx = idx;
    resetConfirmUntil = now + 3000;
    beep(1400, 60);
    return;
  }

  beep(800, 200);
  if (idx == 0) {
    // delete char: wipe /characters/, reboot into ASCII mode
    File d = LittleFS.open("/characters");
    if (d && d.isDirectory()) {
      File e;
      while ((e = d.openNextFile())) {
        char path[80];
        snprintf(path, sizeof(path), "/characters/%s", e.name());
        if (e.isDirectory()) {
          File f;
          while ((f = e.openNextFile())) {
            char fp[128];
            snprintf(fp, sizeof(fp), "%s/%s", path, f.name());
            f.close();
            LittleFS.remove(fp);
          }
          e.close();
          LittleFS.rmdir(path);
        } else {
          e.close();
          LittleFS.remove(path);
        }
      }
      d.close();
    }
  } else {
    // factory reset: NVS namespace wipe + filesystem format + BLE bonds.
    _prefs.begin("buddy", false);
    _prefs.clear();
    _prefs.end();
    LittleFS.format();
    bleClearBonds();
  }
  delay(300);
  ESP.restart();
}

// Footer hint row inside a menu panel.
const int MENU_HINT_H = 12;
static void drawMenuHints(const Palette& p, int mx, int mw, int hy) {
  spr.drawFastHLine(mx + 6, hy - 4, mw - 12, p.textDim);
  spr.setTextColor(p.textDim, PANEL);
  spr.setCursor(mx + 8, hy);
  spr.print("up/dn move   enter ok");
}

// Overlay panels are centered on the whole screen and sized to fit the
// 135px height: 10px rows rather than the StickC's 14.
const int MENU_ROW_H = 10;
static void panelGeom(uint8_t n, int& mx, int& my, int& mw, int& mh) {
  mw = 150;
  mh = 12 + n * MENU_ROW_H + MENU_HINT_H;
  mx = (W - mw) / 2;
  my = (H - mh) / 2;
}

static void drawSettings() {
  const Palette& p = characterPalette();
  int mx, my, mw, mh;
  panelGeom(SETTINGS_N, mx, my, mw, mh);
  spr.fillRoundRect(mx, my, mw, mh, 4, PANEL);
  spr.drawRoundRect(mx, my, mw, mh, 4, p.textDim);
  spr.setTextSize(1);
  Settings& s = settings();
  bool vals[] = { s.sound, s.bt, s.wifi, s.hud };
  for (int i = 0; i < SETTINGS_N; i++) {
    bool sel = (i == settingsSel);
    spr.setTextColor(sel ? p.text : p.textDim, PANEL);
    spr.setCursor(mx + 6, my + 6 + i * MENU_ROW_H);
    spr.print(sel ? "> " : "  ");
    spr.print(settingsItems[i]);
    spr.setCursor(mx + mw - 36, my + 6 + i * MENU_ROW_H);
    spr.setTextColor(p.textDim, PANEL);
    if (i == 0) {
      spr.printf("%u/4", brightLevel);
    } else if (i >= 1 && i <= 4) {
      spr.setTextColor(vals[i-1] ? COL_GREEN : p.textDim, PANEL);
      spr.print(vals[i-1] ? " on" : "off");
    } else if (i == 5) {
      uint8_t total = buddySpeciesCount() + (gifAvailable ? 1 : 0);
      uint8_t pos   = buddyMode ? buddySpeciesIdx() + 1 : total;
      spr.printf("%u/%u", pos, total);
    }
  }
  drawMenuHints(p, mx, mw, my + mh - 10);
}

static void drawReset() {
  const Palette& p = characterPalette();
  int mx, my, mw, mh;
  panelGeom(RESET_N, mx, my, mw, mh);
  spr.fillRoundRect(mx, my, mw, mh, 4, PANEL);
  spr.drawRoundRect(mx, my, mw, mh, 4, HOT);
  spr.setTextSize(1);
  for (int i = 0; i < RESET_N; i++) {
    bool sel = (i == resetSel);
    spr.setTextColor(sel ? p.text : p.textDim, PANEL);
    spr.setCursor(mx + 6, my + 6 + i * MENU_ROW_H);
    spr.print(sel ? "> " : "  ");
    bool armed = (i == resetConfirmIdx) &&
                 (int32_t)(millis() - resetConfirmUntil) < 0;
    if (armed) spr.setTextColor(HOT, PANEL);
    spr.print(armed ? "really?" : resetItems[i]);
  }
  drawMenuHints(p, mx, mw, my + mh - 10);
}

void menuConfirm() {
  switch (menuSel) {
    case 0: settingsOpen = true; menuOpen = false; settingsSel = 0; break;
    case 1: hw::powerOff(); break;
    case 2:
    case 3:
      menuOpen = false;
      displayMode = DISP_INFO;
      infoPage = (menuSel == 2) ? INFO_PG_KEYS : INFO_PG_CREDITS;
      applyDisplayMode();
      break;
    case 4: dataSetDemo(!dataDemo()); break;
    case 5: menuOpen = false; characterInvalidate(); break;
  }
}

void drawMenu() {
  const Palette& p = characterPalette();
  int mx, my, mw, mh;
  panelGeom(MENU_N, mx, my, mw, mh);
  spr.fillRoundRect(mx, my, mw, mh, 4, PANEL);
  spr.drawRoundRect(mx, my, mw, mh, 4, p.textDim);
  spr.setTextSize(1);
  for (int i = 0; i < MENU_N; i++) {
    bool sel = (i == menuSel);
    spr.setTextColor(sel ? p.text : p.textDim, PANEL);
    spr.setCursor(mx + 6, my + 6 + i * MENU_ROW_H);
    spr.print(sel ? "> " : "  ");
    spr.print(menuItems[i]);
    if (i == 4) spr.print(dataDemo() ? "  on" : "  off");
  }
  drawMenuHints(p, mx, mw, my + mh - 10);
}

// ── panel helpers ────────────────────────────────────────────────────
// Everything except the pet column draws through these, so the panel
// origin is defined in exactly one place.

static void panelClear(const Palette& p) {
  spr.fillRect(PANEL_X - 4, 0, W - PANEL_X + 4, H, p.bg);
  spr.drawFastVLine(PANEL_X - 4, 0, H, p.textDim);
}

static void panelHeader(const Palette& p, int& y, const char* title,
                        uint8_t page, uint8_t pages) {
  spr.setTextColor(p.text, p.bg);
  spr.setCursor(PANEL_X, y);
  spr.print(title);
  if (pages > 1) {
    spr.setTextColor(p.textDim, p.bg);
    spr.setCursor(W - 24, y);
    spr.printf("%u/%u", page + 1, pages);
  }
  y += 12;
}

static const char* const MON[] = {
  "Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"
};
static const char* const DOW[] = {"Sun","Mon","Tue","Wed","Thu","Fri","Sat"};

// Clock face. On the StickC this appeared by itself while charging, keyed
// off USB voltage; the Cardputer has no VBUS sense, so it is a screen you
// cycle to with Tab instead.
static void drawClock() {
  const Palette& p = characterPalette();
  panelClear(p);
  int y = 4;

  if (!dataRtcValid()) {
    panelHeader(p, y, "Clock", 0, 1);
    spr.setTextColor(p.textDim, p.bg);
    spr.setCursor(PANEL_X, y);      spr.print("no time yet.");
    spr.setCursor(PANEL_X, y + 10); spr.print("the bridge sets");
    spr.setCursor(PANEL_X, y + 20); spr.print("it on connect.");
    return;
  }

  hw::Clock c = hw::clockNow();
  char hm[6]; snprintf(hm, sizeof(hm), "%02u:%02u", c.hour, c.minute);
  spr.setTextColor(p.text, p.bg);
  spr.setTextSize(3);
  spr.setCursor(PANEL_X + 2, 34);
  spr.print(hm);
  spr.setTextSize(1);
  spr.setTextColor(p.textDim, p.bg);
  spr.setCursor(PANEL_X + 2, 62);
  spr.printf(":%02u", c.second);
  uint8_t mi = (c.month >= 1 && c.month <= 12) ? c.month - 1 : 0;
  spr.setCursor(PANEL_X + 2, 78);
  spr.printf("%s %s %02u", DOW[c.weekday % 7], MON[mi], c.day);
}

PersonaState derive(const TamaState& s) {
  if (!s.connected)            return P_IDLE;
  if (s.sessionsWaiting > 0)   return P_ATTENTION;
  if (s.recentlyCompleted)     return P_CELEBRATE;
  if (s.sessionsRunning >= 3)  return P_BUSY;
  return P_IDLE;   // connected, 0+ sessions, nothing urgent — hang out
}

void triggerOneShot(PersonaState s, uint32_t durMs) {
  activeState = s;
  oneShotUntil = millis() + durMs;
}

void drawPasskey() {
  const Palette& p = characterPalette();
  spr.fillSprite(p.bg);
  spr.setTextSize(1);
  spr.setTextColor(p.textDim, p.bg);
  spr.setCursor(8, 24);  spr.print("BLUETOOTH PAIRING");
  spr.setTextSize(3);
  spr.setTextColor(p.text, p.bg);
  char b[8]; snprintf(b, sizeof(b), "%06lu", (unsigned long)blePasskey());
  spr.setCursor((W - 18 * 3) / 2, 56);
  spr.print(b);
  spr.setTextSize(1);
  spr.setTextColor(p.textDim, p.bg);
  spr.setCursor(8, 104); spr.print("enter this on the computer");
}

void drawInfo() {
  const Palette& p = characterPalette();
  panelClear(p);
  spr.setTextSize(1);
  int y = 4;
  auto ln = [&](const char* fmt, ...) {
    char b[32]; va_list a; va_start(a, fmt); vsnprintf(b, sizeof(b), fmt, a); va_end(a);
    spr.setCursor(PANEL_X, y); spr.print(b); y += 8;
  };

  if (infoPage == 0) {
    panelHeader(p, y, "About", infoPage, INFO_PAGES);
    spr.setTextColor(p.textDim, p.bg);
    ln("I watch your");
    ln("coding sessions.");
    y += 4;
    ln("I sleep when");
    ln("nothing's happening,");
    ln("wake when you start,");
    ln("get impatient when");
    ln("approvals pile up.");
    y += 4;
    spr.setTextColor(p.text, p.bg);
    ln("enter approves a");
    ln("prompt from here.");

  } else if (infoPage == INFO_PG_KEYS) {
    panelHeader(p, y, "Keys", infoPage, INFO_PAGES);
    spr.setTextColor(p.text, p.bg);    ln("enter  approve");
    spr.setTextColor(p.textDim, p.bg); ln("       select");
    spr.setTextColor(p.text, p.bg);    ln("esc/n  deny, back");
    spr.setTextColor(p.text, p.bg);    ln("tab    next screen");
    spr.setTextColor(p.text, p.bg);    ln("space  next page");
    spr.setTextColor(p.text, p.bg);    ln("up/dn  scroll");
    spr.setTextColor(p.text, p.bg);    ln("m      menu");
    spr.setTextColor(p.text, p.bg);    ln("z      dizzy");
    y += 4;
    spr.setTextColor(p.text, p.bg);    ln("side button");
    spr.setTextColor(p.textDim, p.bg); ln("  tap  screen off");
    ln("  hold power off");

  } else if (infoPage == 2) {
    panelHeader(p, y, "Sessions", infoPage, INFO_PAGES);
    spr.setTextColor(p.textDim, p.bg);
    ln("  sessions  %u", tama.sessionsTotal);
    ln("  running   %u", tama.sessionsRunning);
    ln("  waiting   %u", tama.sessionsWaiting);
    y += 6;
    spr.setTextColor(p.text, p.bg);
    ln("LINK");
    spr.setTextColor(p.textDim, p.bg);
    ln("  via       %s", dataScenarioName());
    ln("  ble       %s", !bleConnected() ? "-" : bleSecure() ? "encrypted" : "OPEN");
    uint32_t age = (millis() - tama.lastUpdated) / 1000;
    ln("  last msg  %lus", (unsigned long)age);
    ln("  state     %s", stateNames[activeState]);

  } else if (infoPage == 3) {
    panelHeader(p, y, "Device", infoPage, INFO_PAGES);

    int pct = hw::batteryPct();
    int mV  = hw::batteryMilliVolts();
    bool chg = hw::charging();

    spr.setTextColor(p.text, p.bg);
    spr.setTextSize(2);
    spr.setCursor(PANEL_X, y);
    spr.printf("%d%%", pct);
    spr.setTextSize(1);
    spr.setTextColor(chg ? COL_GREEN : p.textDim, p.bg);
    spr.setCursor(PANEL_X + 48, y + 4);
    spr.print(chg ? "charging" : "battery");
    y += 20;

    spr.setTextColor(p.textDim, p.bg);
    ln("  battery  %d.%02dV", mV/1000, (mV%1000)/10);
    y += 6;

    spr.setTextColor(p.text, p.bg);
    ln("SYSTEM");
    spr.setTextColor(p.textDim, p.bg);
    if (ownerName()[0]) ln("  owner    %s", ownerName());
    uint32_t up = millis() / 1000;
    ln("  uptime   %luh %02lum", up / 3600, (up / 60) % 60);
    ln("  heap     %uKB", ESP.getFreeHeap() / 1024);
    ln("  bright   %u/4", brightLevel);
    ln("  bt       %s", settings().bt ? (dataBtActive() ? "linked" : "on") : "off");

  } else if (infoPage == 4) {
    panelHeader(p, y, "Bluetooth", infoPage, INFO_PAGES);
    bool linked = settings().bt && dataBtActive();

    spr.setTextColor(linked ? COL_GREEN : (settings().bt ? HOT : p.textDim), p.bg);
    spr.setTextSize(2);
    spr.setCursor(PANEL_X, y);
    spr.print(linked ? "linked" : (settings().bt ? "discover" : "off"));
    spr.setTextSize(1);
    y += 20;

    spr.setTextColor(p.text, p.bg);
    ln("  %s", btName);
    spr.setTextColor(p.textDim, p.bg);
    uint8_t mac[6] = {0};
    if (esp_read_mac(mac, ESP_MAC_BT) != ESP_OK) esp_read_mac(mac, ESP_MAC_WIFI_STA);
    ln("  %02X:%02X:%02X", mac[0], mac[1], mac[2]);
    ln("  %02X:%02X:%02X", mac[3], mac[4], mac[5]);
    y += 6;

    if (linked) {
      uint32_t age = (millis() - tama.lastUpdated) / 1000;
      ln("  last msg  %lus", (unsigned long)age);
    } else if (settings().bt) {
      spr.setTextColor(p.text, p.bg);
      ln("TO PAIR");
      spr.setTextColor(p.textDim, p.bg);
      ln(" pair in Windows");
      ln(" Bluetooth settings,");
      ln(" then run the");
      ln(" bridge daemon.");
    }

  } else {
    panelHeader(p, y, "Credits", infoPage, INFO_PAGES);
    spr.setTextColor(p.textDim, p.bg);
    ln("made by");
    spr.setTextColor(p.text, p.bg);
    ln("Felix Rieseberg");
    y += 8;
    spr.setTextColor(p.textDim, p.bg);
    ln("source");
    spr.setTextColor(p.text, p.bg);
    ln("github.com/");
    ln(" anthropics/");
    ln(" claude-desktop-buddy");
    y += 8;
    spr.setTextColor(p.textDim, p.bg);
    ln("hardware");
    ln("M5Cardputer");
    ln("ESP32-S3");
  }
}

// Greedy word-wrap into fixed-width rows. Continuation rows get a leading
// space. Returns number of rows written.
static uint8_t wrapInto(const char* in, char out[][24], uint8_t maxRows, uint8_t width) {
  uint8_t row = 0, col = 0;
  const char* p = in;
  while (*p && row < maxRows) {
    while (*p == ' ') p++;                     // skip leading spaces
    const char* w = p;
    while (*p && *p != ' ') p++;
    uint8_t wlen = p - w;
    if (wlen == 0) break;
    uint8_t need = (col > 0 ? 1 : 0) + wlen;
    if (col + need > width) {
      out[row][col] = 0;
      if (++row >= maxRows) return row;
      out[row][0] = ' '; col = 1;              // continuation indent
    }
    if (col > 1 || (col == 1 && out[row][0] != ' ')) out[row][col++] = ' ';
    else if (col == 1 && row > 0) {}           // already have the indent space
    while (wlen > width - col) {
      uint8_t take = width - col;
      memcpy(&out[row][col], w, take); col += take; w += take; wlen -= take;
      out[row][col] = 0;
      if (++row >= maxRows) return row;
      out[row][0] = ' '; col = 1;
    }
    memcpy(&out[row][col], w, wlen); col += wlen;
  }
  if (col > 0 && row < maxRows) { out[row][col] = 0; row++; }
  return row;
}

static void drawApproval() {
  const Palette& p = characterPalette();
  panelClear(p);
  spr.setTextSize(1);

  spr.setTextColor(p.textDim, p.bg);
  spr.setCursor(PANEL_X, 4);
  uint32_t waited = (millis() - promptArrivedMs) / 1000;
  if (waited >= 10) spr.setTextColor(HOT, p.bg);
  spr.printf("approve? %lus", (unsigned long)waited);

  // Size 2 only if it fits one line of the panel (10 chars at 12px).
  int toolLen = strlen(tama.promptTool);
  spr.setTextColor(p.text, p.bg);
  spr.setTextSize(toolLen <= 10 ? 2 : 1);
  spr.setCursor(PANEL_X, 20);
  spr.print(tama.promptTool);
  spr.setTextSize(1);

  // Hint wraps into the panel width under the tool name.
  static char hint[4][24];
  uint8_t rows = wrapInto(tama.promptHint, hint, 4, PANEL_COLS);
  spr.setTextColor(p.textDim, p.bg);
  for (uint8_t i = 0; i < rows; i++) {
    spr.setCursor(PANEL_X, 44 + i * 9);
    spr.print(hint[i]);
  }

  if (responseSent) {
    spr.setTextColor(p.textDim, p.bg);
    spr.setCursor(PANEL_X, H - 12);
    spr.print("sent...");
  } else {
    spr.setTextColor(COL_GREEN, p.bg);
    spr.setCursor(PANEL_X, H - 22);
    spr.print("enter  approve");
    spr.setTextColor(HOT, p.bg);
    spr.setCursor(PANEL_X, H - 12);
    spr.print("esc/n  deny");
  }
}

static void tinyHeart(int x, int y, bool filled, uint16_t col) {
  if (filled) {
    spr.fillCircle(x - 2, y, 2, col);
    spr.fillCircle(x + 2, y, 2, col);
    spr.fillTriangle(x - 4, y + 1, x + 4, y + 1, x, y + 5, col);
  } else {
    spr.drawCircle(x - 2, y, 2, col);
    spr.drawCircle(x + 2, y, 2, col);
    spr.drawLine(x - 4, y + 1, x, y + 5, col);
    spr.drawLine(x + 4, y + 1, x, y + 5, col);
  }
}

static void drawPetStats(const Palette& p, int y) {
  spr.setTextSize(1);

  spr.setTextColor(p.textDim, p.bg);
  spr.setCursor(PANEL_X, y); spr.print("mood");
  uint8_t mood = statsMoodTier();
  uint16_t moodCol = (mood >= 3) ? COL_RED : (mood >= 2) ? HOT : p.textDim;
  for (int i = 0; i < 4; i++) tinyHeart(PANEL_X + 42 + i * 14, y + 3, i < mood, moodCol);

  y += 14;
  spr.setCursor(PANEL_X, y); spr.print("fed");
  uint8_t fed = statsFedProgress();
  for (int i = 0; i < 10; i++) {
    int px = PANEL_X + 30 + i * 8;
    if (i < fed) spr.fillCircle(px, y + 3, 2, p.body);
    else spr.drawCircle(px, y + 3, 2, p.textDim);
  }

  y += 14;
  spr.setCursor(PANEL_X, y); spr.print("energy");
  uint8_t en = statsEnergyTier();
  uint16_t enCol = (en >= 4) ? 0x07FF : (en >= 2) ? 0xFFE0 : HOT;
  for (int i = 0; i < 5; i++) {
    int px = PANEL_X + 46 + i * 12;
    if (i < en) spr.fillRect(px, y, 9, 6, enCol);
    else spr.drawRect(px, y, 9, 6, p.textDim);
  }

  y += 16;
  spr.fillRoundRect(PANEL_X, y, 40, 12, 3, p.body);
  spr.setTextColor(p.bg, p.body);
  spr.setCursor(PANEL_X + 5, y + 3); spr.printf("Lv %u", stats().level);

  y += 16;
  spr.setTextColor(p.textDim, p.bg);
  spr.setCursor(PANEL_X, y);      spr.printf("approved %u", stats().approvals);
  spr.setCursor(PANEL_X, y + 9);  spr.printf("denied   %u", stats().denials);
  uint32_t nap = stats().napSeconds;
  spr.setCursor(PANEL_X, y + 18); spr.printf("napped   %luh%02lum", nap/3600, (nap/60)%60);
  auto tokFmt = [&](const char* label, uint32_t v, int yPx) {
    spr.setCursor(PANEL_X, yPx);
    if (v >= 1000000)   spr.printf("%s%lu.%luM", label, v/1000000, (v/100000)%10);
    else if (v >= 1000) spr.printf("%s%lu.%luK", label, v/1000, (v/100)%10);
    else                spr.printf("%s%lu", label, v);
  };
  tokFmt("tokens   ", stats().tokens, y + 27);
  tokFmt("today    ", tama.tokensToday, y + 36);
}

static void drawPetHowTo(const Palette& p, int y) {
  spr.setTextSize(1);
  auto ln = [&](uint16_t c, const char* s) {
    spr.setTextColor(c, p.bg); spr.setCursor(PANEL_X, y); spr.print(s); y += 9;
  };
  auto gap = [&]() { y += 4; };

  ln(p.body,    "MOOD");
  ln(p.textDim, " approve fast = up");
  ln(p.textDim, " deny lots = down"); gap();

  ln(p.body,    "FED");
  ln(p.textDim, " 50K tokens =");
  ln(p.textDim, " level up + confetti"); gap();

  ln(p.body,    "ENERGY");
  ln(p.textDim, " refills while the");
  ln(p.textDim, " screen is off"); gap();

  ln(p.textDim, "idle 30s = off");
  ln(p.textDim, "any key = wake");
}

void drawPet() {
  const Palette& p = characterPalette();
  panelClear(p);
  int y = 4;

  // Header: owner's pet name, page counter
  spr.setTextSize(1);
  spr.setTextColor(p.text, p.bg);
  spr.setCursor(PANEL_X, y);
  if (ownerName()[0]) spr.printf("%s's %s", ownerName(), petName());
  else                spr.print(petName());
  spr.setTextColor(p.textDim, p.bg);
  spr.setCursor(W - 24, y);
  spr.printf("%u/%u", petPage + 1, PET_PAGES);
  y += 12;

  if (petPage == 0) drawPetStats(p, y);
  else drawPetHowTo(p, y);
}

void drawHUD() {
  if (tama.promptId[0]) { drawApproval(); return; }
  const Palette& p = characterPalette();
  const int SHOW = 12, LH = 9;
  panelClear(p);
  spr.setTextSize(1);

  if (tama.lineGen != lastLineGen) { msgScroll = 0; lastLineGen = tama.lineGen; wake(); }

  int y = 4;
  spr.setTextColor(p.text, p.bg);
  spr.setCursor(PANEL_X, y);
  spr.printf("%.20s", tama.msg);
  y += 12;

  if (tama.nLines == 0) return;

  // Wrap all transcript lines into a flat display buffer. Track which
  // transcript index each display row came from, so we can dim older ones.
  static char disp[32][24];
  static uint8_t srcOf[32];
  uint8_t nDisp = 0;
  for (uint8_t i = 0; i < tama.nLines && nDisp < 32; i++) {
    uint8_t got = wrapInto(tama.lines[i], &disp[nDisp], 32 - nDisp, PANEL_COLS);
    for (uint8_t j = 0; j < got; j++) srcOf[nDisp + j] = i;
    nDisp += got;
  }

  uint8_t maxBack = (nDisp > SHOW) ? (nDisp - SHOW) : 0;
  if (msgScroll > maxBack) msgScroll = maxBack;

  int end = (int)nDisp - msgScroll;
  int start = end - SHOW; if (start < 0) start = 0;
  uint8_t newest = tama.nLines - 1;
  for (int i = 0; start + i < end; i++) {
    uint8_t row = start + i;
    bool fresh = (srcOf[row] == newest) && (msgScroll == 0);
    spr.setTextColor(fresh ? p.text : p.textDim, p.bg);
    spr.setCursor(PANEL_X, y + i * LH);
    spr.print(disp[row]);
  }
  if (msgScroll > 0) {
    spr.setTextColor(p.body, p.bg);
    spr.setCursor(W - 18, H - 10);
    spr.printf("-%u", msgScroll);
  }
}

void setup() {
  hw::begin();
  startBt();
  applyBrightness();
  lastInteractMs = millis();
  statsLoad();
  settingsLoad();
  petNameLoad();
  buddyInit();

  // 240x135 at 16bpp is ~64KB. Fall back to PSRAM if the internal heap
  // can't spare it after the BLE stack is up.
  spr.setColorDepth(16);
  if (!spr.createSprite(W, H)) {
    spr.setPsram(true);
    spr.createSprite(W, H);
  }

  characterInit(nullptr);  // scan /characters/ for whatever is installed
  gifAvailable = characterLoaded();
  // species NVS: 0..N-1 = ASCII species, 0xFF = use GIF (also the default,
  // so a fresh install lands on the GIF). With no GIF installed, 0xFF falls
  // through to buddyInit()'s clamped default.
  buddyMode = !(gifAvailable && speciesIdxLoad() == SPECIES_GIF);
  applyDisplayMode();

  {
    const Palette& p = characterPalette();
    spr.fillSprite(p.bg);
    spr.setTextDatum(lgfx::middle_center);
    spr.setTextSize(2);
    if (ownerName()[0]) {
      char line[40];
      snprintf(line, sizeof(line), "%s's", ownerName());
      spr.setTextColor(p.text, p.bg);   spr.drawString(line, W/2, H/2 - 12);
      spr.setTextColor(p.body, p.bg);   spr.drawString(petName(), W/2, H/2 + 12);
    } else {
      // First boot, no owner pushed yet — say hi.
      spr.setTextColor(p.body, p.bg);   spr.drawString("Hello!", W/2, H/2 - 12);
      spr.setTextSize(1);
      spr.setTextColor(p.textDim, p.bg);
      spr.drawString("a buddy appears", W/2, H/2 + 12);
    }
    spr.setTextDatum(lgfx::top_left); spr.setTextSize(1);
    spr.pushSprite(0, 0);
    delay(1800);
  }

  Serial.printf("buddy: %s\n", buddyMode ? "ASCII mode" : "GIF character loaded");
}

// Selection movement shared by all three overlay panels.
static void moveSel(uint8_t& sel, uint8_t n, int delta) {
  beep(1800, 30);
  sel = (uint8_t)((sel + n + delta) % n);
}

void loop() {
  hw::update();
  t++;
  uint32_t now = millis();

  dataPoll(&tama);
  if (statsPollLevelUp()) triggerOneShot(P_CELEBRATE, 3000);
  baseState = derive(tama);

  // After waking the screen, hold sleep for 12s so users see the wake-up
  // animation. Urgent states (attention, celebrate, busy) override this.
  if (baseState == P_IDLE && (int32_t)(now - wakeTransitionUntil) < 0) baseState = P_SLEEP;

  if ((int32_t)(now - oneShotUntil) >= 0) activeState = baseState;

  // Prompt arrival: beep, reset response flag
  if (strcmp(tama.promptId, lastPromptId) != 0) {
    strncpy(lastPromptId, tama.promptId, sizeof(lastPromptId)-1);
    lastPromptId[sizeof(lastPromptId)-1] = 0;
    responseSent = false;
    if (tama.promptId[0]) {
      promptArrivedMs = millis();
      wake();
      beep(1200, 80);   // alert chirp
      // Jump to the approval screen no matter what was open — drawApproval
      // only runs from drawHUD which only runs in DISP_NORMAL.
      displayMode = DISP_NORMAL;
      menuOpen = settingsOpen = resetOpen = false;
      applyDisplayMode();
    }
  }

  bool inPrompt = tama.promptId[0] && !responseSent;

  // Any key wakes the screen; the keypress that woke it is swallowed so
  // waking doesn't also change screens.
  bool woke = false;
  if (hw::anyPressed()) {
    if (screenOff) woke = true;
    wake();
  }

  // Side button: tap toggles the screen, hold powers down.
  if (hw::buttonHeld(2000)) {
    beep(600, 200);
    hw::powerOff();
  } else if (hw::buttonShort()) {
    if (screenOff) wake();
    else sleepScreen();
  }

  if (!woke && !screenOff) {
    // ── approval decisions ──
    if (inPrompt && (hw::pressed(hw::K_ENTER) || hw::pressed(hw::K_APPROVE))) {
      char cmd[96];
      snprintf(cmd, sizeof(cmd), "{\"cmd\":\"permission\",\"id\":\"%s\",\"decision\":\"once\"}", tama.promptId);
      sendCmd(cmd);
      responseSent = true;
      uint32_t tookS = (millis() - promptArrivedMs) / 1000;
      statsOnApproval(tookS);
      beep(2400, 60);
      if (tookS < 5) triggerOneShot(P_HEART, 2000);
    } else if (inPrompt && (hw::pressed(hw::K_ESC) || hw::pressed(hw::K_DENY))) {
      char cmd[96];
      snprintf(cmd, sizeof(cmd), "{\"cmd\":\"permission\",\"id\":\"%s\",\"decision\":\"deny\"}", tama.promptId);
      sendCmd(cmd);
      responseSent = true;
      statsOnDenial();
      beep(600, 60);
    }

    // ── overlay panels ──
    else if (resetOpen) {
      if (hw::pressed(hw::K_DOWN))      { moveSel(resetSel, RESET_N, +1); resetConfirmIdx = 0xFF; }
      else if (hw::pressed(hw::K_UP))   { moveSel(resetSel, RESET_N, -1); resetConfirmIdx = 0xFF; }
      else if (hw::pressed(hw::K_ENTER)) { beep(2400, 30); applyReset(resetSel); }
      else if (hw::pressed(hw::K_ESC))   { resetOpen = false; }
    } else if (settingsOpen) {
      if (hw::pressed(hw::K_DOWN))       moveSel(settingsSel, SETTINGS_N, +1);
      else if (hw::pressed(hw::K_UP))    moveSel(settingsSel, SETTINGS_N, -1);
      else if (hw::pressed(hw::K_ENTER)) { beep(2400, 30); applySetting(settingsSel); }
      else if (hw::pressed(hw::K_ESC))   { settingsOpen = false; characterInvalidate(); }
    } else if (menuOpen) {
      if (hw::pressed(hw::K_DOWN))       moveSel(menuSel, MENU_N, +1);
      else if (hw::pressed(hw::K_UP))    moveSel(menuSel, MENU_N, -1);
      else if (hw::pressed(hw::K_ENTER)) { beep(2400, 30); menuConfirm(); }
      else if (hw::pressed(hw::K_ESC) || hw::pressed(hw::K_MENU)) {
        menuOpen = false; characterInvalidate();
      }
    }

    // ── screen navigation ──
    else {
      if (hw::pressed(hw::K_MENU)) {
        beep(800, 60);
        menuOpen = true;
        menuSel = 0;
      } else if (hw::pressed(hw::K_TAB)) {
        beep(1800, 30);
        displayMode = (displayMode + 1) % DISP_COUNT;
        applyDisplayMode();
      } else if (hw::pressed(hw::K_SPACE)) {
        beep(2400, 30);
        if (displayMode == DISP_INFO)     infoPage = (infoPage + 1) % INFO_PAGES;
        else if (displayMode == DISP_PET) petPage  = (petPage + 1) % PET_PAGES;
      } else if (hw::pressed(hw::K_DIZZY)) {
        // Stands in for the StickC's shake-to-dizzy: no IMU here.
        triggerOneShot(P_DIZZY, 2000);
      } else if (displayMode == DISP_NORMAL) {
        if (hw::pressed(hw::K_UP))        msgScroll = (msgScroll >= 30) ? 30 : msgScroll + 1;
        else if (hw::pressed(hw::K_DOWN)) msgScroll = (msgScroll == 0) ? 0 : msgScroll - 1;
      }
    }
  }

  static uint32_t lastPasskey = 0;
  uint32_t pk = blePasskey();
  if (pk && !lastPasskey) { wake(); beep(1800, 60); }
  lastPasskey = pk;

  if (!screenOff) {
    if (buddyMode) {
      buddyTick(activeState);
    } else if (characterLoaded()) {
      characterSetState(activeState);
      characterTick();
    } else {
      const Palette& p = characterPalette();
      spr.fillRect(0, 0, PET_W, H, p.bg);
      spr.setTextColor(p.textDim, p.bg);
      spr.setTextSize(1);
      if (xferActive()) {
        uint32_t done = xferProgress(), total = xferTotal();
        spr.setCursor(8, 50);
        spr.print("installing");
        spr.setCursor(8, 62);
        spr.printf("%luK / %luK", done/1024, total/1024);
        int barW = PET_W - 16;
        spr.drawRect(8, 76, barW, 8, p.textDim);
        if (total > 0) {
          int fill = (int)((uint64_t)barW * done / total);
          if (fill > 1) spr.fillRect(9, 77, fill - 1, 6, p.body);
        }
      } else {
        spr.setCursor(8, 60);
        spr.print("no character");
      }
    }

    if (blePasskey()) drawPasskey();
    else if (displayMode == DISP_INFO)  drawInfo();
    else if (displayMode == DISP_PET)   drawPet();
    else if (displayMode == DISP_CLOCK) drawClock();
    else if (settings().hud)            drawHUD();

    if (resetOpen) drawReset();
    else if (settingsOpen) drawSettings();
    else if (menuOpen) drawMenu();
    spr.pushSprite(0, 0);
  }

  // millis() not the cached `now`: wake() runs after `now` is captured, so
  // now - lastInteractMs underflows when a key is held → flicker.
  if (!screenOff && !inPrompt && millis() - lastInteractMs > SCREEN_OFF_MS) {
    sleepScreen();
  }

  delay(screenOff ? 100 : 16);
}

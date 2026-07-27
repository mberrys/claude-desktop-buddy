# claude-desktop-buddy

Claude for macOS and Windows can connect Claude Cowork and Claude Code to
maker devices over BLE, so developers and makers can build hardware that
displays permission prompts, recent messages, and other interactions. We've
been impressed by the creativity of the maker community around Claude -
providing a lightweight, opt-in API is our way of making it easier to build
fun little hardware devices that integrate with Claude.

> **Building your own device?** You don't need any of the code here. See
> **[REFERENCE.md](REFERENCE.md)** for the wire protocol: Nordic UART
> Service UUIDs, JSON schemas, and the folder push transport.

As an example, we built a desk pet on ESP32 that lives off permission
approvals and interaction with Claude. It sleeps when nothing's happening,
wakes when sessions start, gets visibly impatient when an approval prompt is
waiting, and lets you approve or deny right from the device.

<p align="center">
  <img src="docs/device.jpg" alt="the buddy firmware running" width="500">
</p>

## Hardware

The firmware targets the **M5Cardputer** (ESP32-S3) with the Arduino
framework, driving its 240×135 landscape screen and built-in keyboard. All
board-specific code sits behind `src/hw.h`, so porting to another ESP32
board means reimplementing that one header's worth of display, input, and
power calls.

> Earlier revisions of this firmware targeted the M5StickC Plus. That build
> is gone: the Cardputer has no IMU and no AXP192, so shake-to-dizzy and
> face-down napping were replaced rather than abstracted (see
> [Controls](#controls)). Git history has the StickC version if you want it.

## Flashing

Install
[PlatformIO Core](https://docs.platformio.org/en/latest/core/installation/),
then:

```bash
pio run -t upload
```

If you're starting from a previously-flashed device, wipe it first:

```bash
pio run -t erase && pio run -t upload
```

Once running, you can also wipe everything from the device itself: **m →
settings → reset → factory reset → press enter twice**.

## Pairing

### With Cursor on Windows

Run the host-side bridge in **[bridge/](bridge/README.md)** — it speaks the
same protocol the Claude desktop apps do, driven by Cursor's agent hooks, so
shell commands and MCP tool calls come to the device for approval.

### With the Claude desktop apps

Enable developer mode (**Help → Troubleshooting → Enable Developer Mode**),
open **Developer → Open Hardware Buddy…**, click **Connect**, and pick your
device from the list. The OS prompts for Bluetooth permission on first
connect; grant it.

<p align="center">
  <img src="docs/menu.png" alt="Developer → Open Hardware Buddy… menu item" width="420">
  <img src="docs/hardware-buddy-window.png" alt="Hardware Buddy window with Connect button and folder drop target" width="420">
</p>

Once paired, the bridge auto-reconnects whenever both sides are awake.

If discovery isn't finding the Cardputer:

- Make sure it's awake (any keypress)
- Check its settings menu → bluetooth is on

## Other hosts (Codex, ChatGPT desktop)

Only the Claude desktop apps implement the BLE bridge natively. To drive the
same firmware from **Codex CLI or ChatGPT desktop on Windows 11**, run
`tools/win_bridge` — a Python script that plays the desktop app's side of the
protocol: it connects to the stick as a BLE central, tails Codex's session
logs for activity, and exposes a loopback HTTP endpoint anything else can
push status (and approval prompts) into.

```powershell
cd tools\win_bridge
pip install -r requirements.txt
python -m buddy_bridge --scan     # confirm the stick is visible
python -m buddy_bridge --demo     # verify the link with fake traffic
python -m buddy_bridge --owner Michael
```

No firmware changes are needed. See
[tools/win_bridge/README.md](tools/win_bridge/README.md) for setup, what does
and doesn't round-trip (approvals are display-only from the log tail), and
troubleshooting.

## Controls

The Cardputer's keyboard replaces the StickC's two buttons.

|                        | Normal            | Pet / Info / Clock | Menus       | Approval    |
| ---------------------- | ----------------- | ------------------ | ----------- | ----------- |
| **enter**              |                   |                    | select      | **approve** |
| **y**                  |                   |                    |             | **approve** |
| **esc**                |                   |                    | close       | **deny**    |
| **n** / **backspace**  |                   |                    |             | **deny**    |
| **tab**                | next screen       | next screen        |             |             |
| **space**              |                   | next page          |             |             |
| **up / down**          | scroll transcript |                    | move        |             |
| **m**                  | menu              | menu               | close menu  |             |
| **z**                  | dizzy             |                    |             |             |
| **side button** (tap)  | screen on/off     |                    |             |             |
| **side button** (2s)   | power off         |                    |             |             |

The screen auto-powers-off after 30s of no interaction (kept on while an
approval prompt is up). Any keypress wakes it — and the keypress that wakes
the screen is swallowed, so waking never also changes screens.

**What the Cardputer can't do.** It has no IMU, so shake-to-dizzy became the
`z` key and face-down napping became "naps while the screen is off" (energy
still refills). It has no notification LED, so `attention` signals with a
chirp and the screen instead. And with no RTC, the clock is software-only:
the bridge sets it on connect and it resets on reboot.

## ASCII pets

Eighteen pets, each with seven animations (sleep, idle, busy, attention,
celebrate, dizzy, heart). Menu → "next pet" cycles them with a counter.
Choice persists to NVS.

## GIF pets

If you want a custom GIF character instead of an ASCII buddy, drag a
character pack folder onto the drop target in the Hardware Buddy window. The
app streams it over BLE and the device switches to GIF mode live. **Settings
→ delete char** reverts to ASCII mode.

A character pack is a folder with `manifest.json` and 96px-wide GIFs:

```json
{
  "name": "bufo",
  "colors": {
    "body": "#6B8E23",
    "bg": "#000000",
    "text": "#FFFFFF",
    "textDim": "#808080",
    "ink": "#000000"
  },
  "states": {
    "sleep": "sleep.gif",
    "idle": ["idle_0.gif", "idle_1.gif", "idle_2.gif"],
    "busy": "busy.gif",
    "attention": "attention.gif",
    "celebrate": "celebrate.gif",
    "dizzy": "dizzy.gif",
    "heart": "heart.gif"
  }
}
```

State values can be a single filename or an array. Arrays rotate: each
loop-end advances to the next GIF, useful for an idle activity carousel so
the home screen doesn't loop one clip forever.

GIFs are 96px wide, which fits the 112px pet column on the left of the
landscape screen. Height up to 135px renders full-size; taller packs (the
old portrait art went to ~140px) automatically drop to half scale rather
than getting cropped. Crop tight to the character — transparent margins
waste screen and shrink the sprite. `tools/prep_character.py` handles the
resize: feed it source GIFs at any sizes and it produces a 96px-wide set
where the character is the same scale in every state.

The whole folder must fit under 1.8MB —
`gifsicle --lossy=80 -O3 --colors 64` typically cuts 40–60%.

See `characters/bufo/` for a working example.

If you're iterating on a character and would rather skip the BLE round-trip,
`tools/flash_character.py characters/bufo` stages it into `data/` and runs
`pio run -t uploadfs` directly over USB.

## The seven states

| State       | Trigger                     | Feel                        |
| ----------- | --------------------------- | --------------------------- |
| `sleep`     | bridge not connected        | eyes closed, slow breathing |
| `idle`      | connected, nothing urgent   | blinking, looking around    |
| `busy`      | sessions actively running   | sweating, working           |
| `attention` | approval pending            | alert, **chirps**           |
| `celebrate` | level up (every 50K tokens) | confetti, bouncing          |
| `dizzy`     | you pressed `z`             | spiral eyes, wobbling       |
| `heart`     | approved in under 5s        | floating hearts             |

## Project layout

```
src/
  main.cpp       — loop, state machine, UI screens
  hw.h / hw.cpp  — all Cardputer-specific code: display, keyboard,
                   power, and the software clock (no RTC on this board)
  buddy.cpp      — ASCII species dispatch + render helpers
  buddies/       — one file per species, seven anim functions each
  ble_bridge.cpp — Nordic UART service, line-buffered TX/RX
  character.cpp  — GIF decode + render
  data.h         — wire protocol, JSON parse
  xfer.h         — folder push receiver
  stats.h        — NVS-backed stats, settings, owner, species choice
characters/      — example GIF character packs
tools/           — generators and converters
bridge/          — host-side bridge that drives the device from Cursor
```

## Availability

The BLE API is only available when the desktop apps are in developer mode
(**Help → Troubleshooting → Enable Developer Mode**). It's intended for
makers and developers and isn't an officially supported product feature.

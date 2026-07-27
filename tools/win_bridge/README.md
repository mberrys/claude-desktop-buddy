# Windows bridge — Codex / ChatGPT desktop

The firmware in this repo speaks a BLE protocol that the **Claude** desktop
apps implement natively. Codex CLI and ChatGPT desktop don't implement it —
they have no hardware-buddy feature and no BLE bridge to enable. So on
Windows 11 this script plays the part the Claude desktop app normally plays:
it connects to the stick as a BLE central, watches what Codex is doing, and
sends the same snapshots, turn entries, and commands the device already
understands.

**No firmware changes are needed.** The stick can't tell the difference.

## What you get

| Buddy feature                        | Works with Codex? |
| ------------------------------------ | ----------------- |
| sleep / idle / busy states           | yes               |
| session + running counts             | yes               |
| transcript lines, `msg` one-liner    | yes               |
| token counter, levels, celebrate     | yes               |
| approval prompts on the device       | see below         |
| approve/deny from the device buttons | see below         |
| character pack push (GIF pets)       | yes, `--push`     |
| time sync, owner name, status poll   | yes               |

### About approvals

Codex has no public API for answering an approval from outside its own UI, so
there are two levels of support:

- **Log tail (automatic).** If your Codex build logs approval requests to the
  session rollout, the device lights up `attention` and shows the command.
  Pressing A/B on the stick is recorded and clears the prompt on the display,
  but Codex itself still waits for your answer in the terminal. Display-only.
- **Control endpoint (scripted, fully round-trip).** Anything that can raise
  its own prompt — a wrapper script, an MCP tool, a CI gate — can `POST
  /prompt`, then block on `GET /decision?id=…&wait=60` to get back the button
  the user actually pressed. That path is a real remote approval.

## Setup

Windows 11, Python 3.10+, and a Bluetooth adapter Windows already sees.

```powershell
cd tools\win_bridge
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Pair the stick once through Windows first — **Settings → Bluetooth & devices
→ Add device → Bluetooth → Claude-XXXX**. The firmware requires an encrypted
link, so Windows prompts for a PIN; type the 6-digit passkey the stick
displays. WinRT will not hand out the GATT characteristics until this bond
exists.

Then:

```powershell
python -m buddy_bridge --scan                 # confirm it's visible
python -m buddy_bridge --demo                 # fake traffic, verifies the link
python -m buddy_bridge --owner Michael        # the real thing
```

`--demo` is the fastest way to tell a BLE problem from a Codex problem: it
cycles idle → busy → attention → done with no Codex involved.

## Options

```
--name-prefix Claude     advertised-name filter (matches the firmware default)
--address AA:BB:...      skip the name filter and connect to one device
--owner NAME             name the device greets you by
--sessions-dir PATH      override %USERPROFILE%\.codex\sessions
--no-codex               don't tail logs (control endpoint only)
--no-http / --host/--port  control endpoint (default 127.0.0.1:8787)
--no-pair                skip the bond attempt (unencrypted firmware forks)
--push FOLDER            stream a character pack, then keep running
--demo                   synthetic traffic
--scan                   list matching devices and exit
-v                       debug logging (shows every line on the wire)
```

Pushing a GIF pet works exactly like the desktop app's drop target:

```powershell
python -m buddy_bridge --push ..\..\characters\bufo
```

## Where the Codex data comes from

Codex CLI writes one JSONL rollout per session under
`%USERPROFILE%\.codex\sessions\YYYY\MM\DD\rollout-*.jsonl` (or
`$CODEX_HOME/sessions`). The bridge tails every log touched in the last 15
minutes and folds records into a snapshot: user/assistant messages become
transcript lines, `function_call` records become the `msg` line, `token_count`
records drive the token counter, and a log going quiet for 25 seconds drops
that session out of `running`.

The rollout schema has changed across Codex releases, so the parser is
deliberately loose — it handles both the nested `{"payload": {...}}` form and
the older flat records, and skips anything it doesn't recognise. If a future
release renames things, the worst case is a less detailed transcript, not a
crash. Logs that already exist when the bridge starts are seeked to the end,
so you only see live activity.

For a snappier end-of-turn transition, add the notify hook to
`%USERPROFILE%\.codex\config.toml`:

```toml
notify = ["python", "-m", "buddy_bridge.notify_hook"]
```

It fires the moment a turn completes instead of waiting for the next poll,
and silently does nothing when the bridge isn't running.

## Control endpoint

Bound to `127.0.0.1:8787`. No auth — anything that can reach the port can
approve a tool call, so don't bind it to a routable address.

```
POST /snapshot   {"total":1,"running":1,"msg":"building"}    merge fields
POST /entry      {"text":"yarn test"}                        push a transcript line
POST /tokens     {"n":1200}                                  add output tokens
POST /prompt     {"id":"x","tool":"Shell","hint":"rm -rf"}   raise an approval
POST /prompt/clear   {"id":"x"}                              drop it
GET  /decision?id=x&wait=30    -> {"decision":"once"|"deny"|null}
GET  /state                    -> the snapshot as sent to the device
```

This is also the answer for **ChatGPT desktop**, which exposes no local log or
hook to read: drive the buddy from whatever automation you already have
(a wrapper script, a scheduled task, an MCP tool) by POSTing to these routes.

Approval round-trip from a script:

```powershell
$body = '{"id":"deploy-1","tool":"Deploy","hint":"prod release"}'
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8787/prompt -Body $body -ContentType application/json
$answer = Invoke-RestMethod -Uri "http://127.0.0.1:8787/decision?id=deploy-1&wait=60"
if ($answer.decision -ne "once") { throw "denied on the buddy" }
```

## Tests

```powershell
python test_bridge.py
```

Covers snapshot clipping and token deltas, the Codex log parser (both schema
shapes, partial writes, garbage lines), every HTTP route, MTU-boundary
framing and notification reassembly, ack/timeout handling, and the folder
push sequence — all against fakes, so no device or Codex install is needed.

## Troubleshooting

- **`--scan` finds nothing but reports other BLE devices.** The stick is
  asleep or its bluetooth is off: press a button, then check settings →
  bluetooth on the device.
- **Connects, then drops immediately.** The bond is missing or stale. Remove
  the device in Windows Bluetooth settings, then on the stick do hold A →
  settings → reset → unpair, and pair again.
- **Connects but the screen stays asleep.** The device treats >30s without a
  snapshot as disconnected. Run with `-v`; if you see writes going out, the
  link is fine and the issue is that Codex isn't producing events.
- **No transcript lines.** Check `--sessions-dir` points at a directory that
  actually has `rollout-*.jsonl` files in it, and that the session started
  *after* the bridge did.
- **`--push` fails partway.** The pack must be a flat folder under 1.8MB.

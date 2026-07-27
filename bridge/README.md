# Cursor bridge (Windows 11)

Drives the buddy device (an M5Cardputer) from **Cursor** instead of the
Claude desktop app.

The device firmware doesn't change at all — this speaks the same BLE
protocol documented in [REFERENCE.md](../REFERENCE.md), so the pet sleeps,
works, and asks for approvals exactly as it does on the Claude side. What
changes is where the events come from: Cursor's
[agent hooks](https://cursor.com/docs/agent/hooks) instead of Claude
sessions.

You can approve or deny Cursor's shell commands and MCP tool calls from the
Cardputer's keyboard: **enter** approves, **esc** denies.

## How it fits together

```
Cursor ──stdin/stdout──> run_hook.py ──loopback TCP──> daemon ──BLE──> device
        (one process                  (JSON lines,      (holds the
         per event)                    token auth)       connection)
```

Cursor spawns a fresh process per hook event, but a BLE link has to outlive
those, hence the split. The daemon is the piece that plays the role the
Claude desktop app plays in REFERENCE.md: it sends the time sync, the owner
name, and the heartbeat snapshots, and it listens for the device's
permission decisions.

## Setup

**1. Python and dependencies.** Python 3.10+ from python.org (the Microsoft
Store build sandboxes file access in ways that make the hooks awkward):

```powershell
cd bridge
pip install -r requirements.txt
```

**2. Pair the device in Windows.** The firmware asks for LE Secure
Connections bonding, and on Windows the OS owns pairing — bleak can't
trigger it. Wake the Cardputer with any keypress, then **Settings →
Bluetooth & devices → Add device → Bluetooth**, pick the `Claude…` entry,
and enter the 6-digit passkey it shows on screen. You only do this once.

Check it from the bridge:

```powershell
python -m cursor_buddy scan
```

**3. Start the daemon.**

```powershell
python -m cursor_buddy daemon --owner Michael
```

It scans for the first `Claude*` device and reconnects on its own whenever
the Cardputer wakes up. Pass `--address <BLE address>` if you have more than one.
To have it start with Windows, put a shortcut to `scripts\run-daemon.cmd` in
the folder that opens from `Win+R` → `shell:startup`.

**4. Install the hooks.**

```powershell
python -m cursor_buddy install --user      # all workspaces
python -m cursor_buddy install .           # just this one
```

That writes `hooks.json` with absolute paths to your Python and to
`run_hook.py`, merging into any hooks you already have. Restart Cursor.

Check everything is up:

```powershell
python -m cursor_buddy status
```

## What the pet reacts to

| Cursor hook            | Effect on the device                                    |
| ---------------------- | ------------------------------------------------------- |
| `beforeSubmitPrompt`   | session becomes `running` → `busy`; prompt hits the transcript |
| `beforeShellExecution` | **approval prompt** → `attention`, chirp, enter/esc decides |
| `beforeMCPExecution`   | **approval prompt**, tool name and arguments shown       |
| `afterFileEdit`        | `edited main.cpp` in the transcript, feeds the level bar |
| `stop`                 | session stops running → back to `idle`                   |

A conversation with no events for 30 minutes is dropped from the session
count; Cursor has no "chat closed" hook to key off.

**Tokens are an estimate.** Cursor's hooks carry no usage data, so the
level-up counter is driven by ~4 characters of agent-produced text per
token. It paces the celebrations sensibly; it is not a billing figure.

## Approvals

When a gated hook fires, the daemon raises a prompt on the device and the
hook process blocks:

- **enter** (or `y`) → `once` → Cursor gets `{"permission": "allow"}`
- **esc** (or `n`) → `deny` → Cursor gets `{"permission": "deny"}`

**Every failure path falls back to `ask`**, which just means Cursor shows
its own approval UI as if the bridge weren't installed. That happens when
the device is asleep or out of range, when nobody answers within the
timeout, and when the daemon isn't running. A dead bridge can't wedge your
editor.

## More than one window (or more than one IDE)

**Multiple Cursor windows share one pet.** There's a single daemon per
Windows user, and every hook from every window reports into it, so this is
multiplexing rather than picking a winner — which is what the protocol
expects: `total` counts all live conversations, `running` counts the ones
generating right now.

To keep that legible in a 20-column panel:

- Transcript lines and approval prompts get a `[workspace]` tag **only when
  two or more workspaces are actually active**. One window, no tag.
- Approvals queue. The device shows the oldest unanswered prompt and
  `waiting` counts the rest; answer one and the next appears. Each window's
  hook gets its own verdict, so a `deny` in one never leaks into another.

**Cursor and the Claude desktop app can't both be connected.** A BLE
peripheral accepts one host at a time — whoever connects first holds the
link, and the other retries in the background. If the pet stops responding
to Cursor, disconnect it in Claude's Hardware Buddy window (or quit that
app); the daemon reconnects on its own within a few seconds.

If you'd rather one workspace not report at all, install the hooks
per-project (`python -m cursor_buddy install .`) instead of `--user`.

## Configuration

Environment variables, read by the hook process:

| Variable                 | Default                                  | Meaning                                     |
| ------------------------ | ---------------------------------------- | ------------------------------------------- |
| `CURSOR_BUDDY_GATE`      | `beforeShellExecution,beforeMCPExecution` | Which events wait on the device             |
| `CURSOR_BUDDY_TIMEOUT`   | `60`                                     | Seconds to wait before falling back to `ask` |
| `CURSOR_BUDDY_AUTOSTART` | `1`                                      | Start the daemon from a hook if it's down   |
| `CURSOR_BUDDY_PORT`      | `8787`                                   | Loopback IPC port                           |
| `CURSOR_BUDDY_HOME`      | `%USERPROFILE%\.cursor-buddy`            | Endpoint file and today's token count       |

Set `CURSOR_BUDDY_GATE=` (empty) to make everything notify-only, or add
`beforeReadFile` if you want to approve reads too — be warned, the agent
reads constantly.

The IPC socket is loopback-only and every request carries a token from
`%USERPROFILE%\.cursor-buddy\endpoint.json`, so other local processes can't
drive your pet or answer your prompts.

## Troubleshooting

**`scan` finds nothing.** Wake the Cardputer with a keypress, check its
settings menu → bluetooth is on, and confirm Windows Bluetooth is enabled.

**Daemon connects, pet stays asleep.** Asleep is the `sleep` state, which
means *no snapshots arriving*. Run the daemon with `-v` and confirm
snapshots are going out; if they are, the device is likely bonded to a stale
key — **Forget** it in Windows Bluetooth settings and pair again.

**Hooks don't fire.** Restart Cursor after installing. Verify the paths in
`hooks.json` still exist, and that the hook runs at all:

```powershell
echo {"hook_event_name":"stop","status":"done"} | python run_hook.py
```

**Approvals hang for a full minute.** That's the timeout doing its job when
the device isn't reachable — check `python -m cursor_buddy status`. Lower
`CURSOR_BUDDY_TIMEOUT` if you want a snappier fallback.

## Tests

```powershell
python tests\test_bridge.py
```

Runs the daemon, the IPC layer, and the real hook processes against a fake
device — no hardware needed. Covers approve/deny round trips, snapshot
shape, BLE line reassembly, and every fail-open path.

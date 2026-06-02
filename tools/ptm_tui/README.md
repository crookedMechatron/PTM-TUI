# PTM Diagnostic Dashboard

This Textual dashboard runs on the operator's Windows laptop. It does not run `ptm`, `pms`, or `telemetry` locally.

Instead, it uses the laptop's OpenSSH client to run the BRP's existing `control_cli` commands remotely over SSH. The TUI hides those commands from the operator and keeps the interface responsive while telemetry is streaming.

## SSH Model

Multiple SSH sessions are expected in v1:

- one long-running SSH session for `telemetry`
- one optional long-running SSH session for `candump`
- short SSH sessions for `ptm`, `pms`, and BRP service diagnostics commands

No SSH multiplexing is required. Password authentication is acceptable for v1, and SSH keys are optional.

## Windows Setup

From the repository root in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r tools\ptm_tui\requirements.txt
```

The dashboard uses the local `ssh` executable. On Windows, install the OpenSSH Client optional feature or make sure `ssh.exe` is available on `PATH`.

## Usage

```powershell
python tools\ptm_tui\ptm_tui.py --bots-config tools\ptm_tui\bots.json
python tools\ptm_tui\ptm_tui.py --bot BRP08
python tools\ptm_tui\ptm_tui.py --host 192.168.2.48 --user aceng
python tools\ptm_tui\ptm_tui.py --host 192.168.2.48 --user aceng --enable-can
python tools\ptm_tui\ptm_tui.py --host 192.168.2.48 --dry-run
```

Auto-accept a bot's SSH host key the first time this laptop connects:

```powershell
python tools\ptm_tui\ptm_tui.py --host 192.168.2.48 --user aceng --accept-new-host-key
```

This uses OpenSSH `StrictHostKeyChecking=accept-new`. It adds first-seen host keys to the user's normal `known_hosts` file, but still rejects a changed host key later.

Use a key file if required:

```powershell
python tools\ptm_tui\ptm_tui.py --host 192.168.2.48 --user aceng --identity-file C:\Users\operator\.ssh\id_ed25519
```

For controlled local-network use, a password can be supplied from the command line or bot config:

```powershell
python tools\ptm_tui\ptm_tui.py --host 192.168.2.48 --user aceng --ssh-password "password" --accept-new-host-key
```

Bot config example:

```json
[
  {
    "id": "BRP08",
    "host": "192.168.2.48",
    "user": "aceng",
    "password": "password",
    "accept_new_host_key": true
  }
]
```

When a password is configured, the dashboard defaults to the Paramiko SSH backend because Windows OpenSSH password prompts do not behave well inside a fullscreen TUI. The password is not written to the command log, but it is still plain text in the config file, so keep that file on the controlled operator laptop only.

To force a backend explicitly:

```powershell
python tools\ptm_tui\ptm_tui.py --bot BRP08 --ssh-backend paramiko
python tools\ptm_tui\ptm_tui.py --host 192.168.2.48 --user aceng --ssh-backend openssh
```

The TUI automatically adds common BRP command locations to the scripted SSH `PATH`, including `/usr/local/bin/control_cli`.

If the CLI tools are somewhere else, provide their directory:

```powershell
python tools\ptm_tui\ptm_tui.py --host 192.168.2.48 --remote-cli-dir /home/aceng/control_cli/bin
```

## Bot Config

`bots.example.json` can be copied or edited for field use:

```json
[
  {"id": "BRP08", "host": "192.168.2.48", "user": "aceng", "accept_new_host_key": true},
  {"id": "BRP09", "host": "192.168.2.49", "user": "aceng", "accept_new_host_key": true}
]
```

Launch from a bot id:

```powershell
python tools\ptm_tui\ptm_tui.py --bot BRP08
```

Use `--bots-config path\to\bots.json` to point at a different file.

The dashboard also shows a bot dropdown populated from the selected bot config. Changing bot stops the current telemetry/CAN streams, switches evidence logs, and starts streams for the new bot. Bot switching is blocked while PTM is marked as started by this UI.

## Test Config

`test_config.json` controls local display/safety thresholds:

```json
{
  "motor_current_thresholds": {
    "warning_amps": 7.5,
    "overcurrent_amps": 8.0
  }
}
```

Use another file with:

```powershell
python tools\ptm_tui\ptm_tui.py --bot BRP09 --test-config tools\ptm_tui\test_config.json
```

## Operator Keys

The top control row has two obvious toggle buttons:

- `Relay On` / `Relay Off`
- `Start PTM` / `Stop PTM`
- `Relay off on exit` checkbox, checked by default

Each button has a status label above it. Relay status is updated from `PMSPTM.relayPTM` telemetry when available, so the UI can show an already-active relay after logging in. PTM status is shown from successful PTM start/stop commands in this version. The PTM toggle still requires the UI to be armed first.

When `Relay off on exit` is checked, quitting the TUI or pressing Ctrl+C sends a final remote `pms -t 0` after any PTM stop attempt.

The `Telemetry Trends` pane keeps compact sparklines for M1-M4 current plus supply V/I. These are visual trends only; overcurrent safety still uses the latest parsed numeric motor current values.

Keyboard shortcuts are also available:

- `a`: arm or disarm the local UI
- `p`: PTM relay on
- `o`: PTM relay off
- `s`: start PTM, only when armed
- `x`: stop PTM immediately
- `r`: clear local warnings
- `d`: refresh BRP service diagnostics
- `c`: toggle CAN panel visibility when launched with `--enable-can`
- `q`: quit

On exit, if PTM was started by this UI, the dashboard attempts a remote PTM stop.

## Remote Commands

The dashboard runs these commands remotely over SSH on the bot itself. Because the tools are running locally on the BRP, the TUI does not pass `-a <address>`.

```text
ptm -i
ptm -o
pms -t 1
pms -t 0
telemetry --port_grpc 50051 --port_mqtt 1883 --sampling_interval 100 --console_messages
```

Service diagnostics are manual via `d` by default. Use `--enable-service-diagnostics` to run them automatically on startup, or `--disable-service-diagnostics` to disable them entirely.

With `--enable-can`, it also opens:

```text
candump -tz can0
```

The dedicated `ptm` executable is used for start and stop. `bot_status` is not used because it is not present on the BRPs tested in the field.

## Evidence Logs

Commands, warnings, telemetry lines, and optional CAN lines are written to timestamped JSONL files under:

```text
tools\ptm_tui\evidence\
```

Dry-run mode logs the exact SSH commands it would run and generates simulated telemetry, so it can be tested without a BRP.

## Known Limitations

- If Wi-Fi drops, telemetry may freeze and commands may fail.
- Emergency stop should not rely only on this UI.
- Overcurrent stop is advisory/software-level; firmware and hardware protection are still required.
- CAN snooping is informational only in v1 and is not used as the only safety input.

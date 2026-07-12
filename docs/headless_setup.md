# Headless VM & CI/CD Deployment Guide

This document describes how to configure and run Sunaba in headless environments (such as GCE instances, CI/CD runners, or remote servers) where standard desktop environments, keyrings, and notification agents are unavailable.

---

## 1. DBus & Headless Keyring Troubleshooting

In standard local setups, Sunaba coordinates with the OS-level credential store (Gnome Keyring / Keychain) to store and retrieve GitHub App private keys or credentials securely. 

On headless server nodes (such as Google Compute Engine VMs or Linux runners), launching `systemctl --user` without a GUI session can cause keyring initialization to fail with `No such interface` or `unlocked keyring` exceptions.

### Solution: Launch via `dbus-run-session`

To run the daemon under systemd in a headless environment, wrap the execution inside a mock DBus session to provide a local session bus.

#### Systemd Unit Configuration (`~/.config/systemd/user/sunaba.service`)
Modify the `ExecStart` directive to run through `dbus-run-session`:

```ini
[Unit]
Description=Sunaba Sandbox MCP Daemon (Headless)
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/dbus-run-session -- /home/masuda/.venv/bin/sunaba --transport streamable-http --port 8750 --dashboard-port 8751
Restart=always

[Install]
WantedBy=default.target
```

### Option: Disable Token Broker Downloads
If your CI/CD runner is restricted and cannot pull down external token broker binaries at runtime, disable the download step by setting:
```bash
SUNABA_TOKEN_BROKER_NO_DOWNLOAD=1
```
This forces Sunaba to fall back to the static `GITHUB_TOKEN` environment variable or the local GitHub App configuration instead of fetching broker binaries.

---

## 2. Notification Configuration in Headless Environments

In a headless environment, desktop notifications (`notify-send` on Linux) will fail silently. This is the expected and handled behavior. 

For headless setups, Webhook notifications are the recommended alert channel.

### Configuring Webhook Alerts
Pass the `--webhook-url` flag (or configure the respective systemd argument) to dispatch JSON HTTP POST payloads containing alerts to a Slack/Discord webhook or a custom monitoring endpoint:

```bash
sunaba --webhook-url "https://hooks.slack.com/services/..." \
       --failure-threshold 5 \
       --long-run-seconds 300
```

#### Alert Payload Format
When consecutive failures exceed the threshold or a long-running job is flagged, Sunaba dispatches a payload structured as follows:

```json
{
  "event": "alert",
  "type": "consecutive_failures" | "long_running_job" | "boundary_crossing",
  "container_id": "abc123def456",
  "detail": "Command run failed 5 times in a row.",
  "timestamp": "2026-07-12T10:20:00Z"
}
```

---

## 3. Remote WSL2 / VM Port Forwarding

The Sunaba dashboard binds to `127.0.0.1:8751` by default. 

If running on a remote headless VM (like GCE) or a headless remote server, you can securely access the dashboard from your local developer machine using an SSH tunnel:

```bash
ssh -L 8751:127.0.0.1:8751 -L 8750:127.0.0.1:8750 user@gce-vm-ip
```

This forwards both the MCP endpoint (`8750`) and the Web Dashboard (`8751`) to your local loopback address securely without exposing the server ports to the public internet.

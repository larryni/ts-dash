# <img src="static/icon.svg" width="36" height="36" alt="" align="top"> tailnetDash

> **Note:** This project is 100% vibe coded with [Claude](https://claude.com/claude-code).

A small self-hosted dashboard for your Tailscale network. It polls the Tailscale API, so new devices
appear and removed ones are archived automatically.

- Per device: IP and name, online state, last seen, Tailscale SSH, sshd on :22, HTTPS on :443,
  client version and update status, key expiry, exit node and subnet router status, uptime
- Archive of removed devices, with their last known details
- Timeline of joins, removals, online/offline changes, version and routing changes
- Types (Server, Laptop…), notes, ping and relay latency, per-device latency history
- Table or card layout, grouping, sortable and configurable columns, light/dark theme, page zoom
- CSV / JSON export and purging of old archived devices
- Login with a username and password you choose on first run, with "remember me"

Stack: Python, Flask, SQLite and one static HTML page. No build step.

## Requirements

- Python 3.9 or newer
- A machine that is **on your tailnet** (the dashboard probes ports 22 and 443 and measures ping
  *from this machine*)
- A Tailscale API access token (see below)

## 1. Install system packages

**Arch / Manjaro / EndeavourOS**

```sh
sudo pacman -S --needed python python-pip
```

**Debian / Ubuntu / Raspberry Pi OS / Mint**

```sh
sudo apt update
sudo apt install python3 python3-venv python3-pip
```

(`python3-venv` is a separate package on Debian-based systems. Without it the next step fails with
"ensurepip is not available".)

## 2. Get the files and install the dependencies

Put the project somewhere permanent, for example `~/Projects/ts-dash`, so it contains `app.py`,
`requirements.txt` and the `static/` folder.

Modern Arch and Debian mark the system Python as "externally managed", so a plain
`pip install` is refused. Use a virtual environment:

```sh
cd ~/Projects/ts-dash
python3 -m venv .venv
```

Activate it. Use the line that matches your shell:

```sh
source .venv/bin/activate         # bash / zsh
source .venv/bin/activate.fish    # fish
```

Then:

```sh
pip install -r requirements.txt
```

## 3. Create a Tailscale API access token

1. Open the Tailscale admin console → **Settings → Keys**.
2. Under **API access tokens**, choose **Generate access token** and copy the `tskey-api-…` value.
3. This is the token the dashboard needs. An auth key (used to add a machine to the tailnet) is a
   different thing and will not work.

You don't need to put the token in an environment variable. The first time you open the
dashboard it asks for it, checks it with Tailscale and saves it on the server in a file called
`api_key` next to the database, readable only by your user. The page never shows it again.

API access tokens expire after at most 90 days. The header warns you two weeks ahead. When a key
expires or is revoked, the dashboard asks for a new one by itself. You can also replace it at any
time with the key button in the header.

The dashboard only reads data. It never changes your tailnet.

## 4. Run it

```sh
python app.py
```

Open `http://<this-machine>:8080/` — for example `http://moya:8080/` using the MagicDNS name.

1. **Choose a username and password.** On the very first visit the dashboard asks you to create its
   login (password at least 8 characters). Do this straight after starting it: until a login
   exists, whoever opens the page first gets to choose it.
2. **Paste your API access token** when asked. The first sync takes a few seconds.

### Logging in

- Without **Remember me**, you stay signed in until you close the browser or leave the dashboard
  unused for 12 hours.
- With **Remember me**, the browser stays signed in for 30 days, counted from the last time you used
  it. Leave it off on a shared computer.
- The account menu (the person icon at the right of the header) shows who is signed in and has
  **Change password…** and **Sign out**. Changing the password needs the current one and signs out
  every other browser; the one you are using stays signed in.
- After 5 wrong passwords from one address, each further try has to wait (30 seconds, then longer).

Passwords are stored as a salted scrypt hash, never in plain text. The browser's login cookie is a
random token, and the database keeps only a hash of it.

**Forgot the password?** Stop the dashboard and run, from its folder:

```sh
python app.py --reset-login
```

This removes the login and signs every browser out (your devices, history and API key are kept).
Start the dashboard again and the next visit asks you to create a new login.

### Security: who can reach it?

The login keeps casual visitors out, but by default the dashboard listens on all network interfaces
(`HOST=0.0.0.0`) and uses plain HTTP, so the password crosses the network unencrypted unless the
connection itself is encrypted. Over Tailscale it is (WireGuard), so restrict the dashboard to
Tailscale only:

```sh
export HOST=$(tailscale ip -4)      # bind to this machine's tailnet address only
```

or use `HOST=127.0.0.1` if you put something like `tailscale serve` in front of it.

With `tailscale serve` you also get HTTPS, and the login cookie is then marked Secure automatically.

## Configuration

All settings are environment variables.

| Variable | Default | Meaning |
|---|---|---|
| `TS_API_KEY` | *(none)* | Optional. Used only when no key has been saved from the page yet |
| `KEY_PATH` | `api_key` next to `DB_PATH` | Where the key entered in the page is saved |
| `TS_TAILNET` | `-` | Tailnet name; `-` means your default tailnet |
| `HOST` / `PORT` | `0.0.0.0` / `8080` | Address and port to listen on |
| `POLL_SECONDS` | `60` | How often to ask the Tailscale API for devices |
| `DB_PATH` | `dashboard.db` | SQLite file (relative to where you start the app) |
| `PROBE_PORTS` | `1` | `0` turns off the :22 / :443 probes and ping |
| `CATEGORIES` | `Application,Server,NAS,Desktop,Laptop,Phone,Tablet,Other` | Choices in the Type drop-down, comma separated |
| `EVENT_RETENTION_DAYS` | `365` | How long timeline events are kept |
| `LATENCY_SAMPLE_SECONDS` | `300` | How often latency is recorded |
| `LATENCY_RETENTION_DAYS` | `30` | How long latency history is kept |
| `ARCHIVE_RETENTION_DAYS` | `0` | Automatically purge archived devices after this many days (`0` = never) |

## 5. Run it in the background (systemd user service)

This works the same on Arch and Debian.

Put any settings in an env file. The API key doesn't need to be in it: you enter that in the page.

```sh
mkdir -p ~/.config/systemd/user
cat > ~/.config/ts-dash.env <<'EOF'
HOST=100.x.y.z
EOF
chmod 600 ~/.config/ts-dash.env
```

Use your own tailnet IP (`tailscale ip -4`) for `HOST`, or leave the file empty.

Create `~/.config/systemd/user/ts-dash.service`. Adjust the path if you installed somewhere else:

```ini
[Unit]
Description=tailnetDash
After=network-online.target

[Service]
WorkingDirectory=%h/Projects/ts-dash
EnvironmentFile=%h/.config/ts-dash.env
ExecStart=%h/Projects/ts-dash/.venv/bin/python app.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

Enable and start it:

```sh
systemctl --user daemon-reload
systemctl --user enable --now ts-dash
loginctl enable-linger "$USER"     # keep it running when you're logged out, and start at boot
```

Useful commands:

```sh
systemctl --user status ts-dash
journalctl --user -u ts-dash -f
systemctl --user restart ts-dash
```

If you bind `HOST` to the tailnet IP and the service starts before Tailscale is up at boot, it
will fail and retry every 10 seconds until the address exists. That is expected.

## Updating

1. Replace `app.py` and `static/index.html` with the new versions (the database is upgraded
   automatically and keeps your history).
2. Restart the app: stop it with Ctrl+C and start it again, or `systemctl --user restart ts-dash`.
3. Hard-refresh the page (Ctrl+Shift+R). The server does not reload itself, so a new page talking to
   an old running server shows errors such as a 404 on a new feature.

## Backup

Everything the dashboard stores (archive, types, notes, timeline, latency) is in one file, the
`DB_PATH` SQLite file. Stop the service and copy it, or use `sqlite3 dashboard.db ".backup copy.db"`.
The API key is kept apart, in the `api_key` file, so backups of the database don't contain it.
The database does hold the login (as a password hash), so restoring a backup also restores the
password that was set at the time.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `error: externally-managed-environment` | Install inside the virtual environment (step 2), not system-wide. |
| `ensurepip is not available` (Debian) | `sudo apt install python3-venv`, then recreate `.venv`. |
| fish prints errors when activating | Use `.venv/bin/activate.fish`, not `activate`. |
| 404 on `/` | `index.html` must be inside the `static/` folder next to `app.py`. |
| "API key rejected" in the header | Token is wrong, revoked or expired (90-day limit). Generate a new one and enter it with the key button. |
| "The key works, but it couldn't be saved" | The app can't write next to the database. Check the folder's permissions, or set `KEY_PATH`. |
| Forgot the dashboard password | Stop the dashboard, run `python app.py --reset-login`, start it again and create a new login. |
| "Too many wrong attempts" | Wait the number of seconds shown, or restart the dashboard to clear the counter. |
| Asked to create a login after updating | Expected once: versions with a login ask you to set one up on the first visit. |
| Everything shows SSH/HTTPS closed or no ping | The host isn't on the tailnet, or a firewall blocks it. Check `tailscale status` on that machine. |
| Page works but a new button gives a 404 | The old server process is still running. Restart it (see Updating). |
| Page looks wrong colours | Some browser extensions (dark-mode or colour changers) restyle pages. Try a private window. |

## Notes on what the columns mean

- **SSH** shows Tailscale SSH as reported by the Tailscale API. "sshd :22" is a separate probe of
  an ordinary SSH server on port 22.
- **HTTPS :443** is only a port probe: it shows that something answers on 443, not that
  `tailscale cert` is set up. The API does not expose certificate status.
- **Ping** is the TCP connect time from the dashboard machine to the device on port 22 or 443,
  not an ICMP ping. A device that blocks both ports shows "no reply".
- **DERP relay** is the device's own latency to its nearest Tailscale relay server.
- Whether a machine accepts subnet routes is not exposed by the API, so the dashboard only shows
  routes a device advertises and whether they are approved.
- Uptime needs about an hour of tracked data before it shows a figure. Time while the dashboard
  itself wasn't running is excluded.

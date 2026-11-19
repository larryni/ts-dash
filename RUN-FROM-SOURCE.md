# Run tailnetDash from source (without Docker)

This runs the dashboard directly with Python, optionally as a systemd user service. Most people
will find the [Docker quick start](README.md#quick-start-docker) easier. The
[configuration](README.md#configuration), [login](README.md#logging-in) and
[troubleshooting](README.md#troubleshooting) sections in the README apply here too.

## Requirements

- Python 3.9 or newer
- A machine that is **on your tailnet** (the dashboard probes ports 22 and 443 and measures ping
  *from this machine*)
- A Tailscale API access token (see [step 3](#3-create-a-tailscale-api-access-token))

## 1. Install system packages

**Arch / Manjaro / EndeavourOS**

```sh
sudo pacman -S --needed python python-pip git
```

**Debian / Ubuntu / Raspberry Pi OS / Mint**

```sh
sudo apt update
sudo apt install python3 python3-venv python3-pip git
```

(`python3-venv` is a separate package on Debian-based systems. Without it the next step fails with
"ensurepip is not available".)

## 2. Get the files and install the dependencies

Put the project somewhere permanent, for example `~/Projects/ts-dash`:

```sh
git clone https://github.com/larryni/ts-dash.git ~/Projects/ts-dash
cd ~/Projects/ts-dash
```

Modern Arch and Debian mark the system Python as "externally managed", so a plain
`pip install` is refused. Use a virtual environment:

```sh
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

Follow [Tailscale API access token](README.md#tailscale-api-access-token) in the README. When run
from source, the dashboard saves the token in a file called `api_key` next to the database,
readable only by your user.

## 4. Run it

```sh
python app.py
```

Open `http://<this-machine>:8080/` — for example `http://moya:8080/` using the MagicDNS name.

1. **Choose a username and password.** On the very first visit the dashboard asks you to create its
   login (password at least 8 characters). Do this straight after starting it: until a login
   exists, whoever opens the page first gets to choose it.
2. **Paste your API access token** when asked. The first sync takes a few seconds.

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

## 5. Run it in the background (systemd user service)

This works the same on Arch and Debian.

Put any settings from the [configuration table](README.md#configuration) in an env file. The API
key doesn't need to be in it: you enter that in the page.

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

1. Get the new version: `git pull` in the project folder (the database is upgraded automatically
   and keeps your history). If `requirements.txt` changed, run `pip install -r requirements.txt`
   again inside the virtual environment.
2. Restart the app: stop it with Ctrl+C and start it again, or `systemctl --user restart ts-dash`.
3. Hard-refresh the page (Ctrl+Shift+R). The server does not reload itself, so a new page talking to
   an old running server shows errors such as a 404 on a new feature.

## Backup

Stop the service and copy the `DB_PATH` file (`dashboard.db` by default), or use
`sqlite3 dashboard.db ".backup copy.db"`. See [Backup](README.md#backup) in the README for what it
contains.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `error: externally-managed-environment` | Install inside the virtual environment (step 2), not system-wide. |
| `ensurepip is not available` (Debian) | `sudo apt install python3-venv`, then recreate `.venv`. |
| fish prints errors when activating | Use `.venv/bin/activate.fish`, not `activate`. |
| 404 on `/` | `index.html` must be inside the `static/` folder next to `app.py`. |
| "The key works, but it couldn't be saved" | The app can't write next to the database. Check the folder's permissions, or set `KEY_PATH`. |
| Page works but a new button gives a 404 | The old server process is still running. Restart it (see [Updating](#updating)). |

More problems, including the API key and the login, are in the README's
[troubleshooting](README.md#troubleshooting) table.

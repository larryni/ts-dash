"""Tailscale dashboard: polls the Tailscale API, tracks devices in SQLite,
archives devices that disappear, and serves a small web UI."""
import csv
import io
import json
import os
import socket
import sqlite3
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests
from flask import Flask, Response, abort, jsonify, request, send_from_directory

TAILNET = os.environ.get("TS_TAILNET", "-")   # "-" = your default tailnet
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "60"))
DB_PATH = os.environ.get("DB_PATH", "dashboard.db")
# The API access token (tskey-api-...) is entered in the web page and saved here, readable
# only by the owner. TS_API_KEY still works as a starting value when no key has been saved.
KEY_PATH = os.environ.get("KEY_PATH") or os.path.join(os.path.dirname(DB_PATH) or ".", "api_key")
KEY_WARN_DAYS = 14  # show a warning this long before the key expires
PROBE = os.environ.get("PROBE_PORTS", "1") == "1"
ONLINE_WINDOW = 300  # fallback: seen within 5 min counts as active
EVENT_RETENTION_DAYS = int(os.environ.get("EVENT_RETENTION_DAYS", "365"))
# A silence longer than this between polls (dashboard stopped, API outage) is a "gap":
# we know nothing about the devices then, so it is left out of uptime figures.
GAP_SECONDS = max(300, POLL_SECONDS * 5)
# Latency is sampled on its own (slower) schedule so the table stays small.
LATENCY_SAMPLE_SECONDS = int(os.environ.get("LATENCY_SAMPLE_SECONDS", "300"))
LATENCY_RETENTION_DAYS = int(os.environ.get("LATENCY_RETENTION_DAYS", "30"))
# Automatically purge archived devices after this many days (0 = never).
ARCHIVE_RETENTION_DAYS = int(os.environ.get("ARCHIVE_RETENTION_DAYS", "0"))
NOTE_MAX = 5000
CATEGORIES = [c.strip() for c in os.environ.get(
    "CATEGORIES", "Application,Server,NAS,Desktop,Laptop,Phone,Tablet,Other").split(",") if c.strip()]

app = Flask(__name__, static_folder="static")
lock = threading.Lock()
state = {"last_poll": None, "last_error": None}
dns_cache = {}
last_prune = 0.0
wake = threading.Event()  # set to make the poller run now instead of after its sleep


class KeyRejected(Exception):
    """Tailscale refused the API key (wrong, expired or revoked)."""


def read_key_file():
    try:
        with open(KEY_PATH) as f:
            return f.read().strip() or None
    except FileNotFoundError:
        return None


def write_key_file(key):
    """Save the key so only the owner can read it, replacing any old one in one step."""
    tmp = KEY_PATH + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key + "\n")
    os.chmod(tmp, 0o600)  # in case the .tmp file was left over with other permissions
    os.replace(tmp, KEY_PATH)


# A key saved from the page wins over the environment: it is the more recent choice.
api_key = {"value": read_key_file() or os.environ.get("TS_API_KEY", "").strip() or None,
           "status": "unchecked",   # unchecked | ok | invalid | missing
           "expires": None, "expires_checked": 0.0}
if not api_key["value"]:
    api_key["status"] = "missing"


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS devices (
                id TEXT PRIMARY KEY, name TEXT, data TEXT,
                first_seen TEXT, updated_at TEXT,
                ssh_open INTEGER, https_open INTEGER);
            CREATE TABLE IF NOT EXISTS archive (
                id TEXT PRIMARY KEY, name TEXT, data TEXT,
                first_seen TEXT, archived_at TEXT,
                ssh_open INTEGER, https_open INTEGER);
            -- kept separate so a device's type survives archiving/restoring
            CREATE TABLE IF NOT EXISTS labels (
                device_id TEXT PRIMARY KEY, category TEXT);
            -- timeline of things that happened to devices
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
                device_id TEXT, device_name TEXT, kind TEXT NOT NULL, detail TEXT);
            CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
            CREATE INDEX IF NOT EXISTS events_dev ON events(device_id, id);
            -- state changes only: 1 online, 0 offline, -1 not on the tailnet; drives uptime
            CREATE TABLE IF NOT EXISTS presence (device_id TEXT, ts TEXT, state INTEGER);
            CREATE INDEX IF NOT EXISTS presence_dev ON presence(device_id, ts);
            -- periods when nothing was polling (excluded from uptime)
            CREATE TABLE IF NOT EXISTS gaps (start_ts TEXT, end_ts TEXT);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            -- free-text notes; separate so they survive archiving/restoring
            CREATE TABLE IF NOT EXISTS notes (
                device_id TEXT PRIMARY KEY, note TEXT, updated_at TEXT);
            -- ms: TCP round trip from this server (NULL = no answer while Tailscale says online)
            -- relay_ms: the device's own latency to its preferred DERP relay
            CREATE TABLE IF NOT EXISTS latency (
                device_id TEXT, ts TEXT, ms REAL, relay_ms REAL);
            CREATE INDEX IF NOT EXISTS latency_dev ON latency(device_id, ts);
            """
        )
        cols = [r["name"] for r in c.execute("PRAGMA table_info(devices)")]
        if "rtt_ms" not in cols:  # databases from earlier versions
            c.execute("ALTER TABLE devices ADD COLUMN rtt_ms REAL")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def tcp_probe(ip, port, timeout=1.5, banner=False):
    """Try a TCP connection. Returns (open, rtt_ms, banner_bytes). The round-trip time is
    reported whenever the other end answered at all, including a refusal (a closed port
    still replies immediately); a timeout gives None."""
    t0 = time.perf_counter()
    try:
        sock = socket.create_connection((ip, port), timeout=timeout)
    except ConnectionRefusedError:
        return 0, (time.perf_counter() - t0) * 1000, b""
    except OSError:
        return 0, None, b""
    rtt = (time.perf_counter() - t0) * 1000
    data = b""
    with sock:
        if banner:
            sock.settimeout(timeout)
            try:
                data = sock.recv(64)
            except OSError:
                pass
    return 1, rtt, data


def probe(dev):
    """-> (ssh_state, https_open, rtt_ms). ssh_state: 0 closed, 1 a regular SSH server answers,
    2 Tailscale SSH (its built-in server announces itself as SSH-2.0-Tailscale)."""
    ip = next((a for a in dev.get("addresses", []) if "." in a), None)
    if not (PROBE and ip):
        return None, None, None
    o22, r22, bn = tcp_probe(ip, 22, banner=True)
    o443, r443, _ = tcp_probe(ip, 443)
    ssh = 0 if not o22 else (2 if bn.startswith(b"SSH-2.0-Tailscale") else 1)
    rtts = [r for r in (r22, r443) if r is not None]
    return ssh, o443, (round(min(rtts), 1) if rtts else None)


def ts_request(path, key=None, params=None, tailnet=True):
    """GET from the Tailscale API. A 401 (or 403 on the device list) means the key itself is
    no good and raises KeyRejected, so callers can tell it apart from a network problem."""
    key = key or api_key["value"]
    if not key:
        raise KeyRejected("no API key")
    base = f"https://api.tailscale.com/api/v2/tailnet/{TAILNET}/" if tailnet else "https://api.tailscale.com/api/v2/"
    r = requests.get(base + path, params=params, auth=(key, ""), timeout=20)
    if r.status_code == 401 or (r.status_code == 403 and path == "devices"):
        raise KeyRejected(f"Tailscale rejected the API key ({r.status_code})")
    r.raise_for_status()
    return r.json()


def fetch_devices(key=None):
    return ts_request("devices", key=key, params={"fields": "all"}).get("devices", [])


def key_id(key):
    """tskey-api-<id>-<secret> -> <id>, which is not secret and identifies the key."""
    parts = (key or "").split("-")
    return parts[2] if len(parts) >= 4 and parts[0] == "tskey" else None


def refresh_key_expiry(force=False):
    """Look up when the current key expires (at most hourly). Best effort: a failure just
    leaves the expiry unknown."""
    if not force and time.time() - api_key["expires_checked"] < 3600:
        return
    api_key["expires_checked"] = time.time()
    kid = key_id(api_key["value"])
    if not kid:
        return
    try:
        api_key["expires"] = ts_request(f"keys/{kid}").get("expires")
    except Exception:
        pass


def epoch(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def short_ver(v):
    return (v or "").split("-")[0]


def meta_get(c, key):
    r = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r["value"] if r else None


def meta_set(c, key, value):
    c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)", (key, value))


def log_event(c, ts, did, name, kind, detail=None):
    c.execute("INSERT INTO events (ts, device_id, device_name, kind, detail) VALUES (?,?,?,?,?)",
              (ts, did, name, kind, json.dumps(detail) if detail else None))


def record_gap(c, ts):
    last = meta_get(c, "last_poll")
    if last and epoch(ts) - epoch(last) > GAP_SECONDS:
        c.execute("INSERT INTO gaps (start_ts, end_ts) VALUES (?,?)", (last, ts))


def track_presence(c, did, name, ts, st, emit):
    """Write a presence row when a device's state changes. An online/offline event is only
    logged for a real flip between the two, not for a device joining, returning or leaving."""
    last = c.execute("SELECT ts, state FROM presence WHERE device_id=? ORDER BY ts DESC, rowid DESC LIMIT 1",
                     (did,)).fetchone()
    if last and last["state"] == st:
        return
    c.execute("INSERT INTO presence (device_id, ts, state) VALUES (?,?,?)", (did, ts, st))
    if emit and last and last["state"] in (0, 1) and st in (0, 1):
        log_event(c, ts, did, name, "online" if st == 1 else "offline",
                  {"after": max(0, int(epoch(ts) - epoch(last["ts"])))})


def diff_device(old, new):
    """What changed between two snapshots of the same device, as (kind, detail) pairs."""
    out = []
    if old.get("name") and new.get("name") and old["name"] != new["name"]:
        out.append(("renamed", {"from": old["name"], "to": new["name"]}))
    ov, nv = short_ver(old.get("clientVersion")), short_ver(new.get("clientVersion"))
    if ov and nv and ov != nv:
        out.append(("version", {"from": ov, "to": nv}))
    if bool(old.get("sshEnabled")) != bool(new.get("sshEnabled")):
        out.append(("ssh", {"enabled": bool(new.get("sshEnabled"))}))
    oe, ne = exit_node_status(old), exit_node_status(new)
    if oe != ne:
        out.append(("exit_node", {"from": oe, "to": ne}))
    so, sn = subnet_info(old), subnet_info(new)
    det = {}
    for label, key in (("advertised", "advertised"), ("approved", "approved")):
        added = [r for r in sn[key] if r not in so[key]]
        removed = [r for r in so[key] if r not in sn[key]]
        if added:
            det[label + "_added"] = added
        if removed:
            det[label + "_removed"] = removed
    if det:
        out.append(("subnets", det))
    if bool(old.get("keyExpiryDisabled")) != bool(new.get("keyExpiryDisabled")):
        out.append(("key", {"disabled": bool(new.get("keyExpiryDisabled"))}))
    elif old.get("expires") and new.get("expires") and new["expires"] > old["expires"]:
        out.append(("key", {"expires": new["expires"]}))  # key was renewed
    if old.get("authorized") is not None and bool(old.get("authorized")) != bool(new.get("authorized")):
        out.append(("authorized", {"to": bool(new.get("authorized"))}))
    return out


def poll_once():
    devices = fetch_devices()
    if not devices:  # never mass-archive on an empty/odd response
        return
    with ThreadPoolExecutor(max_workers=16) as ex:
        probes = list(ex.map(probe, devices))

    ts = now()
    with lock, db() as c:
        # The very first poll only records a baseline: devices that already existed are not
        # "joined", and changes since an older snapshot have no known time.
        baselined = meta_get(c, "baselined") is not None
        record_gap(c, ts)
        seen = set()
        for dev, (ssh, https, rtt) in zip(devices, probes):
            did = dev["id"]
            seen.add(did)
            name = dev.get("hostname") or dev.get("name")
            existing = c.execute("SELECT data, first_seen FROM devices WHERE id=?", (did,)).fetchone()
            restored = c.execute("SELECT first_seen FROM archive WHERE id=?", (did,)).fetchone()
            first = (existing or restored or {"first_seen": ts})["first_seen"]
            if baselined:
                if existing:
                    try:
                        old = json.loads(existing["data"])
                    except ValueError:
                        old = {}
                    for kind, detail in diff_device(old, dev):
                        log_event(c, ts, did, name, kind, detail)
                elif restored:
                    log_event(c, ts, did, name, "restored")
                else:
                    ip = next((a for a in dev.get("addresses", []) if "." in a), None)
                    log_event(c, ts, did, name, "joined",
                              {"ip": ip, "os": dev.get("os"), "version": short_ver(dev.get("clientVersion"))})
            track_presence(c, did, name, ts, 1 if is_online(dev) else 0, emit=baselined)
            if restored:
                c.execute("DELETE FROM archive WHERE id=?", (did,))
            c.execute(
                """INSERT INTO devices (id,name,data,first_seen,updated_at,ssh_open,https_open,rtt_ms)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET name=excluded.name, data=excluded.data,
                     updated_at=excluded.updated_at, ssh_open=excluded.ssh_open,
                     https_open=excluded.https_open, rtt_ms=excluded.rtt_ms""",
                (did, dev.get("name"), json.dumps(dev), first, ts, ssh, https, rtt),
            )
        # Anything we hold that Tailscale no longer lists gets archived with its last snapshot.
        for row in c.execute("SELECT * FROM devices").fetchall():
            if row["id"] not in seen:
                c.execute(
                    "INSERT OR REPLACE INTO archive VALUES (?,?,?,?,?,?,?)",
                    (row["id"], row["name"], row["data"], row["first_seen"], ts,
                     row["ssh_open"], row["https_open"]),
                )
                c.execute("DELETE FROM devices WHERE id=?", (row["id"],))
                try:
                    rname = json.loads(row["data"]).get("hostname") or row["name"]
                except ValueError:
                    rname = row["name"]
                if baselined:
                    log_event(c, ts, row["id"], rname, "removed")
                track_presence(c, row["id"], rname, ts, -1, emit=False)
        if PROBE:
            last_s = meta_get(c, "latency_last")
            if not last_s or epoch(ts) - epoch(last_s) >= LATENCY_SAMPLE_SECONDS:
                for dev, (_, _, rtt) in zip(devices, probes):
                    if is_online(dev):  # an offline device not answering isn't packet loss
                        rl = relay_info(dev)
                        c.execute("INSERT INTO latency (device_id, ts, ms, relay_ms) VALUES (?,?,?,?)",
                                  (dev["id"], ts, rtt, rl["ms"] if rl else None))
                meta_set(c, "latency_last", ts)
        if not baselined:
            meta_set(c, "baselined", ts)
        meta_set(c, "last_poll", ts)


def ts_get(path):
    return ts_request(path)


def refresh_dns():
    """Cache the tailnet's DNS settings. Each section is fetched on its own so one
    failure (e.g. a missing permission) doesn't hide the others."""
    out = {"errors": {}}

    def grab(key, path, fn):
        try:
            out[key] = fn(ts_get(path))
        except Exception as e:
            out["errors"][key] = str(e)

    grab("nameservers", "dns/nameservers", lambda j: j.get("dns") or [])
    grab("magic_dns", "dns/preferences", lambda j: bool(j.get("magicDNS")))
    grab("search_paths", "dns/searchpaths", lambda j: j.get("searchPaths") or [])
    grab("split_dns", "dns/split-dns", lambda j: j or {})
    with db() as c:
        names = [r["name"] for r in c.execute("SELECT name FROM devices")]
    suffixes = Counter(n.split(".", 1)[1] for n in names if n and "." in n)
    out["domain"] = suffixes.most_common(1)[0][0] if suffixes else None
    out["updated"] = now()
    dns_cache.clear()
    dns_cache.update(out)


def prune_events():
    global last_prune
    if time.time() - last_prune < 3600:
        return
    last_prune = time.time()
    cutoff = datetime.fromtimestamp(time.time() - EVENT_RETENTION_DAYS * 86400, timezone.utc).isoformat(timespec="seconds")
    lat_cut = datetime.fromtimestamp(time.time() - LATENCY_RETENTION_DAYS * 86400, timezone.utc).isoformat(timespec="seconds")
    with lock, db() as c:
        c.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
        c.execute("DELETE FROM latency WHERE ts < ?", (lat_cut,))
        if ARCHIVE_RETENTION_DAYS > 0:
            purge_archive(c, ARCHIVE_RETENTION_DAYS)


def poll_loop():
    while True:
        if not api_key["value"]:
            # nothing to do until a key is entered in the page
            state["last_error"] = None
            wake.wait()
            wake.clear()
            continue
        used = api_key["value"]
        try:
            poll_once()
            api_key["status"] = "ok"
            state.update(last_poll=now(), last_error=None)
            prune_events()
        except KeyRejected as e:
            if api_key["value"] == used:  # not if a new key was saved while this poll ran
                api_key["status"] = "invalid"
                state["last_error"] = str(e)
        except Exception as e:  # keep looping on transient failures
            state["last_error"] = str(e)
        if api_key["status"] == "ok":
            refresh_key_expiry()
            try:
                refresh_dns()
            except Exception as e:
                dns_cache.update(errors={"all": str(e)})
        wake.wait(POLL_SECONDS)
        wake.clear()


def is_online(dev):
    if "connectedToControl" in dev:
        return bool(dev["connectedToControl"])
    try:
        seen = datetime.fromisoformat(dev["lastSeen"].replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - seen).total_seconds() < ONLINE_WINDOW
    except Exception:
        return False


def exit_node_status(d):
    """'active' if advertised and approved, 'pending' if advertised only, else None."""
    default = {"0.0.0.0/0", "::/0"}
    if default & set(d.get("advertisedRoutes") or []):
        return "active" if default & set(d.get("enabledRoutes") or []) else "pending"
    return None


DEFAULT_ROUTES = {"0.0.0.0/0", "::/0"}


def subnet_info(d):
    """Subnet routes (default routes excluded): advertised by the device, approved in the
    admin console, and still pending approval."""
    adv = [r for r in (d.get("advertisedRoutes") or []) if r not in DEFAULT_ROUTES]
    ena = [r for r in (d.get("enabledRoutes") or []) if r not in DEFAULT_ROUTES]
    return {"advertised": adv, "approved": [r for r in adv if r in ena],
            "pending": [r for r in adv if r not in ena]}


def uptime_map(windows=(7, 30), end=None):
    """Percentage of tracked time each device was online, per window (in days).
    Time before tracking began, while a device was off the tailnet, and gaps in our own
    polling are all left out. A window with under an hour of data gives None."""
    with db() as c:
        rows = c.execute("SELECT device_id, ts, state FROM presence ORDER BY device_id, ts, rowid").fetchall()
        gaps = [(epoch(r["start_ts"]), epoch(r["end_ts"])) for r in c.execute("SELECT start_ts, end_ts FROM gaps")]
        last_poll = meta_get(c, "last_poll")
    end = end if end is not None else time.time()
    if last_poll and end - epoch(last_poll) > GAP_SECONDS:
        gaps.append((epoch(last_poll), end))  # we're mid-gap right now

    def overlap(a, b, g0, g1):
        return max(0.0, min(b, g1) - max(a, g0))

    seqs = {}
    for r in rows:
        seqs.setdefault(r["device_id"], []).append((epoch(r["ts"]), r["state"]))
    out = {}
    for did, seq in seqs.items():
        ivs = [(seq[i][0], seq[i + 1][0] if i + 1 < len(seq) else end, seq[i][1]) for i in range(len(seq))]
        res = {"tracked_days": round(max(0.0, end - seq[0][0]) / 86400, 1)}
        for w in windows:
            start = end - w * 86400
            observed = up = 0.0
            for a, b, st in ivs:
                if st < 0:
                    continue
                a = max(a, start)
                if b <= a:
                    continue
                length = (b - a) - sum(overlap(a, b, g0, g1) for g0, g1 in gaps)
                observed += length
                if st == 1:
                    up += length
            res[str(w)] = round(100 * up / observed, 1) if observed >= 3600 else None
        out[did] = res
    return out


def labels_map():
    with db() as c:
        return {r["device_id"]: r["category"] for r in c.execute("SELECT * FROM labels")}


def notes_map():
    with db() as c:
        return {r["device_id"]: r["note"] for r in c.execute("SELECT device_id, note FROM notes")}


def relay_info(d):
    """The DERP relay the device prefers and its latency to it (device -> relay, not to us)."""
    lat = (d.get("clientConnectivity") or {}).get("latency") or {}
    best = None
    for name, v in lat.items():
        if not isinstance(v, dict) or v.get("latencyMs") is None:
            continue
        if v.get("preferred"):
            return {"name": name, "ms": round(v["latencyMs"], 1)}
        if best is None or v["latencyMs"] < best["ms"]:
            best = {"name": name, "ms": round(v["latencyMs"], 1)}
    return best


def pctl(vals, p):
    """Percentile of an already sorted list, linear interpolation."""
    if not vals:
        return None
    k = (len(vals) - 1) * p / 100
    lo = int(k)
    hi = min(lo + 1, len(vals) - 1)
    return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)


def downsample(points, n):
    """Shrink [(t, ms, relay)] to at most n buckets; a bucket is lost only if every sample in it was."""
    if len(points) <= n:
        return [{"t": t, "ms": ms, "relay": rl} for t, ms, rl in points]
    out = []
    size = len(points) / n
    for i in range(n):
        chunk = points[int(i * size):int((i + 1) * size)]
        if not chunk:
            continue
        oks = [m for _, m, _ in chunk if m is not None]
        rls = [r for _, _, r in chunk if r is not None]
        out.append({"t": chunk[len(chunk) // 2][0],
                    "ms": round(sum(oks) / len(oks), 1) if oks else None,
                    "relay": round(sum(rls) / len(rls), 1) if rls else None})
    return out


def lat_stats(did, days=7, end=None, points=150):
    end = end if end is not None else time.time()
    cut = datetime.fromtimestamp(end - days * 86400, timezone.utc).isoformat(timespec="seconds")
    with db() as c:
        rows = c.execute("SELECT ts, ms, relay_ms FROM latency WHERE device_id=? AND ts>=? ORDER BY ts",
                         (did, cut)).fetchall()
    pts = [(epoch(r["ts"]), r["ms"], r["relay_ms"]) for r in rows]
    oks = sorted(m for _, m, _ in pts if m is not None)
    rls = sorted(r for _, _, r in pts if r is not None)
    diffs = [abs(b[1] - a[1]) for a, b in zip(pts, pts[1:]) if a[1] is not None and b[1] is not None]
    r1 = lambda v: None if v is None else round(v, 1)
    return {
        "days": days, "samples": len(pts), "ok": len(oks),
        "loss_pct": round(100 * (len(pts) - len(oks)) / len(pts), 1) if pts else None,
        "min": r1(oks[0] if oks else None), "median": r1(pctl(oks, 50)), "p95": r1(pctl(oks, 95)),
        "max": r1(oks[-1] if oks else None),
        "avg": r1(sum(oks) / len(oks)) if oks else None,
        "jitter": r1(sum(diffs) / len(diffs)) if diffs else None,
        "relay_median": r1(pctl(rls, 50)),
        "series": downsample(pts, points),
    }


def purge_archive(c, older_than_days=None, dry_run=False):
    """Permanently drop archived devices (all, or those archived more than N days ago), plus
    what we keep about them. The event log is history and is left alone. Returns the ids."""
    if older_than_days is None:
        rows = c.execute("SELECT id FROM archive").fetchall()
    else:
        cut = datetime.fromtimestamp(time.time() - older_than_days * 86400, timezone.utc).isoformat(timespec="seconds")
        rows = c.execute("SELECT id FROM archive WHERE archived_at < ?", (cut,)).fetchall()
    ids = [r["id"] for r in rows]
    if not dry_run:
        for did in ids:
            for t, col in (("archive", "id"), ("labels", "device_id"), ("notes", "device_id"),
                           ("latency", "device_id"), ("presence", "device_id")):
                c.execute(f"DELETE FROM {t} WHERE {col}=?", (did,))
    return ids


def shape(row, labels, archived=False, notes=None):
    d = json.loads(row["data"])
    out = {
        "id": row["id"],
        "name": d.get("name"),
        "hostname": d.get("hostname"),
        "addresses": d.get("addresses", []),
        "category": labels.get(row["id"]),
        "note": (notes or {}).get(row["id"]),
        "exit_node": exit_node_status(d),
        "subnets": subnet_info(d),
        "tags": d.get("tags", []),
        "client_version": d.get("clientVersion"),
        "update_available": d.get("updateAvailable"),
        "authorized": d.get("authorized"),
        "key_expires": d.get("expires"),
        "online": False if archived else is_online(d),
        "last_seen": d.get("lastSeen"),
        "ssh_open": row["ssh_open"],
        "ts_ssh": d.get("sshEnabled"),  # Tailscale SSH, as reported by the API
        "https_open": row["https_open"],
        "ping_ms": None if archived or "rtt_ms" not in row.keys() else row["rtt_ms"],
        "relay": None if archived else relay_info(d),
        "first_seen": row["first_seen"],
        "raw": d,
    }
    if archived:
        out["archived_at"] = row["archived_at"]
    return out


def active_items():
    with db() as c:
        rows = c.execute("SELECT * FROM devices ORDER BY name").fetchall()
    labels, notes, up = labels_map(), notes_map(), uptime_map()
    items = [shape(r, labels, notes=notes) for r in rows]
    for it in items:
        it["uptime"] = up.get(it["id"])
    return items


def archived_items():
    with db() as c:
        rows = c.execute("SELECT * FROM archive ORDER BY archived_at DESC").fetchall()
    labels, notes = labels_map(), notes_map()
    return [shape(r, labels, archived=True, notes=notes) for r in rows]


@app.get("/api/devices")
def api_devices():
    return jsonify(active_items())


@app.get("/api/archive")
def api_archive():
    return jsonify(archived_items())


@app.delete("/api/archive/<did>")
def api_archive_delete(did):
    with lock, db() as c:
        for t, col in (("archive", "id"), ("labels", "device_id"), ("notes", "device_id"),
                       ("latency", "device_id"), ("presence", "device_id")):
            c.execute(f"DELETE FROM {t} WHERE {col}=?", (did,))
    return "", 204


@app.post("/api/archive/purge")
def api_archive_purge():
    body = request.get_json(silent=True) or {}
    days = body.get("older_than_days")
    if days is not None:
        try:
            days = float(days)
            if days < 0:
                raise ValueError
        except (TypeError, ValueError):
            abort(400, "older_than_days must be a non-negative number")
    with lock, db() as c:
        ids = purge_archive(c, days, dry_run=bool(body.get("dry_run")))
    return jsonify({"count": len(ids), "ids": ids, "dry_run": bool(body.get("dry_run"))})


@app.put("/api/devices/<did>/note")
def api_note(did):
    body = request.get_json(silent=True) or {}
    note = body.get("note")
    if note is not None and not isinstance(note, str):
        abort(400, "note must be text")
    note = (note or "").strip()
    if len(note) > NOTE_MAX:
        abort(400, f"note is limited to {NOTE_MAX} characters")
    with lock, db() as c:
        known = c.execute("SELECT 1 FROM devices WHERE id=? UNION SELECT 1 FROM archive WHERE id=?",
                          (did, did)).fetchone()
        if not known:
            abort(404, "unknown device")
        if note:
            c.execute("INSERT OR REPLACE INTO notes (device_id, note, updated_at) VALUES (?,?,?)",
                      (did, note, now()))
        else:
            c.execute("DELETE FROM notes WHERE device_id=?", (did,))
    return jsonify({"id": did, "note": note or None})


@app.get("/api/devices/<did>/latency")
def api_latency(did):
    try:
        days = min(max(float(request.args.get("days", 7)), 0.04), 365)
    except ValueError:
        abort(400, "bad days")
    return jsonify(lat_stats(did, days))


def csv_cell(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, (list, tuple)):
        v = "; ".join(str(x) for x in v)
    v = str(v)
    # stop spreadsheet apps from running a cell that starts like a formula
    return "'" + v if v and v[0] in "=+-@\t\r" else v


def send_table(rows, fields, name, fmt):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    if fmt == "json":
        return Response(json.dumps(rows, indent=2), mimetype="application/json",
                        headers={"Content-Disposition": f'attachment; filename="{name}-{stamp}.json"'})
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(fields)
    for r in rows:
        w.writerow([csv_cell(r.get(f)) for f in fields])
    return Response("\ufeff" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{name}-{stamp}.csv"'})


DEVICE_FIELDS = ["name", "hostname", "status", "type", "ip", "online", "last_seen", "first_seen", "archived_at",
                 "exit_node", "subnets_advertised", "subnets_approved", "tailscale_ssh", "sshd_22", "https_443",
                 "version", "update_available", "key_expiry_disabled", "key_expires", "authorized", "tags", "uptime_7d", "uptime_30d",
                 "ping_ms", "relay", "relay_ms", "note"]


def flat_device(it, status):
    ip = next((a for a in it["addresses"] if "." in a), None)
    up = it.get("uptime") or {}
    return {
        "name": it["name"], "hostname": it["hostname"], "status": status, "type": it["category"], "ip": ip,
        "online": it["online"], "last_seen": it["last_seen"], "first_seen": it["first_seen"],
        "archived_at": it.get("archived_at"), "exit_node": it["exit_node"],
        "subnets_advertised": it["subnets"]["advertised"], "subnets_approved": it["subnets"]["approved"],
        "tailscale_ssh": it["ts_ssh"],
        "sshd_22": {0: "closed", 1: "sshd", 2: "tailscale"}.get(it["ssh_open"]),
        "https_443": None if it["https_open"] is None else bool(it["https_open"]),
        "version": short_ver(it["client_version"]), "update_available": it["update_available"],
        "key_expiry_disabled": bool((it["raw"] or {}).get("keyExpiryDisabled")),
        "key_expires": None if (it["raw"] or {}).get("keyExpiryDisabled") else it["key_expires"], "authorized": it["authorized"], "tags": it["tags"],
        "uptime_7d": up.get("7"), "uptime_30d": up.get("30"), "ping_ms": it["ping_ms"],
        "relay": (it["relay"] or {}).get("name"), "relay_ms": (it["relay"] or {}).get("ms"), "note": it["note"],
    }


@app.get("/api/export/devices")
def api_export_devices():
    fmt = "json" if request.args.get("format") == "json" else "csv"
    scope = request.args.get("scope", "all")
    if scope not in ("active", "archived", "all"):
        abort(400, "scope must be active, archived or all")
    src = []
    if scope in ("active", "all"):
        src += [(it, "active") for it in active_items()]
    if scope in ("archived", "all"):
        src += [(it, "archived") for it in archived_items()]
    rows = []
    for it, status in src:
        r = flat_device(it, status)
        if fmt == "json" and request.args.get("raw") == "1":
            r["raw"] = it["raw"]
        rows.append(r)
    return send_table(rows, DEVICE_FIELDS, "tailscale-devices", fmt)


@app.get("/api/export/events")
def api_export_events():
    fmt = "json" if request.args.get("format") == "json" else "csv"
    try:
        days = float(request.args["days"]) if request.args.get("days") else None
    except ValueError:
        abort(400, "bad days")
    sql, args = "SELECT * FROM events", []
    if days is not None:
        sql += " WHERE ts >= ?"
        args.append(datetime.fromtimestamp(time.time() - days * 86400, timezone.utc).isoformat(timespec="seconds"))
    with db() as c:
        rows = c.execute(sql + " ORDER BY id DESC", args).fetchall()
    out = []
    for r in rows:
        try:
            detail = json.loads(r["detail"]) if r["detail"] else None
        except ValueError:
            detail = None
        out.append({"time": r["ts"], "device": r["device_name"], "device_id": r["device_id"],
                    "event": r["kind"], "detail": detail if fmt == "json" else (json.dumps(detail) if detail else None)})
    return send_table(out, ["time", "device", "device_id", "event", "detail"], "tailscale-events", fmt)


@app.get("/api/categories")
def api_categories():
    return jsonify(CATEGORIES)


@app.put("/api/devices/<did>/category")
def api_set_category(did):
    cat = (request.get_json(silent=True) or {}).get("category") or None
    if cat is not None and cat not in CATEGORIES:
        abort(400, "unknown category")
    with lock, db() as c:
        old = c.execute("SELECT category FROM labels WHERE device_id=?", (did,)).fetchone()
        old = old["category"] if old else None
        if cat is None:
            c.execute("DELETE FROM labels WHERE device_id=?", (did,))
        else:
            c.execute("INSERT OR REPLACE INTO labels VALUES (?,?)", (did, cat))
        if cat != old:
            row = (c.execute("SELECT data, name FROM devices WHERE id=?", (did,)).fetchone()
                   or c.execute("SELECT data, name FROM archive WHERE id=?", (did,)).fetchone())
            if row:
                try:
                    name = json.loads(row["data"]).get("hostname") or row["name"]
                except ValueError:
                    name = row["name"]
                log_event(c, now(), did, name, "type", {"from": old, "to": cat})
    return "", 204



@app.get("/api/events")
def api_events():
    q = request.args
    try:
        limit = min(max(int(q.get("limit", 100)), 1), 500)
        before = int(q["before"]) if q.get("before") else None
        days = float(q["days"]) if q.get("days") else None
    except ValueError:
        abort(400, "bad parameter")
    where, args = [], []
    if q.get("device"):
        where.append("device_id = ?")
        args.append(q["device"])
    kinds = [k for k in q.get("kind", "").split(",") if k]
    if kinds:
        where.append("kind IN (%s)" % ",".join("?" * len(kinds)))
        args += kinds
    if before is not None:
        where.append("id < ?")
        args.append(before)
    if days is not None:
        where.append("ts >= ?")
        args.append(datetime.fromtimestamp(time.time() - days * 86400, timezone.utc).isoformat(timespec="seconds"))
    sql = "SELECT * FROM events" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT ?"
    with db() as c:
        rows = c.execute(sql, args + [limit + 1]).fetchall()
        names = c.execute("SELECT device_id, device_name, MAX(id) FROM events WHERE device_id IS NOT NULL "
                          "GROUP BY device_id ORDER BY device_name COLLATE NOCASE").fetchall()
        since = meta_get(c, "baselined")
    events = []
    for r in rows[:limit]:
        try:
            detail = json.loads(r["detail"]) if r["detail"] else {}
        except ValueError:
            detail = {}
        events.append({"id": r["id"], "ts": r["ts"], "device_id": r["device_id"],
                       "device_name": r["device_name"], "kind": r["kind"], "detail": detail})
    return jsonify({"events": events, "more": len(rows) > limit, "tracking_since": since,
                    "devices": [{"id": n["device_id"], "name": n["device_name"]} for n in names]})


@app.get("/api/dns")
def api_dns():
    return jsonify(dns_cache)


def key_info():
    """What the page may know about the key. The key itself is never sent back."""
    exp = api_key["expires"]
    days = None
    if exp:
        try:
            days = round((epoch(exp) - time.time()) / 86400, 1)
        except ValueError:
            pass
    return {"status": api_key["status"], "id": key_id(api_key["value"]),
            "expires": exp, "expires_in_days": days,
            "expiring": days is not None and days <= KEY_WARN_DAYS}


@app.get("/api/status")
def api_status():
    return jsonify({**state, "key": key_info()})


@app.post("/api/key")
def api_set_key():
    # Only a JSON body is accepted (no form posts), so another web site can't submit this
    # from a visitor's browser without a CORS preflight, which this server never allows.
    body = request.get_json(silent=True)
    key = (body or {}).get("key") if isinstance(body, dict) else None
    if not isinstance(key, str) or not key.strip():
        return jsonify(error="Enter an API access token."), 400
    key = key.strip()
    if key.startswith("tskey-auth-"):
        return jsonify(error="That is an auth key (for adding machines). The dashboard needs an API access token, tskey-api-…"), 400
    if not key.startswith("tskey-api-") or any(ch.isspace() for ch in key) or len(key) > 200:
        return jsonify(error="That doesn't look like an API access token. It should start with tskey-api-"), 400
    try:
        fetch_devices(key)  # check it before replacing a working key
    except KeyRejected:
        return jsonify(error="Tailscale rejected this key. Check it was copied completely and hasn't expired or been revoked."), 400
    except requests.RequestException as e:
        return jsonify(error=f"Couldn't reach Tailscale to check the key: {e}"), 502
    try:
        write_key_file(key)
    except OSError as e:
        return jsonify(error=f"The key works, but it couldn't be saved to {KEY_PATH}: {e}"), 500
    api_key.update(value=key, status="ok", expires=None, expires_checked=0.0)
    refresh_key_expiry(force=True)
    state["last_error"] = None
    wake.set()  # sync now rather than at the next scheduled poll
    return jsonify(key_info())


@app.get("/")
def index():
    return send_from_directory("static", "index.html")


if __name__ == "__main__":
    init_db()
    threading.Thread(target=poll_loop, daemon=True).start()
    app.run(host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8080")))

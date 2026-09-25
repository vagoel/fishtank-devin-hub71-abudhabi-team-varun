#!/usr/bin/env python3
"""Set the HeatGuard wearable's WiFi and backend link over USB.

    .venv/bin/python scripts/heatguard/set_wifi.py                    # prompts for SSID and password
    .venv/bin/python scripts/heatguard/set_wifi.py --ssid SiteWiFi    # prompts for the password only
    .venv/bin/python scripts/heatguard/set_wifi.py --url ws://192.168.1.10:8000/v1/device/ws
    .venv/bin/python scripts/heatguard/set_wifi.py --token            # prompts for the device token
    .venv/bin/python scripts/heatguard/set_wifi.py --clear-url        # back to the Cloud Run default

The password and the token are read with getpass: not echoed, not in shell history,
never printed and never written to disk. Everything goes into the wearable's NVS
(namespace 'uiflow'): ssid0/pswd0 (the same place UiFlow keeps them), hg_url (the
backend WebSocket, ws:// or wss://; unset = the Cloud Run service), hg_token (the
shared device token; unset = none) and hg_worker (who wears it, for incidents: a
worker id or a JSON object; unset = the backend assigns one).

WiFi is only changed when --ssid is given or no link option is. If a local HeatGuard
server holds the USB serial port, this asks it to let go first
(POST /api/v1/bridge {"enabled": false}) and hands it back after.
"""
import argparse
import getpass
import glob
import json
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
API = "http://127.0.0.1:8000/api/v1/bridge"


def find_port(port):
    if port:
        return port
    ports = sorted(glob.glob("/dev/cu.usbmodem*"))
    if not ports:
        sys.exit("No wearable found on /dev/cu.usbmodem*. Plug it in over USB or pass --port.")
    if len(ports) > 1:
        print("Several USB serial ports found, using %s (pass --port to choose): %s"
              % (ports[0], ", ".join(ports)))
    return ports[0]


def mpremote_bin():
    for local in (ROOT / ".venv" / "bin" / "mpremote", ROOT / "backend" / ".venv" / "bin" / "mpremote"):
        if local.exists():
            return str(local)
    found = shutil.which("mpremote")
    if not found:
        sys.exit("mpremote not found. Install it into .venv: .venv/bin/pip install mpremote")
    return found


def bridge(api, enabled):
    """Ask a running server to release (False) or retake (True) the serial port. Best effort."""
    req = urllib.request.Request(api, data=json.dumps({"enabled": enabled}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            r.read()
        return True
    except Exception:
        return False


def build_code(sets, clears):
    lines = ["import esp32", "n = esp32.NVS('uiflow')"]
    for key, value in sets:
        lines.append("n.set_str(%r, %r)" % (key, value))
    for key in clears:
        lines += ["try:", "    n.erase_key(%r)" % key, "except OSError:", "    pass"]
    lines += ["n.commit()", "print('HG_NVS_OK')"]
    return "\n".join(lines)


def check_url(url):
    url = url.strip()
    if not (url.startswith("ws://") or url.startswith("wss://")):
        sys.exit("--url must start with ws:// or wss://, e.g. wss://host/v1/device/ws")
    if len(url) > 200 or " " in url or "?" in url:
        sys.exit("--url: no spaces or query string (device_id and token are added by the firmware).")
    return url


def check_worker(worker):
    worker = worker.strip()
    if worker.startswith("{"):
        try:
            if not isinstance(json.loads(worker), dict):
                raise ValueError
        except ValueError:
            sys.exit('--worker must be a worker id or a JSON object like {"name": "Ravi Kumar", "worker_id": "W-0042"}')
    if not worker or len(worker.encode()) > 400:
        sys.exit("--worker must be 1-400 bytes.")
    return worker


def main():
    ap = argparse.ArgumentParser(description="Set the HeatGuard wearable's WiFi and backend link over USB.")
    ap.add_argument("--ssid", help="network name (prompted if omitted and no link option is given)")
    ap.add_argument("--url", help="backend WebSocket, ws:// or wss://host[:port]/v1/device/ws (NVS hg_url)")
    ap.add_argument("--clear-url", action="store_true", help="erase hg_url: back to the Cloud Run default")
    ap.add_argument("--token", action="store_true", help="prompt for the device token (NVS hg_token, hidden)")
    ap.add_argument("--clear-token", action="store_true", help="erase hg_token")
    ap.add_argument("--worker", help="worker id or JSON object for incidents (NVS hg_worker)")
    ap.add_argument("--clear-worker", action="store_true", help="erase hg_worker")
    ap.add_argument("--port", help="serial port (default: first /dev/cu.usbmodem*)")
    ap.add_argument("--api", default=API, help="server bridge endpoint (default %(default)s)")
    args = ap.parse_args()

    link_opts = (args.url or args.clear_url or args.token or args.clear_token
                 or args.worker or args.clear_worker)
    sets, clears, notes = [], [], []
    secrets = []

    if args.ssid is not None or not link_opts:
        ssid = args.ssid if args.ssid is not None else input("WiFi SSID: ")
        ssid = ssid.strip()
        if not ssid or len(ssid.encode()) > 32:
            sys.exit("SSID must be 1-32 bytes.")
        password = getpass.getpass("WiFi password for %r (hidden, empty for an open network): " % ssid)
        if password and not 8 <= len(password) <= 63:
            sys.exit("WPA/WPA2 passwords are 8-63 characters.")
        if password and getpass.getpass("Again: ") != password:
            sys.exit("Passwords don't match.")
        sets += [("ssid0", ssid), ("pswd0", password)]
        if password:
            secrets.append(password)
        notes.append("WiFi %r (password hidden)" % ssid)
        password = None

    if args.url and args.clear_url:
        sys.exit("Use --url or --clear-url, not both.")
    if args.url:
        sets.append(("hg_url", check_url(args.url)))
        notes.append("backend %s" % args.url.strip())
    elif args.clear_url:
        clears.append("hg_url")
        notes.append("backend back to the Cloud Run default")

    if args.token and args.clear_token:
        sys.exit("Use --token or --clear-token, not both.")
    if args.token:
        token = getpass.getpass("Device token (hidden): ").strip()
        if not token or len(token) > 200 or any(c.isspace() for c in token):
            sys.exit("The token must be 1-200 characters without spaces.")
        sets.append(("hg_token", token))
        secrets.append(token)
        notes.append("device token (hidden)")
        token = None
    elif args.clear_token:
        clears.append("hg_token")
        notes.append("device token erased")

    if args.worker and args.clear_worker:
        sys.exit("Use --worker or --clear-worker, not both.")
    if args.worker:
        sets.append(("hg_worker", check_worker(args.worker)))
        notes.append("worker %s" % args.worker.strip())
    elif args.clear_worker:
        clears.append("hg_worker")
        notes.append("worker erased")

    port = find_port(args.port)
    mp = mpremote_bin()
    code = build_code(sets, clears)
    sets = None

    released = bridge(args.api, False)
    if released:
        print("Server released the serial port.")
        time.sleep(1.0)
    try:
        # The code string is passed as an argument, never saved to a file.
        r = subprocess.run([mp, "connect", port, "exec", code], capture_output=True, text=True, timeout=60)
        output = (r.stdout or "") + (r.stderr or "")
        for secret in secrets:
            output = output.replace(repr(secret)[1:-1], "***").replace(secret, "***")
        if r.returncode != 0 or "HG_NVS_OK" not in output:
            print(output.strip()[-800:])
            sys.exit("Could not write the settings (mpremote exit %d)." % r.returncode)
        print("Saved on the wearable: %s." % "; ".join(notes))
        subprocess.run([mp, "connect", port, "reset"], capture_output=True, text=True, timeout=30)
        print("Wearable reset. It joins WiFi and connects to the backend in a few seconds.")
    except subprocess.TimeoutExpired:
        sys.exit("mpremote timed out talking to %s." % port)
    finally:
        code = None
        secrets = None
        if released:
            time.sleep(3.0)  # let the board re-enumerate after the reset
            if bridge(args.api, True):
                print("Server serial bridge re-enabled.")


if __name__ == "__main__":
    main()

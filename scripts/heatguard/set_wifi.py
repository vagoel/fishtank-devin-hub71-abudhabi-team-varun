#!/usr/bin/env python3
"""Set the HeatGuard wearable's WiFi over USB.

    .venv/bin/python scripts/set_wifi.py                    # prompts for SSID and password
    .venv/bin/python scripts/set_wifi.py --ssid SiteWiFi    # prompts for the password only
    .venv/bin/python scripts/set_wifi.py --ssid SiteWiFi --server 192.168.1.10

The password is read with getpass: it is not echoed, not in shell history, never
printed and never written to disk. The credentials go into the wearable's NVS
(namespace 'uiflow', keys ssid0/pswd0, the same place UiFlow keeps them), plus
'hg_server' when --server pins a gateway IP instead of using the discovery beacon.

If the HeatGuard server is running it holds the USB serial port, so this asks it
to let go first (POST /api/v1/bridge {"enabled": false}) and hands it back after.
"""
import argparse
import getpass
import glob
import ipaddress
import json
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
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
    local = ROOT / ".venv" / "bin" / "mpremote"
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


def build_code(ssid, password, server):
    lines = [
        "import esp32",
        "n = esp32.NVS('uiflow')",
        "n.set_str('ssid0', %r)" % ssid,
        "n.set_str('pswd0', %r)" % password,
    ]
    if server:
        lines.append("n.set_str('hg_server', %r)" % server)
    lines += ["n.commit()", "print('HG_WIFI_OK')"]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Set the HeatGuard wearable's WiFi over USB.")
    ap.add_argument("--ssid", help="network name (prompted if omitted)")
    ap.add_argument("--server", help="fixed gateway IP (optional; default is auto-discovery)")
    ap.add_argument("--port", help="serial port (default: first /dev/cu.usbmodem*)")
    ap.add_argument("--api", default=API, help="server bridge endpoint (default %(default)s)")
    args = ap.parse_args()

    server = None
    if args.server:
        try:
            server = str(ipaddress.ip_address(args.server.strip()))
        except ValueError:
            sys.exit("--server must be an IP address, e.g. 192.168.1.10")

    ssid = args.ssid if args.ssid is not None else input("WiFi SSID: ")
    ssid = ssid.strip()
    if not ssid or len(ssid.encode()) > 32:
        sys.exit("SSID must be 1-32 bytes.")
    password = getpass.getpass("WiFi password for %r (hidden, empty for an open network): " % ssid)
    if password and not 8 <= len(password) <= 63:
        sys.exit("WPA/WPA2 passwords are 8-63 characters.")
    if password and getpass.getpass("Again: ") != password:
        sys.exit("Passwords don't match.")

    port = find_port(args.port)
    mp = mpremote_bin()
    code = build_code(ssid, password, server)

    released = bridge(args.api, False)
    if released:
        print("Server released the serial port.")
        time.sleep(1.0)
    try:
        # The code string is passed as an argument, never saved to a file.
        r = subprocess.run([mp, "connect", port, "exec", code], capture_output=True, text=True, timeout=60)
        output = (r.stdout or "") + (r.stderr or "")
        if password:
            output = output.replace(repr(password)[1:-1], "***").replace(password, "***")
        if r.returncode != 0 or "HG_WIFI_OK" not in output:
            print(output.strip()[-800:])
            sys.exit("Could not write the WiFi settings (mpremote exit %d)." % r.returncode)
        print("Saved WiFi %r%s on the wearable (password hidden)."
              % (ssid, (", gateway " + server) if server else ""))
        subprocess.run([mp, "connect", port, "reset"], capture_output=True, text=True, timeout=30)
        print("Wearable reset. It joins %r and finds the gateway%s in a few seconds."
              % (ssid, "" if server else " via the discovery beacon"))
    except subprocess.TimeoutExpired:
        sys.exit("mpremote timed out talking to %s." % port)
    finally:
        password = code = None
        if released:
            time.sleep(3.0)  # let the board re-enumerate after the reset
            if bridge(args.api, True):
                print("Server serial bridge re-enabled.")


if __name__ == "__main__":
    main()

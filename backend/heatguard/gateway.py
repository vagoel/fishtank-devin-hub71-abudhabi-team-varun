"""LAN device transports (only started with HEATGUARD_LAN=1; Cloud Run has no UDP or USB).

WiFi (primary): wearables stream JSON datagrams to UDP :47800. UDP keeps the
50 Hz sampling loop on the wrist non-blocking; a slow or dropped packet never
stalls fall detection. Alarm events carry a sequence number and are resent
until the server acks them, so the one thing that must arrive, does.

Discovery: the server broadcasts a small beacon on UDP :47801 every 2 s, so a
wearable finds the gateway on any network without being told its IP.

USB serial (fallback): same JSON lines, prefixed with '@'. Used only while the
wearable has no WiFi.
"""
import asyncio
import glob
import json
import os
import re
import socket
import subprocess
import threading
import time

UDP_PORT = int(os.environ.get("HEATGUARD_UDP_PORT", "47800"))
BEACON_PORT = int(os.environ.get("HEATGUARD_BEACON_PORT", "47801"))
VOICE_PORT = int(os.environ.get("HEATGUARD_VOICE_PORT", "47802"))


def parse(raw):
    s = raw.strip()
    if s.startswith("@"):
        s = s[1:]
    if not s.startswith("{"):
        return None
    try:
        return json.loads(s)
    except ValueError:
        return None


class UdpProto(asyncio.DatagramProtocol):
    def __init__(self, on_msg):
        self.on_msg = on_msg
        self.transport = None
        self.rx = 0

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        self.rx += 1
        obj = parse(data.decode("utf-8", "replace"))
        if obj:
            self.on_msg(obj, "wifi", addr)


class UdpGateway:
    def __init__(self, on_msg):
        self.proto = UdpProto(on_msg)

    async def start(self):
        loop = asyncio.get_running_loop()
        await loop.create_datagram_endpoint(lambda: self.proto, local_addr=("0.0.0.0", UDP_PORT))

    def stop(self):
        if self.proto.transport:
            self.proto.transport.close()
            self.proto.transport = None

    def send(self, addr, obj):
        if self.proto.transport and addr:
            self.proto.transport.sendto((json.dumps(obj, separators=(",", ":")) + "\n").encode(), addr)


def _ifaces():
    """[(ip, broadcast)] for non-loopback IPv4 interfaces, macOS ifconfig or Linux ip."""
    try:
        txt = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=3).stdout
        found = re.findall(r"inet (?!127\.)(\d+\.\d+\.\d+\.\d+) netmask \S+(?: broadcast (\S+))?", txt)
        if found:
            return found
    except Exception:
        pass
    try:
        txt = subprocess.run(["ip", "-o", "-4", "addr"], capture_output=True, text=True, timeout=3).stdout
        return re.findall(r"inet (?!127\.)(\d+\.\d+\.\d+\.\d+)/\d+(?: brd (\S+))?", txt)
    except Exception:
        return []


def broadcast_addresses():
    return sorted({"255.255.255.255"} | {b for _, b in _ifaces() if b})


def lan_ips():
    return [ip for ip, _ in _ifaces()]


async def beacon(http_port, every=2.0):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setblocking(False)
    msg = json.dumps({"hg": 1, "udp": UDP_PORT, "http": http_port, "voice": VOICE_PORT}).encode()
    addrs, refreshed = broadcast_addresses(), time.time()
    while True:
        if time.time() - refreshed > 30:
            addrs, refreshed = broadcast_addresses(), time.time()
        for a in addrs:
            try:
                sock.sendto(msg, (a, BEACON_PORT))
            except OSError:
                pass
        await asyncio.sleep(every)


class SerialBridge:
    """Reads the wearable over USB when it has no WiFi. Runs in a thread."""

    def __init__(self, loop, on_msg, port=None):
        self.loop = loop
        self.on_msg = on_msg
        self.port_hint = port
        self.enabled = os.environ.get("HEATGUARD_SERIAL", "1") != "0"
        self.port = None
        self.connected = False
        self._ser = None
        self._lock = threading.Lock()
        self._stop = False
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self._stop, self.enabled = True, False

    def _find(self):
        if self.port_hint:
            return self.port_hint
        ports = sorted(glob.glob("/dev/cu.usbmodem*") + glob.glob("/dev/ttyACM*"))
        return ports[0] if ports else None

    def _run(self):
        import serial
        while not self._stop:
            if not self.enabled:
                self._close()
                time.sleep(0.5)
                continue
            port = self._find()
            if not port:
                time.sleep(2)
                continue
            try:
                ser = serial.Serial(port, 115200, timeout=0.3)
                with self._lock:
                    self._ser, self.port, self.connected = ser, port, True
                self.send({"cmd": "hello"})
                while self.enabled:
                    line = ser.readline()
                    if not line:
                        continue
                    obj = parse(line.decode("utf-8", "replace"))
                    if obj:
                        self.loop.call_soon_threadsafe(self.on_msg, obj, "serial", None)
            except Exception:
                pass
            self._close()
            time.sleep(2)

    def _close(self):
        with self._lock:
            if self._ser:
                try:
                    self._ser.close()
                except Exception:
                    pass
            self._ser, self.connected = None, False

    def send(self, obj):
        with self._lock:
            if not self._ser:
                return False
            try:
                self._ser.write((json.dumps(obj, separators=(",", ":")) + "\n").encode())
                return True
            except Exception:
                return False

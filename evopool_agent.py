#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
evopool_agent — PROOF OF CONCEPT / TEMPLATE.

A tiny local HTTP bridge to an Espa "Silen" variable-speed pool pump, so you can
automate it from Home Assistant — something the Espa *evopool* app does not allow.

================================  READ THIS  ===================================
This is a PROOF OF CONCEPT and a TEMPLATE, not a finished product.

The pump's Bluetooth protocol is PROPRIETARY to Espa and is *intentionally not
included here*. The only two device-specific functions — `ble_read()` and
`ble_set()` — are left as STUBS. Decoding the protocol of *your own* pump is the
interesting part and is easy to reproduce yourself: capture the Bluetooth traffic
between the evopool app and the pump, then let an AI assistant help you make sense
of it (see README.md → "Method"). Fill in the two stubs and you are done.

Everything ELSE in this file is generic and reusable as-is:
  - the HTTP service (GET /state, POST /set, POST /pause|/resume) with Basic auth,
  - on-demand reads with a small cache (Home Assistant drives the cadence),
  - single-link BLE handling: connect -> talk -> DISCONNECT every time (+ /pause),
    so the evopool app can still connect when you need it.

Not affiliated with Espa. No warranty whatsoever. Use entirely at your own risk.
===============================================================================
"""
import json, time, os, re, subprocess, configparser

CONF = os.environ.get("EVOPOOL_CONFIG", os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.ini"))
_cf = configparser.ConfigParser(); _cf.read(CONF)
_ble = _cf["ble"] if _cf.has_section("ble") else {}
MAC     = _ble.get("mac", "AA:BB:CC:DD:EE:FF")          # your pump's BLE address
ADAPTER = _ble.get("adapter", "hci0")
CHARSEG = _ble.get("char", "serviceXXXX/charYYYY")      # GATT path of your pump's control characteristic
CHAR    = "/org/bluez/%s/dev_%s/%s" % (ADAPTER, MAC.replace(":", "_"), CHARSEG)


# =====================  DEVICE-SPECIFIC  —  IMPLEMENT YOURSELF  =================
# These two functions are the ONLY pump-specific parts of the project. The Espa
# protocol is proprietary and deliberately not published. Decode your own pump
# (see README → "Method") and implement them. The bt_session() helper below gives
# you a generic connect / notify / write / disconnect scaffold to build on.

def ble_read():
    """Return the pump's current state, e.g.:
        {"mode": "filtration", "niveau": 4, "puissance": 312}
    Implement: connect, request the status, parse the reply.
    (Two values are enough to automate everything: the running mode and the speed.)"""
    raise NotImplementedError("Decode your pump's protocol and implement ble_read() — see README.md")


def ble_set(param, value):
    """Apply ONE configuration setting and return (ok: bool, state: dict).
    The pump config holds SEVERAL writable fields — everything on the app's
    configuration screen: the two filtration speeds, the two backwash speeds, the
    constant-mode speed, the cycle/first-filtration durations and the ramp. `param`
    selects which one. Our Home Assistant examples only schedule the low filtration
    speed, but any field works.
    Implement: read current config, change the selected field, write it back."""
    raise NotImplementedError("Decode your pump's protocol and implement ble_set() — see README.md")
# ===============================================================================


def bt_session(frames, scan=False):
    """Generic BlueZ scaffold: connect to the pump, select its control
    characteristic, enable notifications, write each frame (write-without-response),
    capture the notifications, then DISCONNECT (so the evopool app can connect).
    `frames` is a list of `bytes` you build in ble_read()/ble_set(). Returns the
    captured notification bytes. (Generic — no proprietary content here.)"""
    conn = (["echo 'scan on'", "sleep 5", "echo 'scan off'"] if scan else [])
    cmds = conn + ["echo 'connect %s'" % MAC, "sleep 11",
                   "echo 'menu gatt'", "echo 'select-attribute %s'" % CHAR, "echo 'notify on'", "sleep 1"]
    for fr in frames:
        cmds += ['echo \'write "%s" 0 command\'' % " ".join("0x%02x" % b for b in fr), "sleep 1"]
    cmds += ["sleep 1", "echo 'disconnect %s'" % MAC, "sleep 2", "echo quit"]
    out = subprocess.run(["sh", "-c", "( " + " ; ".join(cmds) + " ) | bluetoothctl 2>&1"],
                         capture_output=True, text=True, timeout=160).stdout
    out = re.sub(r"\x1b\[[0-9;]*m", "", out)
    s = []
    for ln in out.splitlines():
        for m in re.finditer(r"((?:[0-9a-f]{2} ){4,16})", ln):
            s += [int(b, 16) for b in m.group(1).split()]
    return bytes(s)


def bt_disconnect():
    try: subprocess.run(["sh", "-c", "echo 'disconnect %s' | bluetoothctl >/dev/null 2>&1" % MAC], timeout=15)
    except Exception: pass


# ----------------------  HTTP web-service (generic)  ---------------------------
def webservice():
    import http.server, base64, threading, queue
    w = _cf["web"] if _cf.has_section("web") else {}
    port = int(w.get("port", 8095)); user = w.get("user", "admin"); pw = w.get("pass", "changeme")
    ttl = int(w.get("ttl", 30))     # anti-burst cache: skip a BLE read if cached value is younger than ttl
    cred = "Basic " + base64.b64encode(("%s:%s" % (user, pw)).encode()).decode()
    cache = {"state": {"mode": "unknown"}, "ts": 0.0}
    ble_lock = threading.Lock(); ctrl = {"paused_until": 0.0}; setq = queue.Queue()

    def safe(fn, *a):
        try: return fn(*a)
        except NotImplementedError as e: return {"error": str(e)}
        except Exception as e: return {"error": "%s" % e}

    def get_state():
        paused = time.time() < ctrl["paused_until"]
        if not paused:
            with ble_lock:
                if time.time() - cache["ts"] >= ttl:
                    r = safe(ble_read)
                    if isinstance(r, dict) and "error" not in r and r:
                        cache["state"] = r; cache["ts"] = time.time()
                    elif isinstance(r, dict) and "error" in r:
                        cache["state"] = r
        st = dict(cache["state"]); st["paused"] = paused; st["age_s"] = int(time.time() - cache["ts"])
        return st

    def set_worker():
        while True:
            p, v = setq.get()
            if time.time() < ctrl["paused_until"]: continue
            with ble_lock:
                r = safe(ble_set, p, v)
                if isinstance(r, tuple) and len(r) == 2 and isinstance(r[1], dict):
                    cache["state"] = r[1]; cache["ts"] = time.time()
    threading.Thread(target=set_worker, daemon=True).start()

    class H(http.server.BaseHTTPRequestHandler):
        def _ok(self):
            if self.headers.get("Authorization", "") == cred: return True
            self.send_response(401); self.send_header("WWW-Authenticate", 'Basic realm="evopool"'); self.end_headers(); return False
        def _send(self, code, obj):
            b = json.dumps(obj).encode()
            self.send_response(code); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
        def _body(self):
            try: return json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            except Exception: return {}
        def do_GET(self):
            if not self._ok(): return
            self._send(200, get_state()) if self.path.split("?")[0] == "/state" else self._send(404, {"err": "not found"})
        def do_POST(self):
            if not self._ok(): return
            p = self.path.split("?")[0]
            if p == "/set":
                d = self._body(); setq.put((d.get("param"), d.get("valeur", d.get("value")))); self._send(202, {"queued": d})
            elif p == "/pause":
                d = self._body()
                try: mins = float(d.get("minutes", 5))
                except Exception: mins = 5
                ctrl["paused_until"] = time.time() + mins * 60; bt_disconnect()
                self._send(200, {"paused": True, "minutes": mins})
            elif p == "/resume":
                ctrl["paused_until"] = 0.0; self._send(200, {"paused": False})
            else: self._send(404, {"err": "not found"})
        def log_message(self, *a): pass
    print("evopool_agent on :%d  (implement ble_read/ble_set first)" % port)
    http.server.ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()


if __name__ == "__main__":
    import sys
    a = sys.argv[1:]
    if a and a[0] == "read":
        print(json.dumps(ble_read(), ensure_ascii=False))
    elif a and a[0] == "set" and len(a) == 3:
        print(ble_set(a[1], a[2]))
    else:
        webservice()

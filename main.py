#!/usr/bin/env python3
"""
Bambu Lab mini progress widget.

A small, borderless, always-on-top card showing your current print progress.
Settings are saved after the first run, so afterwards just:

    python main.py

First run walks you through setup (cloud login or local LAN).
Other options:
    python main.py --login      # redo cloud login (e.g. token expired)
    python main.py --reset      # forget saved settings
    python main.py --demo       # preview without a printer

Controls: drag to move, double-click or right-click for compact mode / quit.
Requires:  pip install paho-mqtt
"""
import argparse
import getpass
import json
import math
import os
import random
import ssl
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont

APP = "bambu-widget"

# ---------------------------------------------------------------- palette
KEY = "#010203"  # transparent colour key (Windows) for rounded corners
CARD = "#16171b"
EDGE = "#2b2d35"
TRACK = "#2a2c34"
TEXT = "#f4f5f7"
DIM = "#8d909b"
RED = "#ff5c6c"
ORANGE = "#ff9f43"
BLUE = "#4aa8ff"
GREEN = "#3ddc97"
YELLOW = "#f5c542"

STATES = {
    "RUNNING": ("Printing", GREEN),
    "PREPARE": ("Preparing", YELLOW),
    "SLICING": ("Preparing", YELLOW),
    "INIT": ("Preparing", YELLOW),
    "PAUSE": ("Paused", ORANGE),
    "FINISH": ("Done", BLUE),
    "FAILED": ("Failed", RED),
    "IDLE": ("Idle", DIM),
}

# ---------------------------------------------------------------- config
def config_path():
    base = os.getenv("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, APP, "config.json")


def load_config():
    try:
        with open(config_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    path = config_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        try:
            os.chmod(path, 0o600)  # token = password, keep it private
        except OSError:
            pass
    except OSError as e:
        print("Could not save config:", e)


def is_ready(cfg):
    if cfg.get("mode") == "cloud":
        return all(cfg.get(k) for k in ("uid", "token", "serial"))
    if cfg.get("mode") == "local":
        return all(cfg.get(k) for k in ("ip", "serial", "code"))
    return False


# ---------------------------------------------------------------- data sources
data = {}
status = {"connected": False, "error": ""}
lock = threading.Lock()


def start_mqtt(host, serial, username, password, auth_hint):
    import paho.mqtt.client as mqtt

    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    except AttributeError:  # paho-mqtt 1.x
        client = mqtt.Client()

    client.username_pw_set(username, password)
    client.tls_set(cert_reqs=ssl.CERT_NONE)
    client.tls_insecure_set(True)

    report = f"device/{serial}/report"
    request = f"device/{serial}/request"

    def on_connect(c, userdata, flags, rc, *args):
        failed = rc.is_failure if hasattr(rc, "is_failure") else rc != 0
        with lock:
            status["connected"] = not failed
            status["error"] = auth_hint if failed else ""
        if not failed:
            c.subscribe(report)
            c.publish(request, json.dumps({"pushing": {"sequence_id": "1", "command": "pushall"}}))

    def on_disconnect(c, userdata, *args):
        with lock:
            status["connected"] = False

    def on_message(c, userdata, msg):
        try:
            payload = json.loads(msg.payload)
        except ValueError:
            return
        p = payload.get("print")
        if isinstance(p, dict):
            with lock:
                data.update(p)  # reports are partial updates -> merge

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=1, max_delay=15)
    client.connect_async(host, 8883, keepalive=30)
    client.loop_start()


def start_demo():
    def run():
        with lock:
            status["connected"] = True
        layers = 240
        while True:
            with lock:
                data.update(gcode_state="PREPARE", mc_percent=0, subtask_name="Benchy_demo.3mf",
                            total_layer_num=layers, layer_num=0, nozzle_temper=150, bed_temper=45,
                            mc_remaining_time=150)
            time.sleep(2)
            for pct in range(0, 101):
                with lock:
                    data.update(
                        gcode_state="PAUSE" if 55 <= pct < 58 else "RUNNING",
                        mc_percent=pct,
                        mc_remaining_time=max(0, int((100 - pct) * 1.5)),
                        layer_num=int(layers * pct / 100),
                        nozzle_temper=219.4 + random.random(),
                        bed_temper=60.0,
                    )
                time.sleep(0.35)
            with lock:
                data.update(gcode_state="FINISH", mc_percent=100, mc_remaining_time=0)
            time.sleep(6)

    threading.Thread(target=run, daemon=True).start()


# ---------------------------------------------------------------- cloud login
def cloud_login(cfg):
    import urllib.request

    api = "https://api.bambulab.com"

    def call(path, body=None, token=None):
        headers = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(
            api + path, data=json.dumps(body).encode() if body is not None else None, headers=headers
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())

    email = input("Bambu account email: ").strip()
    password = getpass.getpass("Password: ")
    res = call("/v1/user-service/user/login", {"account": email, "password": password})
    token = res.get("accessToken")
    if not token:
        print("A verification code was sent to your email.")
        code = input("Code: ").strip()
        res = call("/v1/user-service/user/login", {"account": email, "code": code})
        token = res.get("accessToken")
    if not token:
        raise SystemExit(f"Login failed: {res}")

    uid = None
    try:
        uid = call("/v1/design-user-service/my/preference", token=token).get("uid")
    except Exception as e:  # noqa
        print("Could not fetch uid:", e)
    if not uid:
        uid = input("Could not detect your UID. Enter it manually: ").strip()

    serial = ""
    try:
        devs = call("/v1/iot-service/api/user/bind", token=token).get("devices", [])
    except Exception as e:  # noqa
        print("Could not list printers:", e)
        devs = []
    if len(devs) == 1:
        serial = devs[0].get("dev_id", "")
        print(f"Printer: {devs[0].get('name')} ({serial})")
    elif len(devs) > 1:
        for i, d in enumerate(devs, 1):
            print(f"  {i}) {d.get('name')}  {d.get('dev_id')}")
        n = int(input("Which printer? ").strip() or "1")
        serial = devs[n - 1].get("dev_id", "")
    if not serial:
        serial = input("Printer serial number: ").strip()

    cfg.update(mode="cloud", uid=str(uid), token=token, serial=serial,
               host=cfg.get("host", "us.mqtt.bambulab.com"))
    save_config(cfg)
    print("Saved. From now on just run: python main.py\n")


def setup_wizard(cfg):
    print("\nFirst-time setup")
    print("  1) Cloud - printer is linked to my Bambu account (view progress)")
    print("  2) Local - LAN Only Mode + Developer Mode on the printer")
    choice = input("Choose 1 or 2 [1]: ").strip() or "1"
    if choice == "2":
        cfg.update(mode="local",
                   ip=input("Printer IP: ").strip(),
                   serial=input("Serial number: ").strip(),
                   code=input("Access code: ").strip())
        save_config(cfg)
        print("Saved. From now on just run: python main.py\n")
    else:
        cloud_login(cfg)


# ---------------------------------------------------------------- drawing helpers
def blend(fg, bg, a):
    f = [int(fg[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(bg[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(int(f[i] * a + b[i] * (1 - a)) for i in range(3))


def rrect(c, x1, y1, x2, y2, r, **kw):
    r = min(r, (x2 - x1) / 2, (y2 - y1) / 2)
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return c.create_polygon(pts, smooth=True, **kw)


def fmt_remaining(minutes):
    try:
        m = int(minutes)
    except (TypeError, ValueError):
        return "--"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


# ---------------------------------------------------------------- widget
class Widget:
    W = 320
    H_FULL = 116
    H_COMPACT = 58

    def __init__(self, cfg, persist=True):
        self.cfg = cfg
        self.persist = persist
        self.root = r = tk.Tk()
        r.title("Bambu")
        r.overrideredirect(True)
        r.attributes("-topmost", True)
        try:
            r.attributes("-transparentcolor", KEY)  # rounded corners (Windows)
        except tk.TclError:
            pass
        try:
            r.attributes("-alpha", 0.97)
        except tk.TclError:
            pass
        r.configure(bg=KEY)

        fam = "Segoe UI"
        self.f_name = tkfont.Font(family=fam, size=10, weight="bold")
        self.f_big = tkfont.Font(family=fam, size=16, weight="bold")
        self.f_mid = tkfont.Font(family=fam, size=11, weight="bold")
        self.f_pct = tkfont.Font(family=fam, size=15, weight="bold")
        self.f_small = tkfont.Font(family=fam, size=9)
        self.f_pill = tkfont.Font(family=fam, size=8, weight="bold")

        self.compact = bool(cfg.get("compact", False))
        self.H = self.H_COMPACT if self.compact else self.H_FULL
        self.canvas = tk.Canvas(r, width=self.W, height=self.H, bg=KEY, highlightthickness=0, bd=0)
        self.canvas.pack()

        x = max(0, min(int(cfg.get("x", 24)), r.winfo_screenwidth() - self.W))
        y = max(0, min(int(cfg.get("y", 24)), r.winfo_screenheight() - self.H))
        r.geometry(f"{self.W}x{self.H}+{x}+{y}")

        self.shown = 0.0
        self.on_top = tk.BooleanVar(value=True)

        c = self.canvas
        c.bind("<ButtonPress-1>", self._press)
        c.bind("<B1-Motion>", self._drag)
        c.bind("<ButtonRelease-1>", self._release)
        c.bind("<Double-Button-1>", lambda e: self.toggle_compact())
        c.bind("<Button-3>", self._menu)
        c.bind("<Button-2>", self._menu)

        self.menu = tk.Menu(r, tearoff=0)
        self.menu.add_command(label="Compact / full", command=self.toggle_compact)
        self.menu.add_checkbutton(label="Always on top", variable=self.on_top, command=self._toggle_top)
        self.menu.add_separator()
        self.menu.add_command(label="Quit", command=r.destroy)

        self.tick()

    # -- interaction
    def _press(self, e):
        self._dx, self._dy = e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y()
        self._moved = False

    def _drag(self, e):
        self._moved = True
        self.root.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

    def _release(self, e):
        if getattr(self, "_moved", False):
            self.cfg["x"], self.cfg["y"] = self.root.winfo_x(), self.root.winfo_y()
            if self.persist:
                save_config(self.cfg)

    def _menu(self, e):
        self.menu.tk_popup(e.x_root, e.y_root)

    def _toggle_top(self):
        self.root.attributes("-topmost", self.on_top.get())

    def toggle_compact(self):
        self.compact = not self.compact
        self.H = self.H_COMPACT if self.compact else self.H_FULL
        self.canvas.config(height=self.H)
        self.root.geometry(f"{self.W}x{self.H}")
        self.cfg["compact"] = self.compact
        if self.persist:
            save_config(self.cfg)

    # -- helpers
    def fit(self, text, font, maxw):
        if font.measure(text) <= maxw:
            return text
        while text and font.measure(text + "\u2026") > maxw:
            text = text[:-1]
        return text + "\u2026"

    def pill(self, x, y, label, color):
        w = self.f_pill.measure(label) + 18
        rrect(self.canvas, x, y - 9, x + w, y + 9, 9, fill=blend(color, CARD, 0.18), outline="")
        self.canvas.create_text(x + w / 2, y, text=label, fill=color, font=self.f_pill)
        return x + w

    # -- main loop
    def tick(self):
        with lock:
            d = dict(data)
            connected = status["connected"]
            err = status["error"]

        try:
            target = float(d.get("mc_percent", 0))
        except (TypeError, ValueError):
            target = 0.0
        self.shown += (target - self.shown) * 0.25
        if abs(target - self.shown) < 0.05:
            self.shown = target

        self.draw(d, connected, err)
        self.root.after(70, self.tick)

    def draw(self, d, connected, err):
        c, W, H = self.canvas, self.W, self.H
        c.delete("all")
        rrect(c, 2, 2, W - 2, H - 2, 16, fill=CARD, outline=EDGE)

        has_data = bool(d)
        st = d.get("gcode_state", "IDLE")
        label, color = STATES.get(st, (str(st).title(), DIM))
        if not has_data:
            label, color = ("Offline", RED) if err else ("Waiting", DIM)
        frac = max(0.0, min(1.0, self.shown / 100.0))

        name = d.get("subtask_name") or d.get("gcode_file") or ""
        rem = d.get("mc_remaining_time")

        # connection dot
        c.create_oval(W - 20, 14, W - 13, 21, fill=GREEN if connected else RED, outline="")

        if self.compact:
            self._draw_compact(d, has_data, st, label, color, frac, rem)
            return

        # --- progress ring
        cx, cy, r = 58, 58, 38
        c.create_oval(cx - r, cy - r, cx + r, cy + r, outline=TRACK, width=8)
        if frac >= 0.999:
            c.create_oval(cx - r, cy - r, cx + r, cy + r, outline=color, width=8)
        elif frac > 0.001:
            c.create_arc(cx - r, cy - r, cx + r, cy + r, start=90, extent=-360 * frac,
                         style="arc", outline=color, width=8)
            for ang in (90, 90 - 360 * frac):  # round caps
                px = cx + r * math.cos(math.radians(ang))
                py = cy - r * math.sin(math.radians(ang))
                c.create_oval(px - 4, py - 4, px + 4, py + 4, fill=color, outline="")
        if st == "FINISH" and has_data:
            c.create_text(cx, cy, text="\u2713", fill=color, font=self.f_big)
        else:
            txt = f"{int(round(self.shown))}%" if has_data else "--"
            c.create_text(cx, cy, text=txt, fill=TEXT, font=self.f_pct)

        x0 = 112
        maxw = W - x0 - 30

        # --- title
        if not has_data:
            title = "Can't connect" if err else ("Waiting for printer\u2026" if connected else "Connecting\u2026")
        else:
            title = name or "No active print"
        c.create_text(x0, 22, text=self.fit(title, self.f_name, maxw), fill=TEXT, font=self.f_name, anchor="w")

        # --- main line
        if not has_data and err:
            c.create_text(x0, 38, text=err, fill=RED, font=self.f_small, anchor="nw", width=W - x0 - 14)
        elif st in ("RUNNING", "PAUSE", "PREPARE") and rem is not None and has_data:
            t = fmt_remaining(rem)
            c.create_text(x0, 49, text=t, fill=TEXT, font=self.f_big, anchor="w")
            tw = self.f_big.measure(t)
            c.create_text(x0 + tw + 5, 52, text="left", fill=DIM, font=self.f_small, anchor="w")
            try:
                eta = time.strftime("%H:%M", time.localtime(time.time() + int(rem) * 60))
                c.create_text(W - 16, 52, text=f"ETA {eta}", fill=DIM, font=self.f_small, anchor="e")
            except (TypeError, ValueError):
                pass
        elif has_data:
            msg = {"FINISH": "Print complete", "FAILED": "Print failed"}.get(st, "No active print")
            c.create_text(x0, 49, text=msg, fill=color if st in ("FINISH", "FAILED") else DIM,
                          font=self.f_mid, anchor="w")

        if not has_data:
            return

        # --- status pill + layer
        end = self.pill(x0, 77, label, color)
        ln, tl = d.get("layer_num"), d.get("total_layer_num")
        if tl:
            c.create_text(end + 10, 77, text=f"Layer {ln}/{tl}", fill=DIM, font=self.f_small, anchor="w")

        # --- temps
        try:
            nz, bd = float(d.get("nozzle_temper")), float(d.get("bed_temper"))
        except (TypeError, ValueError):
            return
        x = x0
        for dot, val in ((ORANGE, f"Nozzle {nz:.0f}\u00b0"), (BLUE, f"Bed {bd:.0f}\u00b0")):
            c.create_oval(x, 96, x + 7, 103, fill=dot, outline="")
            c.create_text(x + 12, 99.5, text=val, fill=DIM, font=self.f_small, anchor="w")
            x += 12 + self.f_small.measure(val) + 16

    def _draw_compact(self, d, has_data, st, label, color, frac, rem):
        c, W, H = self.canvas, self.W, self.H
        cy = H / 2
        txt = f"{int(round(self.shown))}%" if has_data else "--"
        c.create_text(16, cy, text=txt, fill=TEXT, font=self.f_pct, anchor="w")
        bx1, bx2 = 76, W - 100
        rrect(c, bx1, cy - 4, bx2, cy + 4, 4, fill=TRACK, outline="")
        if has_data and frac > 0.001:
            rrect(c, bx1, cy - 4, bx1 + max(8, (bx2 - bx1) * frac), cy + 4, 4, fill=color, outline="")
        if has_data and st in ("RUNNING", "PAUSE", "PREPARE") and rem is not None:
            right = fmt_remaining(rem)
        else:
            right = label
        c.create_text(W - 26, cy, text=right, fill=TEXT if right != label else color,
                      font=self.f_mid, anchor="e")

    def run(self):
        self.root.mainloop()


# ---------------------------------------------------------------- entry point
def main():
    ap = argparse.ArgumentParser(description="Bambu Lab mini progress widget")
    ap.add_argument("--login", action="store_true", help="redo cloud login")
    ap.add_argument("--reset", action="store_true", help="forget saved settings")
    ap.add_argument("--demo", action="store_true", help="preview without a printer")
    ap.add_argument("--ip")
    ap.add_argument("--serial")
    ap.add_argument("--code")
    ap.add_argument("--uid")
    ap.add_argument("--token")
    ap.add_argument("--host", help="cloud MQTT host (default us.mqtt.bambulab.com)")
    a = ap.parse_args()

    cfg = load_config()

    if a.reset:
        try:
            os.remove(config_path())
        except OSError:
            pass
        print("Saved settings removed.")
        return

    if a.demo:
        start_demo()
        Widget({}, persist=False).run()  # demo never touches the saved config
        return

    # command-line values override (and get saved)
    for key in ("ip", "serial", "code", "uid", "token", "host"):
        v = getattr(a, key)
        if v:
            cfg[key] = v
            if key in ("ip", "code"):
                cfg["mode"] = "local"
            elif key in ("uid", "token"):
                cfg["mode"] = "cloud"

    if a.login:
        cloud_login(cfg)
    elif not is_ready(cfg):
        setup_wizard(cfg)

    if not is_ready(cfg):
        sys.exit("Setup incomplete. Run again to retry.")

    save_config(cfg)

    if cfg["mode"] == "cloud":
        start_mqtt(cfg.get("host", "us.mqtt.bambulab.com"), cfg["serial"], f"u_{cfg['uid']}",
                   cfg["token"], "Login failed. Token expired? Run: python main.py --login")
    else:
        start_mqtt(cfg["ip"], cfg["serial"], "bblp", cfg["code"],
                   "Rejected. Check access code and that LAN mode is on.")

    Widget(cfg).run()


if __name__ == "__main__":
    main()

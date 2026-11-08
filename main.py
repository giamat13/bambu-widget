#!/usr/bin/env python3
"""
Bambu Lab mini progress widget.
A small, borderless, always-on-top window showing the current print progress.

Setup:
    pip install paho-mqtt

Cloud mode (printer bound to your Bambu account, no LAN mode needed):
    python main.py --login                       # one-time: get uid, token, serial
    python main.py --cloud --uid 123456 --token <TOKEN> --serial 01P00A000000000

Local mode (printer needs LAN Only Mode + Developer Mode):
    python main.py --ip 192.168.1.50 --serial 01P00A000000000 --code 12345678

Demo (no printer):
    python main.py --demo

Env vars BAMBU_IP / BAMBU_SERIAL / BAMBU_CODE / BAMBU_UID / BAMBU_TOKEN work too.

Controls: drag with left mouse to move, right-click for menu (compact / quit).
"""
import argparse
import json
import os
import random
import ssl
import threading
import time
import tkinter as tk

BG = "#1b1b1f"
FG = "#f2f2f2"
DIM = "#8b8b94"
TRACK = "#34343b"
COLORS = {
    "RUNNING": "#3ddc84",
    "PREPARE": "#f5c542",
    "PAUSE": "#f5a142",
    "FINISH": "#4aa8ff",
    "FAILED": "#ff5c5c",
    "IDLE": "#8b8b94",
}

data = {}
status = {"connected": False, "error": ""}
lock = threading.Lock()


# ---------------------------------------------------------------- data sources
def start_mqtt(host, serial, username, password):
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
            status["error"] = "auth/connect failed" if failed else ""
        if not failed:
            c.subscribe(report)
            # ask the printer for a full status dump
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
                data.update(p)  # reports are partial updates, so merge

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=1, max_delay=15)
    client.connect_async(host, 8883, keepalive=30)
    client.loop_start()


def start_demo():
    def run():
        pct = 0
        total_layers = 240
        with lock:
            status["connected"] = True
        while True:
            pct = (pct + random.choice([0, 1])) % 101
            with lock:
                data.update(
                    gcode_state="FINISH" if pct == 100 else "RUNNING",
                    mc_percent=pct,
                    mc_remaining_time=max(0, int((100 - pct) * 1.3)),
                    layer_num=int(total_layers * pct / 100),
                    total_layer_num=total_layers,
                    nozzle_temper=219.6 + random.random(),
                    bed_temper=60.0,
                    subtask_name="benchy_demo",
                )
            time.sleep(1)

    threading.Thread(target=run, daemon=True).start()



# ------------------------------------------------------------ cloud login helper
def cloud_login():
    """Interactive: email + password (+ emailed code) -> prints uid, token, devices."""
    import getpass
    import urllib.request

    api = "https://api.bambulab.com"

    def call(path, body=None, token=None):
        headers = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(
            api + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())

    email = input("Bambu account email: ").strip()
    password = getpass.getpass("Password: ")
    res = call("/v1/user-service/user/login", {"account": email, "password": password})
    token = res.get("accessToken")
    if not token:
        print("A verification code was sent to your email (or login needs 2FA).")
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

    print("\n--- save these (token expires in ~3 months) ---")
    print("UID  :", uid)
    print("TOKEN:", token)
    try:
        devs = call("/v1/iot-service/api/user/bind", token=token).get("devices", [])
        for d in devs:
            print("PRINTER:", d.get("name"), "| serial:", d.get("dev_id"))
    except Exception as e:  # noqa
        print("Could not list printers:", e)
    print("\nRun:  python main.py --cloud --uid", uid, "--token <TOKEN> --serial <SERIAL>")

# ------------------------------------------------------------------------- UI
def fmt_remaining(minutes):
    try:
        m = int(minutes)
    except (TypeError, ValueError):
        return "--"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


class Widget:
    def __init__(self):
        self.root = tk.Tk()
        r = self.root
        r.title("Bambu")
        r.overrideredirect(True)
        r.attributes("-topmost", True)
        try:
            r.attributes("-alpha", 0.94)
        except tk.TclError:
            pass
        r.configure(bg=BG)
        self.compact = False
        self.W = 250
        r.geometry(f"{self.W}x112+24+24")

        self.name = tk.Label(r, text="Connecting...", bg=BG, fg=DIM, font=("Segoe UI", 9), anchor="w")
        self.name.place(x=10, y=6, width=self.W - 90)
        self.state = tk.Label(r, text="", bg=BG, fg=DIM, font=("Segoe UI", 8, "bold"), anchor="e")
        self.state.place(x=self.W - 80, y=6, width=70)

        self.pct = tk.Label(r, text="--%", bg=BG, fg=FG, font=("Segoe UI", 22, "bold"), anchor="w")
        self.pct.place(x=10, y=24)
        self.eta = tk.Label(r, text="", bg=BG, fg=FG, font=("Segoe UI", 9), anchor="e", justify="right")
        self.eta.place(x=self.W - 130, y=30, width=120)

        self.bar = tk.Canvas(r, width=self.W - 20, height=8, bg=TRACK, highlightthickness=0)
        self.bar.place(x=10, y=68)
        self.fill = self.bar.create_rectangle(0, 0, 0, 8, width=0, fill=COLORS["IDLE"])

        self.layers = tk.Label(r, text="", bg=BG, fg=DIM, font=("Segoe UI", 8), anchor="w")
        self.layers.place(x=10, y=84)
        self.temps = tk.Label(r, text="", bg=BG, fg=DIM, font=("Segoe UI", 8), anchor="e")
        self.temps.place(x=self.W - 160, y=84, width=150)

        # drag + menu
        for w in (r, self.name, self.state, self.pct, self.eta, self.layers, self.temps, self.bar):
            w.bind("<ButtonPress-1>", self._start_drag)
            w.bind("<B1-Motion>", self._drag)
            w.bind("<Button-3>", self._menu)
            w.bind("<Button-2>", self._menu)
        self.menu = tk.Menu(r, tearoff=0)
        self.menu.add_command(label="Compact / full", command=self.toggle_compact)
        self.menu.add_command(label="Quit", command=r.destroy)

        self.refresh()

    def _start_drag(self, e):
        self._dx, self._dy = e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y()

    def _drag(self, e):
        self.root.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

    def _menu(self, e):
        self.menu.tk_popup(e.x_root, e.y_root)

    def toggle_compact(self):
        self.compact = not self.compact
        self.root.geometry(f"{self.W}x{70 if self.compact else 112}")
        self.bar.place(x=10, y=(56 if self.compact else 68))
        if self.compact:
            self.layers.place_forget()
            self.temps.place_forget()
        else:
            self.layers.place(x=10, y=84)
            self.temps.place(x=self.W - 160, y=84, width=150)

    def refresh(self):
        with lock:
            d = dict(data)
            connected = status["connected"]
            err = status["error"]

        if not connected and not d:
            self.name.config(text=err or "Connecting...", fg=DIM)
        else:
            st = d.get("gcode_state", "IDLE")
            color = COLORS.get(st, DIM)
            name = d.get("subtask_name") or d.get("gcode_file") or "No job"
            self.name.config(text=name[:32], fg=FG)
            self.state.config(text=st, fg=color)

            pct = d.get("mc_percent")
            pct = int(pct) if isinstance(pct, (int, float)) else 0
            self.pct.config(text=f"{pct}%")
            self.bar.update_idletasks()
            self.bar.coords(self.fill, 0, 0, (self.W - 20) * pct / 100, 8)
            self.bar.itemconfig(self.fill, fill=color)

            rem = d.get("mc_remaining_time")
            if st in ("RUNNING", "PAUSE") and rem is not None:
                eta_clock = time.strftime("%H:%M", time.localtime(time.time() + int(rem) * 60))
                self.eta.config(text=f"{fmt_remaining(rem)} left\nETA {eta_clock}")
            elif st == "FINISH":
                self.eta.config(text="Done")
            else:
                self.eta.config(text="")

            ln, tl = d.get("layer_num"), d.get("total_layer_num")
            self.layers.config(text=f"Layer {ln}/{tl}" if tl else "")
            nz, bd = d.get("nozzle_temper"), d.get("bed_temper")
            if nz is not None and bd is not None:
                self.temps.config(text=f"Nozzle {float(nz):.0f}\N{DEGREE SIGN}  Bed {float(bd):.0f}\N{DEGREE SIGN}")

        self.root.after(500, self.refresh)

    def run(self):
        self.root.mainloop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ip", default=os.getenv("BAMBU_IP"))
    ap.add_argument("--serial", default=os.getenv("BAMBU_SERIAL"))
    ap.add_argument("--code", default=os.getenv("BAMBU_CODE"))
    ap.add_argument("--cloud", action="store_true", help="use Bambu cloud MQTT")
    ap.add_argument("--uid", default=os.getenv("BAMBU_UID"))
    ap.add_argument("--token", default=os.getenv("BAMBU_TOKEN"))
    ap.add_argument("--host", default="us.mqtt.bambulab.com", help="cloud MQTT host")
    ap.add_argument("--login", action="store_true", help="get cloud uid/token/serial")
    ap.add_argument("--demo", action="store_true")
    a = ap.parse_args()

    if a.login:
        cloud_login()
        return

    if a.demo:
        start_demo()
    elif a.cloud:
        if not (a.uid and a.token and a.serial):
            ap.error("cloud mode needs --uid, --token and --serial (run --login first)")
        start_mqtt(a.host, a.serial, f"u_{a.uid}", a.token)
    else:
        if not (a.ip and a.serial and a.code):
            ap.error("local mode needs --ip, --serial and --code (or use --cloud / --demo)")
        start_mqtt(a.ip, a.serial, "bblp", a.code)
    Widget().run()


if __name__ == "__main__":
    main()

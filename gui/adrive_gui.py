"""ADrive Control — desktop GUI for ADrive linear actuators.

Uses only the public `adrive` SDK API (scan / connect / Actuator methods), so the
source holds no protocol, gains or raw access. Tk (Python standard library):
no extra runtime dependency, one-file exe with PyInstaller.

    python gui/adrive_gui.py            # real actuators (USB / CAN auto-detect)
    python gui/adrive_gui.py --sim 4    # four simulated axes, no hardware

Threads: per axis one poller (status ~20 Hz) and one command runner; both only
put events on a queue. Every Tk call happens on the main thread (`_pump`).
"""
import argparse
import collections
import csv
import math
import os
import pathlib
import queue
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, ttk

import adrive
import tuning

APP = "ADrive Control"
HERE = pathlib.Path(getattr(sys, "_MEIPASS", pathlib.Path(__file__).resolve().parent))
LOGO_PNG = HERE / "assets" / "aeirobot_logo.png"
MAX_AXES = 4
POLL_S = 0.05
PLOT_S = 10.0
LEASE_S = 1.0          # force command lease; renewed every RENEW_MS while the force is on (F06)
RENEW_MS = 300
CLOSE_S = 3.0          # window close waits this long for stop + port close, then exits regardless
JOG_STEPS = (0.01, 0.1, 1.0, 10.0)
PRESETS = (0, 25, 50, 75, 100)
ACCEL_FRACTION = 0.25  # synchronized move: share of time accelerating (same as examples/sync_4axis.py)

# AeiROBOT palette (booth GUI reference)
GREEN, YELLOW = "#8EC31F", "#FBCA4D"
BG, CARD, LINE, SOFT, FIELD = "#F3F5EF", "#FFFFFF", "#E2E7DA", "#EEF2E7", "#F7F9F3"
INK, MUTED, GOOD, AMBER, BAD = "#1F2A17", "#7B8573", "#5E8D0B", "#B7791F", "#D9363E"
PLOT_GOAL, PLOT_IQ, PLOT_F = "#E9A92E", "#6F7B66", "#B7791F"
AXIS_COLORS = (GREEN, "#3A7CA5", "#8E5BB5", "#E07B39")
BUTTON_KINDS = {"primary": (GREEN, INK, "#7FB118"), "danger": (BAD, "#FFFFFF", "#C22E35"),
                "normal": (SOFT, INK, "#E1E8D6")}


def mix(a, b, t):
    pa, pb = [int(a[i:i + 2], 16) for i in (1, 3, 5)], [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(pa, pb))


def sync_profile(dists, vmax, amax):
    """Common duration T for all axes (slowest one within limits) and per-axis (speed, accel)."""
    f = ACCEL_FRACTION
    T = max(max(d / (vmax * (1 - f)), math.sqrt(d / (amax * f * (1 - f)))) for d in dists)
    T = max(T, 1e-3)
    out = []
    for d in dists:
        v = max(d, 1e-6) / (T * (1 - f))
        out.append((v, v / (f * T)))
    return T, out


class Axis:
    """One connected actuator. Worker threads talk to the GUI only through `events`."""

    def __init__(self, target, act, events, index):
        self.target, self.act, self.events, self.index = target, act, events, index
        self.info = dict(act.info())
        self.limits = dict(act.limits())
        self.status = None
        self.goal = None              # last commanded position [mm]
        self.busy = None              # label of the running command
        self.force = None             # (newtons, max_speed) while the GUI renews a force lease
        self.lost = False
        self.hist = collections.deque(maxlen=int(PLOT_S / POLL_S * 1.5))
        self.cmds = queue.Queue()
        self.alive = True
        for fn in (self._run, self._poll):
            threading.Thread(target=fn, daemon=True).start()

    @property
    def name(self):
        return f"Axis {self.index + 1}"

    def submit(self, label, fn):
        self.cmds.put((label, fn))

    def drain(self):
        while True:
            try:
                self.cmds.get_nowait()
            except queue.Empty:
                return

    def _run(self):
        while self.alive:
            try:
                label, fn = self.cmds.get(timeout=0.2)
            except queue.Empty:
                continue
            self.busy = label
            try:
                fn()
                self.events.put(("done", self, label))
            except Exception as e:  # SDK errors and anything else: report, never kill the worker
                self.events.put(("error", self, f"{label}: {e}"))
            finally:
                self.busy = None

    def _poll(self):
        while self.alive:
            try:
                s = self.act.status()
                self.events.put(("status", self, dict(s), time.monotonic()))
            except Exception as e:
                if not self.lost:
                    self.events.put(("lost", self, str(e)))
                self.lost = True
                time.sleep(0.5)
            time.sleep(POLL_S)

    def close(self):
        self.alive = False
        self.force = None
        self.drain()
        for name in ("estop", "close"):   # output off first; each step independent
            try:
                getattr(self.act, name)()
            except Exception:
                pass


class App:
    def __init__(self, root, sim=0):
        self.root = root
        self.events = queue.Queue()
        self.axes = []
        self.active = None
        self.found = []               # scan results not connected yet
        self.csv = None
        self.closing = False
        self.t0 = time.monotonic()
        self.scale = root.winfo_fpixels("1i") / 96.0
        have = set(tkfont.families(root))
        pick = lambda *names: next((n for n in names if n in have), names[-1])  # noqa: E731
        self.f_ui = pick("Segoe UI", "Noto Sans", "DejaVu Sans")
        self.f_num = pick("Bahnschrift", "Segoe UI", "DejaVu Sans")
        self.f_mono = pick("Consolas", "DejaVu Sans Mono", "Courier New")
        self.logo = None
        try:
            self.logo = tk.PhotoImage(file=str(LOGO_PNG)).subsample(2)
        except tk.TclError:
            pass
        bi = adrive.build_info()
        sha = bi.get('build_sha', 'unknown')
        self.build = f"SDK {bi.get('version', '?')} · build {sha[:12] if sha != 'unknown' else 'untracked source copy'}"
        root.title(f"{APP} — AeiROBOT")
        root.configure(bg=BG)
        root.minsize(self.px(1180), self.px(760))
        self._style()
        self._build()
        root.protocol("WM_DELETE_WINDOW", self.close)
        for cls in ("Button", "Checkbutton"):
            root.unbind_class(cls, "<Key-space>")   # Space must only ever stop, never press a focused button
        for key in ("<space>", "<Escape>"):
            root.bind_all(key, lambda _e: self.estop_all())
        root.after(50, self._pump)
        root.after(100, self._draw_plot)
        root.after(RENEW_MS, self._renew_force)
        for k in range(sim):
            self.connect(f"sim:{k + 1}")
        if not sim:
            self.scan()

    # ---------- widgets (same look as the booth GUIs) ----------
    def px(self, n):
        return int(round(n * self.scale))

    def _font(self, size, weight="normal", kind="ui"):
        return ({"ui": self.f_ui, "num": self.f_num, "mono": self.f_mono}[kind], size, weight)

    def _style(self):
        st = ttk.Style(self.root)
        st.theme_use("clam")
        st.configure("Treeview", background=CARD, fieldbackground=CARD, foreground=INK, rowheight=self.px(22),
                     font=self._font(9), borderwidth=0)
        st.configure("Treeview.Heading", background=SOFT, foreground=MUTED, font=self._font(9, "bold"), relief="flat")
        st.map("Treeview", background=[("selected", mix(GREEN, CARD, 0.55))], foreground=[("selected", INK)])
        st.configure("TNotebook", background=CARD, borderwidth=0)
        st.configure("TNotebook.Tab", background=SOFT, foreground=MUTED, padding=(self.px(14), self.px(5)),
                     font=self._font(9, "bold"))
        st.configure("Horizontal.TScale", background=GREEN, troughcolor=SOFT, bordercolor=LINE,
                     lightcolor=GREEN, darkcolor=GREEN)
        st.map("TNotebook.Tab", background=[("selected", CARD)], foreground=[("selected", INK)])

    def _label(self, parent, text="", var=None, size=9, weight="normal", fg=MUTED, kind="ui", **kw):
        return tk.Label(parent, text=text, textvariable=var, bg=parent.cget("bg"), fg=fg,
                        font=self._font(size, weight, kind), **kw)

    def _card(self, parent, title, **pack):
        outer = tk.Frame(parent, bg=CARD, highlightthickness=1, highlightbackground=LINE)
        outer.pack(**{"fill": "x", "pady": (0, self.px(10)), **pack})
        h = tk.Frame(outer, bg=CARD)
        h.pack(fill="x", padx=self.px(14), pady=(self.px(10), 0))
        tk.Frame(h, bg=GREEN, width=self.px(4), height=self.px(14)).pack(side="left")
        self._label(h, title, size=10, weight="bold", fg=INK).pack(side="left", padx=(self.px(8), 0))
        body = tk.Frame(outer, bg=CARD)
        body.pack(fill="both", expand=True, padx=self.px(14), pady=(self.px(8), self.px(12)))
        return body, h

    def _button(self, parent, text, cmd, kind="normal", big=False, **kw):
        bg, fg, hover = BUTTON_KINDS[kind]
        return tk.Button(parent, text=text, command=cmd, bg=bg, fg=fg, activebackground=hover, activeforeground=fg,
                         disabledforeground=mix(fg, bg, 0.6), relief="flat", bd=0, highlightthickness=0,
                         cursor="hand2", padx=self.px(16 if big else 10), pady=self.px(9 if big else 4),
                         font=self._font(11 if big else 9, "normal" if kind == "normal" else "bold"), **kw)

    def _entry(self, parent, var, width=8):
        return tk.Entry(parent, textvariable=var, width=width, bg=FIELD, fg=INK, insertbackground=INK, relief="flat",
                        bd=0, justify="right", font=self._font(11, kind="num"), highlightthickness=1,
                        highlightbackground=LINE, highlightcolor=GREEN)

    def _scale(self, parent, var, lo, hi, label):
        f = tk.Frame(parent, bg=CARD)
        self._label(f, label, width=13, anchor="w").pack(side="left")
        self._label(f, var=var, width=5, anchor="e", size=10, fg=INK, kind="num").pack(side="right")
        s = ttk.Scale(f, variable=var, from_=lo, to=hi, orient="horizontal",
                      command=lambda v: var.set(int(float(v))))
        s.pack(side="left", fill="x", expand=True, padx=(0, self.px(6)))
        return f, s

    # ---------- layout ----------
    def _build(self):
        p = self.px
        head = tk.Frame(self.root, bg=CARD)
        head.pack(fill="x")
        row = tk.Frame(head, bg=CARD)
        row.pack(fill="x", padx=p(20), pady=p(12))
        if self.logo is not None:
            tk.Label(row, image=self.logo, bg=CARD).pack(side="left", padx=(0, p(18)))
        titles = tk.Frame(row, bg=CARD)
        titles.pack(side="left")
        self._label(titles, APP, size=15, weight="bold", fg=INK).pack(anchor="w")
        self._label(titles, f"AeiROBOT ADrive linear actuator  ·  USB / CAN  ·  {self.build}", size=9).pack(anchor="w")
        self.btn_estop = self._button(row, "■  EMERGENCY STOP   (Space / Esc)", self.estop_all, "danger", big=True)
        self.btn_estop.pack(side="right")
        self.rec_var = tk.StringVar(value="●  Record CSV")
        self.btn_rec = self._button(row, "", self.toggle_csv, textvariable=self.rec_var)
        self.btn_rec.pack(side="right", padx=p(12))
        tk.Frame(head, bg=GREEN, height=p(4)).pack(fill="x")

        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True, padx=p(14), pady=p(12))
        left = tk.Frame(body, bg=BG, width=p(400))
        left.pack(side="left", fill="y", padx=(0, p(12)))
        left.pack_propagate(False)
        right = tk.Frame(body, bg=BG)
        right.pack(side="left", fill="both", expand=True)

        self._build_devices(left)
        self._build_control(left)
        self._build_axes(right)
        plot_body, _ = self._card(right, "Live plot — position · target · current · estimated force", fill="both", expand=True)
        self.plot = tk.Canvas(plot_body, bg=CARD, highlightthickness=0, bd=0, height=p(260))
        self.plot.pack(fill="both", expand=True)

        foot = tk.Frame(self.root, bg=CARD, highlightthickness=1, highlightbackground=LINE)
        foot.pack(fill="x", side="bottom")
        self.msg = tk.StringVar(value="Ready.")
        self.msg_lbl = self._label(foot, var=self.msg, size=9, fg=INK, anchor="w")
        self.msg_lbl.pack(side="left", fill="x", expand=True, padx=p(14), pady=p(5))
        self._label(foot, "Space / Esc = stop all axes (motor idle)", size=8).pack(side="right", padx=p(14))

    def _build_devices(self, parent):
        b, _ = self._card(parent, "Devices")
        cols = ("target", "link", "serial", "firmware", "state")
        self.tree = ttk.Treeview(b, columns=cols, show="headings", height=5, selectmode="browse")
        for c, w in zip(cols, (110, 40, 95, 75, 50)):
            self.tree.heading(c, text=c.capitalize())
            self.tree.column(c, width=self.px(w), anchor="w", stretch=c == "target")
        self.tree.pack(fill="x")
        r = tk.Frame(b, bg=CARD)
        r.pack(fill="x", pady=(self.px(8), 0))
        self.btn_scan = self._button(r, "Scan", self.scan)
        self.btn_scan.pack(side="left")
        self._button(r, "Add simulator", self.add_sim).pack(side="left", padx=self.px(6))
        self._button(r, "Disconnect", self.disconnect).pack(side="right")
        self._button(r, "Connect", self.connect_selected, "primary").pack(side="right", padx=self.px(6))

    def _build_control(self, parent):
        p = self.px
        b, h = self._card(parent, "Control", fill="both", expand=True)
        self.ctl_title = self._label(h, "— no axis selected", size=9, fg=MUTED)
        self.ctl_title.pack(side="left", padx=p(8))
        r = tk.Frame(b, bg=CARD)
        r.pack(fill="x")
        self.btn_home = self._button(r, "⌂ Home", self.cmd_home, "primary")
        self.btn_enable = self._button(r, "Enable", self.cmd_enable)
        self.btn_disable = self._button(r, "Disable", self.cmd_disable)
        self.btn_clear = self._button(r, "Clear faults", self.cmd_clear)
        self.btn_hold = self._button(r, "Hold", self.cmd_hold)
        for w in (self.btn_home, self.btn_enable, self.btn_disable, self.btn_clear, self.btn_hold):
            w.pack(side="left", padx=(0, p(5)))
        r2 = tk.Frame(b, bg=CARD)
        r2.pack(fill="x", pady=(p(6), p(4)))
        self._button(r2, "Home all", self.home_all).pack(side="left", padx=(0, p(5)))
        self._button(r2, "Enable all", self.enable_all).pack(side="left", padx=(0, p(5)))
        self._button(r2, "Tuning …", self.open_tuning).pack(side="right")

        nb = ttk.Notebook(b)
        nb.pack(fill="both", expand=True, pady=(p(6), 0))
        self.nb = nb
        pos = tk.Frame(nb, bg=CARD)
        frc = tk.Frame(nb, bg=CARD)
        nb.add(pos, text="Position")
        nb.add(frc, text="Force (experimental)")
        nb.bind("<<NotebookTabChanged>>", lambda _e: self._tab_changed())
        self.motion = []   # widgets usable only when homed + enabled + no fault

        g = tk.Frame(pos, bg=CARD)
        g.pack(fill="x", pady=(p(8), p(4)))
        self._label(g, "Target [mm]").pack(side="left")
        self.target_var = tk.StringVar(value="1.0")
        e = self._entry(g, self.target_var)
        e.pack(side="left", padx=p(6))
        e.bind("<Return>", lambda _e: self.cmd_go())
        self.btn_go = self._button(g, "Move", self.cmd_go, "primary")
        self.btn_go.pack(side="left")
        self.btn_sync = self._button(g, "Move synced axes", self.cmd_sync_go)
        self.btn_sync.pack(side="left", padx=p(6))
        self.motion += [self.btn_go, self.btn_sync]

        pr = tk.Frame(pos, bg=CARD)
        pr.pack(fill="x", pady=p(3))
        self._label(pr, "Preset", width=6, anchor="w").pack(side="left")
        for pct in PRESETS:
            w = self._button(pr, f"{pct}%", lambda q=pct: self.cmd_preset(q))
            w.pack(side="left", padx=(0, p(3)))
            self.motion.append(w)
        for sign in (-1, 1):
            jr = tk.Frame(pos, bg=CARD)
            jr.pack(fill="x", pady=p(2))
            self._label(jr, "Jog −" if sign < 0 else "Jog +", width=6, anchor="w").pack(side="left")
            for s in JOG_STEPS:
                w = self._button(jr, f"{'−' if sign < 0 else '+'}{s:g}", lambda d=sign * s: self.cmd_jog(d))
                w.pack(side="left", padx=(0, p(3)))
                self.motion.append(w)
        self._label(pos, "mm · presets are % of the soft-limit range · synced axes: tick 'sync' on the axis cards",
                    size=8).pack(anchor="w", pady=(p(2), p(6)))
        self.speed_var = tk.IntVar(value=20)
        self.accel_var = tk.IntVar(value=100)
        f, self.speed_scale = self._scale(pos, self.speed_var, 1, 200, "Speed [mm/s]")
        f.pack(fill="x")
        f, self.accel_scale = self._scale(pos, self.accel_var, 10, 2000, "Accel [mm/s²]")
        f.pack(fill="x")

        tk.Label(frc, text="EXPERIMENTAL — force is estimated from motor current (no load cell, screw friction not "
                           "compensated). Not yet verified against a load cell.", bg=mix(YELLOW, CARD, 0.6), fg=INK,
                 font=self._font(9), wraplength=p(340), justify="left", padx=p(8), pady=p(6)).pack(fill="x", pady=p(8))
        fr = tk.Frame(frc, bg=CARD)
        fr.pack(fill="x", pady=p(3))
        self._label(fr, "Force [N]  (+ = extend)").pack(side="left")
        self.force_var = tk.StringVar(value="5.0")
        self._entry(fr, self.force_var).pack(side="right")
        fr = tk.Frame(frc, bg=CARD)
        fr.pack(fill="x", pady=p(3))
        self._label(fr, "Speed limit [mm/s]").pack(side="left")
        self.fspeed_var = tk.StringVar(value="5.0")
        self._entry(fr, self.fspeed_var).pack(side="right")
        fr = tk.Frame(frc, bg=CARD)
        fr.pack(fill="x", pady=p(8))
        self.btn_fstart = self._button(fr, "Start force", self.cmd_force_start, "primary")
        self.btn_fstart.pack(side="left")
        self.btn_fstop = self._button(fr, "Stop (hold position)", self.cmd_force_stop)
        self.btn_fstop.pack(side="left", padx=p(6))
        self.motion.append(self.btn_fstart)
        self._label(frc, f"The command is a {LEASE_S:g} s lease renewed by this window every {RENEW_MS} ms;\n"
                         "if the GUI hangs or closes, the actuator falls back to holding position.",
                    size=8, justify="left").pack(anchor="w")

    def _build_axes(self, parent):
        b, _ = self._card(parent, "Axes")
        self.axes_row = tk.Frame(b, bg=CARD)
        self.axes_row.pack(fill="x")
        self.cards = []
        for k in range(MAX_AXES):
            c = tk.Frame(self.axes_row, bg=FIELD, highlightthickness=2, highlightbackground=LINE, cursor="hand2")
            c.grid(row=0, column=k, sticky="nsew", padx=(0 if k == 0 else self.px(8), 0))
            self.axes_row.columnconfigure(k, weight=1, uniform="axis")
            v = {n: tk.StringVar(value="") for n in ("title", "badge", "pos", "goal", "detail", "fault", "mode")}
            v["title"].set(f"Axis {k + 1} — empty")
            top = tk.Frame(c, bg=FIELD)
            top.pack(fill="x", padx=self.px(10), pady=(self.px(8), 0))
            tk.Frame(top, bg=AXIS_COLORS[k], width=self.px(10), height=self.px(10)).pack(side="left")
            self._label(top, var=v["title"], size=9, weight="bold", fg=INK).pack(side="left", padx=self.px(6))
            sync = tk.BooleanVar(value=True)
            tk.Checkbutton(top, text="sync", variable=sync, bg=FIELD, fg=MUTED, selectcolor=CARD, bd=0,
                           highlightthickness=0, activebackground=FIELD, font=self._font(8)).pack(side="right")
            badges = tk.Frame(c, bg=FIELD)
            badges.pack(fill="x", padx=self.px(10), pady=self.px(4))
            badge = tk.Label(badges, textvariable=v["badge"], font=self._font(8, "bold"), padx=self.px(6), bg=FIELD)
            badge.pack(side="left")
            mode = tk.Label(badges, textvariable=v["mode"], font=self._font(8, "bold"), padx=self.px(6), bg=SOFT, fg=MUTED)
            mode.pack(side="left", padx=self.px(4))
            self._label(c, var=v["pos"], size=19, weight="bold", fg=INK, kind="num").pack(anchor="w", padx=self.px(10))
            self._label(c, var=v["goal"], size=8).pack(anchor="w", padx=self.px(10))
            self._label(c, var=v["detail"], size=8, justify="left", kind="mono").pack(anchor="w", padx=self.px(10))
            fault = self._label(c, var=v["fault"], size=9, weight="bold", fg=BAD)
            fault.pack(anchor="w", padx=self.px(10), pady=(0, self.px(8)))
            for w in [c] + list(c.winfo_children()) + list(top.winfo_children()) + list(badges.winfo_children()):
                w.bind("<Button-1>", lambda _e, i=k: self.select(i), add="+")
            self.cards.append({"frame": c, "vars": v, "badge": badge, "mode": mode, "sync": sync})

    # ---------- devices ----------
    def log(self, text, bad=False):
        self.msg.set(time.strftime("%H:%M:%S  ") + text)
        self.msg_lbl.configure(fg=BAD if bad else INK)
        print(text, file=sys.stderr, flush=True)

    def _refresh_tree(self):
        self.tree.delete(*self.tree.get_children())
        for ax in self.axes:
            i = ax.info
            state = "lost" if ax.lost else "open"
            link = "sim" if ax.target.startswith("sim") else i.get("link")
            self.tree.insert("", "end", iid=ax.target, values=(ax.target, link, i.get("serial"), i.get("firmware"), state))
        for t in self.found:
            if t not in (a.target for a in self.axes):
                self.tree.insert("", "end", iid=t, values=(t, t.split(":")[0], "—", "—", "found"))

    def scan(self):
        self.btn_scan.configure(state="disabled")
        self.log("Scanning USB and CAN adapters …")

        def run():
            try:
                self.events.put(("scan", adrive.scan()))
            except Exception as e:
                self.events.put(("scan_err", str(e)))
        threading.Thread(target=run, daemon=True).start()

    def add_sim(self):
        used = {a.target for a in self.axes}
        k = next(n for n in range(1, 64) if f"sim:{n}" not in used)
        self.connect(f"sim:{k}")

    def connect_selected(self):
        sel = self.tree.selection()
        if not sel:
            # nothing picked: connect everything found
            for t in self.found:
                self.connect(t)
            return
        self.connect(sel[0])

    def connect(self, target):
        if any(a.target == target for a in self.axes):
            return
        if len(self.axes) >= MAX_AXES:
            self.log(f"At most {MAX_AXES} axes.", bad=True)
            return
        self.log(f"Connecting {target} …")

        def run():
            try:
                self.events.put(("connected", target, adrive.connect(target)))
            except Exception as e:
                self.events.put(("connect_err", target, str(e)))
        threading.Thread(target=run, daemon=True).start()

    def disconnect(self):
        ax = self.cur()
        if ax is None:
            return
        self.axes.remove(ax)
        threading.Thread(target=ax.close, daemon=True).start()
        for k, a in enumerate(self.axes):
            a.index = k
        self.active = 0 if self.axes else None
        self._refresh_tree()
        self._refresh_cards()
        self.log(f"Disconnected {ax.target}")

    def cur(self):
        return self.axes[self.active] if self.active is not None and self.active < len(self.axes) else None

    def select(self, i):
        if i < len(self.axes):
            self.active = i
            self._refresh_cards()

    # ---------- commands ----------
    def _need(self, label):
        ax = self.cur()
        if ax is None:
            self.log("Connect and select an axis first.", bad=True)
        return ax

    def open_tuning(self):
        ax = self._need("tuning")
        return tuning.TuningWindow(self, ax) if ax else None

    def cmd_home(self):
        ax = self._need("home")
        if ax:
            ax.goal = None
            ax.submit("home", lambda: ax.act.home(timeout=60.0))
            self.log(f"{ax.name}: homing toward the retracted end (slow) …")

    def home_all(self):
        for ax in self.axes:
            if ax.status and not ax.status["enabled"]:
                ax.goal = None
                ax.submit("home", lambda a=ax: a.act.home(timeout=60.0))
        self.log("Homing all disabled axes …")

    def cmd_enable(self):
        ax = self._need("enable")
        if ax:
            ax.submit("enable", ax.act.enable)

    def enable_all(self):
        for ax in self.axes:
            if ax.status and ax.status["homed"] and not ax.status["enabled"]:
                ax.submit("enable", ax.act.enable)

    def cmd_disable(self):
        ax = self._need("disable")
        if ax:
            ax.force = None
            ax.submit("disable", ax.act.disable)

    def cmd_clear(self):
        ax = self._need("clear faults")
        if ax:
            ax.submit("clear faults", ax.act.clear_faults)

    def cmd_hold(self):
        ax = self._need("hold")
        if ax:
            ax.force = None
            ax.goal = None
            ax.submit("hold", ax.act.stop)

    def _speed(self):
        return float(self.speed_var.get()), float(self.accel_var.get())

    def _move(self, ax, mm):
        lo, hi = ax.act.soft_limits
        mm = min(max(mm, lo), hi)
        v, a = self._speed()
        ax.goal = mm
        ax.force = None
        ax.submit(f"move {mm:.3f} mm", lambda: ax.act.move_to(mm, speed=v, accel=a, wait=False))

    def _target(self):
        try:
            return float(self.target_var.get())
        except ValueError:
            self.log("Target must be a number (mm).", bad=True)
            return None

    def cmd_go(self):
        ax, mm = self._need("move"), self._target()
        if ax and mm is not None:
            self._move(ax, mm)

    def cmd_preset(self, pct):
        ax = self._need("preset")
        if ax:
            lo, hi = ax.act.soft_limits
            self._move(ax, lo + (hi - lo) * pct / 100.0)

    def cmd_jog(self, d):
        ax = self._need("jog")
        if ax and ax.status:
            base = ax.goal if ax.goal is not None else ax.status["position_mm"]
            self._move(ax, base + d)

    def cmd_sync_go(self):
        """Move every ticked, ready axis to the target so that all arrive at the same time."""
        mm = self._target()
        if mm is None:
            return
        group = [a for a in self.axes if self.cards[a.index]["sync"].get() and self._ready(a)]
        if not group:
            self.log("No ready axis ticked for sync (homed + enabled).", bad=True)
            return
        goals = []
        for a in group:
            lo, hi = a.act.soft_limits
            goals.append(min(max(mm, lo), hi))
        vmax, amax = self._speed()
        T, prof = sync_profile([abs(g - a.status["position_mm"]) for a, g in zip(group, goals)], vmax, amax)
        for a, g in zip(group, goals):
            a.goal, a.force = g, None

        def run():  # one thread issues all commands back to back: smallest start skew
            try:
                for a, g, (v, acc) in zip(group, goals, prof):
                    a.act.move_to(g, speed=v, accel=acc, wait=False)
                self.events.put(("info", f"Synced move of {len(group)} axes, T = {T:.2f} s"))
            except Exception as e:
                self.events.put(("sync_err", str(e)))
        threading.Thread(target=run, daemon=True).start()

    def cmd_force_start(self):
        ax = self._need("force")
        if not ax:
            return
        try:
            n, v = float(self.force_var.get()), float(self.fspeed_var.get())
        except ValueError:
            self.log("Force and speed limit must be numbers.", bad=True)
            return
        ax.goal = None
        ax.submit(f"force {n:g} N", lambda: ax.act.force_control(n, v, lease=LEASE_S))
        ax.force = (n, v)

    def cmd_force_stop(self):
        ax = self.cur()
        if ax and ax.force:
            ax.force = None
            ax.submit("hold", ax.act.stop)

    def _renew_force(self):
        if self.closing:
            return
        # F06 lease: the GUI re-sends the force command while it is alive; a hung GUI lets the lease run out
        for ax in self.axes:
            if ax.force and ax.cmds.empty() and ax.busy is None:
                n, v = ax.force
                ax.submit("force renew", lambda a=ax, n=n, v=v: a.act.force_control(n, v, lease=LEASE_S))
        self.root.after(RENEW_MS, self._renew_force)

    def _tab_changed(self):
        if self.nb.index("current") == 0:
            for ax in self.axes:
                if ax.force:
                    ax.force = None
                    ax.submit("hold", ax.act.stop)

    def estop_all(self):
        """Software stop: every axis idles at once (not a safety-rated E-stop)."""
        for ax in self.axes:
            ax.force = None
            ax.goal = None
            ax.drain()
            threading.Thread(target=self._estop_one, args=(ax,), daemon=True).start()
            # again after whatever command is in flight (e.g. an enable) has finished
            ax.submit("stop", ax.act.estop)
        self.log("EMERGENCY STOP — all axes idle.", bad=True)

    def _estop_one(self, ax):
        try:
            ax.act.estop()
        except Exception as e:
            self.events.put(("error", ax, f"stop: {e}"))

    # ---------- CSV ----------
    def toggle_csv(self, path=None):
        if self.csv:
            self.csv[0].close()
            self.csv = None
            self.rec_var.set("●  Record CSV")
            self.log("CSV recording stopped.")
            return
        path = path or filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")],
                                                    initialfile=time.strftime("adrive-%Y%m%d-%H%M%S.csv"))
        if not path:
            return
        fh = open(path, "w", newline="", encoding="utf-8")
        w = csv.writer(fh)
        w.writerow(["t_s", "axis", "target", "position_mm", "goal_mm", "velocity_mm_s", "current_a", "force_est_n",
                    "bus_voltage", "mode", "enabled", "homed", "fault"])
        self.csv = (fh, w)
        self.rec_var.set("■  Stop recording")
        self.log(f"Recording to {path}")

    # ---------- event pump / view ----------
    def _pump(self):
        if self.closing:
            return
        changed = False
        try:
            while True:
                ev = self.events.get_nowait()
                changed |= self._handle(ev)
        except queue.Empty:
            pass
        if changed:
            self._refresh_cards()
        self.root.after(50, self._pump)

    def _handle(self, ev):
        kind = ev[0]
        if kind == "status":
            _, ax, s, t = ev
            if ax not in self.axes:
                return False
            ax.status = s
            ax.hist.append((t, s["position_mm"], ax.goal, s["current_a"], s["force_n"]))
            if self.csv:
                self.csv[1].writerow([f"{t - self.t0:.3f}", ax.index + 1, ax.target, s["position_mm"], ax.goal,
                                      s["velocity_mm_s"], s["current_a"], s["force_n"], s["bus_voltage"], s["mode"],
                                      s["enabled"], s["homed"], s["fault"]])
            return True
        if kind == "connected":
            _, target, act = ev
            if len(self.axes) >= MAX_AXES:
                act.close()
                return False
            ax = Axis(target, act, self.events, len(self.axes))
            self.axes.append(ax)
            self.found = [t for t in self.found if t != target]
            if self.active is None:
                self.active = ax.index
            self._apply_limits(ax)
            self._refresh_tree()
            self.log(f"Connected {target}: {ax.info.get('model')} serial {ax.info.get('serial')} "
                     f"firmware {ax.info.get('firmware')}")
            return True
        if kind == "scan":
            self.found = ev[1]
            self.btn_scan.configure(state="normal")
            self._refresh_tree()
            self.log(f"Scan: {len(self.found)} actuator(s) found." if self.found else
                     "Scan: no actuator found (check cable / adapter, or add a simulator).")
            return False
        if kind == "scan_err":
            self.btn_scan.configure(state="normal")
            self.log(f"Scan failed: {ev[1]}", bad=True)
            return False
        if kind == "connect_err":
            self.log(f"Connect {ev[1]} failed: {ev[2]}", bad=True)
            return False
        if kind in ("error", "lost"):
            ax, text = ev[1], ev[2]
            if kind == "error":
                ax.force = None
            self.log(f"{ax.name}: {text}", bad=True)
            if kind == "lost":
                self._refresh_tree()
            return True
        if kind == "done":
            ax, label = ev[1], ev[2]
            if label != "force renew":
                self.log(f"{ax.name}: {label} OK")
            return True
        if kind == "sync_err":
            self.estop_all()
            self.log(f"Synced move failed, all axes stopped: {ev[1]}", bad=True)
            return True
        if kind == "info":
            self.log(ev[1])
        return False

    def _apply_limits(self, ax):
        lim = ax.limits
        self.speed_scale.configure(to=int(lim["max_speed_mm_s"]))
        self.accel_scale.configure(to=int(lim["max_accel_mm_s2"]))

    @staticmethod
    def _ready(ax):
        s = ax.status
        return bool(s and s["homed"] and s["enabled"] and not s["fault"] and not ax.lost and ax.busy != "home")

    def _refresh_cards(self):
        for k, card in enumerate(self.cards):
            v = card["vars"]
            ax = self.axes[k] if k < len(self.axes) else None
            card["frame"].configure(highlightbackground=GREEN if k == self.active and ax else LINE)
            if ax is None:
                v["title"].set(f"Axis {k + 1} — empty")
                for n in ("badge", "pos", "goal", "detail", "fault", "mode"):
                    v[n].set("")
                card["badge"].configure(bg=FIELD)
                card["mode"].configure(bg=FIELD)
                continue
            s = ax.status
            v["title"].set(f"{ax.name} · {ax.target}")
            if s is None:
                continue
            homing = ax.busy == "home"
            if homing:
                v["badge"].set("HOMING …")
                card["badge"].configure(bg=mix(YELLOW, CARD, 0.3), fg=INK)
            elif s["homed"]:
                v["badge"].set("HOMED")
                card["badge"].configure(bg=GREEN, fg=INK)
            else:
                v["badge"].set("NOT HOMED")
                card["badge"].configure(bg=AMBER, fg="#FFFFFF")
            v["mode"].set("LINK LOST" if ax.lost else s["mode"].upper())
            card["mode"].configure(bg=mix(BAD, CARD, 0.2) if ax.lost else (SOFT if s["mode"] == "disabled" else mix(GREEN, CARD, 0.5)),
                                   fg="#FFFFFF" if ax.lost else INK)
            v["pos"].set(f"{s['position_mm']:.3f} mm")
            v["goal"].set(f"target {ax.goal:.3f} mm" if ax.goal is not None else ("force " + f"{ax.force[0]:g} N" if ax.force else "target —"))
            cur = s["current_a"]
            fn = s["force_n"]
            v["detail"].set(f"vel {s['velocity_mm_s']:7.2f} mm/s\ncur {cur:6.2f} A  " if not math.isnan(cur) else
                            f"vel {s['velocity_mm_s']:7.2f} mm/s\ncur    —    ")
            v["detail"].set(v["detail"].get() + (f"F≈{fn:6.1f} N" if not math.isnan(fn) else "F≈  —") +
                            f"\nbus {s['bus_voltage']:5.1f} V")
            v["fault"].set(f"FAULT 0x{s['fault']:08X}" if s["fault"] else "")
        self._refresh_buttons()

    def _refresh_buttons(self):
        ax = self.cur()
        s = ax.status if ax else None
        name = f"— {ax.name} ({ax.target})" if ax else "— no axis selected"
        self.ctl_title.configure(text=name)
        homed = bool(s and s["homed"])
        enabled = bool(s and s["enabled"])
        fault = bool(s and s["fault"])
        busy = bool(ax and ax.busy in ("home", "enable"))
        alive = bool(ax and not ax.lost)
        st = lambda ok: "normal" if ok else "disabled"  # noqa: E731
        self.btn_home.configure(state=st(alive and s and not enabled and not busy),
                                text="⌂ Re-home" if homed else "⌂ Home")
        self.btn_enable.configure(state=st(alive and homed and not enabled and not fault and not busy))
        self.btn_disable.configure(state=st(alive and enabled))
        self.btn_clear.configure(state=st(alive and fault and not enabled))
        self.btn_hold.configure(state=st(alive and enabled))
        ready = bool(ax and self._ready(ax))
        for w in self.motion:
            w.configure(state=st(ready))
        if any(self._ready(a) for a in self.axes):
            self.btn_sync.configure(state="normal")
        self.btn_fstop.configure(state=st(bool(ax and ax.force)))

    def _draw_plot(self):
        if self.closing:
            return
        try:
            self._render_plot()
        finally:
            self.root.after(100, self._draw_plot)

    def _render_plot(self):
        c = self.plot
        c.delete("all")
        W, H = c.winfo_width(), c.winfo_height()
        if W < 50 or H < 50:
            return
        now = time.monotonic()
        p = self.px
        x0, x1 = p(56), W - p(12)
        panels = [("position [mm]", 0.5), ("current [A]", 0.25), ("est. force [N]", 0.25)]
        act = self.cur()
        top = p(4)
        legend = [("target", PLOT_GOAL)] + [(a.name, AXIS_COLORS[a.index]) for a in self.axes]
        lx = x0
        for text, col in legend:
            c.create_line(lx, top + p(6), lx + p(16), top + p(6), fill=col, width=2, dash=(4, 3) if text == "target" else None)
            c.create_text(lx + p(20), top + p(6), text=text, anchor="w", fill=MUTED, font=self._font(8))
            lx += p(30) + len(text) * p(6)
        top += p(16)
        avail = H - top - p(18)
        for title, frac in panels:
            h = avail * frac
            y0, y1 = top + p(6), top + h - p(6)
            series = []
            if title.startswith("position"):
                for a in self.axes:
                    series.append(([(t, v) for t, v, *_ in a.hist], AXIS_COLORS[a.index], None, 2))
                if act:
                    series.append(([(t, g) for t, _, g, *_ in act.hist if g is not None], PLOT_GOAL, (4, 3), 2))
            elif act:
                col, idx = (PLOT_IQ, 3) if title.startswith("current") else (PLOT_F, 4)
                series.append(([(r[0], r[idx]) for r in act.hist], col, None, 1))
            vals = [v for pts, *_ in series for t, v in pts if now - t <= PLOT_S and not math.isnan(v)]
            lo, hi = (min(vals), max(vals)) if vals else (0.0, 1.0)
            if hi - lo < 1e-3:
                lo, hi = lo - 0.5, hi + 0.5
            pad = (hi - lo) * 0.08
            lo, hi = lo - pad, hi + pad
            c.create_rectangle(x0, y0, x1, y1, outline=LINE)
            ticks = 4 if frac > 0.3 else 2
            for k in range(ticks + 1):
                y = y0 + (y1 - y0) * k / ticks
                c.create_line(x0, y, x1, y, fill=SOFT)
                c.create_text(x0 - p(4), y, text=f"{hi - (hi - lo) * k / ticks:.3g}", anchor="e", fill=MUTED,
                              font=self._font(7))
            c.create_text(x0 + p(6), y0 + p(8), text=title, anchor="w", fill=MUTED, font=self._font(8, "bold"))
            for pts, col, dash, wdt in series:
                xy = []
                for t, v in pts:
                    if now - t > PLOT_S or math.isnan(v):
                        continue
                    xy += [x1 - (now - t) / PLOT_S * (x1 - x0), y1 - (v - lo) / (hi - lo) * (y1 - y0)]
                if len(xy) >= 4:
                    c.create_line(*xy, fill=col, width=wdt, dash=dash)
            top += h
        for k in range(6):
            x = x0 + (x1 - x0) * k / 5
            c.create_text(x, H - p(8), text=f"{-PLOT_S + PLOT_S * k / 5:.0f} s", fill=MUTED, font=self._font(7))

    def close(self):
        """Stop and close every axis in parallel (at most CLOSE_S), then destroy the window. Safe to call twice."""
        if self.closing:
            return
        self.closing = True
        self.root.withdraw()   # the window goes away at once, even if a port close hangs
        for ax in self.axes:
            ax.force = None
            ax.goal = None
        threads = [threading.Thread(target=ax.close, daemon=True) for ax in self.axes]
        for t in threads:
            t.start()
        end = time.monotonic() + CLOSE_S
        for t in threads:
            t.join(max(0.0, end - time.monotonic()))
        if self.csv:
            self.csv[0].close()
        self.root.destroy()   # also takes the Tuning windows with it


def main(argv=None, run=True):
    ap = argparse.ArgumentParser(description=APP)
    ap.add_argument("--sim", type=int, default=0, help="connect N simulated axes (no hardware)")
    ap.add_argument("--exit-after", type=float, default=0, help=argparse.SUPPRESS)  # CI smoke test
    args = ap.parse_args(argv)
    root = tk.Tk()
    app = App(root, sim=min(args.sim, MAX_AXES))
    if args.exit_after:
        root.after(int(args.exit_after * 1000), app.close)
    if run:
        root.mainloop()
        try:
            app.close()
            for f in (sys.stdout, sys.stderr):   # None in the windowed exe
                f and f.flush()
        finally:
            os._exit(0)   # a stuck native handle or worker thread must not keep the process alive
    return app


if __name__ == "__main__":
    main()

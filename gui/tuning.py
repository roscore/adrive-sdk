"""Tuning window for one axis: gains, step response, stroke limits, factory restore, save to drive.

Public `adrive` API only. Every SDK call runs on the axis command runner (`Axis.submit`); results come
back through `self.q` and are applied on the Tk main thread (`_pump`), like the main window.
"""
import math
import queue
import sys
import time
import tkinter as tk
from tkinter import messagebox, ttk

GAINS = (("pos", "Position gain"), ("vel", "Velocity gain"), ("vel_integrator", "Velocity integrator"))
STEP_S = 1.5        # record this long after the move ends (plus the nominal travel time)
SAMPLE_S = 0.01     # status sampling period; the real rate is lower by the status round trip


def fmt(x):
    return f"{x:.4g}"


def step_metrics(samples, start, target):
    """Overshoot and settling time of a step from `start` to `target`.

    samples: [(t_s since the command, position_mm)]. Settled = within ±max(2 % of the step, 0.01 mm)
    of the target from that sample on; settling_s is None if the last sample is still outside.
    """
    step = target - start
    sign = 1.0 if step >= 0 else -1.0
    band = max(0.02 * abs(step), 0.01)
    over = max([sign * (p - target) for _, p in samples] + [0.0])
    settling = 0.0 if samples else None
    for t, p in samples:
        if abs(p - target) > band:
            settling = None
        elif settling is None:
            settling = t
    return {"overshoot_mm": over, "overshoot_pct": 100.0 * over / abs(step) if step else 0.0,
            "settling_s": settling, "band_mm": band}


class TuningWindow:
    def __init__(self, app, ax):
        self.app, self.ax = app, ax
        self.g = g = sys.modules[type(app).__module__]   # palette lives in the GUI module (may be __main__)
        p = app.px
        self.q = queue.Queue()
        self.gains = self.limits = self.factory = self.stroke = self.metrics = None
        self.trace = None
        self.saved = self.bad = False
        self.win = w = tk.Toplevel(app.root, bg=g.BG)
        w.title(f"Tuning — {ax.name} ({ax.target})")
        w.minsize(p(980), p(640))
        body = tk.Frame(w, bg=g.BG)
        body.pack(fill="both", expand=True, padx=p(14), pady=p(12))
        left = tk.Frame(body, bg=g.BG, width=p(430))
        left.pack(side="left", fill="y", padx=(0, p(12)))
        left.pack_propagate(False)
        right = tk.Frame(body, bg=g.BG)
        right.pack(side="left", fill="both", expand=True)
        self.alive_btns = []

        b, _ = app._card(left, "Gains (RAM until saved)")
        self.scale_var, self.val_var, self.range_var, self.scales = {}, {}, {}, {}
        for key, label in GAINS:
            r = tk.Frame(b, bg=g.CARD)
            r.pack(fill="x", pady=(p(4), 0))
            app._label(r, label, fg=g.INK).pack(side="left")
            self.val_var[key] = tk.StringVar()
            e = app._entry(r, self.val_var[key], width=9)
            e.pack(side="right")
            e.bind("<Return>", lambda _e, k=key: self._entry_to_scale(k))
            self.scale_var[key] = tk.DoubleVar()
            # log scale: the allowed range spans ~25x
            s = ttk.Scale(b, variable=self.scale_var[key], from_=0, to=1, orient="horizontal",
                          command=lambda v, k=key: self.val_var[k].set(fmt(10 ** float(v))))
            s.pack(fill="x")
            self.scales[key] = s
            self.range_var[key] = tk.StringVar(value="min —   ·   factory —   ·   max —")
            app._label(b, var=self.range_var[key], size=8).pack(anchor="w")
        r = tk.Frame(b, bg=g.CARD)
        r.pack(fill="x", pady=(p(8), 0))
        self.btn_apply = app._button(r, "Apply (RAM)", self.apply_gains, "primary")
        self.btn_apply.pack(side="left")
        self.btn_factory = app._button(r, "Restore factory gains", self.restore_factory)
        self.btn_factory.pack(side="left", padx=p(6))
        self.btn_reload = app._button(r, "Reload", self.load)
        self.btn_reload.pack(side="right")
        self.alive_btns += [self.btn_apply, self.btn_factory, self.btn_reload]

        b, _ = app._card(left, "Stroke limits")
        self.stroke_var = tk.StringVar(value="current: —")
        app._label(b, var=self.stroke_var, fg=g.INK).pack(anchor="w")
        r = tk.Frame(b, bg=g.CARD)
        r.pack(fill="x", pady=p(4))
        self.smin_var, self.smax_var = tk.StringVar(), tk.StringVar()
        app._label(r, "min [mm]").pack(side="left")
        app._entry(r, self.smin_var, width=7).pack(side="left", padx=(p(4), p(10)))
        app._label(r, "max [mm]").pack(side="left")
        app._entry(r, self.smax_var, width=7).pack(side="left", padx=p(4))
        r = tk.Frame(b, bg=g.CARD)
        r.pack(fill="x")
        self.remember = tk.BooleanVar(value=False)
        tk.Checkbutton(r, text="Remember on this PC", variable=self.remember, bg=g.CARD, fg=g.INK,
                       selectcolor=g.FIELD, activebackground=g.CARD, bd=0, highlightthickness=0,
                       font=app._font(9)).pack(side="left")
        self.btn_stroke = app._button(r, "Apply", self.apply_stroke, "primary")
        self.btn_stroke.pack(side="right")
        self.alive_btns.append(self.btn_stroke)

        b, _ = app._card(left, "Save to drive")
        app._label(b, "Stores the current gains in the drive. Motor must be disabled;\n"
                      "the drive reboots and the axis must be reconnected.", size=8, justify="left").pack(anchor="w")
        self.btn_save = app._button(b, "Save to drive …", self.save, "danger")
        self.btn_save.pack(anchor="w", pady=(p(6), 0))

        b, _ = app._card(right, "Step response", fill="both", expand=True)
        r = tk.Frame(b, bg=g.CARD)
        r.pack(fill="x")
        self.step_var, self.speed_var = tk.StringVar(value="1.0"), tk.StringVar(value="10")
        app._label(r, "Step [mm]").pack(side="left")
        app._entry(r, self.step_var, width=6).pack(side="left", padx=(p(4), p(10)))
        app._label(r, "Speed [mm/s]").pack(side="left")
        app._entry(r, self.speed_var, width=6).pack(side="left", padx=p(4))
        self.btn_step = app._button(r, "Run step", self.run_step, "primary")
        self.btn_step.pack(side="left", padx=p(10))
        app._label(b, "Steps from the current position; the axis must be homed and enabled.", size=8).pack(anchor="w")
        self.metrics_var = tk.StringVar(value="overshoot —   ·   settling —")
        app._label(b, var=self.metrics_var, size=11, weight="bold", fg=g.INK, kind="num").pack(anchor="w", pady=p(6))
        self.plot = tk.Canvas(b, bg=g.CARD, highlightthickness=0, bd=0, height=p(320))
        self.plot.pack(fill="both", expand=True)
        self.plot.bind("<Configure>", lambda _e: self._draw())

        self.msg = tk.StringVar(value="")
        self.msg_lbl = app._label(w, var=self.msg, fg=g.INK, anchor="w", wraplength=p(940), justify="left")
        self.msg_lbl.pack(fill="x", padx=p(14), pady=(0, p(8)))
        self.load()
        w.after(50, self._pump)

    # ---------- plumbing ----------
    def say(self, text, bad=False):
        self.bad = bad
        self.msg.set(time.strftime("%H:%M:%S  ") + text)
        self.msg_lbl.configure(fg=self.g.BAD if bad else self.g.INK)

    def _job(self, label, fn, done=None):
        def run():
            try:
                r = fn()
            except Exception as e:
                self.q.put((None, f"{label}: {e}"))
                raise   # the main window logs it too
            self.q.put((done, r))
        self.ax.submit(label, run)

    def _pump(self):
        if not self.win.winfo_exists():
            return
        try:
            while True:
                done, r = self.q.get_nowait()
                if done is None:
                    self.say(r, bad=True)
                elif done:
                    done(r)
        except queue.Empty:
            pass
        ax, s = self.ax, self.ax.status
        alive = ax in self.app.axes and not ax.lost and not self.saved
        st = lambda ok: "normal" if ok else "disabled"  # noqa: E731
        for b in self.alive_btns:
            b.configure(state=st(alive))
        self.btn_step.configure(state=st(alive and self.app._ready(ax)))
        self.btn_save.configure(state=st(alive and s is not None and not s["enabled"]))
        self.win.after(50, self._pump)

    def _floats(self, *vars_):
        try:
            return [float(v.get()) for v in vars_]
        except ValueError:
            self.say("Enter numbers.", bad=True)
            return None

    # ---------- gains ----------
    def load(self):
        a = self.ax.act
        self._job("read tuning", lambda: (a.get_gains(), a.gain_limits(), a.factory_gains(), a.get_stroke_limits()),
                  self._loaded)

    def _loaded(self, r):
        self.gains, self.limits, self.factory, self.stroke = r
        for key, _ in GAINS:
            lo, hi = self.limits[key]
            self.scales[key].configure(from_=math.log10(lo), to=math.log10(hi))
            self.scale_var[key].set(math.log10(min(max(self.gains[key], lo), hi)))
            self.val_var[key].set(fmt(self.gains[key]))
            self.range_var[key].set(f"min {fmt(lo)}   ·   factory {fmt(self.factory[key])}   ·   max {fmt(hi)}")
        lo, hi = self.stroke
        self.stroke_var.set(f"current: {lo:.2f} – {hi:.2f} mm")
        if not self.smin_var.get():
            self.smin_var.set(f"{lo:.2f}")
            self.smax_var.set(f"{hi:.2f}")

    def _entry_to_scale(self, key):
        try:
            self.scale_var[key].set(math.log10(float(self.val_var[key].get())))
        except ValueError:
            pass   # bad or non-positive text: Apply reports it

    def apply_gains(self):
        vals = self._floats(*(self.val_var[k] for k, _ in GAINS))
        if vals is None:
            return
        kw = dict(zip((k for k, _ in GAINS), vals))
        self._job("set gains", lambda: self.ax.act.set_gains(**kw), lambda _r: (self.say("Gains applied (RAM)."), self.load()))

    def restore_factory(self):
        self._job("restore factory gains", self.ax.act.restore_factory_gains,
                  lambda _r: (self.say("Factory gains restored (RAM)."), self.load()))

    # ---------- stroke / save ----------
    def apply_stroke(self):
        v = self._floats(self.smin_var, self.smax_var)
        if v is None:
            return
        save = self.remember.get()
        self._job("stroke limits", lambda: self.ax.act.set_stroke_limits(v[0], v[1], save=save),
                  lambda _r: (self.say(f"Stroke limits {v[0]:g} – {v[1]:g} mm" + (" (remembered)" if save else "")),
                              self.load()))

    def save(self):
        if not messagebox.askokcancel(
                "Save to drive", "Store the current gains in the drive?\n\n"
                "• The motor must be disabled.\n• The drive reboots; the link drops.\n"
                "• Disconnect and reconnect this axis afterwards.", parent=self.win, icon="warning"):
            return
        self._job("save to drive", self.ax.act.save, self._saved)

    def _saved(self, _r):
        self.saved = True
        self.ax.lost = True   # link drops on reboot: blocks motion until reconnected
        self.app._refresh_tree()
        self.app._refresh_cards()
        self.say("Saved. The drive is rebooting — Disconnect and reconnect this axis.")

    # ---------- step response ----------
    def run_step(self):
        ax = self.ax
        if not self.app._ready(ax):
            self.say("Step needs the axis homed and enabled, without fault.", bad=True)
            return
        v = self._floats(self.step_var, self.speed_var)
        if v is None:
            return
        size, speed = v
        target = ax.status["position_mm"] + size
        dur = STEP_S + abs(size) / max(speed, 1e-3)
        act = ax.act

        def fn():
            p0 = act.status()["position_mm"]
            t0 = time.monotonic()
            act.move_to(target, speed=speed, wait=False)
            ax.goal, ax.force = target, None   # only once accepted: a rejected step keeps the old target
            out = []
            while (t := time.monotonic() - t0) < dur:
                out.append((t, act.status()["position_mm"]))
                time.sleep(SAMPLE_S)
            return p0, target, out

        self.say(f"Step {size:+g} mm to {target:.3f} mm …")
        self._job(f"step {size:+g} mm", fn, self._stepped)

    def _stepped(self, r):
        self.trace = r
        p0, target, out = r
        m = self.metrics = step_metrics(out, p0, target)
        st = "not settled" if m["settling_s"] is None else f"{m['settling_s'] * 1000:.0f} ms"
        self.metrics_var.set(f"overshoot {m['overshoot_mm']:.3f} mm ({m['overshoot_pct']:.1f} %)   ·   "
                             f"settling {st} (±{m['band_mm']:.3f} mm)   ·   {len(out) / out[-1][0]:.0f} Hz")
        self.say("Step done.")
        self._draw()

    def _draw(self):
        g, p, c = self.g, self.app.px, self.plot
        c.delete("all")
        W, H = c.winfo_width(), c.winfo_height()
        if self.trace is None or W < 50 or H < 50:
            return
        p0, target, out = self.trace
        band = self.metrics["band_mm"]
        T = out[-1][0]
        vals = [v for _, v in out] + [p0, target + band, target - band]
        lo, hi = min(vals), max(vals)
        pad = (hi - lo) * 0.08 or 0.5
        lo, hi = lo - pad, hi + pad
        x0, x1, y0, y1 = p(56), W - p(12), p(22), H - p(22)
        X = lambda t: x0 + t / T * (x1 - x0)  # noqa: E731
        Y = lambda v: y1 - (v - lo) / (hi - lo) * (y1 - y0)  # noqa: E731
        c.create_rectangle(x0, y0, x1, y1, outline=g.LINE)
        for k in range(5):
            y = y0 + (y1 - y0) * k / 4
            c.create_line(x0, y, x1, y, fill=g.SOFT)
            c.create_text(x0 - p(4), y, text=f"{hi - (hi - lo) * k / 4:.3f}", anchor="e", fill=g.MUTED, font=self.app._font(7))
        for k in range(6):
            c.create_text(X(T * k / 5), H - p(10), text=f"{T * k / 5:.2f} s", fill=g.MUTED, font=self.app._font(7))
        for v in (target - band, target + band):
            c.create_line(x0, Y(v), x1, Y(v), fill=g.mix(g.GREEN, g.CARD, 0.7))
        c.create_line(x0, Y(p0), X(0), Y(p0), X(0), Y(target), x1, Y(target), fill=g.PLOT_GOAL, width=2, dash=(4, 3))
        xy = [z for t, v in out for z in (X(t), Y(v))]
        if len(xy) >= 4:
            c.create_line(*xy, fill=g.AXIS_COLORS[self.ax.index % len(g.AXIS_COLORS)], width=2)
        lx = x0
        for text, col in (("target", g.PLOT_GOAL), ("actual", g.AXIS_COLORS[self.ax.index % len(g.AXIS_COLORS)]),
                          ("settling band", g.mix(g.GREEN, g.CARD, 0.7))):
            c.create_line(lx, p(10), lx + p(16), p(10), fill=col, width=2)
            c.create_text(lx + p(20), p(10), text=text, anchor="w", fill=g.MUTED, font=self.app._font(8))
            lx += p(40) + len(text) * p(6)

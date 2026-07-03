#!/usr/bin/env python3
"""
Audio Processor — de-emphasis suite  
=============================================

UI overhaul of Audio Processor — de-emphasis suite.  The DSP back end is UNCHANGED:

  * high_frequency_energy / total_energy / de_emphasis_filter / peak_normalize
    are byte-for-byte the originals.
  * The tau ladders for the two original profiles ("Aggressive", "Moderate")
    are the original if/elif branches, moved verbatim into select_tau() so the
    new in-between profiles can share the same code path.
  * The processing order (analyze -> gate at 0.15 -> pick tau -> filter ->
    int16 spawn -> peak normalize -> export wav) and every print() message are
    identical, so results are bit-identical to original version for the same inputs.

What is new (all UI-side):
  * All processing runs on a worker thread — the window never freezes.
  * The terminal messages are captured live into an in-app console
    (they are still tee'd to the real terminal too).
  * Batch progress bar with file counter, elapsed time and ETA, plus a
    working Stop button (stops cleanly between files).
  * HF-balance meter showing the measured high-freq ratio against the actual
    trigger thresholds (0.15 gate, 0.2 / 0.3 / 0.4 steps) with the selected
    profile's tau printed in each zone.
  * Before/after spectrum plot (log-frequency, dB) with the 10 kHz gate marked.
  * Three new intensity profiles between/below the originals:
    Strong, Gentle, Subtle.  Defaults are untouched (Aggressive, -2 dBFS).

Dependencies: numpy, scipy, pydub, matplotlib  (+ ffmpeg for non-wav input)
Run:  python proc.py
"""

import os
import sys
import io
import time
import queue
import threading
import traceback
import contextlib

import numpy as np
from scipy.signal import lfilter, welch
from pydub import AudioSegment, effects  # 'effects' kept from the original import
import matplotlib
matplotlib.use("TkAgg")
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.ticker import EngFormatter

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import tkinter.font as tkfont

# Explicitly set the ffmpeg path if necessary  (unchanged from original)
from pydub.utils import which
AudioSegment.converter = which("ffmpeg")

# Crisper rendering on Windows HiDPI displays
if sys.platform == "win32":
    try:
        from ctypes import windll
        try:
            windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            windll.user32.SetProcessDPIAware()
    except Exception:
        pass


# ============================================================================
#  ORIGINAL DSP CORE — UNCHANGED
# ============================================================================

def high_frequency_energy(signal, sample_rate, threshold=10000):
    frequencies, psd = welch(signal, sample_rate)
    high_freq_energy = np.sum(psd[frequencies > threshold])
    return high_freq_energy

def total_energy(signal, sample_rate):
    frequencies, psd = welch(signal, sample_rate)
    total_energy = np.sum(psd)
    return total_energy

def de_emphasis_filter(signal, sample_rate, tau):
    rc = tau * 1e-6
    alpha = rc / (rc + (1 / sample_rate))
    filtered_signal = lfilter([1 - alpha], [1, -alpha], signal)
    return filtered_signal

def peak_normalize(audio_segment, target_peak_db):
    current_peak_db = audio_segment.max_dBFS
    change_in_db = target_peak_db - current_peak_db
    print(f"Normalizing audio: current peak {current_peak_db} dBFS, target peak {target_peak_db} dBFS, applying gain {change_in_db} dB")
    return audio_segment.apply_gain(change_in_db)


# ============================================================================
#  INTENSITY PROFILES
#  "Aggressive" and "Moderate" are the ORIGINAL branches, verbatim.
#  "Strong", "Gentle" and "Subtle" are new steps on the same ratio thresholds
#  (> 0.4 / > 0.3 / > 0.2 / > 0.1), so they trigger exactly like the originals.
# ============================================================================

BALANCE_THRESHOLD = 0.15          # same gate value used inside process_audio
RATIO_STEPS = (0.4, 0.3, 0.2, 0.1)

PROFILES = ["Aggressive", "Strong", "Moderate", "Gentle", "Subtle"]
DEFAULT_PROFILE = "Aggressive"    # original default — unchanged
ORIGINAL_PROFILES = ("Aggressive", "Moderate")

PROFILE_BLURB = {
    "Aggressive": "Original preset (default) — heaviest correction.",
    "Strong":     "New — sits halfway between Aggressive and Moderate.",
    "Moderate":   "Original preset — lighter correction.",
    "Gentle":     "New — soft touch for mildly bright material.",
    "Subtle":     "New — minimal, near-transparent trim.",
}

def select_tau(intensity, hf_to_total_ratio):
    """Return the de-emphasis time constant (µs) for a profile + measured ratio.

    The Aggressive / Moderate blocks below are copied unchanged.
    """
    tau = 0
    if intensity == "Aggressive":
        if hf_to_total_ratio > 0.4:
            tau = 25
        elif hf_to_total_ratio > 0.3:
            tau = 20
        elif hf_to_total_ratio > 0.2:
            tau = 15
        elif hf_to_total_ratio > 0.1:
            tau = 10
        else:
            tau = 5
    elif intensity == "Moderate":
        if hf_to_total_ratio > 0.4:
            tau = 20
        elif hf_to_total_ratio > 0.3:
            tau = 15
        elif hf_to_total_ratio > 0.2:
            tau = 10
        elif hf_to_total_ratio > 0.1:
            tau = 5
        else:
            tau = 0
    elif intensity == "Strong":            # new: midpoint of the two originals
        if hf_to_total_ratio > 0.4:
            tau = 22
        elif hf_to_total_ratio > 0.3:
            tau = 18
        elif hf_to_total_ratio > 0.2:
            tau = 12
        elif hf_to_total_ratio > 0.1:
            tau = 8
        else:
            tau = 2
    elif intensity == "Gentle":            # new: softer than Moderate
        if hf_to_total_ratio > 0.4:
            tau = 15
        elif hf_to_total_ratio > 0.3:
            tau = 11
        elif hf_to_total_ratio > 0.2:
            tau = 7
        elif hf_to_total_ratio > 0.1:
            tau = 3
        else:
            tau = 0
    elif intensity == "Subtle":            # new: lightest possible touch
        if hf_to_total_ratio > 0.4:
            tau = 10
        elif hf_to_total_ratio > 0.3:
            tau = 7
        elif hf_to_total_ratio > 0.2:
            tau = 4
        elif hf_to_total_ratio > 0.1:
            tau = 2
        else:
            tau = 0
    return tau

def profile_ladder(intensity):
    """tau values for the zones (>0.4, >0.3, >0.2, gate..0.2) of a profile."""
    return [select_tau(intensity, r) for r in (0.45, 0.35, 0.25, 0.175)]


# ============================================================================
#  PROCESSING PIPELINE — same DSP steps and prints as .process_audio,
#  decoupled from the UI (no canvas/ax arguments; optional emit() callback
#  reports analysis + spectrum data to whichever front end is listening).
# ============================================================================

def _display_spectrum(signal, sample_rate):
    """Smoothed spectrum for display only (Welch PSD in normalized dB)."""
    sig = np.asarray(signal, dtype=np.float64)
    if sig.size < 64:
        return None
    nper = int(min(8192, sig.size))
    f, psd = welch(sig, sample_rate, nperseg=nper)
    peak = psd.max()
    if not np.isfinite(peak) or peak <= 0:
        return None
    db = 10.0 * np.log10(psd / peak + 1e-12)
    return f, db


def process_audio(input_file, output_file, target_peak_db, intensity, emit=None):
    def _emit(kind, **data):
        if emit is not None:
            try:
                emit(kind, data)
            except Exception:
                pass

    print(f"Processing file: {input_file}")
    audio = AudioSegment.from_file(input_file)
    samples = np.array(audio.get_array_of_samples())
    sample_rate = audio.frame_rate

    hf_energy = high_frequency_energy(samples, sample_rate)
    total_signal_energy = total_energy(samples, sample_rate)

    balance_threshold = 0.15

    hf_to_total_ratio = hf_energy / total_signal_energy
    print(f"High-frequency energy: {hf_energy}")
    print(f"Total energy: {total_signal_energy}")
    print(f"High-frequency to total energy ratio: {hf_to_total_ratio}")

    tau = 0

    if hf_to_total_ratio < balance_threshold:
        filtered_samples = samples
        print("No de-emphasis filter applied; audio is well balanced.")
    else:
        tau = select_tau(intensity, hf_to_total_ratio)
        if tau > 0:
            filtered_samples = de_emphasis_filter(samples, sample_rate, tau)
            print(f"Applied de-emphasis filter with tau: {tau} µs")
        else:
            filtered_samples = samples

    if tau == 0:
        print("No de-emphasis filter applied.")

    _emit("analysis",
          file=os.path.basename(input_file),
          ratio=float(hf_to_total_ratio),
          tau=float(tau),
          intensity=intensity)

    filtered_audio = audio._spawn(filtered_samples.astype(np.int16).tobytes())
    filtered_audio = peak_normalize(filtered_audio, target_peak_db)
    filtered_audio.export(output_file, format='wav')
    print(f"File saved: {output_file}")

    # Spectrum snapshot for the UI (replaces the old plot_frequency_response
    # calls — display only, no effect on the audio path).
    try:
        orig = _display_spectrum(samples, sample_rate)
        payload = {
            "file": os.path.basename(input_file),
            "sr": sample_rate,
            "ratio": float(hf_to_total_ratio),
            "tau": float(tau),
        }
        if orig is not None:
            payload["f_orig"], payload["db_orig"] = orig
            if tau > 0:
                filt = _display_spectrum(filtered_samples, sample_rate)
                if filt is not None:
                    payload["f_filt"], payload["db_filt"] = filt
            _emit("plot", **payload)
    except Exception:
        pass

    return output_file


def process_audio_batch(input_folder, output_folder, target_peak_db, intensity,
                        emit=None, cancel_event=None):
    def _emit(kind, **data):
        if emit is not None:
            try:
                emit(kind, data)
            except Exception:
                pass

    # Same extension filter as the original (the bare 'opus' matches .opus)
    names = [n for n in sorted(os.listdir(input_folder))
             if n.endswith(('.mp3', '.wav', '.flac', '.ogg', 'opus'))]
    total = len(names)
    _emit("batch_start", total=total)
    if total == 0:
        print("No matching audio files found in the input folder.")

    done = 0
    errors = 0
    cancelled = False
    for i, file_name in enumerate(names, 1):
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
            print(f"Batch stopped by user after {done} of {total} files.")
            break
        _emit("file_start", index=i, total=total, name=file_name)
        print(f"── [{i}/{total}] {file_name} " + "─" * 24)
        input_file = os.path.join(input_folder, file_name)
        output_file = os.path.join(output_folder, os.path.splitext(file_name)[0] + '.wav')
        try:
            process_audio(input_file, output_file, target_peak_db, intensity, emit=emit)
            done += 1
        except Exception:
            errors += 1
            print(f"ERROR while processing {file_name}:")
            print(traceback.format_exc())
        _emit("file_done", index=i, total=total, name=file_name)

    _emit("batch_end", done=done, total=total, errors=errors, cancelled=cancelled)
    return done, total, cancelled


# ============================================================================
#  THEME
# ============================================================================

C = {
    "bg":      "#101218",
    "panel":   "#181b23",
    "panel2":  "#1f232e",
    "sunken":  "#0c0e13",
    "border":  "#2a2f3c",
    "text":    "#e9ecf3",
    "muted":   "#939db3",
    "dim":     "#5d6679",
    "accent":  "#ffb454",
    "accent2": "#e59a35",
    "blue":    "#5aa9ff",
    "blue2":   "#3f8ae0",
    "green":   "#4cd97b",
    "red":     "#ff6b6b",
    "red2":    "#d94f4f",
    "yellow":  "#ffd166",
    "purple":  "#c792ea",
    "cyan":    "#5fd4dd",
}

ZONE_FILLS = ["#1c3a2e", "#3a3320", "#463621", "#513322", "#5b2d25"]
ZONE_VALUE_COLORS = [C["green"], C["yellow"], C["accent"], "#ff9a4d", C["red"]]

LOG_RULES = [
    ("Processing file:", "head"),
    ("── [", "batch"),
    ("Batch stopped", "warn"),
    ("ERROR", "error"),
    ("Traceback", "error"),
    ("Applied de-emphasis", "accent"),
    ("No de-emphasis", "mutedln"),
    ("well balanced", "mutedln"),
    ("Normalizing audio", "info"),
    ("File saved", "ok"),
]


def _classify(line):
    for needle, tag in LOG_RULES:
        if needle in line:
            return tag
    return "plain"


def shorten_path(p, maxlen=44):
    if not p:
        return "— none selected —"
    if len(p) <= maxlen:
        return p
    return "…" + p[-(maxlen - 1):]


class QueueWriter(io.TextIOBase):
    """stdout/stderr replacement: sends complete lines to the UI queue and
    tees everything to the real terminal."""

    def __init__(self, q):
        self.q = q
        self._buf = ""
        self._real = sys.__stdout__

    def write(self, s):
        try:
            if self._real is not None:
                self._real.write(s)
                self._real.flush()
        except Exception:
            pass
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                self.q.put(("log", {"line": line.rstrip()}))
        return len(s)

    def flush(self):
        if self._buf.strip():
            self.q.put(("log", {"line": self._buf.rstrip()}))
        self._buf = ""


# ============================================================================
#  HF-BALANCE METER  (canvas widget)
# ============================================================================

class BalanceMeter(tk.Canvas):
    """Gauge of hf/total ratio against the real trigger thresholds, with the
    selected profile's tau printed in each zone. Needle eases to new values.

    Layout is computed from live font metrics (not fixed pixels) so it stays
    correct at any Windows display-scaling / DPI setting."""

    MAX_R = 0.5

    def __init__(self, parent, get_profile, **kw):
        super().__init__(parent, bg=C["panel"], highlightthickness=0, bd=0, **kw)
        self.get_profile = get_profile
        self.ratio = None          # last measured ratio
        self.tau = None
        self.file = ""
        self._disp = 0.0           # animated needle position
        self._anim = False
        # real Font objects: point-sized so they follow system DPI scaling,
        # while every coordinate below is derived from their measured metrics
        base = tkfont.nametofont("TkDefaultFont")
        fam = base.actual("family")
        self.f_tiny  = tkfont.Font(family=fam, size=8)
        self.f_small = tkfont.Font(family=fam, size=9)
        self.f_val   = tkfont.Font(family=fam, size=14, weight="bold")
        self._metrics_key = None
        self.sync_height()                       # one-shot, metrics-driven
        self.bind("<Configure>", self._on_configure)

    def _on_configure(self, _e=None):
        self.sync_height()                       # no-op unless DPI/font changed
        self.redraw()

    def sync_height(self):
        """Set the widget's requested height from current font metrics.
        Called outside redraw so drawing never feeds back into geometry."""
        h_tiny  = self.f_tiny.metrics("linespace")
        h_small = self.f_small.metrics("linespace")
        h_val   = self.f_val.metrics("linespace")
        key = (h_tiny, h_small, h_val)
        if key == self._metrics_key:
            return
        self._metrics_key = key
        gap      = max(3, h_tiny // 3)
        bar_h    = max(12, int(h_tiny * 1.2))
        needle_h = max(8, int(h_tiny * 0.8))
        pad      = max(12, h_tiny)
        full = (pad + max(h_tiny, h_small) + gap + h_val + gap + h_tiny
                + needle_h + 2 + bar_h + 4 + h_tiny + pad)
        self.configure(height=full)

    # -- public -------------------------------------------------------------
    def set_reading(self, ratio, tau, file):
        self.ratio = max(0.0, float(ratio))
        self.tau = tau
        self.file = file
        if not self._anim:
            self._anim = True
            self._tick()

    def _tick(self):
        target = min(self.ratio if self.ratio is not None else 0.0, self.MAX_R)
        d = target - self._disp
        if abs(d) < 0.0015:
            self._disp = target
            self._anim = False
            self.redraw()
            return
        self._disp += d * 0.22
        self.redraw()
        self.after(16, self._tick)

    # -- helpers ------------------------------------------------------------
    def _zone_index(self, r):
        if r < BALANCE_THRESHOLD:
            return 0
        if r > 0.4:
            return 4
        if r > 0.3:
            return 3
        if r > 0.2:
            return 2
        return 1

    def _fit(self, text, font, max_w):
        """Truncate text with an ellipsis to fit max_w pixels."""
        if max_w <= 0:
            return ""
        if font.measure(text) <= max_w:
            return text
        ell = "…"
        while text and font.measure(text + ell) > max_w:
            text = text[:-1]
        return (text + ell) if text else ""

    # -- drawing ------------------------------------------------------------
    def redraw(self):
        self.delete("all")
        w = self.winfo_width()
        if w < 80:
            return

        # ---- metrics --------------------------------------------------------
        h_tiny  = self.f_tiny.metrics("linespace")
        h_small = self.f_small.metrics("linespace")
        h_val   = self.f_val.metrics("linespace")
        gap      = max(3, h_tiny // 3)
        bar_h    = max(12, int(h_tiny * 1.2))
        needle_h = max(8, int(h_tiny * 0.8))
        pad      = max(12, h_tiny)

        # ---- row plans, richest first; pick the best that fits --------------
        def build(cap, val, zone, ticks, p):
            y, rows = p, {}
            if cap:
                rows["cap"] = y;  y += max(h_tiny, h_small) + gap
            if val:
                rows["val"] = y;  y += h_val + gap
            if zone:
                rows["zone"] = y; y += h_tiny
            y += (needle_h + 2) if val else 3
            rows["bar"] = y;      y += bar_h
            if ticks:
                y += 4
                rows["ticks"] = y; y += h_tiny
            return rows, y + p

        plans = [(True,  True,  True,  True,  pad),
                 (False, True,  True,  True,  pad),
                 (False, True,  False, True,  pad),
                 (False, True,  False, False, max(6, pad // 2)),
                 (False, False, False, False, max(4, pad // 2))]
        avail = max(self.winfo_height(), self.winfo_reqheight())
        for plan in plans:                 # degrade only if the parent squeezed us
            rows, need = build(*plan)
            if need <= avail:
                break
        cap, val, zone, ticks, pad = plan
        bar_y = rows["bar"]
        x0, x1 = pad, w - pad

        def X(r):
            return x0 + (min(max(r, 0.0), self.MAX_R) / self.MAX_R) * (x1 - x0)

        # ---- caption row -----------------------------------------------------
        if cap:
            cid = self.create_text(x0, rows["cap"], text="HF / TOTAL RATIO",
                                   anchor="nw", fill=C["muted"],
                                   font=self.f_tiny, tags="mtext")
            cap_right = self.bbox(cid)[2]
            if self.ratio is not None and self.file:
                name = self._fit(shorten_path(self.file, 60), self.f_tiny,
                                 x1 - cap_right - 16)
                self.create_text(x1, rows["cap"], text=name, anchor="ne",
                                 fill=C["dim"], font=self.f_tiny, tags="mtext")

        # ---- value row ---------------------------------------------------------
        if val:
            y_val = rows["val"]
            if self.ratio is None:
                self.create_text(x0, y_val, text="—", anchor="nw",
                                 fill=C["dim"], font=self.f_val, tags="mtext")
                self.create_text(x1, y_val + (h_val - h_small) // 2,
                                 text="waiting for analysis", anchor="ne",
                                 fill=C["dim"], font=self.f_small, tags="mtext")
            else:
                zi = self._zone_index(self.ratio)
                vid = self.create_text(x0, y_val, text=f"{self.ratio:.3f}",
                                       anchor="nw", fill=ZONE_VALUE_COLORS[zi],
                                       font=self.f_val, tags="mtext")
                v_right = self.bbox(vid)[2]
                tau_txt = ("gated — no filter" if self.tau in (None, 0)
                           else f"τ applied  {self.tau:g} µs")
                tau_txt = self._fit(tau_txt, self.f_small, x1 - v_right - 16)
                self.create_text(x1, y_val + (h_val - h_small) // 2, text=tau_txt,
                                 anchor="ne", fill=C["text"], font=self.f_small,
                                 tags="mtext")

        # ---- zones: fills + per-zone tau labels --------------------------------
        bounds = [0.0, BALANCE_THRESHOLD, 0.2, 0.3, 0.4, self.MAX_R]
        taus = profile_ladder(self.get_profile())   # [>0.4, >0.3, >0.2, gate..0.2]
        zone_tau = ["no filter", f"τ{taus[3]:g}", f"τ{taus[2]:g}",
                    f"τ{taus[1]:g}", f"τ{taus[0]:g}"]
        for i in range(5):
            xa, xb = X(bounds[i]), X(bounds[i + 1])
            self.create_rectangle(xa, bar_y, xb, bar_y + bar_h,
                                  fill=ZONE_FILLS[i], width=0)
            if not zone:
                continue
            label = zone_tau[i]
            if self.f_tiny.measure(label) > (xb - xa) - 6:   # too narrow?
                label = label.lstrip("τ")                     # digits only
                if self.f_tiny.measure(label) > (xb - xa) - 6:
                    continue
            self.create_text((xa + xb) / 2, rows["zone"], text=label, anchor="n",
                             fill=C["muted"], font=self.f_tiny, tags="mtext")

        # ---- fill up to current value ------------------------------------------
        if self.ratio is not None:
            zi = self._zone_index(self.ratio)
            self.create_rectangle(x0, bar_y + 1, X(self._disp), bar_y + bar_h - 1,
                                  fill=ZONE_VALUE_COLORS[zi], width=0,
                                  stipple="gray50")

        # ---- ticks + collision-aware labels --------------------------------------
        gate_lbl = "0.15 gate"
        gx, x02 = X(BALANCE_THRESHOLD), X(0.2)
        if (gx + self.f_tiny.measure(gate_lbl) / 2 + 4
                > x02 - self.f_tiny.measure("0.2") / 2):
            gate_lbl = "0.15"                                 # drop the word if tight
        up = 5 if ticks else 2
        dn = 5 if ticks else 2
        for r, label, strong in ((BALANCE_THRESHOLD, gate_lbl, True),
                                 (0.2, "0.2", False), (0.3, "0.3", False),
                                 (0.4, "0.4", False)):
            x = X(r)
            self.create_line(x, bar_y - (up if strong else up - 3),
                             x, bar_y + bar_h + (dn if strong else dn - 2),
                             fill=C["yellow"] if strong else C["dim"],
                             width=1, dash=(2, 2) if strong else None)
            if not ticks:
                continue
            half = self.f_tiny.measure(label) / 2
            lx = min(max(x, x0 + half), x1 - half)            # keep inside canvas
            self.create_text(lx, rows["ticks"], text=label, anchor="n",
                             fill=C["yellow"] if strong else C["dim"],
                             font=self.f_tiny, tags="mtext")

        # ---- frame + needle --------------------------------------------------------
        self.create_rectangle(x0, bar_y, x1, bar_y + bar_h,
                              outline=C["border"], width=1, tags="mbar")
        if self.ratio is not None:
            x = X(self._disp)
            self.create_line(x, bar_y - 1, x, bar_y + bar_h + 1,
                             fill=C["text"], width=2)
            if val:                                            # room for the arrow
                nw = max(4, needle_h // 2)
                self.create_polygon(x - nw, bar_y - needle_h,
                                    x + nw, bar_y - needle_h,
                                    x, bar_y - 2, fill=C["text"], outline="")



class VScrollFrame(tk.Frame):
    """Vertical scroll container: put children in .interior. The canvas keeps
    the interior's natural width; wheel scrolling is bound while hovered."""

    def __init__(self, parent, **kw):
        super().__init__(parent, bg=C["bg"], **kw)
        self.canvas = tk.Canvas(self, bg=C["bg"], highlightthickness=0, bd=0)
        self.vbar = tk.Scrollbar(self, orient="vertical", width=11,
                                 command=self.canvas.yview,
                                 bg=C["panel2"], troughcolor=C["bg"],
                                 activebackground=C["border"], bd=0,
                                 elementborderwidth=1, relief="flat",
                                 highlightthickness=0)
        self.canvas.configure(yscrollcommand=self._on_scrollset)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.interior = tk.Frame(self.canvas, bg=C["bg"])
        self._win = self.canvas.create_window((0, 0), window=self.interior,
                                              anchor="nw")
        self.interior.bind("<Configure>", self._on_interior)
        self.canvas.bind("<Configure>", self._on_canvas)
        for w in (self, self.canvas, self.interior):
            w.bind("<Enter>", self._wheel_on)
            w.bind("<Leave>", self._wheel_off)

    # keep natural width; publish scrollregion
    def _on_interior(self, _e=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"),
                              width=self.interior.winfo_reqwidth())

    def _on_canvas(self, e):
        self.canvas.itemconfigure(self._win, width=e.width)

    # show the scrollbar only when needed
    def _on_scrollset(self, lo, hi):
        if float(lo) <= 0.0 and float(hi) >= 1.0:
            self.vbar.pack_forget()
        elif not self.vbar.winfo_ismapped():
            self.vbar.pack(side="right", fill="y")
        self.vbar.set(lo, hi)

    def _wheel(self, e):
        if self.vbar.winfo_ismapped():
            step = -1 if getattr(e, "num", 0) == 4 or e.delta > 0 else 1
            self.canvas.yview_scroll(step, "units")
        return "break"

    def _wheel_on(self, _e=None):
        self.bind_all("<MouseWheel>", self._wheel)
        self.bind_all("<Button-4>", self._wheel)
        self.bind_all("<Button-5>", self._wheel)

    def _wheel_off(self, _e=None):
        self.unbind_all("<MouseWheel>")
        self.unbind_all("<Button-4>")
        self.unbind_all("<Button-5>")


# ============================================================================
#  MAIN APPLICATION
# ============================================================================

class AudioProcessorApp:
    POLL_MS = 60

    def __init__(self, root):
        self.root = root
        self.root.title("Audio Processor de-emphasis suite")
        self.root.configure(bg=C["bg"])
        self.root.geometry("1340x880")
        self.root.minsize(940, 600)

        # --- state ----------------------------------------------------------
        self.input_file = ""
        self.input_folder = ""
        self.output_dir = ""
        self.target_peak_db = -2          # original default — unchanged
        self.intensity = DEFAULT_PROFILE  # original default — unchanged

        self.q = queue.Queue()
        self.worker = None
        self.cancel_event = threading.Event()
        self.running = False
        self.job_mode = None
        self.t0 = None
        self._file_times = []
        self._pulse = 0

        self._init_fonts()
        self._init_styles()
        self.create_widgets()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(self.POLL_MS, self._poll_queue)
        self._log_line("Ready.  DSP core unchanged from original version — pick a source and run.", "mutedln")

    # ------------------------------------------------------------------ fonts
    def _init_fonts(self):
        fams = set(tkfont.families(self.root))
        ui = next((f for f in ("Segoe UI", "SF Pro Text", "Helvetica Neue",
                               "Ubuntu", "DejaVu Sans") if f in fams), "Helvetica")
        mono = next((f for f in ("Cascadia Mono", "Consolas", "JetBrains Mono",
                                 "Menlo", "DejaVu Sans Mono", "Courier New")
                     if f in fams), "Courier")
        self.f_ui = (ui, 10)
        self.f_ui_b = (ui, 10, "bold")
        self.f_small = (ui, 8)
        self.f_small_b = (ui, 8, "bold")
        self.f_title = (ui, 15, "bold")
        self.f_btn = (ui, 10, "bold")
        self.f_mono = (mono, 9)
        self.f_mono_b = (mono, 9, "bold")
        self.f_big = (ui, 12, "bold")

    # ------------------------------------------------------------------ styles
    def _init_styles(self):
        s = ttk.Style(self.root)
        try:
            s.theme_use("clam")
        except Exception:
            pass
        s.configure("Accent.Horizontal.TProgressbar",
                    troughcolor=C["panel2"], bordercolor=C["panel2"],
                    background=C["accent"], lightcolor=C["accent"],
                    darkcolor=C["accent"], thickness=16)
        s.configure("Dark.TCombobox",
                    fieldbackground=C["panel2"], background=C["panel2"],
                    foreground=C["text"], arrowcolor=C["text"],
                    bordercolor=C["border"], lightcolor=C["panel2"],
                    darkcolor=C["panel2"], insertcolor=C["text"], padding=4)
        s.map("Dark.TCombobox",
              fieldbackground=[("readonly", C["panel2"])],
              foreground=[("readonly", C["text"])],
              selectbackground=[("readonly", C["panel2"])],
              selectforeground=[("readonly", C["text"])])
        s.configure("Dark.Vertical.TScrollbar",
                    background=C["panel2"], troughcolor=C["panel"],
                    bordercolor=C["panel"], arrowcolor=C["muted"],
                    lightcolor=C["panel2"], darkcolor=C["panel2"])
        # combobox popup list
        self.root.option_add("*TCombobox*Listbox*Background", C["panel2"])
        self.root.option_add("*TCombobox*Listbox*Foreground", C["text"])
        self.root.option_add("*TCombobox*Listbox*selectBackground", C["accent"])
        self.root.option_add("*TCombobox*Listbox*selectForeground", C["bg"])
        self.root.option_add("*TCombobox*Listbox*Font", self.f_ui)

    # ------------------------------------------------------------- primitives
    def _card(self, parent, title=None):
        outer = tk.Frame(parent, bg=C["border"], bd=0,
                         highlightthickness=0, padx=1, pady=1)
        inner = tk.Frame(outer, bg=C["panel"], padx=12, pady=10)
        inner.pack(fill="both", expand=True)
        if title:
            tk.Label(inner, text=title.upper(), bg=C["panel"], fg=C["muted"],
                     font=self.f_small_b, anchor="w").pack(fill="x", pady=(0, 7))
        return outer, inner

    def _btn(self, parent, text, bg, hover, fg, cmd):
        b = tk.Button(parent, text=text, command=cmd, bg=bg, fg=fg,
                      activebackground=hover, activeforeground=fg,
                      disabledforeground="#69718a", relief="flat", bd=0,
                      padx=12, pady=9, cursor="hand2", font=self.f_btn,
                      highlightthickness=0)
        b._base, b._hover = bg, hover
        b.bind("<Enter>", lambda e: b.config(bg=b._hover) if b["state"] == "normal" else None)
        b.bind("<Leave>", lambda e: b.config(bg=b._base) if b["state"] == "normal" else None)
        return b

    def _small_btn(self, parent, text, cmd):
        return self._btn(parent, text, C["panel2"], "#2a3040", C["text"], cmd)

    # ---------------------------------------------------------------- layout
    def create_widgets(self):
        root = self.root
        root.grid_columnconfigure(0, weight=0)
        root.grid_columnconfigure(1, weight=1)
        root.grid_rowconfigure(1, weight=1)

        # ===== header ========================================================
        header = tk.Frame(root, bg=C["panel"], padx=18, pady=10)
        header.grid(row=0, column=0, columnspan=2, sticky="ew")
        header.grid_columnconfigure(1, weight=1)
        tk.Label(header, text="AUDIO PROCESSOR", bg=C["panel"], fg=C["text"],
                 font=self.f_title).grid(row=0, column=0, sticky="w")
        tk.Label(header, text="adaptive de-emphasis  ·  peak normalize   —   DSP core: (unchanged)",
                 bg=C["panel"], fg=C["muted"], font=self.f_small
                 ).grid(row=1, column=0, sticky="w")
        self.pill = tk.Label(header, text="  IDLE  ", bg=C["panel2"], fg=C["muted"],
                             font=self.f_small_b, padx=10, pady=4)
        self.pill.grid(row=0, column=2, rowspan=2, sticky="e")

        # ===== left rail =====================================================
        rail = tk.Frame(root, bg=C["bg"], padx=14, pady=12)
        rail.grid(row=1, column=0, sticky="nsw")
        root.grid_rowconfigure(1, weight=1)

        # -- actions
        act = tk.Frame(rail, bg=C["bg"])
        act.pack(side="bottom", fill="x", pady=(8, 0))
        act.grid_columnconfigure(0, weight=1)
        act.grid_columnconfigure(1, weight=1)
        self.run_button = self._btn(act, "▶   Run file", C["accent"], "#ffc470",
                                    C["bg"], self.run_job)
        self.run_button.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.run_batch_button = self._btn(act, "⏩   Run batch", C["blue"], "#7cbcff",
                                          C["bg"], self.run_batch_job)
        self.run_batch_button.grid(row=0, column=1, sticky="ew", padx=(4, 0))
        self.stop_button = self._btn(act, "⏹   Stop  (finishes current file)",
                                     C["red2"], C["red"], C["bg"], self.stop_job)
        self.stop_button.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.stop_button.config(state="disabled", bg=C["panel2"])


        vsf = VScrollFrame(rail)
        vsf.pack(side="top", fill="both", expand=True)
        cards = vsf.interior

        # -- source
        card, src = self._card(cards, "source")
        card.pack(fill="x", pady=(0, 10))
        row1 = tk.Frame(src, bg=C["panel"]); row1.pack(fill="x", pady=2)
        self._small_btn(row1, "Single file…", self.browse_input_file).pack(side="left")
        self.lbl_file = tk.Label(row1, text=shorten_path(""), bg=C["panel"],
                                 fg=C["dim"], font=self.f_small, anchor="w", width=34)
        self.lbl_file.pack(side="left", padx=(8, 0), fill="x", expand=True)
        row2 = tk.Frame(src, bg=C["panel"]); row2.pack(fill="x", pady=2)
        self._small_btn(row2, "Batch folder…", self.browse_input_folder).pack(side="left")
        self.lbl_folder = tk.Label(row2, text=shorten_path(""), bg=C["panel"],
                                   fg=C["dim"], font=self.f_small, anchor="w", width=34)
        self.lbl_folder.pack(side="left", padx=(8, 0), fill="x", expand=True)

        # -- output
        card, out = self._card(cards, "output")
        card.pack(fill="x", pady=(0, 10))
        row = tk.Frame(out, bg=C["panel"]); row.pack(fill="x", pady=2)
        self._small_btn(row, "Output folder…", self.browse_output_directory).pack(side="left")
        self.lbl_out = tk.Label(row, text=shorten_path(""), bg=C["panel"],
                                fg=C["dim"], font=self.f_small, anchor="w", width=32)
        self.lbl_out.pack(side="left", padx=(8, 0), fill="x", expand=True)

        # -- settings
        card, st = self._card(cards, "settings")
        card.pack(fill="x", pady=(0, 10))
        r1 = tk.Frame(st, bg=C["panel"]); r1.pack(fill="x", pady=(0, 6))
        tk.Label(r1, text="Peak normalize (dBFS)", bg=C["panel"], fg=C["text"],
                 font=self.f_ui).pack(side="left")
        self.norm_entry = tk.Spinbox(
            r1, from_=-30, to=0, increment=0.5, width=7, format="%.1f",
            bg=C["panel2"], fg=C["text"], insertbackground=C["text"],
            buttonbackground=C["panel2"], relief="flat", font=self.f_ui,
            highlightthickness=1, highlightbackground=C["border"],
            highlightcolor=C["accent"], justify="center")
        self.norm_entry.delete(0, "end")
        self.norm_entry.insert(0, str(self.target_peak_db))
        self.norm_entry.pack(side="right")

        r2 = tk.Frame(st, bg=C["panel"]); r2.pack(fill="x", pady=(0, 4))
        tk.Label(r2, text="Intensity profile", bg=C["panel"], fg=C["text"],
                 font=self.f_ui).pack(side="left")
        self.intensity_var = tk.StringVar(value=self.intensity)
        self.intensity_menu = ttk.Combobox(
            r2, textvariable=self.intensity_var, values=PROFILES,
            state="readonly", style="Dark.TCombobox", width=13, font=self.f_ui)
        self.intensity_menu.pack(side="right")
        self.intensity_menu.bind("<<ComboboxSelected>>", self._on_profile_change)

        self.lbl_blurb = tk.Label(st, text="", bg=C["panel"], fg=C["muted"],
                                  font=self.f_small, anchor="w", justify="left",
                                  wraplength=300)
        self.lbl_blurb.pack(fill="x")
        self.lbl_ladder = tk.Label(st, text="", bg=C["panel"], fg=C["dim"],
                                   font=self.f_mono, anchor="w")
        self.lbl_ladder.pack(fill="x", pady=(3, 0))

        # -- meter
        card, mt = self._card(cards, "hf balance meter")
        card.pack(fill="x", pady=(0, 10))
        self.meter = BalanceMeter(mt, get_profile=lambda: self.intensity_var.get())
        self.meter.pack(fill="x")

        # ===== right side ====================================================
        right = tk.Frame(root, bg=C["bg"], padx=2, pady=12)
        right.grid(row=1, column=1, sticky="nsew", padx=(0, 14))
        right.grid_rowconfigure(0, weight=1)
        right.grid_columnconfigure(0, weight=1)

        paned = tk.PanedWindow(right, orient="vertical", bg=C["bg"],
                               sashwidth=7, sashrelief="flat", bd=0)
        paned.grid(row=0, column=0, sticky="nsew")

        # -- spectrum card
        plot_outer, plot_in = self._card(paned, "spectrum — before / after")
        self.fig = Figure(figsize=(6.4, 3.4), dpi=100, facecolor=C["panel"])
        self.ax = self.fig.add_subplot(111)
        self.fig.subplots_adjust(left=0.075, right=0.985, top=0.90, bottom=0.15)
        self.mpl = FigureCanvasTkAgg(self.fig, master=plot_in)
        self.mpl.get_tk_widget().configure(bg=C["panel"], highlightthickness=0)
        self.mpl.get_tk_widget().pack(fill="both", expand=True)
        self._style_axes(empty=True)
        self.mpl.draw_idle()
        paned.add(plot_outer, minsize=220, stretch="always")

        bottom = tk.Frame(paned, bg=C["bg"])
        paned.add(bottom, minsize=230, stretch="always")
        bottom.grid_rowconfigure(1, weight=1)
        bottom.grid_columnconfigure(0, weight=1)

        # -- progress strip
        pr_outer, pr = self._card(bottom, None)
        pr_outer.grid(row=0, column=0, sticky="ew", pady=(10, 10))
        pr.grid_columnconfigure(0, weight=1)
        top = tk.Frame(pr, bg=C["panel"]); top.grid(row=0, column=0, sticky="ew")
        top.grid_columnconfigure(0, weight=1)
        self.lbl_job = tk.Label(top, text="No job running", bg=C["panel"],
                                fg=C["text"], font=self.f_ui_b, anchor="w")
        self.lbl_job.grid(row=0, column=0, sticky="w")
        self.lbl_time = tk.Label(top, text="", bg=C["panel"], fg=C["muted"],
                                 font=self.f_small, anchor="e")
        self.lbl_time.grid(row=0, column=1, sticky="e")
        self.progress = ttk.Progressbar(pr, style="Accent.Horizontal.TProgressbar",
                                        orient="horizontal", mode="determinate")
        self.progress.grid(row=1, column=0, sticky="ew", pady=(7, 0))

        # -- console card
        con_outer = tk.Frame(bottom, bg=C["border"], padx=1, pady=1)
        con_outer.grid(row=1, column=0, sticky="nsew")
        con = tk.Frame(con_outer, bg=C["panel"], padx=12, pady=10)
        con.pack(fill="both", expand=True)
        con.grid_rowconfigure(1, weight=1)
        con.grid_columnconfigure(0, weight=1)
        head = tk.Frame(con, bg=C["panel"]); head.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        head.grid_columnconfigure(0, weight=1)
        tk.Label(head, text="CONSOLE", bg=C["panel"], fg=C["muted"],
                 font=self.f_small_b, anchor="w").grid(row=0, column=0, sticky="w")
        self.autoscroll = tk.BooleanVar(value=True)
        tk.Checkbutton(head, text="autoscroll", variable=self.autoscroll,
                       bg=C["panel"], fg=C["muted"], activebackground=C["panel"],
                       activeforeground=C["text"], selectcolor=C["panel2"],
                       font=self.f_small, highlightthickness=0, bd=0
                       ).grid(row=0, column=1, sticky="e", padx=(0, 10))
        clear = tk.Label(head, text="clear", bg=C["panel"], fg=C["dim"],
                         font=self.f_small, cursor="hand2")
        clear.grid(row=0, column=2, sticky="e")
        clear.bind("<Button-1>", lambda e: self._clear_log())

        body = tk.Frame(con, bg=C["panel"])
        body.grid(row=1, column=0, sticky="nsew")
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(0, weight=1)
        self.text = tk.Text(body, bg=C["sunken"], fg=C["text"], bd=0,
                            insertbackground=C["text"], relief="flat",
                            font=self.f_mono, wrap="word", padx=10, pady=8,
                            selectbackground=C["border"], state="disabled",
                            highlightthickness=0)
        self.text.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(body, orient="vertical", command=self.text.yview,
                           style="Dark.Vertical.TScrollbar")
        sb.grid(row=0, column=1, sticky="ns")
        self.text.configure(yscrollcommand=sb.set)
        for tag, color, font in (
                ("plain", C["text"], self.f_mono),
                ("ts", C["dim"], self.f_mono),
                ("head", C["cyan"], self.f_mono_b),
                ("batch", C["purple"], self.f_mono_b),
                ("accent", C["accent"], self.f_mono),
                ("info", C["blue"], self.f_mono),
                ("ok", C["green"], self.f_mono),
                ("warn", C["yellow"], self.f_mono),
                ("error", C["red"], self.f_mono),
                ("mutedln", C["muted"], self.f_mono),
                ("divider", C["dim"], self.f_mono)):
            self.text.tag_configure(tag, foreground=color, font=font)

        # ===== status bar ====================================================
        status = tk.Frame(root, bg=C["panel"], padx=14, pady=5)
        status.grid(row=2, column=0, columnspan=2, sticky="ew")
        status.grid_columnconfigure(1, weight=1)
        self.lbl_status = tk.Label(status, text="Idle", bg=C["panel"],
                                   fg=C["muted"], font=self.f_small, anchor="w")
        self.lbl_status.grid(row=0, column=0, sticky="w")
        tk.Label(status,
                 text="profiles: 2 original + 3 new  ·  default Aggressive  ·  outputs 16-bit WAV",
                 bg=C["panel"], fg=C["dim"], font=self.f_small, anchor="e"
                 ).grid(row=0, column=2, sticky="e")

        self._on_profile_change()

    # ------------------------------------------------------------- plot style
    def _style_axes(self, empty=False):
        ax = self.ax
        ax.set_facecolor(C["sunken"])
        for sp in ax.spines.values():
            sp.set_color(C["border"])
        ax.tick_params(colors=C["muted"], labelsize=8)
        ax.grid(True, which="both", color=C["border"], alpha=0.45, lw=0.5)
        ax.set_xlabel("frequency", fontsize=8, color=C["muted"])
        ax.set_ylabel("dB (norm)", fontsize=8, color=C["muted"])
        if empty:
            ax.set_xlim(20, 24000)
            ax.set_xscale("log")
            ax.xaxis.set_major_formatter(EngFormatter(unit=""))
            ax.set_ylim(-100, 5)
            ax.text(0.5, 0.5, "run a job to see the before / after spectrum",
                    transform=ax.transAxes, ha="center", va="center",
                    color=C["dim"], fontsize=9)

    def _draw_plot(self, d):
        ax = self.ax
        ax.clear()
        self._style_axes()
        f0 = d.get("f_orig")
        if f0 is None:
            self.mpl.draw_idle()
            return
        y0 = d["db_orig"]
        m0 = f0 > 0
        tau = d.get("tau", 0)
        has_filt = "f_filt" in d
        if has_filt:
            ax.plot(f0[m0], y0[m0], color=C["muted"], lw=1.0, label="original")
            f1, y1 = d["f_filt"], d["db_filt"]
            m1 = f1 > 0
            ax.plot(f1[m1], y1[m1], color=C["accent"], lw=1.5,
                    label=f"processed  (τ = {tau:g} µs)")
            ax.fill_between(f1[m1], y1[m1], -120, color=C["accent"], alpha=0.06)
            lo = min(y0[m0].min(), y1[m1].min())
        else:
            ax.plot(f0[m0], y0[m0], color=C["accent"], lw=1.4,
                    label="signal  (no filter applied)")
            ax.fill_between(f0[m0], y0[m0], -120, color=C["accent"], alpha=0.06)
            lo = y0[m0].min()
        ax.axvline(10000, color=C["yellow"], lw=0.9, ls="--", alpha=0.65,
                   label="10 kHz HF gate")
        ax.set_xscale("log")
        ax.set_xlim(20, max(1000, d["sr"] / 2))
        ax.xaxis.set_major_formatter(EngFormatter(unit=""))
        ax.set_ylim(max(-110, lo - 4), 5)
        leg = ax.legend(loc="lower left", fontsize=8, frameon=False)
        for t in leg.get_texts():
            t.set_color(C["text"])
        ax.set_title(f"{d['file']}    ·    hf/total {d['ratio']:.3f}",
                     loc="left", fontsize=9, color=C["text"])
        self.mpl.draw_idle()

    # --------------------------------------------------------------- browsing
    def browse_input_file(self):
        p = filedialog.askopenfilename(filetypes=[
            ("Audio Files", "*.mp3 *.wav *.flac *.ogg *.opus"),
            ("All files", "*.*")])
        if p:
            self.input_file = p
            self.lbl_file.config(text=shorten_path(p), fg=C["text"])
            self._log_line(f"Input file set: {p}", "mutedln")

    def browse_input_folder(self):
        p = filedialog.askdirectory()
        if p:
            self.input_folder = p
            self.lbl_folder.config(text=shorten_path(p), fg=C["text"])
            self._log_line(f"Batch folder set: {p}", "mutedln")

    def browse_output_directory(self):
        p = filedialog.askdirectory()
        if p:
            self.output_dir = p
            self.lbl_out.config(text=shorten_path(p), fg=C["text"])
            self._log_line(f"Output folder set: {p}", "mutedln")

    # ------------------------------------------------------------------- jobs
    def _read_settings(self):
        try:
            self.target_peak_db = float(self.norm_entry.get())
        except ValueError:
            messagebox.showerror("Error", "Please enter a valid number for normalization level.")
            return False
        self.intensity = self.intensity_var.get()
        return True

    def run_job(self):
        if self.running:
            return
        if not self.input_file or not self.output_dir:
            messagebox.showerror("Error", "Please select both an input file and an output directory.")
            return
        if not self._read_settings():
            return
        input_file_name = os.path.basename(self.input_file)
        output_file = os.path.join(self.output_dir,
                                   os.path.splitext(input_file_name)[0] + '.wav')
        self._start_job("single",
                        f"Single file  ·  {self.intensity}  ·  peak {self.target_peak_db:g} dBFS")
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)
        self.lbl_job.config(text=f"Processing  {input_file_name}")
        args = (self.input_file, output_file, self.target_peak_db, self.intensity)
        self.worker = threading.Thread(target=self._worker_single, args=args, daemon=True)
        self.worker.start()

    def run_batch_job(self):
        if self.running:
            return
        if not self.input_folder or not self.output_dir:
            messagebox.showerror("Error", "Please select both an input folder and an output directory.")
            return
        if not self._read_settings():
            return
        self._start_job("batch",
                        f"Batch  ·  {self.intensity}  ·  peak {self.target_peak_db:g} dBFS")
        self.progress.configure(mode="determinate", value=0, maximum=1)
        self.lbl_job.config(text="Scanning folder…")
        args = (self.input_folder, self.output_dir, self.target_peak_db, self.intensity)
        self.worker = threading.Thread(target=self._worker_batch, args=args, daemon=True)
        self.worker.start()

    def stop_job(self):
        if not self.running:
            return
        self.cancel_event.set()
        self._set_pill("STOPPING", C["yellow"])
        self._log_line("Stop requested — the current file will finish, then the job stops.", "warn")

    def _start_job(self, mode, desc):
        self.running = True
        self.job_mode = mode
        self.cancel_event.clear()
        self.t0 = time.time()
        self._file_times = [self.t0]
        self._set_buttons(False)
        self._set_pill("PROCESSING", C["accent"])
        self.lbl_status.config(text=desc)
        self.lbl_time.config(text="elapsed 00:00")
        self._log_line("─" * 64, "divider")
        self._log_line("▶ " + desc, "batch")

    def _finish_job(self, status):
        self.running = False
        self.progress.stop()
        self.progress.configure(mode="determinate")
        if status == "done":
            self.progress.configure(maximum=1, value=1)
            self._set_pill("DONE", C["green"])
        elif status == "stopped":
            self._set_pill("STOPPED", C["yellow"])
        else:
            self._set_pill("ERROR", C["red"])
        self._set_buttons(True)
        if self.t0 is not None:
            self.lbl_time.config(text="elapsed " + self._fmt_t(time.time() - self.t0))

    def _set_buttons(self, idle):
        if idle:
            self.run_button.config(state="normal", bg=self.run_button._base)
            self.run_batch_button.config(state="normal", bg=self.run_batch_button._base)
            self.stop_button.config(state="disabled", bg=C["panel2"])
        else:
            self.run_button.config(state="disabled", bg=C["panel2"])
            self.run_batch_button.config(state="disabled", bg=C["panel2"])
            self.stop_button.config(state="normal", bg=self.stop_button._base)

    # ---------------------------------------------------------------- workers
    def _emit(self, kind, data):
        self.q.put((kind, data))

    def _worker_single(self, in_f, out_f, peak, prof):
        w = QueueWriter(self.q)
        ok = True
        with contextlib.redirect_stdout(w), contextlib.redirect_stderr(w):
            try:
                process_audio(in_f, out_f, peak, prof, emit=self._emit)
            except Exception:
                ok = False
                print("ERROR:")
                print(traceback.format_exc())
        w.flush()
        self.q.put(("finished", {"status": "done" if ok else "error",
                                 "summary": f"Saved  {os.path.basename(out_f)}" if ok
                                 else "Single job failed — see console"}))

    def _worker_batch(self, in_dir, out_dir, peak, prof):
        w = QueueWriter(self.q)
        status, summary = "error", "Batch failed — see console"
        with contextlib.redirect_stdout(w), contextlib.redirect_stderr(w):
            try:
                done, total, cancelled = process_audio_batch(
                    in_dir, out_dir, peak, prof,
                    emit=self._emit, cancel_event=self.cancel_event)
                if cancelled:
                    status, summary = "stopped", f"Stopped — {done} of {total} files done"
                else:
                    status, summary = "done", f"Batch complete — {done} of {total} files"
            except Exception:
                print("ERROR:")
                print(traceback.format_exc())
        w.flush()
        self.q.put(("finished", {"status": status, "summary": summary}))

    # ------------------------------------------------------------- queue pump
    def _poll_queue(self):
        try:
            for _ in range(300):
                kind, data = self.q.get_nowait()
                self._handle(kind, data)
        except queue.Empty:
            pass
        if self.running and self.t0 is not None:
            self.lbl_time.config(text="elapsed " + self._fmt_t(time.time() - self.t0)
                                 + self._eta_txt())
            self._pulse = (self._pulse + 1) % 16
            if self._pulse == 0 and self.pill.cget("text").strip() == "PROCESSING":
                cur = self.pill.cget("fg")
                self._set_pill("PROCESSING",
                               C["accent2"] if cur == C["accent"] else C["accent"],
                               keep_text=True)
        self.root.after(self.POLL_MS, self._poll_queue)

    def _eta_txt(self):
        if self.job_mode != "batch" or len(self._file_times) < 2:
            return ""
        total = int(self.progress.cget("maximum"))
        done = len(self._file_times) - 1
        if total <= done:
            return ""
        avg = (self._file_times[-1] - self._file_times[0]) / done
        return "  ·  eta " + self._fmt_t(avg * (total - done))

    @staticmethod
    def _fmt_t(sec):
        sec = int(max(0, sec))
        return f"{sec // 60:02d}:{sec % 60:02d}"

    def _handle(self, kind, data):
        if kind == "log":
            line = data["line"]
            self._log_line(line, _classify(line))
        elif kind == "analysis":
            self.meter.set_reading(data["ratio"], data["tau"], data["file"])
        elif kind == "plot":
            self._draw_plot(data)
        elif kind == "batch_start":
            total = max(1, data["total"])
            self.progress.configure(mode="determinate", maximum=total, value=0)
            self.lbl_job.config(text=f"0 / {data['total']} files")
        elif kind == "file_start":
            self.lbl_job.config(
                text=f"{data['index'] - 1} / {data['total']}   ·   {data['name']}")
        elif kind == "file_done":
            self.progress.configure(value=data["index"])
            self._file_times.append(time.time())
            self.lbl_job.config(text=f"{data['index']} / {data['total']} files")
        elif kind == "batch_end":
            if data["errors"]:
                self._log_line(f"{data['errors']} file(s) failed — see messages above.", "warn")
        elif kind == "finished":
            self._finish_job(data["status"])
            self.lbl_job.config(text=data["summary"])
            tag = {"done": "ok", "stopped": "warn", "error": "error"}[data["status"]]
            self._log_line("■ " + data["summary"], tag)
            self.lbl_status.config(text=data["summary"])
            if data["status"] == "error":
                messagebox.showerror("Processing failed",
                                     "The job hit an error — details are in the console.")

    # ---------------------------------------------------------------- console
    def _log_line(self, line, tag="plain"):
        t = self.text
        t.configure(state="normal")
        t.insert("end", time.strftime("%H:%M:%S  "), ("ts",))
        t.insert("end", line + "\n", (tag,))
        # trim
        n = int(t.index("end-1c").split(".")[0])
        if n > 1500:
            t.delete("1.0", f"{n - 1500}.0")
        t.configure(state="disabled")
        if self.autoscroll.get():
            t.see("end")

    def _clear_log(self):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")

    # ------------------------------------------------------------------ misc
    def _set_pill(self, text, color, keep_text=False):
        self.pill.config(text=f"  {text}  " if not keep_text else self.pill.cget("text"),
                         fg=color)

    def _on_profile_change(self, *_):
        prof = self.intensity_var.get()
        note = PROFILE_BLURB.get(prof, "")
        self.lbl_blurb.config(text=note)
        lad = profile_ladder(prof)
        self.lbl_ladder.config(
            text=f"τ µs   >0.4→{lad[0]:g}   >0.3→{lad[1]:g}   "
                 f">0.2→{lad[2]:g}   gate→{lad[3]:g}")
        self.meter.redraw()

    def _on_close(self):
        if self.running:
            if not messagebox.askokcancel(
                    "Job running", "A job is still running. Quit anyway?"):
                return
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = AudioProcessorApp(root)
    root.mainloop()

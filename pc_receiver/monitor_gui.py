
import tkinter as tk
from tkinter import ttk, filedialog
import threading
import socket
import json
import csv
import os
import traceback
from datetime import datetime, timezone, timedelta
from collections import deque

# pyserial is only needed for the Serial (USB) mode. Keep it optional so the GUI
# still launches — and TCP/Ethernet still works — on a PC that doesn't have it.
try:
    import serial
    import serial.tools.list_ports
    HAS_SERIAL = True
except ImportError:
    serial = None
    HAS_SERIAL = False

import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure


# ── Enterprise dark palette ──────────────────────────────────────────────────
# Tuned for an even, low-glare "operations console" look (think Grafana /
# Datadog): a near-black canvas, slate panels, hairline borders and a small,
# disciplined set of semantic accents. Names are kept stable so the rest of the
# GUI keeps referencing them unchanged.
BG          = "#0a0e14"   # app canvas
BG2         = "#10151e"   # top bar / sidebar
BG3         = "#171d28"   # inputs / wells
BG4         = "#212a38"   # hover
PANEL       = "#0e131b"   # chart plotting surface
GRID        = "#1d2530"   # chart gridlines
ACCENT      = "#3ddc84"   # primary / good / connected
ACCENT_DIM  = "#2aa866"
ACCENT2     = "#f5a623"   # warning / amber
ACCENT3     = "#4d9bff"   # info / blue
ACCENT4     = "#b388ff"   # secondary / violet
TEXT        = "#eef2f7"
TEXT_DIM    = "#6e7d90"
TEXT_MID    = "#9fadbf"
RED         = "#ff5d5d"
RED_DIM     = "#8f2f2f"
GREEN       = "#5ce6a0"
BORDER      = "#222c3a"
CARD        = "#11161f"
CARD_HOVER  = "#171f2b"
GOLD        = "#ffce54"

IST_OFFSET  = timedelta(hours=5, minutes=30)
MAX_POINTS  = 600  # 10 minutes at 1 Hz

# CSV columns the GUI writes. Kept as one constant so we can both write it and
# detect when an existing log was made with a different (older) schema.
CSV_HEADER = [
    "pc_timestamp", "seq", "uptime_us", "gps_locked", "gps_stale",
    "gps_time_utc", "gps_time_ist",
    "latitude", "longitude", "altitude",
    "satellites", "hdop", "fix_quality", "temp_c", "pc_drift_s",
]

# Raspberry Pi USB Vendor ID — every Pico board uses this
PICO_VID = 0x2E8A


# ── Fonts ──────────────────────────────────────────────────────────────────
# Default to Tk's built-in families so the GUI renders on ANY OS even before
# _select_fonts() runs. _select_fonts() (called once a Tk root exists) upgrades
# these to nicer families when installed — Windows, macOS or Linux — and falls
# back gracefully when they aren't. Keeps the app portable to any computer.
FONT_UI   = "TkDefaultFont"
FONT_MONO = "TkFixedFont"


def _select_fonts():
    """Pick the best available UI / monospace font family for this machine."""
    global FONT_UI, FONT_MONO
    import tkinter.font as tkfont
    available = set(tkfont.families())

    for fam in ("Segoe UI", "SF Pro Text", "Helvetica Neue",
                "Ubuntu", "DejaVu Sans", "Arial"):
        if fam in available:
            FONT_UI = fam
            break

    for fam in ("Cascadia Code", "Consolas", "Menlo", "SF Mono",
                "DejaVu Sans Mono", "Courier New"):
        if fam in available:
            FONT_MONO = fam
            break



class TimeSyncTracker:
    """Tracks GPS ↔ PC time offset with rolling average."""
    def __init__(self, window_size=30):
        self.offsets = []
        self.window_size = window_size

    def update(self, gps_utc, pc_utc):
        offset = (gps_utc - pc_utc).total_seconds()
        self.offsets.append(offset)
        if len(self.offsets) > self.window_size:
            self.offsets = self.offsets[-self.window_size:]

    def get_drift_seconds(self):
        if not self.offsets:
            return 0.0
        return -sum(self.offsets) / len(self.offsets)

    def get_corrected_time(self):
        avg = sum(self.offsets) / len(self.offsets) if self.offsets else 0
        return datetime.now(timezone.utc) + timedelta(seconds=avg)

    @property
    def has_data(self):
        return len(self.offsets) > 0


def parse_gps_utc(gps_time_str):
    """Parse GPS UTC time string into datetime."""
    if not gps_time_str:
        return None
    try:
        if gps_time_str.endswith("Z"):
            return datetime.strptime(gps_time_str, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc)
        if "+05:30" in gps_time_str:
            t_str = gps_time_str.replace("+05:30", "")
            t = datetime.strptime(t_str, "%Y-%m-%dT%H:%M:%S")
            return (t - IST_OFFSET).replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    return None


def find_pico_port():
    """
    Scan all COM ports for a Raspberry Pi Pico.
    Returns the port device string (e.g. 'COM3') or None.
    """
    if not HAS_SERIAL:
        return None
    ports = serial.tools.list_ports.comports()
    # Priority 1: match by Raspberry Pi USB Vendor ID
    for p in ports:
        if p.vid == PICO_VID:
            return p.device
    # Priority 2: keyword match in description
    for p in ports:
        desc = (p.description or "").lower()
        if any(k in desc for k in ["pico", "rp2", "rp23", "raspberry"]):
            return p.device
    return None


def list_local_ipv4():
    """
    Best-effort list of this PC's IPv4 addresses, so the user can pick the one
    on the cable to the Pico instead of memorizing it. Most-likely first.
    """
    ips = []

    # The address the OS would use to reach an outside host = primary adapter.
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))   # no packets sent; just picks a route
        ips.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass

    # Everything bound to this hostname (covers the direct-cable adapter).
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None,
                                       socket.AF_INET):
            ip = info[4][0]
            if ip not in ips:
                ips.append(ip)
    except OSError:
        pass

    ips = [ip for ip in ips if not ip.startswith("127.")]
    return ips


def is_valid_ipv4(text):
    parts = text.strip().split(".")
    if len(parts) != 4:
        return False
    for p in parts:
        if not p.isdigit() or not (0 <= int(p) <= 255):
            return False
    return True



class MonitorGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("GPS Temperature Monitor — Pico 2 RP2350")
        self.root.configure(bg=BG)
        self.root.minsize(1280, 780)


        self.conn_thread    = None
        self.running        = False
        self.ser            = None
        self.tcp_server     = None
        self.tcp_conn       = None
        self.record_count   = 0
        self.csv_file       = None
        self.csv_writer     = None
        self.connect_time   = None
        self.last_seq       = 0
        self.seq_gaps       = 0
        self.device_id      = None   # set when the Pico's boot banner is seen
        self.gps_baud       = None   # set when the Pico reports its locked baud
        self._last_pico     = None   # port we last auto-selected (rescan helper)

        # GPS lock-state tracking, so we can timestamp the exact moment a fix is
        # lost or (re)acquired and surface it as a notification + log event.
        self.gps_was_locked = None   # None = unknown yet; else last known state
        self.gps_lost_at    = None   # IST datetime of the most recent lock loss
        self.gps_acquired_at = None  # IST datetime of the most recent lock gain
        self.gps_lost_count = 0      # how many times lock has dropped this session
        self.gps_was_stale  = None   # last known stale flag while locked (None=unknown)

        # One-shot guards so recurring conditions fire a notification only on the
        # meaningful transition, never once per record (which would flood the log).
        self._first_record_seen  = False
        self._csv_error_notified = False
        self._pico_notified_port = None

        self.time_sync = TimeSyncTracker(window_size=30)

        self.temp_min   = None
        self.temp_max   = None
        self.temp_sum   = 0.0
        self.temp_count = 0
        self.last_temp  = None

        self.times_gps  = deque(maxlen=MAX_POINTS)
        self.temps_gps  = deque(maxlen=MAX_POINTS)
        self.times_pc   = deque(maxlen=MAX_POINTS)
        self.temps_pc   = deque(maxlen=MAX_POINTS)


        self._pulse_on  = False
        self._pulse_id  = None

        self._build_ui()
        self._on_mode_change()       # show the panel matching the default mode
        self._refresh_ports()
        self._auto_rescan()          # keep watching for the Pico to appear
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)



    def _build_ui(self):

        top = tk.Frame(self.root, bg=BG2, height=56)
        top.pack(fill="x", side="top")
        top.pack_propagate(False)


        brand_frame = tk.Frame(top, bg=BG2)
        brand_frame.pack(side="left", padx=20, pady=8)

        tk.Label(brand_frame, text="🌡", bg=BG2, fg=ACCENT,
                 font=(FONT_UI, 18)).pack(side="left")
        title_frame = tk.Frame(brand_frame, bg=BG2)
        title_frame.pack(side="left", padx=(8, 0))
        tk.Label(title_frame, text="GPS Temperature Monitor",
                 bg=BG2, fg=TEXT,
                 font=(FONT_UI, 13, "bold")).pack(anchor="w")
        self.subtitle_label = tk.Label(
                 title_frame, text="Pico 2 RP2350  •  GNSS + Temp  •  W5500",
                 bg=BG2, fg=TEXT_DIM, font=(FONT_UI, 8))
        self.subtitle_label.pack(anchor="w")


        right_frame = tk.Frame(top, bg=BG2)
        right_frame.pack(side="right", padx=16, pady=8)

        self.conn_timer_label = tk.Label(right_frame, text="",
                                         bg=BG2, fg=TEXT_DIM,
                                         font=(FONT_UI, 9))
        self.conn_timer_label.pack(side="right", padx=(12, 0))

        self.status_label = tk.Label(right_frame, text="Disconnected",
                                     bg=BG2, fg=TEXT_DIM,
                                     font=(FONT_UI, 10))
        self.status_label.pack(side="right", padx=(6, 0))

        self.status_dot = tk.Label(right_frame, text="●", fg=RED_DIM,
                                   bg=BG2, font=(FONT_UI, 14))
        self.status_dot.pack(side="right")

        self.rate_label = tk.Label(right_frame, text="",
                                   bg=BG2, fg=TEXT_DIM,
                                   font=(FONT_UI, 9))
        self.rate_label.pack(side="right", padx=(0, 16))


        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True, padx=0, pady=0)


        sidebar = tk.Frame(body, bg=BG2, width=240)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        self._build_sidebar(sidebar)


        content = tk.Frame(body, bg=BG)
        content.pack(side="left", fill="both", expand=True)
        self._build_content(content)


        bar = tk.Frame(self.root, bg=BG3, height=30)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)
        self.bottom_label = tk.Label(bar, text="Ready. Configure connection and press Connect.",
                                     bg=BG3, fg=TEXT_DIM, font=(FONT_UI, 9))
        self.bottom_label.pack(side="left", padx=14, pady=5)
        self.gap_label = tk.Label(bar, text="", bg=BG3, fg=RED,
                                  font=(FONT_UI, 9))
        self.gap_label.pack(side="right", padx=14, pady=5)

    def _build_sidebar(self, parent):

        canvas = tk.Canvas(parent, bg=BG2, highlightthickness=0)
        canvas.pack(fill="both", expand=True)
        sb_inner = tk.Frame(canvas, bg=BG2)
        canvas.create_window((0, 0), window=sb_inner, anchor="nw")

        def section(text):
            tk.Label(sb_inner, text=text, bg=BG2, fg=TEXT_DIM,
                     font=(FONT_UI, 8, "bold"),
                     anchor="w").pack(fill="x", padx=18, pady=(18, 4))

        def sep():
            tk.Frame(sb_inner, bg=BORDER, height=1).pack(fill="x", padx=14, pady=6)


        section("CONNECTION MODE")
        mode_frame = tk.Frame(sb_inner, bg=BG2)
        mode_frame.pack(fill="x", padx=14)

        # Default to Serial, but fall back to TCP if pyserial isn't installed so
        # the GUI is still usable over Ethernet on a bare PC.
        self.mode_var = tk.StringVar(value="serial" if HAS_SERIAL else "tcp")
        modes = [("Serial (USB)", "serial"), ("TCP (Ethernet)", "tcp")]
        for text, val in modes:
            rb = tk.Radiobutton(mode_frame, text=text, variable=self.mode_var,
                                value=val, bg=BG2, fg=TEXT_MID,
                                selectcolor=BG3, activebackground=BG2,
                                activeforeground=TEXT,
                                font=(FONT_UI, 9),
                                command=self._on_mode_change)
            if val == "serial" and not HAS_SERIAL:
                rb.config(state="disabled", text="Serial (USB) — pyserial missing")
            rb.pack(anchor="w", pady=1)

        sep()


        self.serial_frame = tk.Frame(sb_inner, bg=BG2)
        self.serial_frame.pack(fill="x")

        section_in = lambda parent, text: tk.Label(
            parent, text=text, bg=BG2, fg=TEXT_DIM,
            font=(FONT_UI, 8, "bold"), anchor="w"
        ).pack(fill="x", padx=18, pady=(6, 3))

        section_in(self.serial_frame, "SERIAL PORT")
        port_row = tk.Frame(self.serial_frame, bg=BG2)
        port_row.pack(fill="x", padx=14)

        self.port_var = tk.StringVar(value="")
        self.port_combo = ttk.Combobox(port_row, textvariable=self.port_var,
                                       width=16, state="readonly")
        self.port_combo.pack(side="left", pady=2)

        self.refresh_btn = self._icon_button(port_row, "⟳", ACCENT3,
                                             self._refresh_ports)
        self.refresh_btn.pack(side="left", padx=4)

        section_in(self.serial_frame, "BAUD RATE")
        self.baud_var = tk.StringVar(value="115200")
        # This dropdown opens the USB-CDC link, whose rate is virtual — any value
        # connects at full USB speed. It also AUTO-UPDATES to the GPS module's
        # rate once the Pico auto-detects and reports it (see _reflect_gps_baud),
        # so you can see the detected rate without picking it by hand. The 4800…
        # list mirrors the firmware's auto-baud candidates.
        self.baud_combo = ttk.Combobox(self.serial_frame, textvariable=self.baud_var,
                                  values=["4800", "9600", "19200", "38400",
                                          "57600", "115200", "230400", "460800",
                                          "921600"],
                                  width=16, state="readonly")
        self.baud_combo.pack(anchor="w", padx=14, pady=2)
        tk.Label(self.serial_frame,
                 text="Auto-updates to the Pico's detected GPS rate. USB is "
                      "virtual, so any value still connects.",
                 bg=BG2, fg=TEXT_DIM, font=(FONT_UI, 7),
                 wraplength=200, justify="left", anchor="w").pack(
                     fill="x", padx=16, pady=(0, 2))

        self.tcp_frame = tk.Frame(sb_inner, bg=BG2)

        section_in(self.tcp_frame, "LISTEN PORT")
        self.tcp_port_var = tk.StringVar(value="5005")
        tcp_port_entry = tk.Entry(self.tcp_frame, textvariable=self.tcp_port_var,
                                  bg=BG3, fg=TEXT, insertbackground=TEXT,
                                  relief="flat", font=(FONT_UI, 10), width=18)
        tcp_port_entry.pack(padx=14, pady=2, fill="x")

        sep()


        # ── Pico target (server IP) ────────────────────────────────────────
        # The IP the Pico dials over Ethernet. Set it once over USB and the
        # Pico saves it to flash — no firmware rebuild when the PC's IP changes.
        section("PICO TARGET (SERVER IP)")
        sip_row = tk.Frame(sb_inner, bg=BG2)
        sip_row.pack(fill="x", padx=14)

        local_ips = list_local_ipv4()
        self.server_ip_var = tk.StringVar(
            value=local_ips[0] if local_ips else "192.168.2.1")
        self.server_ip_combo = ttk.Combobox(sip_row,
                                            textvariable=self.server_ip_var,
                                            values=local_ips, width=14)
        self.server_ip_combo.pack(side="left", pady=2)

        self.send_ip_btn = self._icon_button(sip_row, "→", ACCENT,
                                             self._send_server_ip)
        self.send_ip_btn.pack(side="left", padx=4)

        tk.Label(sb_inner,
                 text="Sends to Pico over USB → saved to flash",
                 bg=BG2, fg=TEXT_DIM,
                 font=(FONT_UI, 7)).pack(anchor="w", padx=16)

        sep()


        self.connect_btn = tk.Button(sb_inner, text="  ▶  CONNECT",
                                     bg=ACCENT, fg=BG,
                                     activebackground=ACCENT_DIM,
                                     activeforeground=BG,
                                     font=(FONT_UI, 10, "bold"),
                                     relief="flat", cursor="hand2",
                                     height=2, bd=0,
                                     command=self._toggle_connect)
        self.connect_btn.pack(fill="x", padx=14, pady=8)
        self._add_hover(self.connect_btn, ACCENT, "#00c864")

        sep()


        section("LOG FILE")
        self.csv_var = tk.StringVar(value="temperature_log.csv")
        csv_entry = tk.Entry(sb_inner, textvariable=self.csv_var,
                             bg=BG3, fg=TEXT, insertbackground=TEXT,
                             relief="flat", font=(FONT_UI, 9), width=22)
        csv_entry.pack(padx=14, pady=2, fill="x")

        browse_btn = self._text_button(sb_inner, "Browse…", ACCENT3,
                                       self._browse_csv)
        browse_btn.pack(anchor="w", padx=14, pady=2)

        sep()


        section("SESSION")
        self.rec_count_var = tk.StringVar(value="0")
        tk.Label(sb_inner, textvariable=self.rec_count_var,
                 bg=BG2, fg=ACCENT, font=(FONT_UI, 32, "bold")).pack(padx=14)
        tk.Label(sb_inner, text="records logged",
                 bg=BG2, fg=TEXT_DIM, font=(FONT_UI, 9)).pack(padx=14)

        sep()


        clear_btn = self._text_button(sb_inner, "Clear Chart", TEXT_DIM,
                                      self._clear_chart)
        clear_btn.pack(fill="x", padx=14, pady=2)


        sb_inner.update_idletasks()
        canvas.configure(scrollregion=canvas.bbox("all"))

    def _build_content(self, parent):

        cards1 = tk.Frame(parent, bg=BG)
        cards1.pack(fill="x", padx=16, pady=(14, 4))

        self.temp_card = self._make_kpi(cards1, "TEMPERATURE", "–.–– °C",
                                        ACCENT, "°C")
        self.lock_card = self._make_kpi(cards1, "GPS STATUS", "NO LOCK",
                                        RED, "")
        self.time_card = self._make_kpi(cards1, "IST TIME", "––:––:––",
                                        ACCENT3, "")
        self.alt_card  = self._make_kpi(cards1, "ALTITUDE", "–– m",
                                        ACCENT2, "m")


        cards2 = tk.Frame(parent, bg=BG)
        cards2.pack(fill="x", padx=16, pady=(0, 6))

        self.sat_card   = self._make_kpi(cards2, "SATELLITES", "0",
                                         GOLD, "")
        self.hdop_card  = self._make_kpi(cards2, "HDOP", "–",
                                         ACCENT4, "")
        self.drift_card = self._make_kpi(cards2, "PC DRIFT", "–",
                                         TEXT_MID, "s")
        self.uptime_card = self._make_kpi(cards2, "PICO UPTIME", "0 s",
                                          TEXT_DIM, "s")


        cards3 = tk.Frame(parent, bg=BG)
        cards3.pack(fill="x", padx=16, pady=(0, 6))

        self.lat_card = self._make_kpi(cards3, "LATITUDE",  "–", ACCENT3, "")
        self.lon_card = self._make_kpi(cards3, "LONGITUDE", "–", ACCENT3, "")


        # IMPORTANT: pack the fixed bottom pieces (logs, then detail strip)
        # BEFORE the expanding chart. pack() hands out space in call order, so
        # reserving the bottom panels first means the chart only takes what's
        # left — the records and notifications stay on screen even on a short
        # window, instead of being pushed off the bottom.

        # ── Logs row: sensor records (left) + notifications (right) ──────────
        # Two fixed-height panels side by side: the streaming record log keeps
        # its full width, while discrete events get their own readable column.
        log_row = tk.Frame(parent, bg=BG, height=150)
        log_row.pack(side="bottom", fill="x", padx=16, pady=(0, 10))
        log_row.pack_propagate(False)  # prevent children from resizing this frame

        rec_outer = tk.Frame(log_row, bg=BORDER, bd=0)
        rec_outer.pack(side="left", fill="both", expand=True)
        self.log_text = self._build_log_panel(
            rec_outer, "RECENT RECORDS", wrap="none", hscroll=True)

        notif_outer = tk.Frame(log_row, bg=BORDER, bd=0, width=380)
        notif_outer.pack(side="right", fill="y", padx=(8, 0))
        notif_outer.pack_propagate(False)
        self.notif_text = self._build_log_panel(
            notif_outer, "NOTIFICATIONS", wrap="word", hscroll=False)


        # ── Detail strip (date / fix / temp stats / GPS event) ───────────────
        detail = tk.Frame(parent, bg=BG2, height=42)
        detail.pack(side="bottom", fill="x", padx=16, pady=(0, 4))
        detail.pack_propagate(False)

        self.date_label  = self._detail_item(detail, "📅 DATE", "–")
        tk.Frame(detail, bg=BORDER, width=1).pack(side="left", fill="y", pady=8)
        self.fix_label   = self._detail_item(detail, "📡 FIX", "–")
        tk.Frame(detail, bg=BORDER, width=1).pack(side="left", fill="y", pady=8)
        self.stats_label = self._detail_item(detail, "📊 TEMP", "–")
        tk.Frame(detail, bg=BORDER, width=1).pack(side="left", fill="y", pady=8)
        self.gps_event_label = self._detail_item(detail, "🛰 GPS EVENT", "–")


        # ── Live chart (expands to fill whatever height is left) ─────────────
        chart_outer = tk.Frame(parent, bg=BORDER, bd=0)
        chart_outer.pack(side="top", fill="both", expand=True, padx=16,
                         pady=(0, 4))
        chart_frame = tk.Frame(chart_outer, bg=CARD, bd=0)
        chart_frame.pack(fill="both", expand=True, padx=1, pady=1)

        chart_head = tk.Frame(chart_frame, bg=BG3)
        chart_head.pack(fill="x")
        tk.Label(chart_head, text="LIVE TELEMETRY", bg=BG3, fg=TEXT,
                 font=(FONT_UI, 9, "bold")).pack(side="left", padx=12, pady=5)
        tk.Label(chart_head,
                 text="Temperature  —  last 10 min @ 1 Hz",
                 bg=BG3, fg=TEXT_DIM,
                 font=(FONT_UI, 8)).pack(side="left", pady=5)
        self._build_chart(chart_frame)



    def _build_log_panel(self, parent, title, wrap, hscroll):
        """Build one bordered log panel (header + scrollable Text) and return
        its Text widget. Shared by the records and notifications panels so they
        stay visually identical."""
        frame = tk.Frame(parent, bg=BG2, bd=0)
        frame.pack(fill="both", expand=True, padx=1, pady=1)

        header = tk.Frame(frame, bg=BG3)
        header.pack(fill="x")
        tk.Label(header, text=title, bg=BG3, fg=TEXT_DIM,
                 font=(FONT_UI, 8, "bold")).pack(side="left", padx=12, pady=4)

        # horizontal scrollbar (records only) goes on the frame bottom so it sits
        # outside the text body; pack it before the body so it isn't squeezed out.
        if hscroll:
            h_scroll = tk.Scrollbar(frame, orient="horizontal")
            h_scroll.pack(side="bottom", fill="x")

        body = tk.Frame(frame, bg=BG)
        body.pack(fill="both", expand=True, padx=1, pady=(0, 1))

        v_scroll = tk.Scrollbar(body, orient="vertical")
        v_scroll.pack(side="right", fill="y")

        text = tk.Text(body, bg=BG, fg=TEXT_DIM, font=(FONT_MONO, 9),
                       relief="flat", state="disabled",
                       selectbackground=BG3, padx=8, pady=4,
                       wrap=wrap, yscrollcommand=v_scroll.set)
        text.pack(side="left", fill="both", expand=True)
        v_scroll.config(command=text.yview)

        if hscroll:
            h_scroll.config(command=text.xview)
            text.configure(xscrollcommand=h_scroll.set)

        text.tag_config("gps",   foreground=ACCENT)
        text.tag_config("nogps", foreground=ACCENT2)
        text.tag_config("dim",   foreground=TEXT_DIM)
        text.tag_config("alert", foreground=RED)
        text.tag_config("even",  background="#0e1319")
        return text

    def _make_kpi(self, parent, label, value, color, unit):

        outer = tk.Frame(parent, bg=BORDER, bd=0)
        outer.pack(side="left", fill="both", expand=True, padx=3, pady=2)

        card = tk.Frame(outer, bg=CARD, bd=0)
        card.pack(fill="both", expand=True, padx=1, pady=1)


        accent_line = tk.Frame(card, bg=color, width=3)
        accent_line.pack(side="left", fill="y")

        inner = tk.Frame(card, bg=CARD)
        inner.pack(side="left", fill="both", expand=True, padx=(10, 12), pady=8)

        tk.Label(inner, text=label, bg=CARD, fg=TEXT_DIM,
                 font=(FONT_UI, 7, "bold"), anchor="w").pack(fill="x")

        val_frame = tk.Frame(inner, bg=CARD)
        val_frame.pack(fill="x")

        val_lbl = tk.Label(val_frame, text=value, bg=CARD, fg=color,
                           font=(FONT_UI, 18, "bold"), anchor="w")
        val_lbl.pack(side="left")


        trend_lbl = tk.Label(val_frame, text="", bg=CARD, fg=TEXT_DIM,
                             font=(FONT_UI, 10), anchor="w")
        trend_lbl.pack(side="left", padx=(4, 0))

        val_lbl._trend = trend_lbl
        val_lbl._color = color
        return val_lbl

    def _detail_item(self, parent, label, value):
        f = tk.Frame(parent, bg=BG2)
        f.pack(side="left", fill="both", expand=True, padx=10, pady=4)
        tk.Label(f, text=label, bg=BG2, fg=TEXT_DIM,
                 font=(FONT_UI, 7, "bold")).pack(anchor="w")
        lbl = tk.Label(f, text=value, bg=BG2, fg=TEXT,
                       font=(FONT_UI, 10))
        lbl.pack(anchor="w")
        return lbl

    def _icon_button(self, parent, text, color, command):
        btn = tk.Button(parent, text=text, bg=BG3, fg=color,
                        activebackground=BG4, activeforeground=color,
                        relief="flat", font=(FONT_UI, 12),
                        cursor="hand2", command=command, bd=0)
        self._add_hover(btn, BG3, BG4)
        return btn

    def _text_button(self, parent, text, color, command):
        btn = tk.Button(parent, text=text, bg=BG3, fg=color,
                        activebackground=BG4, activeforeground=color,
                        relief="flat", font=(FONT_UI, 9),
                        cursor="hand2", command=command, bd=0)
        self._add_hover(btn, BG3, BG4)
        return btn

    def _add_hover(self, widget, normal_bg, hover_bg):
        widget.bind("<Enter>", lambda e: widget.config(bg=hover_bg))
        widget.bind("<Leave>", lambda e: widget.config(bg=normal_bg))

    def _style_axis(self, ax, title, ylabel):
        """Apply the shared enterprise chart styling to one subplot."""
        ax.set_facecolor(PANEL)
        ax.set_title(title, color=TEXT, fontsize=9.5, fontweight="bold",
                     loc="left", pad=9)
        ax.set_ylabel(ylabel, color=TEXT_DIM, fontsize=8)
        ax.tick_params(colors=TEXT_DIM, labelsize=7.5, length=0)
        ax.grid(True, which="major", color=GRID, linewidth=0.6, alpha=0.7)
        ax.set_axisbelow(True)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))
        for side, spine in ax.spines.items():
            spine.set_color(BORDER)
            spine.set_linewidth(0.8)
            if side in ("top", "right"):
                spine.set_visible(False)

    def _build_chart(self, parent):
        plt.style.use("dark_background")
        self.fig = Figure(figsize=(8, 3.2), dpi=100, facecolor=CARD)

        # Single temperature trend, filling the panel. Generous margins keep the
        # axis labels readable as the window resizes.
        self.ax = self.fig.add_subplot(111)
        self.fig.subplots_adjust(left=0.07, right=0.96, top=0.90, bottom=0.16)
        self._style_axis(self.ax, "TEMPERATURE  ·  °C", "Temperature (°C)")
        self.line_gps, = self.ax.plot([], [], "-", color=ACCENT, linewidth=2.0,
                                      solid_capstyle="round",
                                      label="GPS time", zorder=5)
        self.line_pc,  = self.ax.plot([], [], "-", color=ACCENT2, linewidth=1.6,
                                      solid_capstyle="round", alpha=0.85,
                                      label="PC time (drift-corrected)", zorder=4)
        self._temp_fill = None
        leg = self.ax.legend(loc="upper left", fontsize=7.5, ncol=2,
                             frameon=True, facecolor=BG2, edgecolor=BORDER,
                             labelcolor=TEXT_MID, borderpad=0.6,
                             handlelength=1.4)
        leg.get_frame().set_linewidth(0.8)

        for lbl in self.ax.get_xticklabels():
            lbl.set_rotation(0)
            lbl.set_ha("center")

        self.canvas = FigureCanvasTkAgg(self.fig, master=parent)
        self.canvas.draw()
        self.canvas.get_tk_widget().pack(fill="both", expand=True)

        self._animate()



    def _on_mode_change(self):
        mode = self.mode_var.get()
        if mode == "serial":
            self.tcp_frame.pack_forget()
            self.serial_frame.pack(fill="x", after=self.serial_frame.master.winfo_children()[2])
        else:
            self.serial_frame.pack_forget()
            self.tcp_frame.pack(fill="x", after=self.tcp_frame.master.winfo_children()[2])

    def _toggle_connect(self):
        if self.running:
            self._disconnect()
        else:
            self._connect()

    def _connect(self):

        path = self.csv_var.get()
        try:
            # If the file exists but its header doesn't match our current schema
            # (e.g. an older 7-column log), roll over to a fresh timestamped file
            # so columns never silently misalign.
            if os.path.exists(path) and os.path.getsize(path) > 0:
                with open(path, "r", encoding="utf-8", newline="") as f:
                    existing_header = f.readline().strip().split(",")
                if existing_header != CSV_HEADER:
                    base, ext = os.path.splitext(path)
                    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    path = f"{base}_{stamp}{ext or '.csv'}"
                    self.csv_var.set(path)
                    self._notify(
                        f"Existing log uses an old format — writing to "
                        f"{os.path.basename(path)} instead.", level="warn")

            new_file = not (os.path.exists(path) and os.path.getsize(path) > 0)
            self.csv_file   = open(path, "a", newline="", encoding="utf-8")
            self.csv_writer = csv.writer(self.csv_file)
            if new_file:
                self.csv_writer.writerow(CSV_HEADER)
                self.csv_file.flush()
            # Fresh session: re-arm the one-shot notifications and report logging.
            self._csv_error_notified = False
            self._first_record_seen  = False
            self._notify(
                f"{'Created log' if new_file else 'Appending to log'} "
                f"{os.path.basename(path)}", level="info")
        except IOError as e:
            self._notify(f"Cannot open CSV: {e}", level="error")
            return

        mode = self.mode_var.get()
        if mode == "serial":
            self._connect_serial()
        else:
            self._connect_tcp()

    def _connect_serial(self):
        if not HAS_SERIAL:
            self._notify("Serial mode needs pyserial — run: pip install pyserial",
                         level="error")
            self._close_csv()
            return

        port = self.port_var.get()
        if not port or port == "(no ports)":
            self._notify("No serial port selected — plug in the Pico and press ⟳.",
                         level="error")
            self._close_csv()
            return
        baud = int(self.baud_var.get())
        try:
            self.ser = serial.Serial(port, baud, timeout=2)
        except serial.SerialException as e:
            self._notify(f"Cannot open {port}: {e}", level="error")
            self._close_csv()
            return

        self.running = True
        self.connect_time = datetime.now()
        self.connect_btn.config(text="  ■  DISCONNECT", bg=RED,
                                activebackground=RED_DIM)
        self._add_hover(self.connect_btn, RED, RED_DIM)
        self._notify(f"Serial connected — {port} @ {baud} baud. Waiting for data…",
                     level="ok")
        self._start_pulse()

        self.conn_thread = threading.Thread(target=self._serial_read_loop,
                                            daemon=True)
        self.conn_thread.start()

    def _connect_tcp(self):
        try:
            tcp_port = int(self.tcp_port_var.get())
            if not (0 < tcp_port < 65536):
                raise ValueError
        except ValueError:
            self._notify(f"Invalid TCP port: '{self.tcp_port_var.get()}' — "
                         f"use 1–65535.", level="error")
            self._close_csv()
            return
        try:
            self.tcp_server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.tcp_server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.tcp_server.bind(("0.0.0.0", tcp_port))
            self.tcp_server.listen(1)
            self.tcp_server.settimeout(1.0)
        except OSError as e:
            self._notify(f"Cannot bind TCP port {tcp_port}: {e}", level="error")
            self._close_csv()
            return

        self.running = True
        self.connect_time = datetime.now()
        self.connect_btn.config(text="  ■  STOP LISTENER", bg=RED,
                                activebackground=RED_DIM)
        self._add_hover(self.connect_btn, RED, RED_DIM)
        self._notify(f"TCP listening on 0.0.0.0:{tcp_port} — waiting for Pico…",
                     level="info")
        self._start_pulse()

        self.conn_thread = threading.Thread(target=self._tcp_accept_loop,
                                            daemon=True)
        self.conn_thread.start()

    def _send_server_ip(self):
        """
        Push the server IP to the Pico over USB as 'SETIP a.b.c.d'. Works whether
        or not we're already serial-connected; if not, opens a transient USB
        connection just to send the command. (The Pico ignores it harmlessly if
        it's a sensor record line — it's a distinct command verb.)
        """
        ip = self.server_ip_var.get().strip()
        if not is_valid_ipv4(ip):
            self._notify(f"Invalid IP: '{ip}' — use a.b.c.d", level="error")
            return

        # SETIP travels to the Pico over its USB serial link, so it needs
        # pyserial regardless of whether we're in TCP or Serial mode.
        if not HAS_SERIAL:
            self._notify("Setting the server IP needs pyserial (USB) — "
                         "run: pip install pyserial", level="error")
            return

        cmd = f"SETIP {ip}\n".encode()

        # 1) Reuse the live serial connection if we have one.
        if self.ser and self.ser.is_open:
            try:
                self.ser.write(cmd)
                self._notify(f"Sent server IP {ip} to Pico (saving to flash)…",
                             level="ok")
            except (serial.SerialException, OSError) as e:
                self._notify(f"Send failed: {e}", level="error")
            return

        # 2) Otherwise open a brief USB connection just to deliver the command.
        port = find_pico_port()
        if not port:
            cur = self.port_var.get()
            port = cur if cur and cur not in ("(no ports)",
                                              "(pyserial not installed)") else None
        if not port:
            self._notify("No Pico on USB — plug in via USB to set the IP.",
                         level="error")
            return
        try:
            baud = int(self.baud_var.get())
            tmp = serial.Serial(port, baud, timeout=2)
            tmp.write(cmd)
            tmp.flush()
            tmp.close()
            self._notify(
                f"Sent server IP {ip} to Pico via {port} (saved to flash).",
                level="ok")
        except (serial.SerialException, ValueError) as e:
            self._notify(f"Could not send via {port}: {e}", level="error")

    def _disconnect(self):
        self.running = False
        self._stop_pulse()

        if self.ser and self.ser.is_open:
            self.ser.close()
            self.ser = None

        if self.tcp_conn:
            try:
                self.tcp_conn.close()
            except Exception:
                pass
            self.tcp_conn = None

        if self.tcp_server:
            try:
                self.tcp_server.close()
            except Exception:
                pass
            self.tcp_server = None

        log_name = (os.path.basename(self.csv_file.name)
                    if self.csv_file else None)
        self._close_csv()

        self.connect_btn.config(text="  ▶  CONNECT", bg=ACCENT,
                                activebackground=ACCENT_DIM)
        self._add_hover(self.connect_btn, ACCENT, "#00c864")
        if log_name:
            self._notify(
                f"Disconnected — {self.record_count} record(s) logged to "
                f"{log_name}.", level="info")
        else:
            self._notify(
                f"Disconnected — {self.record_count} record(s) this session.",
                level="info")
        self.status_dot.config(fg=RED_DIM)
        self.status_label.config(text="Disconnected", fg=TEXT_DIM)
        self.conn_timer_label.config(text="")

    def _close_csv(self):
        if self.csv_file:
            try:
                self.csv_file.close()
            except Exception:
                pass
            self.csv_file   = None
            self.csv_writer = None



    def _serial_read_loop(self):
        while self.running:
            try:
                raw = self.ser.readline()
            except serial.SerialException:
                self.root.after(0, lambda: self._notify(
                    "Serial disconnected — scanning for the Pico…", level="warn"))

                try:
                    self.ser.close()
                except Exception:
                    pass
                import time
                time.sleep(2)
                if not self.running:
                    break
                # re-scan: Pico might have moved to a different USB port
                baud = int(self.baud_var.get())
                new_port = find_pico_port() or self.port_var.get()
                try:
                    self.ser = serial.Serial(new_port, baud, timeout=2)
                    # update the dropdown to show the new port
                    self.root.after(0, lambda p=new_port: (
                        self.port_var.set(p),
                        self._notify(f"Serial reconnected on {p}", level="ok")
                    ))
                except serial.SerialException:
                    pass
                continue

            if not raw:
                continue

            line = raw.decode("utf-8", errors="ignore").strip()
            if not line.startswith("{"):
                continue

            try:
                rec = json.loads(line)
                self.root.after(0, lambda r=rec: self._handle_record(r))
            except json.JSONDecodeError:
                pass



    def _tcp_accept_loop(self):
        while self.running:
            try:
                conn, addr = self.tcp_server.accept()
                # Pico sends ~1/s; treat multi-second silence as a dead client
                # so we drop back to accept() and pick up its reconnect.
                conn.settimeout(10)
                self.tcp_conn = conn
                self.root.after(0, lambda a=addr: self._notify(
                    f"Pico connected over TCP from {a[0]}:{a[1]}", level="ok"))
                self._tcp_read_loop(conn)
                conn.close()
                self.tcp_conn = None
                if self.running:
                    self.root.after(0, lambda: self._notify(
                        "Pico disconnected (TCP) — waiting for reconnect…",
                        level="warn"))
            except socket.timeout:
                continue
            except OSError:
                break

    def _tcp_read_loop(self, conn):
        buffer = ""
        while self.running:
            try:
                raw_data = conn.recv(4096)
            except socket.timeout:
                # >10s silence: assume the Pico vanished (reset / cable pull
                # with no clean FIN). Break so _tcp_accept_loop can re-accept.
                self.root.after(0, lambda: self._notify(
                    "No TCP data for 10 s — waiting for Pico to reconnect…",
                    level="warn"))
                break
            except (ConnectionResetError, OSError):
                break

            if not raw_data:
                break

            text = raw_data.decode("utf-8", errors="ignore")
            buffer += text

            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    self.root.after(0, lambda r=rec: self._handle_record(r))
                except json.JSONDecodeError:
                    pass



    def _handle_record(self, rec):
        # Lines without "seq" are status messages, not sensor records:
        #   - the device identity banner (boot / TCP connect)
        #   - SETIP / GETIP acknowledgements
        # Recognize them, update the status bar, and do NOT count as a record.
        if "seq" not in rec:
            dev = rec.get("device")
            msg = rec.get("msg")

            # The locked GPS baud arrives in the boot/connect banner and in a
            # dedicated "gps_ready" status line — capture it from either.
            baud = rec.get("gps_baud")
            if baud:
                self.gps_baud = baud
                self._update_subtitle()
                self._reflect_gps_baud(baud)

            if dev:
                self.device_id = dev
                mode = "Ethernet" if self.mode_var.get() == "tcp" else "USB"
                extra = f"  •  GPS @ {baud} baud" if baud else ""
                self._notify(
                    f"{dev} v{rec.get('version', '?')} recognized via {mode}{extra}",
                    level="ok")
            elif msg == "gps_ready":
                self._notify(
                    f"GPS ready — auto-detected {baud} baud" if baud
                    else "GPS ready", level="ok")
            elif msg == "ip_set":
                saved = rec.get("saved", False)
                where = "saved to flash" if saved else "applied (flash save FAILED)"
                self._notify(
                    f"Pico server IP set to {rec.get('server_ip', '?')} — {where}",
                    level="ok" if saved else "warn")
            elif msg == "ip":
                src = rec.get("source", "?")
                self._notify(
                    f"Pico server IP: {rec.get('server_ip', '(default)')} [{src}]",
                    level="info")
            elif msg == "error":
                self._notify(f"Pico error: {rec.get('detail', 'error')}",
                             level="error")
            return

        self.record_count += 1
        self.rec_count_var.set(str(self.record_count))

        if not self._first_record_seen:
            self._first_record_seen = True
            link = "Ethernet" if self.mode_var.get() == "tcp" else "USB"
            self._notify(f"First record received over {link} — data link live.",
                         level="ok")

        temp       = rec.get("temp_c")
        gps_locked = rec.get("gps_locked", False)
        gps_time   = rec.get("gps_time")
        gps_stale  = rec.get("gps_stale", False)
        lat        = rec.get("lat")
        lon        = rec.get("lon")
        alt        = rec.get("alt")
        uptime_us  = rec.get("ts_uptime_us", 0)
        uptime_sec = uptime_us / 1_000_000
        seq        = rec.get("seq", 0)
        sats       = rec.get("sats", 0)
        hdop       = rec.get("hdop")
        fix_q      = rec.get("fix_q", 0)


        if self.last_seq > 0 and seq > self.last_seq + 1:
            gap = seq - self.last_seq - 1
            self.seq_gaps += gap
            self.gap_label.config(
                text=f"⚠ {self.seq_gaps} record(s) missed")
            self._notify(
                f"Sequence gap: {gap} record(s) missing "
                f"(seq {self.last_seq+1}–{seq-1})", level="warn")
        self.last_seq = seq


        pc_now_utc = datetime.now(timezone.utc)
        if gps_locked and gps_time:
            gps_utc = parse_gps_utc(gps_time)
            if gps_utc:
                self.time_sync.update(gps_utc, pc_now_utc)


        # ── GPS lock-state transitions ───────────────────────────────────────
        # Timestamp the exact record where the fix drops or returns and surface
        # it as a notification + log event — answering "when did we lose GPS?".
        if gps_locked and gps_time:
            _gu = parse_gps_utc(gps_time)
            event_ist = ((_gu + IST_OFFSET).replace(tzinfo=None)
                         if _gu else self._now_ist())
        else:
            event_ist = self._now_ist()

        if self.gps_was_locked is None:
            # First record of the session — announce the starting fix state.
            self.gps_was_locked = gps_locked
            if gps_locked:
                self.gps_acquired_at = event_ist
                self.gps_event_label.config(text=f"Locked {event_ist:%H:%M:%S}",
                                            fg=ACCENT)
                self._notify(f"GPS fix present at {event_ist:%H:%M:%S} IST",
                             level="ok")
            else:
                self._notify("No GPS fix yet — acquiring satellites…",
                             level="info")
        elif gps_locked != self.gps_was_locked:
            if gps_locked:
                self.gps_acquired_at = event_ist
                self.gps_event_label.config(text=f"Locked {event_ist:%H:%M:%S}",
                                            fg=ACCENT)
                self._notify(f"GPS lock ACQUIRED at {event_ist:%H:%M:%S} IST",
                             level="ok")
            else:
                self.gps_lost_at = event_ist
                self.gps_lost_count += 1
                self.gps_event_label.config(text=f"Lost {event_ist:%H:%M:%S}",
                                            fg=ACCENT2)
                self._notify(
                    f"GPS lock LOST at {event_ist:%H:%M:%S} IST "
                    f"(loss #{self.gps_lost_count})", level="warn")
            self.gps_was_locked = gps_locked

        # ── GPS stale-fix transitions (a fix held but not freshly updated) ────
        # Only meaningful while locked; reset when unlocked so a re-lock that is
        # immediately stale notifies again.
        if gps_locked:
            if self.gps_was_stale is None:
                self.gps_was_stale = gps_stale
                if gps_stale:
                    self._notify("GPS fix is STALE — position may be outdated",
                                 level="warn")
            elif gps_stale != self.gps_was_stale:
                if gps_stale:
                    self._notify("GPS fix went STALE — no fresh update",
                                 level="warn")
                else:
                    self._notify("GPS fix is fresh again", level="ok")
                self.gps_was_stale = gps_stale
        else:
            self.gps_was_stale = None


        if isinstance(temp, (int, float)):
            trend = ""
            trend_color = TEXT_DIM
            if self.last_temp is not None:
                diff = temp - self.last_temp
                if diff > 0.05:
                    trend = "▲"
                    trend_color = RED
                elif diff < -0.05:
                    trend = "▼"
                    trend_color = ACCENT3
                else:
                    trend = "–"

            self.last_temp = temp
            self.temp_card.config(text=f"{temp:.2f} °C")
            self.temp_card._trend.config(text=trend, fg=trend_color)


            if self.temp_min is None or temp < self.temp_min:
                self.temp_min = temp
            if self.temp_max is None or temp > self.temp_max:
                self.temp_max = temp
            self.temp_sum += temp
            self.temp_count += 1
            temp_avg = self.temp_sum / self.temp_count

            self.stats_label.config(
                text=f"Min:{self.temp_min:.1f}  Max:{self.temp_max:.1f}  "
                     f"Avg:{temp_avg:.1f}")


        if gps_locked:
            stale_txt = " (stale)" if gps_stale else ""
            self.lock_card.config(text=f"LOCKED ✓{stale_txt}", fg=ACCENT)
            self.lock_card._trend.config(text="")
            if gps_time:
                gps_utc = parse_gps_utc(gps_time)
                if gps_utc:
                    ist = gps_utc + IST_OFFSET
                    self.time_card.config(text=ist.strftime("%H:%M:%S"))
                    self.date_label.config(text=ist.strftime("%d %b %Y"))
        else:
            self.lock_card.config(text="NO LOCK ⚠", fg=ACCENT2)
            # Keep the moment the fix was lost visible right on the card.
            if self.gps_lost_at:
                self.lock_card._trend.config(
                    text=f"since {self.gps_lost_at:%H:%M:%S}", fg=ACCENT2)
            # Drift-corrected IST clock keeps the display on true time on any PC,
            # regardless of the PC's own timezone, even with no GPS fix.
            now_ist = self._now_ist()
            self.time_card.config(text=now_ist.strftime("%H:%M:%S"))
            self.date_label.config(text=now_ist.strftime("%d %b %Y") + " (PC)")


        if isinstance(alt, (int, float)):
            self.alt_card.config(text=f"{alt:.1f} m")
        else:
            self.alt_card.config(text="– m")


        self.sat_card.config(text=str(sats) if sats else "0")
        if isinstance(hdop, (int, float)):
            # HDOP: <1=excellent, 1-2=good, 2-5=moderate, >5=poor
            if hdop < 1.0:
                hcolor = ACCENT
            elif hdop < 2.0:
                hcolor = GREEN
            elif hdop < 5.0:
                hcolor = ACCENT2
            else:
                hcolor = RED
            self.hdop_card.config(text=f"{hdop:.1f}", fg=hcolor)
        else:
            self.hdop_card.config(text="–", fg=TEXT_DIM)


        fix_names = {0: "No fix", 1: "GPS", 2: "DGPS", 6: "Estimated"}
        self.fix_label.config(text=fix_names.get(fix_q, f"Q={fix_q}"))


        if self.time_sync.has_data:
            drift = self.time_sync.get_drift_seconds()
            drift_color = ACCENT if abs(drift) < 1.0 else (
                ACCENT2 if abs(drift) < 5.0 else RED)
            self.drift_card.config(text=f"{drift:+.2f} s", fg=drift_color)


        if uptime_sec < 60:
            self.uptime_card.config(text=f"{uptime_sec:.0f} s")
        elif uptime_sec < 3600:
            self.uptime_card.config(
                text=f"{int(uptime_sec // 60)}m {int(uptime_sec % 60)}s")
        else:
            hrs = int(uptime_sec // 3600)
            mins = int((uptime_sec % 3600) // 60)
            self.uptime_card.config(text=f"{hrs}h {mins}m")


        if isinstance(lat, (int, float)):
            self.lat_card.config(text=f"{lat:.5f}°")
        else:
            self.lat_card.config(text="–")

        if isinstance(lon, (int, float)):
            self.lon_card.config(text=f"{lon:.5f}°")
        else:
            self.lon_card.config(text="–")


        if isinstance(temp, (int, float)):
            if gps_locked and gps_time:
                gps_utc = parse_gps_utc(gps_time)
                # GPS-locked points sit on true UTC→IST (matplotlib needs naive).
                t_plot = ((gps_utc + IST_OFFSET).replace(tzinfo=None)
                          if gps_utc else self._now_ist())
                self.times_gps.append(t_plot)
                self.temps_gps.append(temp)
            else:
                # No fix: place the point on the SAME true-IST axis using the
                # drift-corrected PC clock (PC time + measured GPS↔PC offset).
                # Without this the no-GPS segment would sit on raw PC-local time,
                # mis-aligning it from the GPS segment by the clock drift (and by
                # the whole UTC offset on a non-IST machine).
                self.times_pc.append(self._now_ist())
                self.temps_pc.append(temp)


        if self.csv_writer:
            try:
                gps_time_utc_str = ""
                gps_time_ist_str = ""
                if gps_locked and gps_time:
                    gps_utc = parse_gps_utc(gps_time)
                    if gps_utc:
                        gps_time_utc_str = gps_utc.strftime(
                            "%Y-%m-%dT%H:%M:%SZ")
                        ist = gps_utc + IST_OFFSET
                        gps_time_ist_str = ist.strftime(
                            "%Y-%m-%dT%H:%M:%S+05:30")
                elif not gps_locked:
                    if self.time_sync.has_data:
                        corrected = self.time_sync.get_corrected_time()
                        gps_time_utc_str = corrected.strftime(
                            "%Y-%m-%dT%H:%M:%SZ") + " (corrected)"
                    else:
                        gps_time_ist_str = datetime.now().strftime(
                            "%Y-%m-%dT%H:%M:%S") + " (PC)"

                # Use explicit type checks, NOT `x or ""`: a real 0.0 (sea-level
                # altitude, 0 °C, lat/lon on the equator/prime meridian) is falsy
                # and would otherwise be silently written as an empty cell.
                def _num(v):
                    return v if isinstance(v, (int, float)) else ""

                self.csv_writer.writerow([
                    pc_now_utc.isoformat(),
                    seq, uptime_us,
                    gps_locked, gps_stale,
                    gps_time_utc_str, gps_time_ist_str,
                    _num(lat), _num(lon), _num(alt),
                    sats,
                    _num(hdop),
                    fix_q,
                    _num(temp),
                    f"{self.time_sync.get_drift_seconds():.3f}"
                    if self.time_sync.has_data else ""
                ])
                self.csv_file.flush()
            except IOError as e:
                self._set_status(f"CSV write error: {e}", error=True)
                if not self._csv_error_notified:
                    self._csv_error_notified = True
                    self._notify(f"CSV write error: {e}", level="error")


        self._append_log(rec, gps_locked, gps_time, gps_stale,
                         temp, lat, lon, alt, uptime_sec, seq, sats)


        if isinstance(temp, (int, float)):
            self._set_status(
                f"Receiving… {self.record_count} records | "
                f"Last: {temp:.2f}°C | Seq: {seq}")
        else:
            self._set_status(
                f"Receiving… {self.record_count} records | Seq: {seq}")

    def _append_log(self, rec, gps_locked, gps_time, gps_stale,
                    temp, lat, lon, alt, uptime_sec, seq, sats):
        self.log_text.config(state="normal")

        tag = "gps" if gps_locked else "nogps"

        line_num = int(self.log_text.index("end-1c").split(".")[0])
        extra_tag = "even" if line_num % 2 == 0 else ""

        if gps_locked and isinstance(temp, (int, float)):
            gps_utc = parse_gps_utc(gps_time)
            if gps_utc:
                ist = gps_utc + IST_OFFSET
                t_display = ist.strftime("%H:%M:%S")
            else:
                t_display = str(gps_time)[:19] if gps_time else "–"

            stale = "~" if gps_stale else " "
            if isinstance(lat, (int, float)):
                line = (f"[{seq:05d}]{stale}{t_display}  "
                        f"{temp:.2f}°C  "
                        f"Lat:{lat:.5f}  Lon:{lon:.5f}  "
                        f"Alt:{alt:.1f}m  Sats:{sats}\n")
            else:
                line = (f"[{seq:05d}]{stale}{t_display}  "
                        f"{temp:.2f}°C  Sats:{sats}\n")
            self.log_text.insert("end", line, (tag, extra_tag))
        elif isinstance(temp, (int, float)):
            pc_time = self._now_ist().strftime("%H:%M:%S")
            line = (f"[{seq:05d}] PC:{pc_time}  "
                    f"{temp:.2f}°C  (no GPS)  "
                    f"uptime:{uptime_sec:.0f}s  Sats:{sats}\n")
            self.log_text.insert("end", line, (tag, extra_tag))


        lines = int(self.log_text.index("end-1c").split(".")[0])
        if lines > 200:
            self.log_text.delete("1.0", f"{lines - 200}.0")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def _append_log_alert(self, msg):
        self.log_text.config(state="normal")
        self.log_text.insert("end", msg + "\n", "alert")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def _now_ist(self):
        """Best available 'now' as a naive IST datetime.

        Uses the GPS↔PC drift correction when we have it (PC clock nudged onto
        true GPS time) and is built from UTC + the fixed IST offset, so it reads
        correct IST on ANY PC no matter the machine's own timezone."""
        base = (self.time_sync.get_corrected_time()
                if self.time_sync.has_data
                else datetime.now(timezone.utc))
        return (base + IST_OFFSET).replace(tzinfo=None)

    # Notification colour per severity level.
    _NOTIFY_COLORS = {"info": TEXT_MID, "ok": ACCENT,
                      "warn": ACCENT2, "error": RED}

    def _notify(self, msg, level="info"):
        """Surface a discrete, timestamped event in the status bar AND the
        records log, colour-coded by severity. For streaming throughput updates
        keep using _set_status; reserve this for events worth noticing."""
        color = self._NOTIFY_COLORS.get(level, TEXT_MID)
        ts = self._now_ist().strftime("%H:%M:%S")
        self.bottom_label.config(text=f"[{ts}] {msg}", fg=color)
        self._append_log_event(msg, level)

    def _append_log_event(self, msg, level="info"):
        # Notifications live in their own panel beside the records log.
        tag = {"ok": "gps", "warn": "nogps", "error": "alert"}.get(level, "dim")
        ts = self._now_ist().strftime("%H:%M:%S")
        self.notif_text.config(state="normal")
        self.notif_text.insert("end", f"{ts}  {msg}\n", tag)
        # Trim like _append_log so the panel can't grow without bound.
        lines = int(self.notif_text.index("end-1c").split(".")[0])
        if lines > 200:
            self.notif_text.delete("1.0", f"{lines - 200}.0")
        self.notif_text.see("end")
        self.notif_text.config(state="disabled")



    def _animate(self):
        if self.times_gps or self.times_pc:
            try:
                # ── Temperature trend ───────────────────────────────────────
                self.line_gps.set_data(list(self.times_gps),
                                       list(self.temps_gps))
                self.line_pc.set_data(list(self.times_pc),
                                      list(self.temps_pc))

                all_times = list(self.times_gps) + list(self.times_pc)
                all_temps = list(self.temps_gps) + list(self.temps_pc)

                if self._temp_fill is not None:
                    self._temp_fill.remove()
                    self._temp_fill = None

                if all_times:
                    self.ax.set_xlim(min(all_times), max(all_times))
                if all_temps:
                    margin = max(0.5, (max(all_temps) - min(all_temps)) * 0.15)
                    lo = min(all_temps) - margin
                    self.ax.set_ylim(lo, max(all_temps) + margin)
                    if self.times_gps:           # soft area under the live trace
                        self._temp_fill = self.ax.fill_between(
                            list(self.times_gps), list(self.temps_gps), lo,
                            color=ACCENT, alpha=0.10, linewidth=0, zorder=1)

                self.canvas.draw_idle()
            except Exception as e:
                # Don't hide chart failures — show once per distinct error.
                if getattr(self, "_last_plot_err", None) != str(e):
                    self._last_plot_err = str(e)
                    self._notify(f"Chart render error: {e}", level="error")

        self.root.after(1000, self._animate)



    def _start_pulse(self):
        self._pulse_on = True
        self._pulse_tick()

    def _stop_pulse(self):
        self._pulse_on = False
        if self._pulse_id:
            self.root.after_cancel(self._pulse_id)
            self._pulse_id = None

    def _pulse_tick(self):
        if not self._pulse_on:
            return

        if self.running:
            current = self.status_dot.cget("fg")
            if current == ACCENT:
                self.status_dot.config(fg=ACCENT_DIM)
            else:
                self.status_dot.config(fg=ACCENT)
            self.status_label.config(text="Connected", fg=ACCENT)


            if self.connect_time:
                elapsed = datetime.now() - self.connect_time
                hrs = int(elapsed.total_seconds() // 3600)
                mins = int((elapsed.total_seconds() % 3600) // 60)
                secs = int(elapsed.total_seconds() % 60)
                self.conn_timer_label.config(
                    text=f"{hrs:02d}:{mins:02d}:{secs:02d}")


            if self.connect_time and self.record_count > 0:
                elapsed_s = (datetime.now() - self.connect_time).total_seconds()
                if elapsed_s > 0:
                    rate = self.record_count / elapsed_s
                    self.rate_label.config(text=f"{rate:.1f} rec/s")

        self._pulse_id = self.root.after(800, self._pulse_tick)



    def _refresh_ports(self):
        """Refresh the COM port dropdown list, auto-select the Pico if found."""
        if not HAS_SERIAL:
            self.port_combo["values"] = ["(pyserial not installed)"]
            self.port_var.set("(pyserial not installed)")
            return
        all_ports = serial.tools.list_ports.comports()
        pico_port = find_pico_port()

        # build display list: mark the Pico port with a star
        port_list = []
        for p in sorted(all_ports, key=lambda x: x.device):
            port_list.append(p.device)

        if not port_list:
            port_list = ["(no ports)"]

        self.port_combo["values"] = port_list

        # auto-select: prefer the Pico, else first port, else keep current
        if pico_port and pico_port in port_list:
            self.port_var.set(pico_port)
        elif port_list and self.port_var.get() not in port_list:
            self.port_var.set(port_list[0])

        # show detection result in status bar
        if pico_port:
            self._set_status(f"Pico detected on {pico_port}")
        elif all_ports:
            self._set_status(f"{len(all_ports)} COM port(s) found, no Pico detected")

    def _auto_rescan(self):
        """Keep watching for a Pico to appear — plugged in, or still booting
        (the auto-baud sweep adds a few seconds) when the GUI opened — and
        auto-select it, so detection is hands-off no matter the timing. Pauses
        while a session is live so it never disturbs the active port."""
        if not HAS_SERIAL:
            self.root.after(2000, self._auto_rescan)
            return
        if not self.running:
            try:
                live = [p.device for p in sorted(
                    serial.tools.list_ports.comports(), key=lambda x: x.device)]
                pico = find_pico_port()

                if live and list(self.port_combo["values"]) != live:
                    self.port_combo["values"] = live

                cur = self.port_var.get()
                # Don't fight a deliberate manual selection: only auto-pick the
                # Pico when nothing valid is chosen, or we chose the last port.
                if pico and cur != pico and (
                        cur in ("", "(no ports)") or cur not in live
                        or cur == self._last_pico):
                    self.port_var.set(pico)
                    self._last_pico = pico
                    if self._pico_notified_port != pico:
                        self._pico_notified_port = pico
                        self._notify(f"Pico detected on {pico} — ready to "
                                     f"connect.", level="ok")
                    else:
                        self._set_status(f"Pico detected on {pico} — press Connect.")
                elif pico and cur == pico:
                    self._last_pico = pico
                if not pico:
                    # Pico unplugged: re-arm so a re-plug notifies again.
                    self._pico_notified_port = None
            except Exception:
                pass
        self.root.after(2000, self._auto_rescan)

    def _browse_csv(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
            initialfile="temperature_log.csv")
        if path:
            self.csv_var.set(path)
            self._notify(f"Log file set to {os.path.basename(path)}",
                         level="info")

    def _clear_chart(self):
        self.times_gps.clear()
        self.temps_gps.clear()
        self.times_pc.clear()
        self.temps_pc.clear()
        self.line_gps.set_data([], [])
        self.line_pc.set_data([], [])
        if self._temp_fill is not None:
            self._temp_fill.remove()
            self._temp_fill = None
        self.canvas.draw_idle()
        self._notify("Chart cleared.", level="info")

    def _set_status(self, msg, error=False):
        self.bottom_label.config(text=msg, fg=RED if error else TEXT_DIM)
        if not error and self.running:
            self.status_dot.config(fg=ACCENT)
            self.status_label.config(text="Connected", fg=ACCENT)

    def _update_subtitle(self):
        """Reflect the auto-detected GPS baud in the header subtitle."""
        if self.gps_baud:
            text = f"Pico 2 RP2350  •  GPS @ {self.gps_baud} baud  •  W5500"
        else:
            text = "Pico 2 RP2350  •  GNSS + Temp  •  W5500"
        self.subtitle_label.config(text=text)

    def _reflect_gps_baud(self, baud):
        """Auto-select the Pico's detected GPS baud in the baud dropdown.

        The firmware auto-detects the GPS module's rate and reports it (in the
        boot/connect banner and the 'gps_ready' line); this surfaces that rate
        in the dropdown so the user never has to guess it. The USB-CDC link is
        virtual, so updating this value never disturbs the live connection — it
        only takes effect if the port is (re)opened, and any rate connects."""
        val = str(baud)
        values = list(self.baud_combo["values"])
        if val not in values:
            self.baud_combo["values"] = sorted(values + [val], key=int)
        if self.baud_var.get() != val:
            self.baud_var.set(val)

    def _on_close(self):
        self._disconnect()
        self.root.destroy()



def apply_style():
    style = ttk.Style()
    style.theme_use("clam")
    style.configure("TCombobox",
                    fieldbackground=BG3,
                    background=BG3,
                    foreground=TEXT,
                    arrowcolor=TEXT_DIM,
                    bordercolor=BORDER,
                    lightcolor=BORDER,
                    darkcolor=BORDER,
                    selectbackground=BG4,
                    selectforeground=TEXT)
    style.map("TCombobox",
              fieldbackground=[("readonly", BG3)],
              foreground=[("readonly", TEXT)])

    style.configure("TRadiobutton",
                    background=BG2,
                    foreground=TEXT_MID,
                    indicatorcolor=BG3)


if __name__ == "__main__":
    root = tk.Tk()
    _select_fonts()          # pick portable fonts now that a Tk root exists
    apply_style()
    app = MonitorGUI(root)
    root.mainloop()

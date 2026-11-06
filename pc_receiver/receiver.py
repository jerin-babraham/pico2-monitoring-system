#!/usr/bin/env python3
"""
receiver.py — Standalone GPS Temperature Monitor Receiver
==========================================================
One file that handles everything:
  - Serial (USB) receiving with auto-detect
  - TCP (Ethernet) receiving
  - Live temperature chart
  - Offline CSV plotting
  - CSV logging with GPS time sync

Usage:
  python receiver.py serial              # auto-detect Pico on any USB port
  python receiver.py serial --port COM3  # force a specific port
  python receiver.py serial --plot       # with live chart
  python receiver.py tcp                 # listen on port 5005 for Pico
  python receiver.py tcp --plot          # with live chart
  python receiver.py plot                # plot from temperature_log.csv
  python receiver.py plot my_data.csv    # plot from specific file
  python receiver.py list-ports          # show all COM ports

Requirements:
  pip install pyserial matplotlib
"""

import socket
import json
import csv
import os
import sys
import time
import argparse
from datetime import datetime, timezone, timedelta

# pyserial — needed for serial mode
try:
    import serial
    import serial.tools.list_ports
    HAS_SERIAL = True
except ImportError:
    HAS_SERIAL = False

# matplotlib — needed for --plot and plot subcommand
try:
    import matplotlib
    matplotlib.use("TkAgg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    HAS_MATPLOTLIB = True
except ImportError:
    HAS_MATPLOTLIB = False


# ───────────────────────────────────────────────────────────────────
# Config
# ───────────────────────────────────────────────────────────────────

BAUD_RATE     = 115200
TCP_HOST      = "0.0.0.0"
TCP_PORT      = 5005
CSV_FILE      = "temperature_log.csv"
BUFFER_SIZE   = 4096
IST_OFFSET    = timedelta(hours=5, minutes=30)

# Raspberry Pi's USB Vendor ID — every Pico uses this
PICO_VID = 0x2E8A

# terminal colors (ANSI)
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
RESET  = "\033[0m"


def enable_windows_ansi():
    """Enable ANSI color codes and UTF-8 on Windows."""
    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
        except Exception:
            pass
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


# ───────────────────────────────────────────────────────────────────
# Pico auto-detection
# ───────────────────────────────────────────────────────────────────

def find_pico_port():
    """
    Scan all COM ports and find the Pico.
    Priority 1: match by Raspberry Pi USB Vendor ID (0x2E8A)
    Priority 2: keyword match in device description
    """
    if not HAS_SERIAL:
        return None

    ports = serial.tools.list_ports.comports()

    for p in ports:
        if p.vid == PICO_VID:
            return p.device

    for p in ports:
        desc = (p.description or "").lower()
        if any(k in desc for k in ["pico", "rp2", "rp23", "raspberry"]):
            return p.device

    return None


def list_all_ports():
    """Print every COM port currently visible."""
    if not HAS_SERIAL:
        print(f"{RED}  pyserial not installed. Run: pip install pyserial{RESET}")
        return
    ports = serial.tools.list_ports.comports()
    if not ports:
        print("  (no COM ports found)")
        return
    for p in sorted(ports, key=lambda x: x.device):
        vid_str = f"VID={p.vid:04X}" if p.vid else "VID=----"
        print(f"  {p.device:8s}  {vid_str}  {p.description}")


# ───────────────────────────────────────────────────────────────────
# Time sync (GPS ↔ PC clock drift tracking)
# ───────────────────────────────────────────────────────────────────

class TimeSyncTracker:
    """Rolling average of GPS vs PC time offset."""

    def __init__(self, window_size=30):
        self.offsets = []
        self.window_size = window_size
        self.avg_offset = timedelta(0)

    def update(self, gps_utc, pc_utc):
        offset = gps_utc - pc_utc
        self.offsets.append(offset.total_seconds())
        if len(self.offsets) > self.window_size:
            self.offsets = self.offsets[-self.window_size:]
        avg_secs = sum(self.offsets) / len(self.offsets)
        self.avg_offset = timedelta(seconds=avg_secs)

    def get_corrected_time(self):
        return datetime.now(timezone.utc) + self.avg_offset

    def get_drift_seconds(self):
        if not self.offsets:
            return 0.0
        return -sum(self.offsets) / len(self.offsets)

    @property
    def has_data(self):
        return len(self.offsets) > 0


def parse_gps_utc(gps_time_str):
    """Parse GPS time string to UTC datetime."""
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


# ───────────────────────────────────────────────────────────────────
# Live plotter (embedded, no separate file needed)
# ───────────────────────────────────────────────────────────────────

class LivePlotter:
    """Real-time temperature chart using matplotlib."""

    MAX_POINTS = 300

    def __init__(self):
        if not HAS_MATPLOTLIB:
            raise ImportError("matplotlib is required for live plotting")

        self.times_gps = []
        self.temps_gps = []
        self.times_pc  = []
        self.temps_pc  = []

        plt.style.use("dark_background")
        self.fig, self.ax = plt.subplots(figsize=(10, 4))
        self.fig.canvas.manager.set_window_title(
            "GPS Temperature Monitor — Live")

        self.line_gps, = self.ax.plot([], [], "o-",
            color="#00ff88", markersize=3, linewidth=1.2,
            label="GPS Time", alpha=0.9)
        self.line_pc,  = self.ax.plot([], [], "o-",
            color="#ff9944", markersize=3, linewidth=1.2,
            label="PC Time (no GPS)", alpha=0.7)

        self.ax.set_xlabel("Time", fontsize=10)
        self.ax.set_ylabel("Temperature (°C)", fontsize=10)
        self.ax.set_title("Live Temperature — Pico 2 RP2350",
                          fontsize=12, fontweight="bold")
        self.ax.legend(loc="upper left", fontsize=8)
        self.ax.grid(True, alpha=0.2)
        self.ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))
        self.fig.autofmt_xdate()
        self.fig.tight_layout()

        plt.ion()
        plt.show(block=False)
        self.fig.canvas.draw()
        self.fig.canvas.flush_events()
        self._plot_count = 0

    def add_point(self, timestamp, temp_c, gps_locked):
        if gps_locked:
            self.times_gps.append(timestamp)
            self.temps_gps.append(temp_c)
            if len(self.times_gps) > self.MAX_POINTS:
                self.times_gps = self.times_gps[-self.MAX_POINTS:]
                self.temps_gps = self.temps_gps[-self.MAX_POINTS:]
        else:
            self.times_pc.append(timestamp)
            self.temps_pc.append(temp_c)
            if len(self.times_pc) > self.MAX_POINTS:
                self.times_pc = self.times_pc[-self.MAX_POINTS:]
                self.temps_pc = self.temps_pc[-self.MAX_POINTS:]

        self.line_gps.set_data(self.times_gps, self.temps_gps)
        self.line_pc.set_data(self.times_pc, self.temps_pc)

        all_times = self.times_gps + self.times_pc
        all_temps = self.temps_gps + self.temps_pc
        if all_times and all_temps:
            self.ax.set_xlim(min(all_times), max(all_times))
            self.ax.set_ylim(min(all_temps) - 1, max(all_temps) + 1)
            self.ax.relim()

        try:
            self.fig.canvas.draw_idle()
            self.fig.canvas.flush_events()
            self._plot_count += 1
            if self._plot_count % 5 == 0:
                plt.pause(0.001)
        except Exception:
            pass

    def close(self):
        try:
            plt.close(self.fig)
        except Exception:
            pass


# ───────────────────────────────────────────────────────────────────
# Shared display / CSV helpers
# ───────────────────────────────────────────────────────────────────

def format_record(rec, record_num, time_sync, compact=False):
    """Format one record for terminal display."""
    temp       = rec.get("temp_c", "?")
    uptime_us  = rec.get("ts_uptime_us", 0)
    gps_locked = rec.get("gps_locked", False)
    gps_time   = rec.get("gps_time")
    lat        = rec.get("lat")
    lon        = rec.get("lon")
    alt        = rec.get("alt")
    seq        = rec.get("seq", "?")
    sats       = rec.get("sats", 0)
    hdop       = rec.get("hdop")
    gps_stale  = rec.get("gps_stale", False)
    uptime_sec = uptime_us / 1_000_000

    temp_str = f"{temp:.2f}°C" if isinstance(temp, (int, float)) else str(temp)
    prefix   = f"{DIM}[{record_num:05d} seq:{seq}]{RESET}"

    sat_str  = f"Sats:{sats}" if sats else ""
    hdop_str = f"HDOP:{hdop:.1f}" if isinstance(hdop, (int, float)) else ""
    quality_str = f" | {sat_str} {hdop_str}" if sat_str or hdop_str else ""

    drift_str = ""
    if time_sync.has_data:
        drift = time_sync.get_drift_seconds()
        drift_str = f" | Drift:{drift:+.2f}s"

    if gps_locked:
        alt_str = f"{alt:.1f}m" if isinstance(alt, (int, float)) else "--"
        stale_flag = f" {YELLOW}(stale){RESET}" if gps_stale else ""

        gps_utc = parse_gps_utc(gps_time)
        if gps_utc:
            ist_time = gps_utc + IST_OFFSET
            time_display = ist_time.strftime("%H:%M:%S IST" if compact
                                             else "%Y-%m-%dT%H:%M:%S IST")
        else:
            time_display = str(gps_time)

        return (
            f"{prefix} "
            f"{GREEN}{BOLD}[GPS LOCKED ✓]{RESET}{stale_flag} "
            f"🌡  {BOLD}{temp_str}{RESET}  |  "
            f"🕐 {time_display}  |  "
            f"📍 {lat:.5f}, {lon:.5f}  |  "
            f"⛰  {alt_str}"
            f"{quality_str}{drift_str}  |  "
            f"{DIM}⏱  {uptime_sec:.1f}s{RESET}"
        )
    else:
        pc_time = datetime.now().strftime("%H:%M:%S" if compact
                                          else "%Y-%m-%dT%H:%M:%S")
        return (
            f"{prefix} "
            f"{YELLOW}[NO GPS LOCK ⚠]{RESET} "
            f"🌡  {BOLD}{temp_str}{RESET}  |  "
            f"🕐 PC: {pc_time}  |  "
            f"📍 --"
            f"{quality_str}{drift_str}  |  "
            f"{DIM}⏱  {uptime_sec:.1f}s{RESET}"
        )


CSV_HEADER = [
    "pc_timestamp", "seq", "uptime_us", "gps_locked", "gps_stale",
    "gps_time_utc", "gps_time_ist",
    "latitude", "longitude", "altitude",
    "satellites", "hdop", "fix_quality", "temp_c", "pc_drift_s"
]


def write_record_to_csv(writer, csv_file, rec, time_sync):
    """Write one record to the CSV file."""
    gps_locked   = rec.get("gps_locked", False)
    gps_time_str = rec.get("gps_time")
    pc_now_utc   = datetime.now(timezone.utc)

    gps_time_utc_str = ""
    gps_time_ist_str = ""

    if gps_locked and gps_time_str:
        gps_utc = parse_gps_utc(gps_time_str)
        if gps_utc:
            gps_time_utc_str = gps_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
            ist = gps_utc + IST_OFFSET
            gps_time_ist_str = ist.strftime("%Y-%m-%dT%H:%M:%S+05:30")
    elif not gps_locked:
        if time_sync.has_data:
            corrected = time_sync.get_corrected_time()
            gps_time_utc_str = corrected.strftime(
                "%Y-%m-%dT%H:%M:%SZ") + " (corrected)"
            ist = corrected + IST_OFFSET
            gps_time_ist_str = ist.strftime(
                "%Y-%m-%dT%H:%M:%S") + " (corrected)"
        else:
            pc_local = datetime.now()
            gps_time_ist_str = pc_local.strftime(
                "%Y-%m-%dT%H:%M:%S") + " (PC)"

    writer.writerow([
        pc_now_utc.isoformat(),
        rec.get("seq", ""),
        rec.get("ts_uptime_us", ""),
        rec.get("gps_locked", ""),
        rec.get("gps_stale", ""),
        gps_time_utc_str,
        gps_time_ist_str,
        rec.get("lat") or "",
        rec.get("lon") or "",
        rec.get("alt") or "",
        rec.get("sats", ""),
        rec.get("hdop") or "",
        rec.get("fix_q", ""),
        rec.get("temp_c", ""),
        f"{time_sync.get_drift_seconds():.3f}" if time_sync.has_data else ""
    ])
    csv_file.flush()


def process_record(rec, record_count, last_seq, time_sync, writer, csv_file,
                    plotter, compact=False):
    """Handle one JSON record: display, sync time, log CSV, update chart."""

    # sequence gap detection
    seq = rec.get("seq", 0)
    if last_seq > 0 and seq > last_seq + 1:
        gap = seq - last_seq - 1
        print(f"{RED}  ⚠ SEQUENCE GAP: {gap} record(s) "
              f"missing (seq {last_seq+1}-{seq-1}){RESET}")

    # time sync
    gps_locked   = rec.get("gps_locked", False)
    gps_time_str = rec.get("gps_time")
    pc_now_utc   = datetime.now(timezone.utc)

    if gps_locked and gps_time_str:
        gps_utc = parse_gps_utc(gps_time_str)
        if gps_utc:
            time_sync.update(gps_utc, pc_now_utc)

    # display
    print(format_record(rec, record_count, time_sync, compact=compact))

    # CSV
    if writer and csv_file:
        write_record_to_csv(writer, csv_file, rec, time_sync)

    # live plot
    if plotter is not None:
        if gps_locked and gps_time_str:
            gps_utc = parse_gps_utc(gps_time_str)
            plot_time = (gps_utc + IST_OFFSET if gps_utc else datetime.now())
        else:
            plot_time = datetime.now()

        temp_val = rec.get("temp_c", 0)
        if isinstance(temp_val, (int, float)):
            plotter.add_point(plot_time, temp_val, gps_locked)

    return seq


def handle_status_line(rec):
    """Print a non-record status line from the Pico and return.

    Status lines (the boot/identity banner, "gps_ready", and the SETIP/GETIP
    acknowledgements) carry NO "seq" field — they are not sensor samples, so
    they must never be counted or written to the CSV. The firmware sends the
    identity banner on every TCP (re)connect, so without this guard each
    connect would add a blank data row to the log."""
    dev  = rec.get("device")
    msg  = rec.get("msg")
    baud = rec.get("gps_baud")

    if dev:
        extra = f"  |  GPS @ {baud} baud" if baud else ""
        print(f"{GREEN}  ✓ {dev} v{rec.get('version', '?')} ready{extra}{RESET}")
    elif msg == "gps_ready":
        print(f"{GREEN}  ✓ GPS ready"
              + (f" — auto-detected {baud} baud" if baud else "") + RESET)
    elif msg == "ip_set":
        where = "saved to flash" if rec.get("saved") else "flash save FAILED"
        print(f"{CYAN}  Pico server IP set to "
              f"{rec.get('server_ip', '?')} ({where}){RESET}")
    elif msg == "ip":
        print(f"{CYAN}  Pico server IP: {rec.get('server_ip', '(default)')} "
              f"[{rec.get('source', '?')}]{RESET}")
    elif msg == "error":
        print(f"{RED}  Pico error: {rec.get('detail', 'error')}{RESET}")


# ───────────────────────────────────────────────────────────────────
# Subcommand: serial
# ───────────────────────────────────────────────────────────────────

def cmd_serial(args):
    if not HAS_SERIAL:
        print(f"{RED}  pyserial not installed. Run: pip install pyserial{RESET}")
        return

    time_sync = TimeSyncTracker(window_size=30)

    # plotter
    plotter = None
    if args.plot:
        if not HAS_MATPLOTLIB:
            print(f"{RED}  --plot requires matplotlib. Run: pip install matplotlib{RESET}")
            return
        plotter = LivePlotter()

    # auto-detect or use given port
    port = args.port
    if port is None:
        print(f"{CYAN}  Scanning USB ports for Pico...{RESET}")
        port = find_pico_port()
        if port:
            print(f"{GREEN}  ✓ Pico found on {port} (auto-detected){RESET}")
        else:
            print(f"{RED}  ✗ Could not find a Pico on any USB port.{RESET}")
            print(f"\n{CYAN}  Ports currently visible:{RESET}")
            list_all_ports()
            print(f"\n  To override: python receiver.py serial --port COMx")
            return

    # banner
    print(f"\n{CYAN}{BOLD}")
    print("╔═════════════════════════════════════════════╗")
    print("║   GPS Temperature Monitor — Serial USB      ║")
    print("║   NEO-M8N + Pico 2 RP2350                   ║")
    print("╚═════════════════════════════════════════════╝")
    print(f"{RESET}")
    print(f"  Serial port  : {BOLD}{port} @ {args.baud}{RESET}")
    print(f"  Saving to    : {BOLD}{args.output}{RESET}")
    if plotter:
        print(f"  Live plot    : {BOLD}ENABLED{RESET}")

    # CSV
    csv_exists = os.path.exists(args.output) and os.path.getsize(args.output) > 0
    csv_file = open(args.output, "a", newline="", encoding="utf-8")
    writer = csv.writer(csv_file)
    if not csv_exists:
        writer.writerow(CSV_HEADER)
        csv_file.flush()

    # open serial
    try:
        ser = serial.Serial(port, args.baud, timeout=2)
        print(f"\n{GREEN}  ✓ Connected to {port}{RESET}")

        # check for device banner
        first_line = ser.readline().decode("utf-8", errors="ignore").strip()
        if "VSSC-GPS-Monitor" in first_line:
            print(f"{GREEN}  ✓ Device identified: VSSC GPS Monitor firmware{RESET}")
        elif first_line:
            print(f"{YELLOW}  ? First line: {first_line[:60]}{RESET}")

    except serial.SerialException as e:
        print(f"\n{RED}  ✗ Cannot open {port}: {e}{RESET}")
        print(f"  Try: python receiver.py list-ports")
        csv_file.close()
        return

    print(f"{DIM}  Waiting for data... Press Ctrl+C to stop.{RESET}\n")

    record_count = 0
    last_seq     = 0

    try:
        while True:
            try:
                raw = ser.readline()
            except serial.SerialException:
                print(f"{RED}  Serial disconnected. Reconnecting...{RESET}")
                ser.close()
                time.sleep(2)
                reconnect_port = find_pico_port() or port
                try:
                    ser = serial.Serial(reconnect_port, args.baud, timeout=2)
                    print(f"{GREEN}  ✓ Reconnected on {reconnect_port}{RESET}")
                    port = reconnect_port
                except serial.SerialException:
                    pass
                continue

            if not raw:
                continue

            line = raw.decode("utf-8", errors="ignore").strip()
            if not line or not line.startswith("{"):
                continue

            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue

            # Status lines (banner, gps_ready, SETIP/GETIP acks) have no "seq":
            # show them but don't count or log them as sensor data.
            if "seq" not in record:
                handle_status_line(record)
                continue

            record_count += 1
            last_seq = process_record(
                record, record_count, last_seq, time_sync,
                writer, csv_file, plotter, compact=True)

    except KeyboardInterrupt:
        print(f"\n\n{BOLD}Stopped.{RESET}")
        print(f"  Records: {BOLD}{record_count}{RESET}")
        print(f"  Saved to: {BOLD}{args.output}{RESET}")
        if time_sync.has_data:
            print(f"  PC clock drift: {BOLD}{time_sync.get_drift_seconds():+.3f}s{RESET}")
        print()
    finally:
        csv_file.close()
        ser.close()
        if plotter:
            plotter.close()


# ───────────────────────────────────────────────────────────────────
# Subcommand: tcp
# ───────────────────────────────────────────────────────────────────

def cmd_tcp(args):
    time_sync = TimeSyncTracker(window_size=30)

    # plotter
    plotter = None
    if args.plot:
        if not HAS_MATPLOTLIB:
            print(f"{RED}  --plot requires matplotlib. Run: pip install matplotlib{RESET}")
            return
        plotter = LivePlotter()

    # banner
    print(f"\n{CYAN}{BOLD}")
    print("╔═════════════════════════════════════════════╗")
    print("║   GPS Temperature Monitor — TCP Ethernet    ║")
    print("║   NEO-M8N + Pico 2 RP2350 + W5500           ║")
    print("╚═════════════════════════════════════════════╝")
    print(f"{RESET}")
    print(f"  Listening on : {BOLD}0.0.0.0:{args.tcp_port}{RESET}")
    print(f"  Saving to    : {BOLD}{args.output}{RESET}")
    if plotter:
        print(f"  Live plot    : {BOLD}ENABLED{RESET}")

    # CSV
    csv_exists = os.path.exists(args.output) and os.path.getsize(args.output) > 0
    csv_file = open(args.output, "a", newline="", encoding="utf-8")
    writer = csv.writer(csv_file)
    if not csv_exists:
        writer.writerow(CSV_HEADER)
        csv_file.flush()

    # TCP server
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    server_sock.bind((TCP_HOST, args.tcp_port))
    server_sock.listen(1)

    print(f"\n{CYAN}  Waiting for Pico to connect...{RESET}\n")

    total_records = 0

    try:
        while True:
            conn, addr = server_sock.accept()
            # Pico sends every ~1s. Treat a multi-second silence as a dead
            # client so we break back to accept() and pick up its reconnect.
            conn.settimeout(10)

            print(f"\n{GREEN}{BOLD}✓ Pico connected from {addr[0]}:{addr[1]}{RESET}")
            print(f"{DIM}  Receiving data... Press Ctrl+C to stop.{RESET}\n")

            record_count = 0
            last_seq     = 0
            buffer       = ""

            try:
                while True:
                    try:
                        raw_data = conn.recv(BUFFER_SIZE)
                    except socket.timeout:
                        print(f"\n{YELLOW}  No data for 10s — assuming Pico "
                              f"is gone. Waiting for reconnect.{RESET}")
                        break

                    if not raw_data:
                        print(f"\n{YELLOW}  Pico disconnected (clean close).{RESET}")
                        break

                    text = raw_data.decode("utf-8", errors="ignore")
                    buffer += text

                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        line = line.strip()
                        if not line:
                            continue

                        try:
                            record = json.loads(line)
                        except json.JSONDecodeError as e:
                            print(f"{RED}  JSON parse error: {e}{RESET}")
                            continue

                        # Status lines (the identity banner sent on every
                        # connect, etc.) have no "seq" — show, don't log.
                        if "seq" not in record:
                            handle_status_line(record)
                            continue

                        record_count += 1
                        total_records += 1
                        last_seq = process_record(
                            record, total_records, last_seq, time_sync,
                            writer, csv_file, plotter)

            except ConnectionResetError:
                print(f"\n{YELLOW}  Connection reset by Pico.{RESET}")
            except Exception as e:
                print(f"\n{RED}  Connection error: {e}{RESET}")

            conn.close()
            print(f"{DIM}  Session: {record_count} records | Total: {total_records}{RESET}")
            if time_sync.has_data:
                print(f"{DIM}  PC drift: {time_sync.get_drift_seconds():+.3f}s{RESET}")
            print(f"{CYAN}  Waiting for Pico to reconnect...{RESET}\n")

    except KeyboardInterrupt:
        print(f"\n\n{BOLD}Stopping receiver.{RESET}")
        print(f"  Total records : {BOLD}{total_records}{RESET}")
        print(f"  Saved to      : {BOLD}{args.output}{RESET}")
        if time_sync.has_data:
            print(f"  PC clock drift: {BOLD}{time_sync.get_drift_seconds():+.3f}s{RESET}")
        print()
    finally:
        csv_file.close()
        server_sock.close()
        if plotter:
            plotter.close()


# ───────────────────────────────────────────────────────────────────
# Subcommand: plot (offline CSV viewer)
# ───────────────────────────────────────────────────────────────────

def cmd_plot(args):
    if not HAS_MATPLOTLIB:
        print(f"{RED}  matplotlib not installed. Run: pip install matplotlib{RESET}")
        return

    filepath = args.csv_file

    if not os.path.exists(filepath):
        print(f"File not found: {filepath}")
        print(f"Usage: python receiver.py plot [csv_file]")
        return

    # load data
    times_gps, temps_gps = [], []
    times_pc,  temps_pc  = [], []
    alts_gps             = []

    with open(filepath, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                temp = float(row["temp_c"])
            except (ValueError, KeyError):
                continue

            alt = None
            if "altitude" in row and row["altitude"].strip():
                try:
                    alt = float(row["altitude"])
                except ValueError:
                    pass

            gps_locked = row.get("gps_locked", "").strip().lower() == "true"
            timestamp  = None

            if gps_locked:
                utc_str = row.get("gps_time_utc", "").strip()
                if utc_str and "(corrected)" not in utc_str:
                    try:
                        if utc_str.endswith("Z"):
                            t = datetime.strptime(utc_str, "%Y-%m-%dT%H:%M:%SZ")
                            timestamp = t + IST_OFFSET
                        else:
                            timestamp = datetime.fromisoformat(utc_str)
                    except ValueError:
                        pass

                if timestamp is None:
                    ist_str = row.get("gps_time_ist", "").strip()
                    if ist_str and "(corrected)" not in ist_str:
                        try:
                            clean = ist_str.replace("+05:30", "")
                            timestamp = datetime.strptime(clean, "%Y-%m-%dT%H:%M:%S")
                        except ValueError:
                            pass

                if timestamp:
                    times_gps.append(timestamp)
                    temps_gps.append(temp)
                    if alt is not None:
                        alts_gps.append((timestamp, alt))
            else:
                pc_ts = row.get("pc_timestamp", "").strip()
                if pc_ts:
                    try:
                        timestamp = datetime.fromisoformat(pc_ts)
                        if timestamp.tzinfo:
                            timestamp = timestamp.replace(tzinfo=None) + IST_OFFSET
                    except ValueError:
                        continue
                else:
                    continue
                times_pc.append(timestamp)
                temps_pc.append(temp)

    total = len(times_gps) + len(times_pc)
    if total == 0:
        print(f"No data found in {filepath}")
        return

    print(f"Loaded {total} records from {filepath}")
    print(f"  GPS-locked: {len(times_gps)} points")
    print(f"  PC-time:    {len(times_pc)} points")

    # plot
    plt.style.use("dark_background")
    fig, ax = plt.subplots(figsize=(12, 5))

    if times_gps:
        ax.plot(times_gps, temps_gps, "o-", color="#00ff88",
                markersize=3, linewidth=1.2,
                label=f"GPS Time ({len(times_gps)} pts)", alpha=0.9)

    if times_pc:
        ax.plot(times_pc, temps_pc, "o-", color="#ff9944",
                markersize=3, linewidth=1.2,
                label=f"PC Time ({len(times_pc)} pts)", alpha=0.7)

    ax.set_xlabel("Time (IST)", fontsize=11)
    ax.set_ylabel("Temperature (°C)", fontsize=11)
    ax.set_title(f"Temperature Log — {os.path.basename(filepath)}",
                 fontsize=13, fontweight="bold")
    ax.legend(loc="upper left", fontsize=9)
    ax.grid(True, alpha=0.2)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))
    fig.autofmt_xdate()

    all_temps = temps_gps + temps_pc
    stats_text = (
        f"Min: {min(all_temps):.1f}°C\n"
        f"Max: {max(all_temps):.1f}°C\n"
        f"Avg: {sum(all_temps)/len(all_temps):.1f}°C"
    )
    ax.text(0.98, 0.97, stats_text, transform=ax.transAxes,
            fontsize=9, verticalalignment="top", horizontalalignment="right",
            bbox=dict(boxstyle="round,pad=0.4", facecolor="#333333", alpha=0.8))

    if alts_gps:
        ax2 = ax.twinx()
        alt_times = [a[0] for a in alts_gps]
        alt_vals  = [a[1] for a in alts_gps]
        ax2.plot(alt_times, alt_vals, "s-", color="#88ccff",
                 markersize=2, linewidth=1.0,
                 label=f"Altitude ({len(alts_gps)} pts)", alpha=0.6)
        ax2.set_ylabel("Altitude (m)", fontsize=11, color="#88ccff")
        ax2.tick_params(axis="y", labelcolor="#88ccff")
        ax2.legend(loc="upper right", fontsize=8)

    fig.tight_layout()
    plt.show()


# ───────────────────────────────────────────────────────────────────
# Main entry point
# ───────────────────────────────────────────────────────────────────

def main():
    enable_windows_ansi()

    parser = argparse.ArgumentParser(
        description="GPS Temperature Monitor — Standalone Receiver",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  python receiver.py serial              Auto-detect Pico and start logging
  python receiver.py serial --port COM3  Use a specific COM port
  python receiver.py serial --plot       Serial + live temperature chart
  python receiver.py tcp                 Listen for Pico over Ethernet
  python receiver.py tcp --plot          TCP + live chart
  python receiver.py plot                Plot saved temperature_log.csv
  python receiver.py plot my_data.csv    Plot a specific CSV file
  python receiver.py list-ports          Show all available COM ports
""")

    sub = parser.add_subparsers(dest="command", help="Mode of operation")

    # serial subcommand
    sp_serial = sub.add_parser("serial", help="Receive via USB serial (auto-detects Pico)")
    sp_serial.add_argument("--port", type=str, default=None,
                           help="COM port (omit to auto-detect)")
    sp_serial.add_argument("--baud", type=int, default=BAUD_RATE,
                           help=f"Baud rate (default: {BAUD_RATE})")
    sp_serial.add_argument("--output", type=str, default=CSV_FILE,
                           help=f"CSV file (default: {CSV_FILE})")
    sp_serial.add_argument("--plot", action="store_true",
                           help="Show live temperature chart")

    # tcp subcommand
    sp_tcp = sub.add_parser("tcp", help="Receive via TCP/Ethernet (W5500)")
    sp_tcp.add_argument("--tcp-port", type=int, default=TCP_PORT,
                        help=f"TCP port to listen on (default: {TCP_PORT})")
    sp_tcp.add_argument("--output", type=str, default=CSV_FILE,
                        help=f"CSV file (default: {CSV_FILE})")
    sp_tcp.add_argument("--plot", action="store_true",
                        help="Show live temperature chart")

    # plot subcommand
    sp_plot = sub.add_parser("plot", help="Plot data from a saved CSV file")
    sp_plot.add_argument("csv_file", nargs="?", default=CSV_FILE,
                         help=f"CSV file to plot (default: {CSV_FILE})")

    # list-ports subcommand
    sub.add_parser("list-ports", help="List all available COM ports")

    args = parser.parse_args()

    if args.command == "serial":
        cmd_serial(args)
    elif args.command == "tcp":
        cmd_tcp(args)
    elif args.command == "plot":
        cmd_plot(args)
    elif args.command == "list-ports":
        print(f"\n{CYAN}Available COM ports:{RESET}")
        list_all_ports()
        print()
    else:
        parser.print_help()
        print(f"\n{CYAN}Quick start:{RESET}")
        print(f"  python receiver.py serial       # USB (auto-detect)")
        print(f"  python receiver.py tcp           # Ethernet")
        print(f"  python receiver.py plot           # View saved data")
        print()


if __name__ == "__main__":
    main()

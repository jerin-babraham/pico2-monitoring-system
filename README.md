# Pico 2 Monitoring System

A Raspberry Pi Pico reads a GPS module and a temperature sensor, then streams
the readings to a PC. The PC can receive over USB serial or over a direct
Ethernet cable. Data comes in as one JSON object per line.

## What is in this repo

- Firmware in C for the Pico (gps_parser.c, gps_temp_monitor.c, tcp_client.c,
  w5500_spi.c, config_store.c and their headers)
- Network settings in net_config.h
- Build files: CMakeLists.txt, pico_sdk_import.cmake, .vscode
- PC side in pc_receiver: monitor_gui.py, receiver.py, requirements.txt
- Saved logs as temperature_log.csv files

## Running the PC receiver

- Go into the receiver folder and install the two dependencies:
  - cd pc_receiver
  - pip install -r requirements.txt (this is pyserial and matplotlib)
- Pick how you want to run it:
  - python monitor_gui.py for the desktop dashboard (recommended)
  - python receiver.py serial for USB, it auto-detects the Pico
  - python receiver.py tcp for Ethernet, it listens on port 5005
  - python receiver.py plot to plot a saved temperature_log.csv

The receiver is meant to work on any PC. It finds the Pico by its USB vendor ID
(0x2E8A), falls back to TCP if pyserial is missing, picks a working UI font on
any OS, and shows IST time no matter what the PC timezone is. Nothing is tied to
one machine.

## Building the firmware on another PC

1. Install VS Code and the Raspberry Pi Pico extension. On first use let it
   install Pico SDK 2.2.0 and toolchain 14_2_Rel1. These are the versions pinned
   in CMakeLists.txt and .vscode.
2. If you copied the folder by hand, delete the old build artifacts first, since
   they hold absolute paths from the old machine:
   - rm -rf build .cache
   - On PowerShell: Remove-Item -Recurse -Force build, .cache
   - If you cloned with git instead, these are already ignored.
3. Open the folder in VS Code, pick the Pico kit, and Build. The output is
   build/project-vssc.uf2.
4. Hold BOOTSEL, plug in the Pico, and drop the .uf2 onto the RPI-RP2 drive. You
   can also use the extension's Run task.

## Network setup for Ethernet

A direct Pico to PC Ethernet cable has no router, so neither end gets an IP on
its own. Both fall back to a useless 169.254.x.x address and cannot talk. The
fix is fixed static IPs on the same subnet, which the firmware is already built
around:

- This PC, the wired adapter: 192.168.2.1 with mask 255.255.255.0 and no
  gateway. You set this once per PC.
- The Pico: 192.168.2.2, fixed in firmware.
- TCP port: 5005.

The Pico already dials 192.168.2.1 (saved in its flash), so porting to a new PC
is just one step: give that PC the static IP 192.168.2.1. No firmware rebuild,
nothing to change on the Pico.

### Steps on each new PC

1. Find the right adapter. Run ipconfig and look for the Ethernet adapter the
   Pico cable is in. It is the one showing an autoconfiguration address of
   169.254.x.x. Note its exact name, for example Ethernet or Ethernet 2.
2. Give it the static IP. The fastest way is to open Command Prompt as
   Administrator and run, using the name from step 1:
   - netsh interface ip set address name="Ethernet 2" static 192.168.2.1 255.255.255.0
   - GUI way: Network Connections, right-click the adapter, Properties, Internet
     Protocol Version 4 (TCP/IPv4), set IP 192.168.2.1, mask 255.255.255.0, and
     leave the gateway blank.
3. Allow the firewall. The first time you run the receiver, click Allow access
   on the Windows Firewall prompt and tick both Private and Public. A direct
   cable is often classed as Public. To pre-allow it as admin:
   - netsh advfirewall firewall add rule name="GPS Monitor 5005" dir=in action=allow protocol=TCP localport=5005
4. Verify and run.
   - ping 192.168.2.2, replies mean the link is good
   - python receiver.py tcp, or python monitor_gui.py and pick TCP (Ethernet)

Things to watch for:

- Set the IP on the adapter the cable is actually in, the 169.254 one.
- Do not move the USB to Ethernet dongle to another port afterward. It
  re-enumerates and reverts to APIPA.
- Your Wi-Fi and internet are untouched. Only the wired adapter changes.

### Using a different PC IP or a router

- To use another host address, set the PC to any 192.168.2.x other than .2, then
  re-point the Pico from the GUI's PICO TARGET box. It sends SETIP a.b.c.d over
  USB, saved to the Pico's flash, still with no rebuild.
- Only moving to a different subnet, through a router, needs net_config.h edits
  and a rebuild. All IPs and ports live in net_config.h.

## Firmware to PC wire format

Both receivers read these fields. The firmware writes them in format_sensor_json
in tcp_client.c. It is one compact JSON object per line, ending in a newline, on
TCP port 5005 and on USB serial at 115200 baud.

- A sensor record has a seq field plus these keys: ts_uptime_us, gps_locked,
  gps_stale, gps_time (ISO-8601 ending in Z, or null), lat, lon, alt, sats,
  hdop, fix_q, temp_c.
- Status lines (the boot banner, gps_ready, ip_set or ip, error) have no seq.
  They are shown but not logged.

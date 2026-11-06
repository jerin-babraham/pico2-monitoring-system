/*
 * net_config.h — Network Configuration (EDIT THIS FOR YOUR SETUP)
 * =================================================================
 *
 * All IP addresses, ports, and MAC address in ONE place.
 * When moving to a new PC or network, ONLY edit this file.
 *
 * SETUP GUIDE (direct Ethernet cable, no router):
 * ────────────────────────────────────────────────
 *   1. Plug an Ethernet cable directly between the Pico's W5500
 *      and the PC's USB-to-Ethernet adapter.
 *
 *   2. On the PC, open "Network Connections" → right-click the
 *      Ethernet adapter → Properties → IPv4 → set:
 *        IP address:    192.168.2.1   (must match SERVER_IP below)
 *        Subnet mask:   255.255.255.0
 *        Gateway:       (leave empty)
 *
 *   3. The Pico's IP (PICO_IP) must be on the SAME subnet as the PC.
 *      Default: 192.168.2.2 — no change needed unless there's a conflict.
 *
 *   4. Rebuild firmware after editing:  cmake --build build
 *
 * SETUP GUIDE (via router / DHCP network):
 * ────────────────────────────────────────
 *   1. Set PICO_IP to an unused address on your LAN (e.g. 192.168.1.200)
 *   2. Set PICO_GATEWAY to your router's IP (e.g. 192.168.1.1)
 *   3. Set PICO_SUBNET to 255.255.255.0
 *   4. Set SERVER_IP to your PC's LAN IP (find it with `ipconfig`)
 *   5. Rebuild firmware
 */

#ifndef NET_CONFIG_H
#define NET_CONFIG_H

/* ─── Pico W5500 network identity ─────────────────────────────────────── */

// MAC address — must be unique per device on the same network.
// The prefix 00:08:DC is WIZnet's official OUI (vendor range).
// Change the last 3 bytes (AB:CD:EF) if you run multiple Picos.
#define PICO_MAC        {0x00, 0x08, 0xDC, 0xAB, 0xCD, 0xEF}

// Pico's static IP — must be on the same subnet as the PC
#define PICO_IP         {192, 168, 2, 2}

// Subnet mask — 255.255.255.0 means "first 3 octets must match"
#define PICO_SUBNET     {255, 255, 255, 0}

// Gateway — set to the PC's IP for direct cable, or router IP for LAN
#define PICO_GATEWAY    {192, 168, 2, 1}

// DNS — 8.8.8.8 is Google's public DNS (not used, but required by W5500)
#define PICO_DNS        {  8,   8,   8,   8}


/* ─── PC server (where the Pico sends data) ───────────────────────────── */

// The PC's Ethernet adapter IP — Pico connects TO this address.
//
// NOTE: This is now only the FALLBACK default, used on a fresh device that has
// never been configured. You can change the server IP at runtime — without
// rebuilding — from the GUI: type the PC's IP into the "PICO TARGET (SERVER IP)"
// box and click send. The Pico saves it to flash and reuses it on every boot.
// (Under the hood the GUI sends "SETIP a.b.c.d" over USB.)
#define SERVER_IP       {192, 168, 2, 1}

// TCP port — must match the Python receiver (default: 5005)
#define SERVER_PORT     5005


/* ─── Socket configuration (usually no need to change) ────────────────── */

// Which W5500 socket to use (0-7, socket 0 is fine)
#define TCP_SOCKET      0

// Local port for the Pico's outgoing connection
#define LOCAL_PORT      50000

// After a failed connect, wait this many cycles before retrying (at 1 Hz)
#define RECONNECT_COOLDOWN  5

#endif /* NET_CONFIG_H */

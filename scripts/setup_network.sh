#!/usr/bin/env bash
# Give the rig a stable identity, so a DHCP change stops being an event.
#
# Run on the Pi:   bash scripts/setup_network.sh
# Safe to re-run. Nothing here is destructive; the DHCP lease is left alone.
#
# THE PROBLEM. A Pi on a managed network gets its address from DHCP, and DHCP
# changes its mind -- on a lease expiry, a switch port change, a reboot after a
# power cut. The rig then answers on an address nobody knows, and finding it
# means asking IT or scanning the subnet. That happened once and cost an
# afternoon.
#
# THREE ANSWERS, and the point of installing all of them is that they fail
# independently:
#
#   1. mDNS -- <hostname>.local. Free, already installed on Raspberry Pi OS,
#      works from macOS, Windows 10+, and Linux with avahi. Needs no
#      permission from anybody. Many enterprise networks drop multicast
#      between VLANs, so it is the everyday path and not the guarantee.
#
#   2. A FIXED SECOND ADDRESS on the wired interface, alongside DHCP. The Pi
#      keeps its DHCP lease for internet access AND always answers on a
#      private address of your choosing. Plug a laptop into the Pi with one
#      Ethernet cable, give the laptop an address on the same little subnet,
#      and the rig is reachable with no DHCP server, no router, and no network
#      administrator involved. This is the one that always works.
#
#   3. A DHCP reservation from IT, binding the Pi's MAC to one address. The
#      correct answer on a managed network and the only one that needs a
#      ticket. The MAC addresses are printed at the end so you can send them.
#
# There is a fourth -- USB-C gadget mode, one cable for power and network at a
# fixed 10.12.194.1 -- covered in the README. It is not installed here because
# on a Pi 5 driving two cameras the laptop's USB-C port is usually not up to
# powering the board.

set -euo pipefail

STATIC_ADDR="${TRILOBITE_STATIC_ADDR:-192.168.50.10/24}"
HOSTNAME_WANTED="${TRILOBITE_HOSTNAME:-}"
PORT="${TRILOBITE_PORT:-8000}"

say()  { printf '\n==> %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }

# -- 1. hostname, which is also the mDNS name -------------------------------

current_host="$(hostname)"
if [[ -n "$HOSTNAME_WANTED" && "$HOSTNAME_WANTED" != "$current_host" ]]; then
  say "Hostname: ${current_host} -> ${HOSTNAME_WANTED}"
  sudo hostnamectl set-hostname "$HOSTNAME_WANTED"
  # /etc/hosts must follow, or sudo takes seconds to resolve its own name and
  # every command feels broken.
  if grep -qE "^127\.0\.1\.1" /etc/hosts; then
    sudo sed -i "s/^127\.0\.1\.1.*/127.0.1.1\t${HOSTNAME_WANTED}/" /etc/hosts
  else
    echo -e "127.0.1.1\t${HOSTNAME_WANTED}" | sudo tee -a /etc/hosts >/dev/null
  fi
  current_host="$HOSTNAME_WANTED"
else
  say "Hostname: ${current_host} (unchanged)"
fi

# -- 2. mDNS ----------------------------------------------------------------

say "mDNS (<hostname>.local)"
if ! dpkg -s avahi-daemon >/dev/null 2>&1; then
  note "installing avahi-daemon"
  sudo apt install -y avahi-daemon
fi
sudo systemctl enable --now avahi-daemon >/dev/null 2>&1 || true
if systemctl is-active --quiet avahi-daemon; then
  note "avahi-daemon running -- ${current_host}.local should resolve"
else
  note "avahi-daemon NOT running; ${current_host}.local will not resolve"
fi

# Advertise the web UI as a service too, so the rig shows up in network
# browsers rather than only answering when asked by name.
SERVICE_FILE=/etc/avahi/services/trilobite.service
say "Advertising the web UI on port ${PORT}"
sudo tee "$SERVICE_FILE" >/dev/null <<XML
<?xml version="1.0" standalone='no'?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<!-- Written by scripts/setup_network.sh. Announces the TrilobiteVision UI so
     the rig appears in network browsers instead of having to be looked up. -->
<service-group>
  <name replace-wildcards="yes">TrilobiteVision on %h</name>
  <service>
    <type>_http._tcp</type>
    <port>${PORT}</port>
    <txt-record>path=/</txt-record>
  </service>
</service-group>
XML
sudo systemctl restart avahi-daemon >/dev/null 2>&1 || true
note "wrote ${SERVICE_FILE}"

# -- 3. a fixed second address, alongside DHCP ------------------------------

say "Fixed address ${STATIC_ADDR} on the wired interface"
# Raspberry Pi OS has used NetworkManager since Bookworm; dhcpcd is gone.
if ! command -v nmcli >/dev/null 2>&1; then
  note "nmcli not found -- this is not a NetworkManager system, skipping."
  note "Set a second address by hand for your network stack and re-run."
else
  # The wired connection, whatever it happens to be called. "Wired connection 1"
  # is the default name but it is not guaranteed.
  CON="$(nmcli -t -g NAME,TYPE connection show | awk -F: '$2=="802-3-ethernet"{print $1; exit}')"
  if [[ -z "$CON" ]]; then
    note "no wired connection profile found; plug in Ethernet once and re-run"
  else
    note "connection: ${CON}"
    existing="$(nmcli -t -g ipv4.addresses connection show "$CON" || true)"
    if [[ "$existing" == *"${STATIC_ADDR}"* ]]; then
      note "${STATIC_ADDR} already present"
    else
      # ipv4.method STAYS auto. That is the whole trick: NetworkManager applies
      # the manual addresses IN ADDITION to the DHCP lease, so the rig keeps
      # its normal network access and also always answers here. Setting the
      # method to manual instead would take the Pi off the network entirely --
      # which, done remotely, is how you lose an afternoon to a keyboard and a
      # monitor.
      sudo nmcli connection modify "$CON" +ipv4.addresses "$STATIC_ADDR"
      sudo nmcli connection modify "$CON" ipv4.method auto
      note "added ${STATIC_ADDR}; DHCP left enabled"
      sudo nmcli connection up "$CON" >/dev/null 2>&1 || \
        note "bring the link up yourself, or reboot, to apply it"
    fi
  fi
fi

# -- 4. what to tell IT, and what to type -----------------------------------

say "Interfaces"
ip -br -4 addr show | sed 's/^/    /'

say "MAC addresses -- send these to IT for a DHCP reservation"
for dev in /sys/class/net/*; do
  name="$(basename "$dev")"
  [[ "$name" == "lo" ]] && continue
  [[ -r "$dev/address" ]] && note "$(printf '%-8s %s' "$name" "$(cat "$dev/address")")"
done

say "Reach the rig at"
note "http://${current_host}.local:${PORT}/          <- try this first"
while read -r _ ip _; do
  note "http://${ip%%/*}:${PORT}/"
done < <(ip -br -4 addr show | grep -v '^lo' | awk '{print $1, $3}')

cat <<TEXT

    To use the fixed address, give the laptop one on the same subnet and
    connect the two with a single Ethernet cable -- no router needed:

        Windows   Settings > Network > Ethernet > IP assignment > Edit >
                  Manual > IPv4 on > IP ${STATIC_ADDR%%/*} with last octet
                  changed (e.g. 192.168.50.20), mask 255.255.255.0
        macOS     System Settings > Network > Ethernet > Details > TCP/IP >
                  Configure IPv4: Manually
        Linux     sudo ip addr add 192.168.50.20/24 dev <iface>

    The rig is then always at http://${STATIC_ADDR%%/*}:${PORT}/ regardless of
    what any DHCP server does.

TEXT

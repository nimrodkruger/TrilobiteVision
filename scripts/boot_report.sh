#!/bin/bash
# A flight recorder for a Pi you cannot reach.
#
# Writes everything worth knowing to a text file on the SD card's boot
# partition. You then pull the card, put it in a PC, and read the file. No
# monitor, no keyboard, no network, no guessing.
#
# Full step-by-step, including the PowerShell to install it safely, is in
# README.md under "If you cannot reach the Pi at all".
#
# ---------------------------------------------------------------------------
# WHY IT RUNS TWICE
#
# The kernel argument that starts this (`systemd.run=`) is paired with
# `systemd.unit=kernel-command-line.target`, which boots systemd to a MINIMAL
# target: local filesystems and little else. NetworkManager is not running
# there. ssh is not running there. Asking about them at that moment and
# reporting "inactive" would be an artefact of how the report was taken, not a
# fact about the rig -- and it would send you off diagnosing a service that is
# actually fine.
#
# So:
#
#   STAGE 1, early, from the kernel argument. Records what is already true at
#           that point and cannot be misread: whether the Imager's firstrun.sh
#           completed, which users exist, the interfaces and their MAC
#           addresses, the boot config, the kernel log. Then it installs a
#           systemd unit for stage 2, removes its own kernel argument, and
#           lets systemd reboot.
#
#   STAGE 2, on the ordinary boot that follows, after the network is up.
#           Appends the live picture: addresses, NetworkManager's view, ssh
#           actually running or not, the cameras, the throttling flags. Then it
#           removes its own unit so the boot after that is untouched.
#
# One insertion into cmdline.txt therefore gets you both halves. The Pi boots,
# reboots itself, boots again, and settles. Leave it powered for three minutes.
#
# ---------------------------------------------------------------------------
# WHAT THE RESULT TELLS YOU
#
#   no tv_report.txt at all       The kernel never ran stage 1. The Pi is not
#                                 booting far enough to matter -- suspect the
#                                 card, the image write, or the power supply.
#                                 Reflash before anything else.
#   stage 1 only, no stage 2      It boots minimally but not to multi-user.
#                                 Read the kernel log at the end of stage 1.
#   "firstrun.sh IS STILL PRESENT" The Imager customisation never completed:
#                                 no user, no hostname, and NO SSH. Reflash.
#   stage 2 present, ssh active   It boots and is listening. The problem is
#                                 purely addressing, and stage 2 lists every
#                                 address it has.
#   eth0 carrier=0                No link: cable, port, or PHY. Nothing above
#                                 that is worth debugging yet.

set +e                                    # report everything, stop at nothing

BOOT=/boot/firmware
[ -d "$BOOT" ] || BOOT=/boot
OUT="$BOOT/tv_report.txt"
UNIT=/etc/systemd/system/tv-boot-report.service
WANTS=/etc/systemd/system/multi-user.target.wants/tv-boot-report.service

# The boot partition is mounted read-only in some configurations.
mount -o remount,rw "$BOOT" 2>/dev/null


# ===========================================================================
# STAGE 2 -- the ordinary boot, network up, services started
# ===========================================================================
if [ "$1" = "--stage2" ]; then
  {
    echo
    echo "==========================================================="
    echo "STAGE 2 -- normal boot, $(date -u 2>/dev/null) UTC"
    echo "uptime at capture: $(cut -d' ' -f1 /proc/uptime 2>/dev/null) s"
    echo "==========================================================="
    echo
    echo "-- addresses ------------------------------------------------------"
    ip -br addr show 2>/dev/null
    echo
    echo "carrier (1 = cable in and the far end alive):"
    for n in /sys/class/net/*; do
      [ "$(basename "$n")" = "lo" ] && continue
      printf '  %-8s carrier=%s operstate=%s mac=%s\n' \
        "$(basename "$n")" "$(cat "$n/carrier" 2>/dev/null)" \
        "$(cat "$n/operstate" 2>/dev/null)" "$(cat "$n/address" 2>/dev/null)"
    done
    echo
    echo "routes:"
    ip route 2>/dev/null | sed 's/^/  /'
    echo

    echo "-- NetworkManager -------------------------------------------------"
    nmcli device status 2>/dev/null || echo "  (nmcli failed -- is NetworkManager installed?)"
    echo
    nmcli -f NAME,TYPE,DEVICE connection show 2>/dev/null
    echo
    echo "why is a device disconnected, if it is:"
    nmcli -f GENERAL.STATE,GENERAL.REASON device show 2>/dev/null | sed 's/^/  /'
    echo

    echo "-- ssh ------------------------------------------------------------"
    echo "enabled : $(systemctl is-enabled ssh 2>&1)"
    echo "active  : $(systemctl is-active ssh 2>&1)"
    echo "listening sockets:"
    ss -lntp 2>/dev/null | sed 's/^/  /' || netstat -lntp 2>/dev/null | sed 's/^/  /'
    echo

    echo "-- avahi / mDNS ---------------------------------------------------"
    echo "enabled : $(systemctl is-enabled avahi-daemon 2>&1)"
    echo "active  : $(systemctl is-active avahi-daemon 2>&1)"
    echo

    echo "-- cameras --------------------------------------------------------"
    if command -v rpicam-hello >/dev/null 2>&1; then
      timeout 20 rpicam-hello --list-cameras 2>&1 | sed 's/^/  /'
    else
      echo "  rpicam-hello not installed yet"
    fi
    for d in /dev/video* /dev/media*; do
      [ -e "$d" ] && echo "  $d"
    done
    echo

    echo "-- power ----------------------------------------------------------"
    # Bit 16 is sticky: it survives the brownout that set it.
    echo "throttled : $(vcgencmd get_throttled 2>&1)"
    echo "temp      : $(vcgencmd measure_temp 2>&1)"
    echo

    echo "-- failed units ---------------------------------------------------"
    systemctl --failed --no-pager 2>&1 | sed 's/^/  /'
    echo

    echo "-- last 80 log lines ----------------------------------------------"
    journalctl -b --no-pager -n 80 2>/dev/null || dmesg 2>/dev/null | tail -80
    echo
    echo "=== end of report ==="
  } >> "$OUT" 2>&1

  sync
  # Remove the unit so the next boot is completely untouched.
  rm -f "$WANTS" "$UNIT" 2>/dev/null
  systemctl daemon-reload 2>/dev/null
  sync
  exit 0
fi


# ===========================================================================
# STAGE 1 -- early, minimal boot, straight off the kernel command line
# ===========================================================================

# FIRST, before anything that could fail: take our argument back out of
# cmdline.txt. If a later line of this script were to crash with the argument
# still in place, the Pi would run it again on every boot for ever. Removing
# it first makes the worst case "one wasted boot", not "a brick".
cp "$BOOT/cmdline.txt" "$BOOT/cmdline.txt.tvbak" 2>/dev/null
sed -i 's| systemd\.run=[^ ]*||g; s| systemd\.run_success_action=[^ ]*||g; s| systemd\.run_failure_action=[^ ]*||g; s| systemd\.unit=[^ ]*||g' \
    "$BOOT/cmdline.txt" 2>/dev/null
sync

{
  echo "TrilobiteVision boot report"
  echo "==========================================================="
  echo "STAGE 1 -- early boot, $(date -u 2>/dev/null) UTC"
  echo "uptime at capture: $(cut -d' ' -f1 /proc/uptime 2>/dev/null) s"
  echo "==========================================================="
  echo
  echo "Stage 1 runs before NetworkManager and ssh are started, so it does"
  echo "NOT report on them -- that is what stage 2, below, is for. If there"
  echo "is no stage 2 in this file, the Pi did not reach a normal boot."
  echo

  echo "-- identity -------------------------------------------------------"
  echo "hostname  : $(hostname 2>/dev/null)"
  echo "os        : $(grep PRETTY_NAME /etc/os-release 2>/dev/null | cut -d'"' -f2)"
  echo "kernel    : $(uname -a 2>/dev/null)"
  if [ -r /proc/device-tree/model ]; then
    echo "model     : $(tr -d '\0' < /proc/device-tree/model)"
  fi
  echo

  echo "-- did the Imager's first boot finish? ----------------------------"
  # firstrun.sh still present means the customisation never completed, which
  # means no user account, no hostname and -- the one that matters -- NO SSH,
  # however healthy the network is.
  if [ -f "$BOOT/firstrun.sh" ]; then
    echo "  *** firstrun.sh IS STILL PRESENT ***"
    echo "  The Imager customisation did not complete. No SSH, no hostname,"
    echo "  possibly no user account. Reflash; no network work will help."
  else
    echo "  firstrun.sh absent -- the Imager customisation ran to completion."
  fi
  echo "  non-system users:"
  awk -F: '$3 >= 1000 && $3 < 65534 {printf "    %s (uid %s, home %s, shell %s)\n", $1, $3, $6, $7}' \
      /etc/passwd 2>/dev/null
  echo "  ssh enabled at boot: $(systemctl is-enabled ssh 2>&1)"
  echo "  ssh host keys:"
  for k in /etc/ssh/ssh_host_*_key.pub; do
    [ -e "$k" ] && echo "    $k"
  done
  echo

  echo "-- interfaces and MACs (before any addressing) --------------------"
  for n in /sys/class/net/*; do
    [ "$(basename "$n")" = "lo" ] && continue
    printf '  %-8s carrier=%s operstate=%s mac=%s\n' \
      "$(basename "$n")" "$(cat "$n/carrier" 2>/dev/null)" \
      "$(cat "$n/operstate" 2>/dev/null)" "$(cat "$n/address" 2>/dev/null)"
  done
  echo
  echo "  NetworkManager connection profiles on disk:"
  for f in /etc/NetworkManager/system-connections/*; do
    [ -e "$f" ] || continue
    echo "    --- $f"
    grep -E '^\[|^(id|type|method|address|addresses|may-fail|autoconnect)=' "$f" 2>/dev/null | sed 's/^/      /'
  done
  echo

  echo "-- boot configuration ---------------------------------------------"
  echo "  cmdline.txt (our argument already removed):"
  echo "    $(cat "$BOOT/cmdline.txt" 2>/dev/null)"
  echo "  camera lines in config.txt:"
  grep -E 'camera_auto_detect|dtoverlay=imx|dtparam=i2c' "$BOOT/config.txt" 2>/dev/null | sed 's/^/    /'
  echo "  boot partition contents:"
  for f in "$BOOT"/*; do
    [ -e "$f" ] && printf '    %s\n' "$(basename "$f")"
  done
  echo

  echo "-- filesystems ----------------------------------------------------"
  df -h 2>/dev/null | sed 's/^/  /'
  echo

  echo "-- kernel log, last 80 lines --------------------------------------"
  dmesg 2>/dev/null | tail -80
  echo
  echo "=== end of stage 1 -- stage 2 should follow after the reboot ==="
} > "$OUT" 2>&1

sync

# Install the stage-2 unit. The symlink is written by hand rather than with
# `systemctl enable`, because systemd's dbus is not necessarily up this early
# and the symlink is all `enable` would have done anyway.
SELF="$BOOT/$(basename "$0")"
cat > "$UNIT" <<UNITFILE
[Unit]
Description=TrilobiteVision boot report, stage 2
After=network-online.target multi-user.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/bin/bash ${SELF} --stage2
# Give NetworkManager a moment to finish deciding what it thinks the network
# is; a report taken the instant multi-user is reached catches it mid-DHCP.
ExecStartPre=/bin/sleep 20
RemainAfterExit=no

[Install]
WantedBy=multi-user.target
UNITFILE

mkdir -p "$(dirname "$WANTS")" 2>/dev/null
ln -sf "$UNIT" "$WANTS" 2>/dev/null
sync

exit 0

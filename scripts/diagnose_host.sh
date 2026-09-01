#!/usr/bin/env bash
# Why did the Pi go down, and why can it not see my disk?
#
# Run on the Pi AFTER a crash, before rebooting again if you can:
#
#     bash scripts/diagnose_host.sh | tee ~/trilobite-diagnosis.txt
#
# Two questions, one script, because the answers live in the same places.
#
# The single most useful line in the output is the "get_throttled" one. Its
# bit 16 is sticky since boot: if it is set, the supply dropped below 4.63 V
# and the board browned out. A Pi 5 with two cameras and four busy cores wants
# a real 5 V / 5 A supply, and no amount of software tuning substitutes for it.

set -uo pipefail

hr() { printf '\n=== %s %s\n' "$1" "$(printf '%*s' $((60 - ${#1})) '' | tr ' ' '=')"; }
have() { command -v "$1" >/dev/null 2>&1; }

hr "when and what"
date
uptime
cat /proc/device-tree/model 2>/dev/null && echo
echo "kernel:   $(uname -a)"
echo "os:       $(sed -n 's/^PRETTY_NAME="\(.*\)"/\1/p' /etc/os-release 2>/dev/null)"

hr "power and thermal  <-- read this one first"
if have vcgencmd; then
  T=$(vcgencmd get_throttled)
  echo "$T"
  V=${T#*=}
  bit() { (( ( $1 >> $2 ) & 1 )); }
  N=$((V))
  echo
  bit $N 0  && echo "  !! UNDER-VOLTAGE RIGHT NOW"
  bit $N 1  && echo "  !  ARM frequency capped now"
  bit $N 2  && echo "  !  throttled now"
  bit $N 3  && echo "  !  soft temperature limit active now"
  bit $N 16 && echo "  !! UNDER-VOLTAGE HAS OCCURRED SINCE BOOT  <-- power supply"
  bit $N 17 && echo "  !  frequency capping has occurred"
  bit $N 18 && echo "  !  throttling has occurred"
  bit $N 19 && echo "  !  soft temperature limit has been hit"
  [ "$N" -eq 0 ] && echo "  clean: no under-voltage, no throttling since boot"
  echo
  echo "core temp:   $(vcgencmd measure_temp)"
  echo "core volts:  $(vcgencmd measure_volts core)"
  echo "arm clock:   $(vcgencmd measure_clock arm)"
else
  echo "vcgencmd not found (not a Pi, or raspi-utils missing)"
fi
echo "thermal zone: $(awk '{printf "%.1f C\n", $1/1000}' \
    /sys/class/thermal/thermal_zone0/temp 2>/dev/null || echo unknown)"
have raspinfo && echo "(raspinfo is installed if you need the full dump)"

hr "memory"
free -h
echo
echo "OOM kills in this boot:"
dmesg -T 2>/dev/null | grep -iE 'out of memory|oom-killer|killed process' | tail -20 \
  || echo "  (dmesg needs sudo on this system: try 'sudo dmesg -T | grep -i oom')"

hr "the previous boot's last words"
if have journalctl; then
  echo "--- boots known to journald:"
  journalctl --list-boots 2>/dev/null | tail -5
  echo
  echo "--- last 40 lines before the previous boot ended:"
  journalctl -b -1 -n 40 --no-pager 2>/dev/null \
    || echo "  (no previous boot recorded -- persistent journal may be off:"
  echo "     enable it with 'sudo mkdir -p /var/log/journal && sudo systemd-tmpfiles --create --prefix /var/log/journal')"
  echo
  echo "--- trilobite service, if it runs as one:"
  journalctl -u trilobite -n 40 --no-pager 2>/dev/null || true
else
  echo "journalctl not available"
fi

hr "kernel messages worth reading"
dmesg -T 2>/dev/null | grep -iE \
  'under-voltage|throttl|watchdog|hardware error|firmware|reset|brown|usb .*(disconnect|reset)|cma' \
  | tail -30 || echo "  (needs sudo: 'sudo dmesg -T')"

hr "load right now"
cat /proc/loadavg
have vmstat && vmstat 1 3

hr "block devices  <-- storage question starts here"
if have lsblk; then
  lsblk -o PATH,NAME,SIZE,FSTYPE,LABEL,MOUNTPOINT,RM,TYPE,RO,MODEL
else
  echo "lsblk not found -- 'sudo apt install -y util-linux'"
fi

hr "mounted filesystems"
findmnt -t nodevtmpfs,notmpfs,noproc,nosysfs,nocgroup2 -o TARGET,SOURCE,FSTYPE,OPTIONS 2>/dev/null \
  || grep -v -E '^(proc|sysfs|tmpfs|devtmpfs|cgroup)' /proc/mounts

hr "can anything mount removable media?"
if have udisksctl; then
  echo "udisksctl: $(udisksctl --version 2>&1 | head -1)"
  systemctl is-active udisks2 2>/dev/null | sed 's/^/udisks2 service: /'
else
  echo "!! udisksctl NOT installed. TrilobiteVision's Mount button needs it:"
  echo "     sudo apt install -y udisks2"
fi
echo
echo "filesystem drivers present:"
for fs in exfat ntfs3 ntfs fuseblk vfat ext4; do
  if grep -qw "$fs" /proc/filesystems 2>/dev/null; then
    echo "  $fs: yes"
  else
    echo "  $fs: no"
  fi
done
have mount.exfat && echo "  mount.exfat helper: yes" || echo "  mount.exfat helper: no (sudo apt install -y exfatprogs)"
have ntfs-3g && echo "  ntfs-3g helper: yes" || echo "  ntfs-3g helper: no (sudo apt install -y ntfs-3g)"

hr "recent USB events"
dmesg -T 2>/dev/null | grep -iE 'usb|scsi|sd [a-z]:' | tail -25 \
  || echo "  (needs sudo)"

hr "done"
echo "Send the whole file. The lines that matter most:"
echo "  * anything marked !! in 'power and thermal'"
echo "  * the lsblk table"
echo "  * whether udisksctl is installed"

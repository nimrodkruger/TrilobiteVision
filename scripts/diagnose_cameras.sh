#!/usr/bin/env bash
# Why does libcamera report no cameras?
#
#   bash scripts/diagnose_cameras.sh
#
# Runs every check that distinguishes the possible causes, in the order that
# costs least to fix. Read the summary at the end; it names the layer that is
# broken rather than leaving you to correlate six outputs.

echo "=============================================================="
echo " TrilobiteVision camera diagnosis   $(date -Is)"
echo "=============================================================="

section() { echo; echo "--- $* ------------------------------------------"; }

# ---------------------------------------------------------------- 1. contention
# Cheapest and, during development, the most common. libcamera opens the media
# devices exclusively while enumerating; another process holding them makes the
# cameras vanish rather than fail to acquire, which is why the symptom looks
# like missing hardware.
section "1. Another process holding the cameras"
STALE=$(pgrep -af "trilobite|rpicam|libcamera|picamera" | grep -v "diagnose_cameras" || true)
if [[ -n "$STALE" ]]; then
  echo "!! processes found:"
  echo "$STALE"
  CONTENTION=1
else
  echo "ok  no camera processes running"
  CONTENTION=0
fi
echo "systemd unit:"
systemctl is-active trilobite 2>/dev/null || echo "  (not active)"
echo "media device holders:"
if command -v fuser >/dev/null; then
  fuser -v /dev/media* /dev/video* 2>&1 | head -20 || echo "  none"
else
  echo "  (install psmisc for fuser)"
fi

# ---------------------------------------------------------------- 2. config.txt
# An apt full-upgrade or a firmware update can rewrite config.txt and put
# camera_auto_detect back to 1, which on a two-camera Pi 5 silently loses both.
section "2. Boot configuration"
CFG=/boot/firmware/config.txt
[[ -f "$CFG" ]] || CFG=/boot/config.txt
echo "using $CFG  (modified $(stat -c %y "$CFG" 2>/dev/null))"
grep -nE "camera_auto_detect|dtoverlay=imx|dtoverlay=.*cam" "$CFG" || echo "!! no camera lines at all"
OVERLAYS=$(grep -cE "^dtoverlay=imx296,cam[01]" "$CFG" 2>/dev/null || echo 0)
echo "imx296 overlays present: $OVERLAYS  (expected 2)"

# ---------------------------------------------------------------- 3. kernel
# The decisive test. Overlay loaded + probe succeeded -> driver messages here.
# Overlay loaded + probe failed -> an i2c/power/cable error here.
# Nothing at all -> the overlay never loaded, so it is a config or reboot issue.
section "3. Kernel probe"
dmesg 2>/dev/null | grep -iE "imx296|rp1-cfe|csi|cam1_reg|cam0_reg" | tail -25 \
  || echo "(need sudo for dmesg?  try: sudo dmesg | grep -i imx296)"
echo
echo "loaded module:"
lsmod | grep -E "^imx296" || echo "  imx296 NOT loaded"

# ---------------------------------------------------------------- 4. devices
section "4. Device nodes"
ls -l /dev/media* /dev/video* 2>/dev/null | head -20 || echo "  none present"
command -v v4l2-ctl >/dev/null && v4l2-ctl --list-devices 2>&1 | head -30 \
  || echo "  (v4l2-ctl not installed: sudo apt install v4l-utils)"

# ---------------------------------------------------------------- 5. i2c
# Is the sensor electrically present? 0x1a is the IMX296 address. This is what
# separates "software forgot about the camera" from "the ribbon is loose".
section "5. Sensor on the i2c bus (IMX296 = 0x1a)"
if command -v i2cdetect >/dev/null; then
  for BUS in 4 6 10 11 0 1; do
    OUT=$(sudo i2cdetect -y "$BUS" 2>/dev/null) || continue
    if echo "$OUT" | grep -q " 1a"; then
      echo "bus $BUS: FOUND a device at 0x1a"
    else
      echo "bus $BUS: nothing at 0x1a"
    fi
  done
else
  echo "  (i2c-tools not installed: sudo apt install i2c-tools)"
fi

# ---------------------------------------------------------------- 6. libcamera
section "6. libcamera enumeration"
rpicam-hello --list-cameras 2>&1 | head -30 || echo "  rpicam-hello not found"

# ---------------------------------------------------------------- 7. power
section "7. Power and thermals"
vcgencmd get_throttled 2>/dev/null || echo "  (vcgencmd unavailable)"
echo "  0x0 = fine.  Bit 0 set = under-voltage now, bit 16 = under-voltage since boot."

# ---------------------------------------------------------------- summary
echo
echo "=============================================================="
echo " READ THIS"
echo "=============================================================="
if [[ "$CONTENTION" == "1" ]]; then
  echo "* A camera process is already running. Stop it and retry:"
  echo "      pkill -f 'm trilobite' ; sudo systemctl stop trilobite"
  echo "  libcamera enumerates zero cameras when the media devices are held,"
  echo "  which looks exactly like absent hardware."
elif [[ "$OVERLAYS" -lt 2 ]]; then
  echo "* The imx296 overlays are missing from $CFG."
  echo "  An apt upgrade or firmware update most likely rewrote it. Restore:"
  echo "      camera_auto_detect=0"
  echo "      dtoverlay=imx296,cam0"
  echo "      dtoverlay=imx296,cam1"
  echo "  then reboot."
else
  echo "* Overlays are present and nothing is holding the cameras, so read"
  echo "  sections 3 and 5 together:"
  echo "    - imx296 in dmesg with errors, nothing at 0x1a  -> cable or power."
  echo "      Reseat both CSI ribbons (contacts toward the board, fully home,"
  echo "      latch down). The most likely cause after physical work on the rig."
  echo "    - device at 0x1a but no imx296 module            -> overlay/kernel"
  echo "      mismatch after an upgrade: sudo apt full-upgrade && sudo reboot"
  echo "    - clean dmesg, device present, still 0 cameras   -> send me"
  echo "      sections 3, 5 and 6 in full."
fi
echo

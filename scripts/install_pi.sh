#!/usr/bin/env bash
# Prepare a freshly flashed Raspberry Pi 5 for the flyeye stack.
#
# Run on the Pi:   bash scripts/install_pi.sh
# Safe to re-run.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${HOME}/.venvs/flyeye"
BOOT_CFG=/boot/firmware/config.txt

echo "==> System packages"
sudo apt update
sudo apt full-upgrade -y

# picamera2 and the libcamera Python bindings MUST come from apt. They are C++
# extensions built against the system libcamera; the pip versions drift out of
# step with the shipped libcamera and produce import errors or, worse, silent
# format mismatches. numpy and opencv also come from apt so they are ABI-
# compatible with picamera2's buffers and so you avoid a 40-minute source build
# of opencv on the Pi.
sudo apt install -y \
  python3-picamera2 \
  python3-numpy \
  python3-opencv \
  python3-simplejpeg \
  rpicam-apps \
  python3-venv python3-pip \
  git

echo
echo "==> Camera overlays in ${BOOT_CFG}"
# On a Pi 5 the two CSI connectors are addressed as cam0 and cam1. Automatic
# detection handles a single camera fine but is unreliable with two, so it is
# switched off and each port is declared explicitly.
add_line() {
  local line="$1"
  if ! grep -qxF "$line" "$BOOT_CFG"; then
    echo "  + $line"
    echo "$line" | sudo tee -a "$BOOT_CFG" >/dev/null
    CONFIG_CHANGED=1
  else
    echo "  = $line (already present)"
  fi
}
CONFIG_CHANGED=0
if ! grep -q "^camera_auto_detect=0" "$BOOT_CFG"; then
  sudo sed -i 's/^camera_auto_detect=1/camera_auto_detect=0/' "$BOOT_CFG" || true
fi
add_line "camera_auto_detect=0"
add_line "dtoverlay=imx296,cam0"
add_line "dtoverlay=imx296,cam1"

echo
echo "==> Python virtual environment at ${VENV}"
# --system-site-packages is not optional. Raspberry Pi OS is an
# externally-managed environment (PEP 668), so pip refuses to install into the
# system Python; and picamera2, installed by apt, lives in the system
# site-packages. Without this flag the venv cannot see the camera library and
# every import fails with a confusing ModuleNotFoundError.
python3 -m venv --system-site-packages "$VENV"
# shellcheck disable=SC1091
source "${VENV}/bin/activate"
pip install --upgrade pip
pip install -e "${REPO_DIR}"

echo
echo "==> Verifying"
python -c "import picamera2, numpy, cv2; print('picamera2, numpy, cv2 import OK')" || {
  echo "picamera2 import failed -- was the venv created with --system-site-packages?" >&2
  exit 1
}
python -c "import flyeye; print('flyeye', flyeye.__version__)"

echo
if [[ "$CONFIG_CHANGED" == "1" ]]; then
  echo "config.txt changed -- REBOOT before the cameras will appear:  sudo reboot"
else
  echo "Next:  source ${VENV}/bin/activate && python scripts/probe_cameras.py"
fi

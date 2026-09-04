# Pi troubleshooting

Recovery procedures for a rig that will not answer. None of this is needed for
an ordinary setup — the README's install steps are the path — and all of it is
here because each one cost a session at least once.

**Start with the router.** Nearly everything below exists because of a Pi on a
direct cable to a PC, with no DHCP server anywhere. Put both machines on a
router and the entire class of problem disappears. Reach for this document only
when you cannot.

---

## The Pi has no IPv4 address at all

The ACT LED blinking and the Ethernet lights on means the board booted and the
link is up. If `flyeye.local` still fails, the cause is almost always this:

> **On a direct PC-to-Pi cable there is no DHCP server, and NetworkManager does
> not fall back to an IPv4 link-local address.** Unlike the old `dhcpcd`, it
> does not hand the interface a `169.254.x.x` when DHCP times out — you have to
> configure that. So a freshly flashed Pi on a direct cable has **no IPv4
> address whatsoever** on `eth0`.

Everything IPv4 then fails for the same reason, and none of the obvious repairs
help: pinging it fails, `flyeye.local` has nothing to resolve to, and giving
the PC a static address — `192.168.50.20`, say — cannot work either, because
there is nothing at the other end of that subnet yet. The fixed
`192.168.50.10` is *created by* `setup_network.sh`, which needs a shell to run,
which is what you are trying to get.

**What does exist is IPv6.** Every interface with a live link gets an IPv6
link-local `fe80::` address, always, with no DHCP and no configuration. That is
the way in. From Windows PowerShell:

```powershell
# 1. Which interface is the Pi plugged into?
Get-NetAdapter | Where-Object Status -eq 'Up'          # note the ifIndex

# 2. Ask every device on that link to identify itself (ifIndex 12 here)
ping -6 ff02::1%12

# 3. The Pi is now in the neighbour table
Get-NetNeighbor -AddressFamily IPv6 -InterfaceIndex 12 |
    Where-Object IPAddress -like 'fe80*'

# 4. Go in. The %12 suffix is required: a link-local address is
#    meaningless without saying which link.
ssh flyeye@fe80::xxxx:xxxx:xxxx:xxxx%12
```

Then run `bash scripts/setup_network.sh` and the problem is permanently solved:
the fixed IPv4 address exists from then on, and plain
`ssh flyeye@192.168.50.10` works on any direct cable.

Set the PC's adapter back to **automatic** first. A static IPv4 does not block
the IPv6 route in, but it guarantees the IPv4 one stays broken and it will
confuse the next thing you try.

**When none of that works: read the card.** Two checks with the SD card in the
PC, no monitor and no network involved. Windows mounts the boot partition as a
drive letter — the root filesystem is ext4 and will not appear, which is normal.

1. **Is `firstrun.sh` still on the boot partition?** If it is, the Imager's
   customisation never completed, which means no hostname, no user account and
   **no SSH** — however healthy the network is. Reflash.
2. **Does `cmdline.txt` still contain `systemd.run=...firstrun.sh`?** Same
   conclusion: first boot did not finish.

If both look right, the Pi is booting and configured, and the question is what
it thinks its network is. `scripts/boot_report.sh` answers that with no monitor
and no network — the full procedure is immediately below.

## Reading the Pi's state off the SD card

`scripts/boot_report.sh` runs on the Pi during boot and writes what it finds to
a text file on the SD card's boot partition, which Windows can read. It needs
no monitor, no keyboard and no network. Use it when the Pi is plainly powered
but will not answer.

**It runs in two stages, and that matters.** The kernel argument that starts it
boots systemd to a *minimal* target — local filesystems and little else, with
NetworkManager and ssh not yet started. A report taken there that said "ssh
inactive" would be describing how the report was taken, not the rig. So stage 1
records only what is already true and cannot be misread, installs a systemd
unit, and lets the Pi reboot; stage 2 runs on that ordinary boot, twenty
seconds after the network comes up, and appends the live picture. One
installation, both halves, one file.

**1 — Put the card in the PC.** Windows mounts the boot partition as a drive
letter, typically with `config.txt` and `cmdline.txt` in its root. The other
partition is ext4 and will not appear; that is normal, not a fault. Note the
drive letter — `E:` in what follows.

**2 — Install it.** Run this in PowerShell, with the drive letter and the repo
path adjusted. It does the whole job: copies the script with Unix line endings
and no byte-order mark, backs up `cmdline.txt`, and appends the kernel argument
without adding a newline.

```powershell
$boot = 'E:'
$repo = 'C:\Users\30067913\OneDrive - Western Sydney University\Projects\TrilobiteVision'

# The script, converted to LF and written without a BOM.
$utf8NoBom = New-Object System.Text.UTF8Encoding $false
$s = [IO.File]::ReadAllText("$repo\scripts\boot_report.sh") -replace "`r`n", "`n"
[IO.File]::WriteAllText("$boot\tv_report.sh", $s, $utf8NoBom)

# cmdline.txt: back it up, then append one space and the argument.
Copy-Item "$boot\cmdline.txt" "$boot\cmdline.txt.bak" -Force
$c = ([IO.File]::ReadAllText("$boot\cmdline.txt")).TrimEnd("`r", "`n", " ")
$c += ' systemd.run=/boot/firmware/tv_report.sh systemd.run_success_action=reboot systemd.run_failure_action=reboot systemd.unit=kernel-command-line.target'
[IO.File]::WriteAllText("$boot\cmdline.txt", $c, $utf8NoBom)

# Read it back and check it is still exactly one line.
$check = [IO.File]::ReadAllText("$boot\cmdline.txt")
"lines: " + ($check -split "`n").Count
$check
```

That must print `lines: 1`. If it prints 2, something added a newline and the
Pi may not boot — restore `cmdline.txt.bak` and try again.

Three details in there are not fussiness, they are the three ways this fails
silently:

| detail | what happens otherwise |
|---|---|
| **LF, not CRLF** | The shebang becomes `#!/bin/bash\r`, which is not a program that exists. The script never runs and you get no report at all — indistinguishable from a Pi that never booted. |
| **No byte-order mark** | Three invisible bytes before `#!`, same outcome. |
| **`cmdline.txt` stays one line** | The Pi's firmware reads only the first line. A newline in the wrong place silently drops every argument after it, including `root=`, and the Pi will not boot. |

Doing it by hand instead is fine if you use an editor that shows and controls
line endings — Notepad++, VS Code, `nano` on any Linux box. Notepad on modern
Windows keeps existing LF endings but will not convert CRLF for you, and Word
will destroy the file. The PowerShell above is deterministic, which is why it
is the recommended path.

`systemd.run_failure_action=reboot` is in there deliberately: if the script
fails for any reason, the Pi still reboots rather than sitting at a target you
cannot see. And the script's first action, before anything that could fail, is
to strip its own argument back out of `cmdline.txt` — so the worst case is one
wasted boot, never a Pi that runs it for ever.

**3 — Boot it.** Eject the card properly, put it in the Pi, power on, and leave
it alone for **three minutes**. The sequence is: minimal boot, stage 1, an
automatic reboot, an ordinary boot, then stage 2 twenty seconds later. You will
see the ACT LED go quiet and then busy again as it reboots.

**4 — Read it.** Power off, card back in the PC, open `tv_report.txt` on the
boot partition.

**What the outcome means:**

| what you find | what it means | what to do |
|---|---|---|
| **No `tv_report.txt` at all** | The kernel never ran stage 1 — either it is not booting, or the script was unreadable (line endings, BOM). | Check `cmdline.txt` is intact and one line; then reflash and suspect the card or the write. |
| **`firstrun.sh IS STILL PRESENT`** | The Imager's customisation never completed. No user account, no hostname, and **no SSH** — no amount of network work would ever have helped. | Reflash, and watch for an error at first boot. |
| **Stage 1 only, no stage 2** | It boots minimally but never reaches multi-user. | Read the kernel log at the end of stage 1 — usually a filesystem or service failure. |
| **Stage 2 present, `ssh active: active`** | It boots and is listening. The problem is purely addressing. | Stage 2 lists every address it has; use one. |
| **`eth0 carrier=0`** | No link at all: cable, port, or PHY. | Nothing above that layer is worth debugging. Try another cable and another port. |
| **`throttled` non-zero** | Under-voltage, possibly during boot. Bit 16 is sticky and survives the event that set it. | A real 5 V / 5 A supply before anything else. |

**Afterwards**, the boot partition holds `tv_report.txt`, `cmdline.txt.bak` and
`cmdline.txt.tvbak`. None of them affect anything and all three can be deleted.
The stage-2 unit removes itself, so the next boot is completely untouched.

**Get a micro-HDMI cable.** Everything above is working around not being able to
see the machine. A £10 cable to any HDMI monitor turns every one of these
problems into thirty seconds of looking, permanently. It is the highest-value
item in this whole procedure and the easiest to keep putting off.

**Windows and `.local`.** Windows 10/11 has only partial mDNS support, and
`.local` resolution is unreliable without Apple's **Bonjour Print Services for
Windows** installed. If `ping flyeye.local` fails from Windows but the Pi is
plainly there, install Bonjour before concluding the Pi is at fault. macOS and
Linux resolve `.local` natively and are a useful second opinion.

**Three ways to avoid the whole problem next time**, in order of how well they
work:

| | what | why |
|---|---|---|
| 1 | **Enable USB gadget mode when you flash** — Imager → Edit Settings → Interfaces & Features | One USB-C cable, and the Pi *is* the DHCP server: it hands your PC an address and sits at `10.12.194.1`. No Ethernet, no DHCP server, no mDNS, nothing to resolve. The most reliable first contact there is. See the caveats below. |
| 2 | **Any cheap switch or travel router with DHCP** between PC and Pi | Both ends get real addresses in a known range, and the router's client list shows you the Pi's. Twenty dollars, permanently useful in a lab. |
| 3 | **Set the wifi credentials to your phone's hotspot** when you flash | The phone's client list shows the Pi's address. Works anywhere, needs no hardware. |

Once you are in, `bash scripts/setup_network.sh` makes all of this moot.

---

## Addressing — so the rig stops moving

The Pi takes its address from DHCP, and DHCP changes its mind: on a lease
expiry, a switch port change, a reboot after a power cut. `scripts/setup_network.sh`
installs three answers, and the reason for having all three is that they fail
independently.

| mechanism | address | needs | fails when |
|---|---|---|---|
| **mDNS** | `flyeye.local` | nothing — already on Pi OS | the network drops multicast between VLANs, which many enterprise networks do |
| **Fixed second address** | `192.168.50.10` | one `nmcli` command — **so it does not exist until the script has run once** | never, on a direct cable — it does not involve DHCP at all |
| **DHCP reservation** | whatever IT assigns | a ticket, and the MAC address | the Pi moves to a different network |

**mDNS** is the everyday path. Raspberry Pi OS runs `avahi-daemon`, so
`<hostname>.local` resolves from macOS, Windows 10 and later, and Linux with
avahi installed. The script also advertises the web UI as an `_http._tcp`
service, so the rig appears in network browsers rather than only answering when
asked by name.

**The fixed second address is the one that always works** — afterwards. It is
created by the script, so it is not available for first contact on a freshly
flashed card; see **If you cannot reach the Pi at all** above for that. It is
also worth understanding why it is safe. NetworkManager applies manually configured
addresses *in addition to* the DHCP lease as long as `ipv4.method` stays
`auto`. So the Pi keeps its normal network access and also always answers on
`192.168.50.10`. Give a laptop an address on that subnet, connect the two with
a single Ethernet cable, and the rig is reachable with no DHCP server, no
router and nobody's permission:

```bash
# on the laptop (Linux); Windows and macOS equivalents are in the script output
sudo ip addr add 192.168.50.20/24 dev enp0s31f6
curl http://192.168.50.10:8000/api/status
```

Setting `ipv4.method manual` instead would take the Pi *off* the network
entirely — and doing that over SSH is how you end up needing a monitor and a
keyboard. The script does not do it, and neither should you.

**A DHCP reservation** is the correct answer on a managed network. The script
prints the MAC addresses to send to IT.

**Whatever happens, the rig can be asked where it is.** The startup banner
lists every URL, and so does the status endpoint:

```bash
journalctl -u trilobite | grep -A6 'reachable at'
curl -s http://flyeye.local:8000/api/status | jq -r '.network.urls[]'
python -m trilobite.net          # on the Pi, without starting the app
```

### One USB-C cable

Raspberry Pi OS Trixie images dated 2025-10-20 and later ship `rpi-usb-gadget`,
which turns the Pi 5's USB-C port into a USB Ethernet device. One cable from a
laptop carries power and network, the Pi is always at **10.12.194.1**, and no
network administrator is involved at any point.

```bash
sudo apt install rpi-usb-gadget
sudo rpi-usb-gadget on
sudo reboot
```

Two caveats, and the first is the reason this is not the headline
recommendation for this rig:

- **Power.** That port becomes the Pi's only power input, and a Pi 5 driving
  two cameras and a USB SSD can draw more than a laptop USB-C port will supply.
  The failure mode is a reboot in the middle of a capture. Use a powered hub,
  or keep this for a Pi with nothing else attached.
- **It stops being a USB host port.** Networking and power only, once enabled.

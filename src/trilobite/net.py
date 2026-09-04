"""Where is this rig reachable?

A headless Pi on a university network gets its address from DHCP, and DHCP
changes its mind. The rig went missing once for exactly that reason, and
"serving on http://0.0.0.0:8000" -- the *bind* address, which is not somewhere
you can point a browser -- was no help at all.

So this enumerates every address the rig can actually be reached at and puts
them in the startup log and in `/api/status`. Two consequences worth having:
after a DHCP change one `journalctl -u trilobite | grep -A6 'serving on'` says
where it went, and a script can ask the rig itself with
`curl http://<host>/api/status | jq -r .urls[]`.

The durable fix is not to depend on the number at all -- see
`scripts/setup_network.sh` and the README's Pi setup section -- but the number
is still what you type when mDNS is blocked, so it should be easy to find.

No dependencies. `ip -j addr` is used where it exists (Linux, so the Pi) and a
socket trick is the fallback everywhere else, because the tests for this run on
a Windows desktop.
"""

from __future__ import annotations

import json
import logging
import shutil
import socket
import subprocess

log = logging.getLogger(__name__)

# Addresses that are real but useless to hand to someone.
_SKIP_PREFIXES = ("127.", "169.254.")


def parse_ip_json(payload: str) -> list[tuple[str, str]]:
    """Parse `ip -j -4 addr show` into [(interface, address), ...].

    Split out from the call so it can be tested against a captured sample
    rather than against whatever interfaces the test machine happens to have.
    """
    out: list[tuple[str, str]] = []
    try:
        entries = json.loads(payload)
    except (ValueError, TypeError):
        return out
    for entry in entries or []:
        name = str(entry.get("ifname") or "")
        for addr in entry.get("addr_info") or []:
            if addr.get("family") != "inet":
                continue
            ip = str(addr.get("local") or "")
            if ip and not ip.startswith("127."):
                out.append((name, ip))
    return out


def interface_addresses() -> list[tuple[str, str]]:
    """Every non-loopback IPv4 address, as (interface, address)."""
    ip_bin = shutil.which("ip")
    if ip_bin:
        try:
            res = subprocess.run(                     # noqa: S603 - fixed argv
                [ip_bin, "-j", "-4", "addr", "show"],
                capture_output=True, text=True, timeout=5, check=False,
            )
            if res.returncode == 0:
                found = parse_ip_json(res.stdout)
                if found:
                    return found
        except (OSError, subprocess.SubprocessError) as exc:
            log.debug("ip addr failed: %s", exc)

    # Fallback: ask the routing table which source address would be used to
    # reach the outside world. No packet is sent -- connect() on a UDP socket
    # only fixes the local end -- so this works with no network too, returning
    # nothing rather than hanging.
    addr = _route_source()
    return [("", addr)] if addr else []


def _route_source(target: str = "192.0.2.1") -> str | None:
    """The local address the kernel would use to reach `target`.

    192.0.2.0/24 is TEST-NET-1: reserved, unrouteable, and guaranteed not to
    be something real that a stray packet could disturb.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.settimeout(0.2)
        s.connect((target, 9))
        return str(s.getsockname()[0])
    except OSError:
        return None
    finally:
        s.close()


def mdns_name() -> str | None:
    """`<hostname>.local`, if this host is likely to answer to it.

    Raspberry Pi OS runs avahi-daemon by default, so the name almost always
    works from a Mac, from Windows 10 and later, and from Linux with avahi
    installed. It is the address that survives a DHCP change, so it leads the
    list -- but many enterprise networks drop multicast between VLANs, which is
    why it is offered rather than relied on.
    """
    host = socket.gethostname().split(".")[0]
    return f"{host}.local" if host and host != "localhost" else None


def service_urls(port: int, bound: str = "0.0.0.0", scheme: str = "http") -> list[str]:
    """Every URL this rig answers on, best first.

    `bound` is what uvicorn was told to listen on. If that is a single real
    address rather than a wildcard, it is the only answer -- reporting the
    others would be a lie.
    """
    if bound not in ("0.0.0.0", "::", ""):
        return [f"{scheme}://{bound}:{port}/"]

    urls: list[str] = []
    name = mdns_name()
    if name:
        urls.append(f"{scheme}://{name}:{port}/")
    for _iface, ip in interface_addresses():
        if ip.startswith(_SKIP_PREFIXES):
            continue
        url = f"{scheme}://{ip}:{port}/"
        if url not in urls:
            urls.append(url)
    urls.append(f"{scheme}://127.0.0.1:{port}/")
    return urls


def describe(port: int, bound: str = "0.0.0.0") -> dict:
    """The whole picture, for `/api/status` and the startup banner."""
    return {
        "hostname": socket.gethostname(),
        "mdns": mdns_name(),
        "bound": f"{bound}:{port}",
        "interfaces": [{"interface": i, "address": a} for i, a in interface_addresses()],
        "urls": service_urls(port, bound),
    }


def banner(port: int, bound: str = "0.0.0.0") -> str:
    """The startup message. Multi-line on purpose: this is the thing someone
    scrolls back to find when the rig has moved."""
    info = describe(port, bound)
    lines = [f"serving on {info['bound']} -- reachable at:"]
    for url in info["urls"]:
        note = ""
        if info["mdns"] and info["mdns"] in url:
            note = "   <- survives a DHCP change, if mDNS is not blocked"
        elif "10.12.194.1" in url:
            note = "   <- USB-C gadget link"
        elif "127.0.0.1" in url:
            note = "   <- this Pi only"
        lines.append(f"    {url}{note}")
    return "\n".join(lines)


if __name__ == "__main__":                            # python -m trilobite.net
    print(banner(8000))

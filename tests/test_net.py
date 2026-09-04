"""Where the rig says it is reachable.

Small, but it earns its place: the rig went missing once after a DHCP change,
and the startup line said `serving on http://0.0.0.0:8000` -- the bind address,
which is not somewhere a browser can go. These tests pin the parsing and the
URL ordering against a captured sample, so they say the same thing on a Windows
desktop with no `ip` command as they do on the Pi.
"""

from __future__ import annotations

from trilobite import net

# Real output of `ip -j -4 addr show` on a Pi 5: wired lease, the fixed second
# address setup_network.sh adds, wifi, and the USB-C gadget link.
PI_SAMPLE = """[
 {"ifindex":1,"ifname":"lo","addr_info":[
   {"family":"inet","local":"127.0.0.1","prefixlen":8}]},
 {"ifindex":2,"ifname":"eth0","addr_info":[
   {"family":"inet","local":"10.44.12.87","prefixlen":22},
   {"family":"inet","local":"192.168.50.10","prefixlen":24}]},
 {"ifindex":3,"ifname":"wlan0","addr_info":[
   {"family":"inet","local":"10.44.30.5","prefixlen":22}]},
 {"ifindex":4,"ifname":"usb0","addr_info":[
   {"family":"inet","local":"10.12.194.1","prefixlen":28}]}
]"""


def test_parses_every_ipv4_address_and_drops_loopback():
    got = net.parse_ip_json(PI_SAMPLE)
    assert ("eth0", "10.44.12.87") in got
    assert ("eth0", "192.168.50.10") in got, "a second address on one interface must survive"
    assert ("wlan0", "10.44.30.5") in got
    assert ("usb0", "10.12.194.1") in got
    assert not any(ip.startswith("127.") for _, ip in got)


def test_malformed_output_yields_nothing_rather_than_raising():
    """Called on every /api/status. A parse failure must degrade to "I don't
    know", never take the status endpoint down with it."""
    assert net.parse_ip_json("") == []
    assert net.parse_ip_json("not json") == []
    assert net.parse_ip_json("null") == []
    assert net.parse_ip_json('[{"ifname":"eth0"}]') == []


def test_urls_lead_with_the_name_that_survives_dhcp(monkeypatch):
    monkeypatch.setattr(net, "mdns_name", lambda: "flyeye.local")
    monkeypatch.setattr(net, "interface_addresses",
                        lambda: net.parse_ip_json(PI_SAMPLE))
    urls = net.service_urls(8000)

    assert urls[0] == "http://flyeye.local:8000/", urls
    assert "http://192.168.50.10:8000/" in urls
    assert "http://10.12.194.1:8000/" in urls
    assert urls[-1] == "http://127.0.0.1:8000/", "loopback is the least useful, so last"


def test_link_local_addresses_are_not_offered(monkeypatch):
    """169.254.x.x is what an interface gets when DHCP failed. Real, and not
    something to hand to someone as an address to type."""
    monkeypatch.setattr(net, "mdns_name", lambda: None)
    monkeypatch.setattr(net, "interface_addresses",
                        lambda: [("eth0", "169.254.7.9"), ("eth0", "10.0.0.5")])
    urls = net.service_urls(8000)
    assert not any("169.254" in u for u in urls)
    assert "http://10.0.0.5:8000/" in urls


def test_a_specific_bind_address_is_reported_verbatim(monkeypatch):
    """Bound to one interface, the others are not reachable, and listing them
    would be a lie."""
    monkeypatch.setattr(net, "interface_addresses",
                        lambda: net.parse_ip_json(PI_SAMPLE))
    assert net.service_urls(8000, bound="127.0.0.1") == ["http://127.0.0.1:8000/"]
    assert net.service_urls(9000, bound="10.44.12.87") == ["http://10.44.12.87:9000/"]


def test_the_banner_names_what_each_address_is_for(monkeypatch):
    monkeypatch.setattr(net, "mdns_name", lambda: "flyeye.local")
    monkeypatch.setattr(net, "interface_addresses",
                        lambda: net.parse_ip_json(PI_SAMPLE))
    text = net.banner(8000)
    assert "flyeye.local" in text
    assert "survives a DHCP change" in text
    assert "USB-C gadget link" in text
    assert "this Pi only" in text


def test_describe_is_json_safe():
    """It goes straight into /api/status, so every value has to serialise."""
    import json

    json.dumps(net.describe(8000))


def test_status_carries_the_network_block(tmp_path):
    """The point of the whole module: a rig that moved can be asked where it
    went, instead of scanned for."""
    from trilobite.app import Application
    from trilobite.config import AppConfig, CameraConfig, StorageConfig

    app = Application(AppConfig(
        cameras=[CameraConfig(cam_id="left", backend="synthetic", fps=1000)],
        storage=StorageConfig(root=str(tmp_path / "d")),
    ))
    n = app.status()["network"]
    assert "urls" in n and n["urls"]
    assert "hostname" in n
    assert n["bound"].endswith(":8000")

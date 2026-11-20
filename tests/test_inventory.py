from netbox_netscaler.inventory import ENDPOINTS, build_inventory, fetch_pair
from netbox_netscaler.nitro import NitroError

FIXTURE_FOR = {
    "config/lbvserver": "lbvserver.json",
    "config/lbvserver_servicegroupmember_binding?bulkbindings=yes": "lbvserver_sgmember_binding.json",
    "config/servicegroup_servicegroupmember_binding?bulkbindings=yes": "sg_member_binding.json",
    "config/lbvserver_service_binding?bulkbindings=yes": "lbvserver_service_binding_empty.json",
    "config/gslbvserver": "gslbvserver.json",
    "config/gslbvserver_domain_binding?bulkbindings=yes": "gslb_domain_binding.json",
    "config/gslbvserver_gslbservice_binding?bulkbindings=yes": "gslb_service_binding.json",
}


def _raw(fixture):
    return {path: fixture(name) for path, name in FIXTURE_FOR.items()}


def _vserver(inv, name):
    return next(v for v in inv.vservers if v.name == name)


def test_endpoints_cover_all_fixtures():
    assert set(ENDPOINTS) == set(FIXTURE_FOR)


def test_build_inventory_attaches_members(fixture):
    inv = build_inventory("Pair A", "ns-a1.example.com", _raw(fixture))
    app1 = _vserver(inv, "app1_443_vsvr")
    assert (app1.ip, app1.port, app1.protocol, app1.state, app1.pair) == ("192.0.2.10", 443, "SSL", "UP", "Pair A")
    assert [(m.ip, m.state, m.servername, m.servicegroup) for m in app1.members] == [
        ("198.51.100.11", "UP", "app1a.example.com", "app1_443_sg"),
        ("198.51.100.12", "DOWN", "app1b.example.com", "app1_443_sg"),
    ]
    assert inv.available is True and inv.node == "ns-a1.example.com" and inv.error is None


def test_empty_binding_without_list_key(fixture):
    inv = build_inventory("Pair A", "ns-a1.example.com", _raw(fixture))
    assert _vserver(inv, "empty_80_vsvr").members == []


def test_gslb_targets_parsed(fixture):
    inv = build_inventory("Pair A", "ns-a1.example.com", _raw(fixture))
    [gslb] = inv.gslb
    assert gslb.vserver == "app1-gslb"
    assert gslb.domains == ["app1.example.com"]
    assert gslb.service_targets == [("192.0.2.10", 443)]
    assert gslb.state == "UP"


def test_fetch_pair_unavailable_on_error():
    def factory(host, *args, **kwargs):
        class _C:
            def get(self, path):
                raise NitroError(f"{host}: refused")

        return _C()

    inv = fetch_pair({"name": "Pair A", "nodes": ["ns-a1.example.com"]}, {"username": "u", "password": "p"}, factory)
    assert inv.available is False
    assert "Pair A" in inv.error
    assert inv.vservers == [] and inv.gslb == []


def test_fetch_pair_uses_pair_credentials_override(fixture):
    seen = []

    def factory(host, username, password, timeout, verify_ssl):
        seen.append((host, username, password, timeout, verify_ssl))

        class _C:
            def get(self, path):
                if path == "config/hanode":
                    return fixture("hanode_primary.json")
                return fixture(FIXTURE_FOR[path])

        return _C()

    pair = {"name": "Pair A", "nodes": ["ns-a1.example.com"], "username": "pairuser", "password": "pairpw"}
    inv = fetch_pair(pair, {"username": "u", "password": "p", "timeout": 9, "verify_ssl": False}, factory)
    assert inv.available is True and len(inv.vservers) == 4
    assert all(s == ("ns-a1.example.com", "pairuser", "pairpw", 9, False) for s in seen)


def _factory_with(fixture, overrides):
    def factory(host, *args):
        class _C:
            def get(self, path):
                if path in overrides:
                    value = overrides[path]
                    if isinstance(value, Exception):
                        raise value
                    return value
                if path == "config/hanode":
                    return fixture("hanode_primary.json")
                return fixture(FIXTURE_FOR[path])

        return _C()

    return factory


def test_gslb_endpoint_refused_keeps_lb_data(fixture):
    """A box without GSLB may refuse the GSLB endpoints; the lb VIPs must still show."""
    factory = _factory_with(fixture, {"config/gslbvserver": NitroError("ns-a1.example.com: errorcode 257")})
    inv = fetch_pair({"name": "Pair A", "nodes": ["ns-a1.example.com"]}, {}, factory)
    assert inv.available is True
    assert len(inv.vservers) == 4 and inv.gslb == []
    assert "gslbvserver" in inv.warning


def test_fetch_pair_never_raises_on_malformed_response(fixture):
    factory = _factory_with(fixture, {"config/lbvserver": {"errorcode": 0, "lbvserver": [{"ipv46": "192.0.2.1"}]}})
    inv = fetch_pair({"name": "Pair A", "nodes": ["ns-a1.example.com"]}, {}, factory)
    assert inv.available is False
    assert "Pair A" in inv.error

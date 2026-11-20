from datetime import datetime, timezone

import pytest

from netbox_netscaler.inventory import GslbName, Member, PairInventory, Vserver
from netbox_netscaler.matching import Match, gslb_for, is_name_mismatch, memberships_for_ips, stale_vservers

NOW = datetime(2026, 11, 20, tzinfo=timezone.utc)


def _match(key, name, status="active", dns=""):
    return Match(obj_key=key, obj_name=name, obj_url=f"/x/{key}/", status=status, ip_dns_name=dns)


def _vs(name, ip, port, members, pair="Pair A", state="UP"):
    return Vserver(pair=pair, name=name, ip=ip, port=port, protocol="SSL", state=state, members=members)


def _m(ip, state, servername="", sg="sg"):
    return Member(servicegroup=sg, ip=ip, port=443, state=state, servername=servername)


@pytest.fixture
def inventories():
    a = PairInventory(
        name="Pair A",
        available=True,
        node="ns-a1.example.com",
        error=None,
        fetched_at=NOW,
        vservers=[
            _vs("app1_443_vsvr", "192.0.2.10", 443, [_m("198.51.100.11", "UP", "app1a.example.com"), _m("198.51.100.12", "DOWN", "app1b.example.com")]),
            _vs("dead_443_vsvr", "192.0.2.20", 443, [_m("198.51.100.60", "DOWN", "wrongname.example.com")], state="DOWN"),
            _vs("empty_80_vsvr", "192.0.2.30", 80, [], state="DOWN"),
            _vs("dual_443_vsvr", "192.0.2.40", 443, [_m("198.51.100.21", "UP"), _m("198.51.100.22", "UP")]),
            _vs("retired_443_vsvr", "192.0.2.50", 443, [_m("198.51.100.70", "UP")]),
            _vs("orphan_443_vsvr", "192.0.2.60", 443, [_m("198.51.100.99", "DOWN")], state="DOWN"),
        ],
        gslb=[],
    )
    b = PairInventory(
        name="Pair B",
        available=True,
        node="ns-b1.example.com",
        error=None,
        fetched_at=NOW,
        vservers=[],
        gslb=[GslbName(pair="Pair B", vserver="app1-gslb", domains=["app1.example.com"], state="UP", service_targets=[("192.0.2.10", 443)])],
    )
    return [a, b]


RESOLVER_TABLE = {
    "198.51.100.11": [_match("vm:1", "app1a")],
    "198.51.100.12": [_match("vm:2", "app1b")],
    "198.51.100.60": [_match("vm:3", "labcuic", status="decommissioning")],
    "198.51.100.21": [_match("vm:7", "dual")],
    "198.51.100.22": [_match("vm:7", "dual")],
    "198.51.100.70": [_match("vm:8", "oldbox", status="decommissioning")],
}


def resolve(ip):
    return RESOLVER_TABLE.get(ip, [])


def _only(memberships, vserver):
    return [m for m in memberships if m.vserver.name == vserver]


def test_memberships_found_by_ip(inventories):
    [m] = memberships_for_ips({"198.51.100.11"}, "vm:1", inventories, resolve)
    assert m.vserver.name == "app1_443_vsvr"
    assert m.member.ip == "198.51.100.11"
    assert m.last_live is True
    assert m.ambiguous is False
    assert [g.vserver for g in m.gslb] == ["app1-gslb"]


def test_last_live_false_when_other_member_up(inventories):
    [m] = memberships_for_ips({"198.51.100.12"}, "vm:2", inventories, resolve)
    assert m.last_live is False


def test_ambiguous_ip_single_row(inventories):
    table = dict(RESOLVER_TABLE)
    table["198.51.100.11"] = [_match("vm:1", "app1a"), _match("vm:1", "app1a")]
    memberships = memberships_for_ips({"198.51.100.11"}, "vm:1", inventories, lambda ip: table.get(ip, []))
    assert len(memberships) == 1
    assert memberships[0].ambiguous is True
    assert len(memberships[0].matches) == 2


def test_same_object_two_ips_last_live(inventories):
    memberships = memberships_for_ips({"198.51.100.21", "198.51.100.22"}, "vm:7", inventories, resolve)
    assert len(memberships) == 2
    assert all(m.last_live for m in memberships)


def test_name_mismatch_flagged(inventories):
    [m] = memberships_for_ips({"198.51.100.60"}, "vm:3", inventories, resolve)
    assert m.name_mismatch is True
    assert is_name_mismatch("labcuic.example.com", [_match("vm:3", "labcuic")]) is False
    assert is_name_mismatch("other.example.com", [_match("vm:3", "labcuic", dns="other.example.com")]) is False
    assert is_name_mismatch("anything.example.com", []) is False


def test_stale_reasons(inventories):
    rows = stale_vservers(inventories, resolve)
    reasons = {r.vserver.name: r.reason for r in rows}
    assert reasons == {
        "dead_443_vsvr": "all members down",
        "empty_80_vsvr": "no members",
        "retired_443_vsvr": "members retired",
        "orphan_443_vsvr": "all members down",
    }
    assert [r.vserver.name for r in rows] == sorted(reasons)
    dead = next(r for r in rows if r.vserver.name == "dead_443_vsvr")
    assert dead.name_mismatch is True


def test_stale_member_not_in_netbox():
    inv = PairInventory(
        name="Pair A",
        available=True,
        node="n",
        error=None,
        fetched_at=NOW,
        vservers=[_vs("ghost_443_vsvr", "192.0.2.70", 443, [_m("198.51.100.98", "UP"), _m("198.51.100.99", "DOWN")])],
    )
    table = {"198.51.100.98": [_match("vm:9", "gone", status="offline")]}
    [row] = stale_vservers([inv], lambda ip: table.get(ip, []))
    assert row.reason == "member not in NetBox"


def test_gslb_linked_across_pairs(inventories):
    app1 = inventories[0].vservers[0]
    assert [g.vserver for g in gslb_for(app1, inventories)] == ["app1-gslb"]
    assert gslb_for(inventories[0].vservers[1], inventories) == []


def test_unavailable_pairs_skipped(inventories):
    inventories[0].available = False
    assert memberships_for_ips({"198.51.100.11"}, "vm:1", inventories, resolve) == []
    assert stale_vservers(inventories, resolve) == []

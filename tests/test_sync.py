from datetime import datetime, timezone

from netbox_netscaler.inventory import GslbName, Member, PairInventory, Vserver
from netbox_netscaler.sync import NbIp, Target, plan_sync

NOW = datetime(2026, 11, 20, tzinfo=timezone.utc)
TARGETS = {"Pair A": Target(interface_id=9, vm_name="ns-a2")}


def _vs(name, ip, state="UP", port=443):
    return Vserver(
        pair="Pair A",
        name=name,
        ip=ip,
        port=port,
        protocol="SSL",
        state=state,
        members=[Member("sg", "198.51.100.1", port, state, "")],
    )


def _inv(*vservers, gslb=(), available=True, name="Pair A"):
    return PairInventory(
        name=name,
        available=available,
        node="ns-a2.example.com" if available else None,
        error=None,
        fetched_at=NOW,
        vservers=list(vservers),
        gslb=list(gslb),
    )


def _ip(pk, host, role="vip", status="active", description="", dns="", iface=9):
    return NbIp(
        pk=pk,
        host=host,
        address=f"{host}/32",
        role=role,
        status=status,
        description=description,
        dns_name=dns,
        interface_id=iface,
    )


def test_create_missing_up_vip():
    plan = plan_sync([_inv(_vs("app1_443_vsvr", "192.0.2.10"))], [], TARGETS)
    [create] = plan.creates
    assert (create.host, create.status, create.interface_id, create.vm_name) == ("192.0.2.10", "active", 9, "ns-a2")
    assert create.description == "NetScaler VIP - SSL:443 - UP [app1_443_vsvr]"
    assert plan.updates == [] and plan.conflicts == []


def test_no_create_for_down_or_zero_or_backup():
    inv = _inv(
        _vs("down_443_vsvr", "192.0.2.20", state="DOWN"),
        _vs("zero_443_vsvr", "0.0.0.0"),
        _vs("x_443_backup_vsvr", "192.0.2.21"),
    )
    assert plan_sync([inv], [], TARGETS).creates == []


def test_existing_any_role_blocks_create():
    plan = plan_sync([_inv(_vs("app1_443_vsvr", "192.0.2.10"))], [_ip(1, "192.0.2.10", role="")], TARGETS)
    assert plan.creates == []
    assert [(ip.pk, d.host) for ip, d in plan.conflicts] == [(1, "192.0.2.10")]


def test_update_status_and_description():
    old = "NetScaler VIP - SSL:443 - DOWN [app1_443_vsvr]"
    plan = plan_sync(
        [_inv(_vs("app1_443_vsvr", "192.0.2.10"))], [_ip(1, "192.0.2.10", status="reserved", description=old)], TARGETS
    )
    [change] = plan.updates
    assert change.fields["status"] == ("reserved", "active")
    assert change.fields["description"] == (old, "NetScaler VIP - SSL:443 - UP [app1_443_vsvr]")


def test_update_never_blanks_dns():
    desc = "NetScaler VIP - SSL:443 - UP [app1_443_vsvr]"
    plan = plan_sync(
        [_inv(_vs("app1_443_vsvr", "192.0.2.10"))],
        [_ip(1, "192.0.2.10", description=desc, dns="x.example.com")],
        TARGETS,
    )
    assert plan.updates == []


def test_update_sets_dns_from_gslb():
    desc = "NetScaler VIP - SSL:443 - UP [app1_443_vsvr]"
    gslb = [GslbName("Pair A", "g", ["b.example.com", "a.example.com"], "UP", [("192.0.2.10", 443)])]
    plan = plan_sync(
        [_inv(_vs("app1_443_vsvr", "192.0.2.10"), gslb=gslb)], [_ip(1, "192.0.2.10", description=desc)], TARGETS
    )
    [change] = plan.updates
    assert change.fields == {"dns_name": ("", "a.example.com")}


def test_update_moves_to_primary():
    desc = "NetScaler VIP - SSL:443 - UP [app1_443_vsvr]"
    record = _ip(1, "192.0.2.10", description=desc, iface=5)
    [change] = plan_sync([_inv(_vs("app1_443_vsvr", "192.0.2.10"))], [record], TARGETS).updates
    assert change.fields == {"interface_id": (5, 9)}
    no_target = {"Pair A": Target(interface_id=None, vm_name=None)}
    assert plan_sync([_inv(_vs("app1_443_vsvr", "192.0.2.10"))], [record], no_target).updates == []


def test_priority_up_wins():
    inv = _inv(_vs("a_80_vsvr", "192.0.2.10", state="DOWN", port=80), _vs("b_443_vsvr", "192.0.2.10"))
    [create] = plan_sync([inv], [], TARGETS).creates
    assert create.status == "active"
    assert create.vserver == "b_443_vsvr"


def test_orphan_listed():
    inv = _inv(_vs("app1_443_vsvr", "192.0.2.10"), _vs("x_443_backup_vsvr", "192.0.2.21"))
    records = [_ip(1, "192.0.2.99"), _ip(2, "192.0.2.98", status="deprecated"), _ip(3, "192.0.2.21")]
    plan = plan_sync([inv], records, TARGETS)
    assert [ip.pk for ip in plan.orphans] == [1]
    assert plan.orphans_suppressed is False


def test_orphans_suppressed_when_pair_down():
    plan = plan_sync(
        [_inv(_vs("app1_443_vsvr", "192.0.2.10")), _inv(available=False, name="Pair B")],
        [_ip(1, "192.0.2.99")],
        TARGETS,
    )
    assert plan.orphans == [] and plan.orphans_suppressed is True


def test_fingerprint_changes_with_record():
    inv = [_inv(_vs("app1_443_vsvr", "192.0.2.10"))]
    a = plan_sync(inv, [_ip(1, "192.0.2.10", status="reserved")], TARGETS).updates[0]
    b = plan_sync(inv, [_ip(1, "192.0.2.10", status="deprecated")], TARGETS).updates[0]
    assert a.fingerprint != b.fingerprint
    assert a.fingerprint == plan_sync(inv, [_ip(1, "192.0.2.10", status="reserved")], TARGETS).updates[0].fingerprint


def test_orphan_record_has_fingerprint():
    record = _ip(1, "192.0.2.99")
    [orphan] = plan_sync([_inv(_vs("app1_443_vsvr", "192.0.2.10"))], [record], TARGETS).orphans
    assert orphan.fingerprint and orphan.fingerprint == _ip(1, "192.0.2.99").fingerprint


def test_update_keeps_existing_dns_even_if_gslb_differs():
    """Existing DNS names are usually the user-facing name; GSLB zone names (e.g. *.gslb...) must not replace them."""
    desc = "NetScaler VIP - SSL:443 - UP [app1_443_vsvr]"
    gslb = [GslbName("Pair A", "g", ["app1.gslb.example.com"], "UP", [("192.0.2.10", 443)])]
    record = _ip(1, "192.0.2.10", description=desc, dns="app1.example.com")
    assert plan_sync([_inv(_vs("app1_443_vsvr", "192.0.2.10"), gslb=gslb)], [record], TARGETS).updates == []

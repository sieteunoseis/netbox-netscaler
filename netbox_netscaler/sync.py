"""Pure VIP sync planner: what NetBox IPAM should change to match NetScaler.

Replaces the old netscaler_vip_sync.py script. NetScaler is only read; the plan is applied to NetBox by sync_netbox.
"""

import hashlib
from dataclasses import dataclass, field
from typing import Callable

from .inventory import PairInventory
from .matching import gslb_for

ORPHAN_DESCRIPTION_PREFIX = "NetScaler VIP - "  # written by this plugin and by the old script

STATUS_FOR = {"UP": "active", "OUT OF SERVICE": "deprecated", "DOWN": "reserved"}
PRIORITY = {"UP": 3, "OUT OF SERVICE": 2, "DOWN": 1}


@dataclass
class NbIp:
    pk: int
    host: str
    address: str
    role: str
    status: str
    description: str
    dns_name: str
    interface_id: int | None
    locked: bool = False  # parent's primary/OOB IP: NetBox refuses to reassign it

    @property
    def fingerprint(self) -> str:
        return fingerprint(self)


@dataclass
class Target:
    interface_id: int | None
    vm_name: str | None


@dataclass
class Desired:
    host: str
    pair: str
    node: str | None
    vserver: str
    status: str
    description: str
    dns_name: str
    interface_id: int | None
    vm_name: str | None

    @property
    def fingerprint(self) -> str:
        return _hash(
            self.host,
            self.pair,
            self.node,
            self.vserver,
            self.status,
            self.description,
            self.dns_name,
            self.interface_id,
        )


@dataclass
class Change:
    ip: NbIp
    desired: Desired
    fields: dict[str, tuple]

    @property
    def fingerprint(self) -> str:
        """Covers the record AND the planned change, so Apply never writes values the preview didn't show."""
        return _hash(fingerprint(self.ip), sorted(self.fields.items()))


@dataclass
class SyncPlan:
    creates: list[Desired] = field(default_factory=list)
    updates: list[Change] = field(default_factory=list)
    conflicts: list[tuple[NbIp, Desired]] = field(default_factory=list)
    orphans: list[NbIp] = field(default_factory=list)
    orphans_suppressed: bool = False


def _hash(*values) -> str:
    return hashlib.sha256("|".join(repr(v) for v in values).encode()).hexdigest()[:16]


def fingerprint(ip: NbIp) -> str:
    """Stable hash of a record's current values, to detect changes between preview and apply."""
    return _hash(ip.pk, ip.address, ip.role, ip.status, ip.description, ip.dns_name, ip.interface_id, ip.locked)


def _syncable(vserver) -> bool:
    return bool(vserver.ip) and vserver.ip != "0.0.0.0" and not vserver.name.endswith("_backup_vsvr")


def desired_vips(inventories: list[PairInventory], targets: dict[str, Target]) -> dict[str, Desired]:
    """One desired IPAM record per VIP host, from the highest-priority vserver at that host."""
    by_host: dict[str, list] = {}
    for inv in inventories:
        if not inv.available:
            continue
        for vserver in inv.vservers:
            if _syncable(vserver):
                by_host.setdefault(vserver.ip, []).append((inv, vserver))

    desired = {}
    for host, entries in by_host.items():
        inv, best = sorted(entries, key=lambda e: (-PRIORITY.get(e[1].state, 0), e[1].name))[0]
        domains = sorted({d for _, v in entries for g in gslb_for(v, inventories) for d in g.domains if d})
        target = targets.get(inv.name) or Target(interface_id=None, vm_name=None)
        desired[host] = Desired(
            host=host,
            pair=inv.name,
            node=inv.node,
            vserver=best.name,
            status=STATUS_FOR.get(best.state, "reserved"),
            description=f"NetScaler VIP - {best.protocol}:{best.port} - {best.state} [{best.name}]",
            dns_name=domains[0] if domains else "",
            interface_id=target.interface_id,
            vm_name=target.vm_name,
        )
    return desired


def plan_sync(inventories: list[PairInventory], records: list[NbIp], targets: dict[str, Target]) -> SyncPlan:
    desired = desired_vips(inventories, targets)
    plan = SyncPlan()
    hosts_with_records = {r.host for r in records}

    for host, want in sorted(desired.items()):
        if want.status == "active" and host not in hosts_with_records:
            plan.creates.append(want)

    for record in sorted(records, key=lambda r: (r.host, r.pk)):
        want = desired.get(record.host)
        if want is None:
            continue
        if record.role != "vip":
            plan.conflicts.append((record, want))
            continue
        fields = {}
        if record.status != want.status:
            fields["status"] = (record.status, want.status)
        if record.description != want.description:
            fields["description"] = (record.description, want.description)
        if want.dns_name and not record.dns_name:  # fill blanks only; never replace a curated name
            fields["dns_name"] = (record.dns_name, want.dns_name)
        if want.interface_id is not None and record.interface_id != want.interface_id and not record.locked:
            fields["interface_id"] = (record.interface_id, want.interface_id)
        if fields:
            plan.updates.append(Change(ip=record, desired=want, fields=fields))

    # A pair that is down, or "up" but returned no vservers, would make all its VIPs look orphaned.
    if any(not inv.available or not inv.vservers for inv in inventories):
        plan.orphans_suppressed = True
    else:
        known = {v.ip for inv in inventories for v in inv.vservers if v.ip}
        plan.orphans = [
            r
            for r in sorted(records, key=lambda r: (r.host, r.pk))
            if r.role == "vip"
            and r.host not in known
            and r.status != "deprecated"
            and r.description.startswith(ORPHAN_DESCRIPTION_PREFIX)
        ]
    return plan


@dataclass
class Action:
    key: str
    kind: str  # create | update | orphan
    row: object  # Desired | Change | NbIp


def plan_rows(plan: SyncPlan) -> dict[str, object]:
    rows = {f"create:{d.host}": d for d in plan.creates}
    rows.update({f"update:{c.ip.pk}": c for c in plan.updates})
    rows.update({f"orphan:{ip.pk}": ip for ip in plan.orphans})
    return rows


def select_actions(
    plan: SyncPlan, selected: set[str], fingerprints: dict[str, str], changeable_pks: set[int]
) -> tuple[list[Action], list[tuple[str, str]]]:
    """Which selected rows may run: still in the plan, unchanged since preview, and changeable by this user."""
    rows = plan_rows(plan)
    actions, skipped = [], []
    for key in sorted(selected):
        row = rows.get(key)
        if row is None:
            skipped.append((key, "no longer in the plan"))
            continue
        if fingerprints.get(key) != row.fingerprint:
            skipped.append((key, "changed since preview"))
            continue
        kind = key.split(":", 1)[0]
        if kind != "create":
            pk = row.ip.pk if kind == "update" else row.pk
            if pk not in changeable_pks:
                skipped.append((key, "no permission to change this IP address"))
                continue
        actions.append(Action(key=key, kind=kind, row=row))
    return actions, skipped


def run_actions(actions: list[Action], execute: Callable[[Action], str]) -> dict:
    """Run each action; a failing row is reported and skipped, the others still apply."""
    bucket = {"create": "created", "update": "updated", "orphan": "deprecated"}
    result = {"created": [], "updated": [], "deprecated": [], "skipped": []}
    for action in actions:
        try:
            label = execute(action)
        except Exception as exc:  # validation/permission/DoesNotExist: report, don't 500 the batch
            messages = getattr(exc, "messages", None)
            result["skipped"].append((action.key, "; ".join(messages) if messages else str(exc)))
            continue
        if label is None:
            continue
        result[bucket[action.kind]].append(label)
    return result

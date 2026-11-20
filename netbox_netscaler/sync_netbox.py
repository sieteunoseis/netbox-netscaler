"""Apply a VIP sync plan to NetBox IPAM. NetScaler is never written to.

Which rows run is decided by the pure `sync.select_actions`; each row runs in its own savepoint via
`sync.run_actions`, so one invalid row is reported and the rest still apply.
"""

from functools import reduce
from operator import or_

from dcim.models import Device
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q
from extras.models import JournalEntry
from ipam.models import IPAddress
from tenancy.models import Tenant
from virtualization.models import VirtualMachine, VMInterface

from .cache import get_inventories, plugin_settings
from .sync import Action, NbIp, SyncPlan, Target, plan_rows, plan_sync, run_actions, select_actions

__all__ = ["apply", "build_plan", "load_records", "load_targets", "plan_rows"]


def _locked_ip_ids() -> set[int]:
    """IPs designated primary/OOB on their parent: NetBox refuses to reassign these."""
    ids = set()
    for model, fields in (
        (VirtualMachine, ("primary_ip4_id", "primary_ip6_id")),
        (Device, ("primary_ip4_id", "primary_ip6_id", "oob_ip_id")),
    ):
        for row in model.objects.values_list(*fields):
            ids.update(i for i in row if i)
    return ids


def _nbip(address: IPAddress, vmi_type_id: int, locked: set[int]) -> NbIp:
    return NbIp(
        pk=address.pk,
        host=str(address.address.ip),
        address=str(address.address),
        role=address.role or "",
        status=address.status,
        description=address.description or "",
        dns_name=address.dns_name or "",
        interface_id=address.assigned_object_id if address.assigned_object_type_id == vmi_type_id else None,
        locked=address.pk in locked,
    )


def load_records(hosts: set[str]) -> list[NbIp]:
    """Every IPAddress at the given hosts, plus every role=vip record (for orphan detection)."""
    vmi_type_id = ContentType.objects.get_for_model(VMInterface).pk
    locked = _locked_ip_ids()
    query = Q(role="vip")
    if hosts:
        query |= reduce(or_, (Q(address__net_host=h) for h in hosts))
    return [_nbip(a, vmi_type_id, locked) for a in IPAddress.objects.filter(query)]


def load_targets(inventories) -> dict[str, Target]:
    """Per pair: the NetBox VM named after the current Primary's short hostname, and its first interface."""
    targets = {}
    for inv in inventories:
        if not inv.available or not inv.node:
            continue
        vm = VirtualMachine.objects.filter(name__iexact=inv.node.split(".")[0]).first()
        iface = VMInterface.objects.filter(virtual_machine=vm).order_by("pk").first() if vm else None
        targets[inv.name] = Target(interface_id=iface.pk if iface else None, vm_name=vm.name if vm else None)
    return targets


def build_plan(inventories=None) -> SyncPlan:
    inventories = get_inventories() if inventories is None else inventories
    hosts = {v.ip for inv in inventories if inv.available for v in inv.vservers if v.ip and v.ip != "0.0.0.0"}
    return plan_sync(inventories, load_records(hosts), load_targets(inventories))


def _journal(address: IPAddress, user, text: str) -> None:
    if user.has_perm("extras.add_journalentry"):
        JournalEntry.objects.create(assigned_object=address, created_by=user, kind="info", comments=text)


def apply(plan: SyncPlan, selected: set[str], fingerprints: dict[str, str], user) -> dict:
    """Apply the selected rows of a freshly built plan, honouring object-level permissions."""
    slug = plugin_settings().get("vip_tenant") or ""
    tenant = Tenant.objects.filter(slug=slug).first() if slug else None
    changeable = set(IPAddress.objects.restrict(user, "change").values_list("pk", flat=True))
    actions, skipped = select_actions(plan, selected, fingerprints, changeable)

    def execute(action: Action) -> str:
        with transaction.atomic():  # savepoint: a failing row rolls back alone
            row = action.row
            if action.kind == "create":
                if IPAddress.objects.filter(address__net_host=row.host).exists():
                    raise ValueError("a record with this address now exists")
                address = IPAddress(
                    address=f"{row.host}/32",
                    status=row.status,
                    role="vip",
                    description=row.description,
                    dns_name=row.dns_name,
                    tenant=tenant,
                )
                if row.interface_id:
                    address.assigned_object = VMInterface.objects.get(pk=row.interface_id)
                address.full_clean()
                address.save()
                if not IPAddress.objects.restrict(user, "add").filter(pk=address.pk).exists():
                    raise PermissionDenied("no permission to add this IP address")
                _journal(address, user, f"Created from NetScaler {row.pair} ({row.node}) vserver {row.vserver}")
                return str(address)

            pk = row.ip.pk if action.kind == "update" else row.pk
            address = IPAddress.objects.restrict(user, "change").get(pk=pk)
            address.snapshot()
            if action.kind == "update":
                for name, (_, new) in row.fields.items():
                    if name == "interface_id":
                        address.assigned_object = VMInterface.objects.get(pk=new)
                    else:
                        setattr(address, name, new)
                note = f"Synced from NetScaler {row.desired.pair} ({row.desired.node}): {', '.join(row.fields)}"
            else:
                address.status = "deprecated"
                note = "Deprecated: VIP not found on any configured NetScaler pair"
            address.full_clean()
            address.save()
            if not IPAddress.objects.restrict(user, "change").filter(pk=address.pk).exists():
                raise PermissionDenied("change would move this IP address outside your permissions")
            _journal(address, user, note)
            return str(address)

    with transaction.atomic():
        result = run_actions(actions, execute)
    result["skipped"] = skipped + result["skipped"]
    return result

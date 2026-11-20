"""NetScaler tab on VM/Device pages, Stale VIPs page, and cache refresh."""

from functools import lru_cache

from dcim.models import Device
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View
from ipam.models import IPAddress
from netbox.views import generic
from utilities.views import ViewTab, register_model_view
from virtualization.models import VirtualMachine, VMInterface

from .api import memberships_for
from .cache import cached_inventories, clear, get_inventories
from .matching import gslb_for, memberships_for_ips, stale_vservers, vservers_at
from .resolve import ips_for_object, resolve_ip

TAB_WEIGHT = settings.PLUGINS_CONFIG.get("netbox_netscaler", {}).get("tab_weight", 9005)
TEMPLATE = "netbox_netscaler/netscaler_tab.html"


def _visible(obj):
    """Never contacts NetScaler: show the tab on any object that has an IP."""
    return bool(ips_for_object(obj))


def _badge(obj):
    """Count of VIPs from cache only; blank when the cache is cold."""
    inventories = cached_inventories()
    if inventories is None:
        return None
    count = len({(m.vserver.pair, m.vserver.name) for m in memberships_for(obj, inventories)})
    return count or None


def _group_by_vip(memberships):
    groups = {}
    for m in memberships:
        groups.setdefault((m.vserver.pair, m.vserver.name), []).append(m)
    return [
        {"vserver": rows[0].vserver, "rows": rows, "gslb": rows[0].gslb, "last_live": rows[0].last_live}
        for rows in groups.values()
    ]


def _render_tab(request, obj, tab):
    inventories = get_inventories()
    memberships = memberships_for(obj, inventories)
    return render(
        request,
        TEMPLATE,
        {
            "object": obj,
            "tab": tab,
            "pairs": inventories,
            "vips": _group_by_vip(memberships),
            "ips": sorted(ips_for_object(obj)),
        },
    )


@register_model_view(VirtualMachine, name="netscaler", path="netscaler")
class VirtualMachineNetScalerView(generic.ObjectView):
    queryset = VirtualMachine.objects.all()
    template_name = TEMPLATE
    tab = ViewTab(
        label="NetScaler",
        weight=TAB_WEIGHT,
        permission="virtualization.view_virtualmachine",
        visible=_visible,
        badge=_badge,
        hide_if_empty=False,
    )

    def get(self, request, pk):
        return _render_tab(request, self.get_object(pk=pk), self.tab)


@register_model_view(Device, name="netscaler", path="netscaler")
class DeviceNetScalerView(generic.ObjectView):
    queryset = Device.objects.all()
    template_name = TEMPLATE
    tab = ViewTab(
        label="NetScaler",
        weight=TAB_WEIGHT,
        permission="dcim.view_device",
        visible=_visible,
        badge=_badge,
        hide_if_empty=False,
    )

    def get(self, request, pk):
        return _render_tab(request, self.get_object(pk=pk), self.tab)


class StaleVipsView(LoginRequiredMixin, PermissionRequiredMixin, View):
    permission_required = "ipam.view_ipaddress"
    template_name = "netbox_netscaler/stale_vips.html"

    def get(self, request):
        inventories = get_inventories()
        rows = stale_vservers(inventories, lru_cache(maxsize=None)(resolve_ip))
        return render(request, self.template_name, {"pairs": inventories, "rows": rows})


class RefreshView(LoginRequiredMixin, View):
    """POST: drop the cached inventory for every pair, then go back to ?next=."""

    def post(self, request):
        if not (
            request.user.has_perm("ipam.view_ipaddress")
            or request.user.has_perm("virtualization.view_virtualmachine")
            or request.user.has_perm("dcim.view_device")
        ):
            return redirect("/")
        clear()
        target = request.POST.get("next") or request.GET.get("next") or "/"
        if not url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}):
            target = "/"
        return redirect(target)


class SyncVipsView(LoginRequiredMixin, PermissionRequiredMixin, View):
    """Preview, then apply selected VIP changes to NetBox IPAM. Never writes to NetScaler."""

    permission_required = "ipam.view_ipaddress"
    template_name = "netbox_netscaler/sync_vips.html"

    def get(self, request):
        from .sync_netbox import build_plan, load_targets

        inventories = get_inventories()
        plan = build_plan(inventories)
        ids = {i for c in plan.updates for i in (c.fields.get("interface_id") or ()) if i}
        names = {
            i.pk: f"{i.virtual_machine.name} / {i.name}"
            for i in VMInterface.objects.filter(pk__in=ids).select_related("virtual_machine")
        }
        for change in plan.updates:
            change.display = [
                (f, *(names.get(v, v or "unassigned") for v in pair)) if f == "interface_id" else (f, *pair)
                for f, pair in change.fields.items()
            ]
        return render(
            request,
            self.template_name,
            {
                "pairs": inventories,
                "plan": plan,
                "targets": load_targets(inventories),
                "can_add": request.user.has_perm("ipam.add_ipaddress"),
                "can_change": request.user.has_perm("ipam.change_ipaddress"),
            },
        )

    def post(self, request):
        from .sync_netbox import apply, build_plan

        selected = set(request.POST.getlist("row"))
        if any(k.startswith("create:") for k in selected) and not request.user.has_perm("ipam.add_ipaddress"):
            raise PermissionDenied
        if any(not k.startswith("create:") for k in selected) and not request.user.has_perm("ipam.change_ipaddress"):
            raise PermissionDenied
        fingerprints = {k: request.POST.get(f"fp:{k}", "") for k in selected}
        result = apply(build_plan(), selected, fingerprints, request.user)
        messages.success(
            request,
            f"Created {len(result['created'])}, updated {len(result['updated'])}, "
            f"deprecated {len(result['deprecated'])}, skipped {len(result['skipped'])}.",
        )
        for key, reason in result["skipped"]:
            messages.warning(request, f"Skipped {key}: {reason}")
        return redirect("plugins:netbox_netscaler:sync_vips")


def _ip_host(address) -> str:
    return str(address.address.ip)


def _ip_usage_count(host, inventories):
    as_vip = len(vservers_at(host, inventories))
    as_member = sum(
        1 for inv in inventories if inv.available for v in inv.vservers if any(m.ip == host for m in v.members)
    )
    return as_vip + as_member


def _ip_badge(address):
    """Cache only; blank when cold or unused."""
    inventories = cached_inventories()
    if inventories is None:
        return None
    return _ip_usage_count(_ip_host(address), inventories) or None


def _ip_owner_key(address) -> str:
    assigned = address.assigned_object
    if getattr(assigned, "virtual_machine", None) is not None:
        return f"vm:{assigned.virtual_machine.pk}"
    if getattr(assigned, "device", None) is not None:
        return f"device:{assigned.device.pk}"
    return f"ip:{address.pk}"


@register_model_view(IPAddress, name="netscaler", path="netscaler")
class IPAddressNetScalerView(generic.ObjectView):
    queryset = IPAddress.objects.all()
    template_name = "netbox_netscaler/ip_tab.html"
    tab = ViewTab(
        label="NetScaler",
        weight=TAB_WEIGHT,
        permission="ipam.view_ipaddress",
        badge=_ip_badge,
        hide_if_empty=False,
    )

    def get(self, request, pk):
        address = self.get_object(pk=pk)
        host = _ip_host(address)
        inventories = get_inventories()
        resolve = lru_cache(maxsize=None)(resolve_ip)
        vips = [
            {
                "vserver": v,
                "members": [(m, resolve(m.ip)) for m in v.members],
                "gslb": gslb_for(v, inventories),
            }
            for v in vservers_at(host, inventories)
        ]
        memberships = memberships_for_ips({host}, _ip_owner_key(address), inventories, resolve)
        return render(
            request,
            self.template_name,
            {
                "object": address,
                "tab": self.tab,
                "pairs": inventories,
                "host": host,
                "vip_rows": vips,
                "vips": _group_by_vip(memberships),
            },
        )

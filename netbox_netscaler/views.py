"""NetScaler tab on VM/Device pages, Stale VIPs page, and cache refresh."""

from functools import lru_cache

from dcim.models import Device
from django.conf import settings
from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View
from netbox.views import generic
from utilities.views import ViewTab, register_model_view
from virtualization.models import VirtualMachine

from .api import memberships_for
from .cache import cached_inventories, clear, get_inventories
from .matching import stale_vservers
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

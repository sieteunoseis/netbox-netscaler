"""Navigation menu items for NetBox NetScaler Plugin."""

from django.conf import settings
from netbox.plugins import PluginMenu, PluginMenuItem

_items = (
    PluginMenuItem(
        link="plugins:netbox_netscaler:stale_vips",
        link_text="Stale VIPs",
        permissions=["ipam.view_ipaddress"],
    ),
)

_menu = PluginMenu(label="NetScaler", groups=(("Load Balancing", _items),), icon_class="mdi mdi-scale-balance")

# top_level_menu=False puts this plugin's items under the shared "Plugins" menu
if settings.PLUGINS_CONFIG.get("netbox_netscaler", {}).get("top_level_menu", False):
    menu = _menu
else:
    menu_items = _items

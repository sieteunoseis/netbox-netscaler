"""
NetBox NetScaler Plugin

Read-only view of Citrix NetScaler lb VIPs, service group members and GSLB names
on VirtualMachine and Device detail pages, plus a Stale VIPs report.
"""

from netbox.plugins import PluginConfig

__version__ = "0.1.0"


class NetScalerConfig(PluginConfig):
    """Plugin configuration for NetBox NetScaler integration."""

    name = "netbox_netscaler"
    verbose_name = "NetScaler"
    description = "Show NetScaler VIPs, service group members and GSLB names that depend on a VM or device"
    version = __version__
    author = "Jeremy Worden"
    author_email = "jeremy.worden@gmail.com"
    base_url = "netscaler"
    min_version = "4.7.5"
    max_version = "5.99"
    required_settings = []
    default_settings = {
        "top_level_menu": False,
        "tab_weight": 9005,
        "username": "",
        "password": "",
        "pairs": [],
        "cache_timeout": 900,
        "timeout": 15,
        "verify_ssl": False,
    }


config = NetScalerConfig

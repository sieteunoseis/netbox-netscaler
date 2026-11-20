"""Test setup: the pure modules are tested without NetBox installed.

`netbox_netscaler/__init__.py` imports `netbox.plugins.PluginConfig`, so a minimal
stand-in is registered here before the package is imported.
"""

import json
import sys
import types
from pathlib import Path

import pytest

if "netbox.plugins" not in sys.modules:
    netbox = types.ModuleType("netbox")
    plugins = types.ModuleType("netbox.plugins")
    plugins.PluginConfig = type("PluginConfig", (), {})
    netbox.plugins = plugins
    sys.modules["netbox"] = netbox
    sys.modules["netbox.plugins"] = plugins

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixture():
    def load(name):
        return json.loads((FIXTURES / name).read_text())

    return load

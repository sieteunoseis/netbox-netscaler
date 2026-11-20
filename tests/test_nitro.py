import inspect
from unittest import mock

import pytest

from netbox_netscaler import nitro
from netbox_netscaler.nitro import NitroClient, NitroError, find_primary


def _response(payload, status=200):
    resp = mock.Mock(status_code=status, text=str(payload))
    resp.json.return_value = payload
    return resp


def test_get_sends_nitro_headers_and_only_get():
    with mock.patch("netbox_netscaler.nitro.requests.get") as get:
        get.return_value = _response({"errorcode": 0, "message": "Done", "lbvserver": []})
        client = NitroClient("ns-a1.example.com", "user", "secret", timeout=7, verify_ssl=False)
        result = client.get("config/lbvserver")

    assert result == {"errorcode": 0, "message": "Done", "lbvserver": []}
    args, kwargs = get.call_args
    assert args[0] == "https://ns-a1.example.com/nitro/v1/config/lbvserver"
    assert kwargs["headers"]["X-NITRO-USER"] == "user"
    assert kwargs["headers"]["X-NITRO-PASS"] == "secret"
    assert kwargs["timeout"] == 7
    assert kwargs["verify"] is False

    source = inspect.getsource(nitro)
    for verb in ("post(", "put(", "delete(", "patch("):
        assert verb not in source


def test_get_raises_on_nonzero_errorcode():
    with mock.patch("netbox_netscaler.nitro.requests.get") as get:
        get.return_value = _response({"errorcode": 354, "message": "Invalid username or password"})
        with pytest.raises(NitroError, match="ns-a1.example.com"):
            NitroClient("ns-a1.example.com", "u", "p").get("config/hanode")


class FakeClient:
    """Stands in for NitroClient: answers hanode from a per-host table, or raises."""

    def __init__(self, answers):
        self.answers = answers

    def factory(self, host):
        answers = self.answers

        class _C:
            def get(self, path):
                value = answers[host]
                if isinstance(value, Exception):
                    raise value
                return value

        return _C()


def _hanode(local_state):
    other = "Secondary" if local_state == "Primary" else "Primary"
    return {
        "errorcode": 0,
        "hanode": [
            {"id": "0", "state": local_state, "ipaddress": "203.0.113.1"},
            {"id": "1", "state": other, "ipaddress": "203.0.113.2"},
        ],
    }


def test_find_primary_when_queried_node_is_secondary():
    fake = FakeClient({"ns-a1.example.com": _hanode("Secondary"), "ns-a2.example.com": _hanode("Primary")})
    assert find_primary(["ns-a1.example.com", "ns-a2.example.com"], fake.factory) == ("ns-a2.example.com", None)


def test_find_primary_first_node_unreachable():
    fake = FakeClient({"ns-a1.example.com": NitroError("timeout"), "ns-a2.example.com": _hanode("Primary")})
    assert find_primary(["ns-a1.example.com", "ns-a2.example.com"], fake.factory) == ("ns-a2.example.com", None)


def test_find_primary_none_primary():
    fake = FakeClient({"ns-a1.example.com": NitroError("timeout"), "ns-a2.example.com": NitroError("refused")})
    host, error = find_primary(["ns-a1.example.com", "ns-a2.example.com"], fake.factory)
    assert host is None
    assert "ns-a1.example.com" in error and "ns-a2.example.com" in error


def test_find_primary_standalone_node():
    fake = FakeClient({"ns-solo.example.com": {"errorcode": 0, "hanode": [{"id": "0", "state": "StayPrimary"}]}})
    assert find_primary(["ns-solo.example.com"], fake.factory) == ("ns-solo.example.com", None)

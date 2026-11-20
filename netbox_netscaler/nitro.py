"""Read-only Citrix NetScaler NITRO client.

Only HTTP GET is used. This module must never change NetScaler configuration.
"""

import logging
from typing import Callable

import requests
import urllib3

logger = logging.getLogger(__name__)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class NitroError(Exception):
    """A NITRO request failed: connection error, non-200 status, or non-zero errorcode."""


class NitroClient:
    """Minimal read-only NITRO client for one NetScaler node."""

    def __init__(self, host: str, username: str, password: str, timeout: int = 15, verify_ssl: bool = False):
        self.host = host
        self.username = username
        self.password = password
        self.timeout = timeout
        self.verify_ssl = verify_ssl

    def get(self, path: str) -> dict:
        """GET https://{host}/nitro/v1/{path} and return the decoded JSON body."""
        url = f"https://{self.host}/nitro/v1/{path}"
        headers = {
            "X-NITRO-USER": self.username,
            "X-NITRO-PASS": self.password,
            "Accept": "application/json",
        }
        try:
            response = requests.get(url, headers=headers, timeout=self.timeout, verify=self.verify_ssl)
        except requests.RequestException as exc:
            raise NitroError(f"{self.host}: {exc.__class__.__name__}: {exc}") from exc

        if response.status_code != 200:
            raise NitroError(f"{self.host}: HTTP {response.status_code} for {path}: {response.text[:200]}")
        try:
            body = response.json()
        except ValueError as exc:
            raise NitroError(f"{self.host}: invalid JSON for {path}") from exc
        if not isinstance(body, dict):
            raise NitroError(f"{self.host}: unexpected JSON for {path}")
        if body.get("errorcode", 0) != 0:
            raise NitroError(f"{self.host}: errorcode {body.get('errorcode')} for {path}: {body.get('message')}")
        return body


def _local_state(hanode: dict) -> str | None:
    """State of the node that answered: NITRO lists the local node as id "0"."""
    for node in hanode.get("hanode", []):
        if str(node.get("id")) == "0":
            return node.get("state")
    return None


def find_primary(nodes: list[str], client_factory: Callable[[str], NitroClient]) -> tuple[str | None, str | None]:
    """Return (primary_host, error) for an HA pair, asking each node about itself.

    A one-node "pair" is a standalone appliance: it is used if it answers, whatever its HA state.
    """
    errors = []
    for host in nodes:
        try:
            state = _local_state(client_factory(host).get("config/hanode"))
        except NitroError as exc:
            message = str(exc)
            errors.append(message if host in message else f"{host}: {message}")
            continue
        if len(nodes) == 1 or state == "Primary":
            return host, None
        errors.append(f"{host}: is {state or 'unknown'}")
    return None, "no Primary found: " + "; ".join(errors)

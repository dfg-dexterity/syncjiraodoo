"""Minimal Odoo external-API client (XML-RPC).

Only what is needed to authenticate and verify connectivity. Odoo exposes two
XML-RPC endpoints: ``/xmlrpc/2/common`` (version + authenticate) and
``/xmlrpc/2/object`` (model calls via ``execute_kw``).
"""

from __future__ import annotations

import socket
import xmlrpc.client
from dataclasses import dataclass

from .config import OdooConfig


@dataclass
class OdooConnectionResult:
    ok: bool
    detail: str
    server_version: str | None = None
    uid: int | None = None


class OdooClient:
    def __init__(self, config: OdooConfig, timeout: float = 15.0) -> None:
        self.config = config
        self.timeout = timeout

    def _server_proxy(self, endpoint: str) -> xmlrpc.client.ServerProxy:
        url = f"{self.config.url}/xmlrpc/2/{endpoint}"
        # allow_none lets Odoo return nulls without raising.
        return xmlrpc.client.ServerProxy(url, allow_none=True)

    def test_connection(self) -> OdooConnectionResult:
        """Verify reachability and authentication against Odoo.

        Steps: read the server version (unauthenticated), then authenticate
        with the configured credentials to obtain a uid.
        """
        previous_timeout = socket.getdefaulttimeout()
        socket.setdefaulttimeout(self.timeout)
        try:
            common = self._server_proxy("common")
            version_info = common.version()
            server_version = str(version_info.get("server_version", "unknown"))

            uid = common.authenticate(
                self.config.db,
                self.config.username,
                self.config.api_key,
                {},
            )
            if not uid:
                return OdooConnectionResult(
                    ok=False,
                    detail=(
                        "Reached Odoo but authentication failed. Check "
                        "ODOO_DB, ODOO_USERNAME and ODOO_API_KEY."
                    ),
                    server_version=server_version,
                )

            # Confirm the session can actually call a model.
            models = self._server_proxy("object")
            models.execute_kw(
                self.config.db,
                uid,
                self.config.api_key,
                "res.users",
                "check_access_rights",
                ["read"],
                {"raise_exception": False},
            )
            return OdooConnectionResult(
                ok=True,
                detail=f"Authenticated as uid {uid}.",
                server_version=server_version,
                uid=int(uid),
            )
        except xmlrpc.client.Fault as exc:
            return OdooConnectionResult(
                ok=False,
                detail=f"Odoo XML-RPC fault: {exc.faultString}",
            )
        except (xmlrpc.client.ProtocolError, OSError) as exc:
            return OdooConnectionResult(
                ok=False,
                detail=f"Could not reach Odoo at {self.config.url}: {exc}",
            )
        finally:
            socket.setdefaulttimeout(previous_timeout)

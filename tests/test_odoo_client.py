from __future__ import annotations

import unittest
import xmlrpc.client
from unittest import mock

from sync_jira_odoo.odoo_client import OdooClient, OdooError


def make_client(common: mock.Mock) -> OdooClient:
    with mock.patch("xmlrpc.client.ServerProxy", return_value=common):
        return OdooClient("https://odoo.test", "db", "user", "key")


class OdooClientErrorTest(unittest.TestCase):
    def test_protocol_error_becomes_odoo_error(self):
        common = mock.Mock()
        common.version.side_effect = xmlrpc.client.ProtocolError(
            "odoo.test/xmlrpc/2/common", 403, "Forbidden", {}
        )
        with self.assertRaises(OdooError) as ctx:
            make_client(common).version()
        self.assertIn("HTTP 403", str(ctx.exception))
        self.assertIn("common.version()", str(ctx.exception))

    def test_server_fault_becomes_odoo_error(self):
        common = mock.Mock()
        common.authenticate.side_effect = xmlrpc.client.Fault(1, "Access Denied")
        with self.assertRaises(OdooError) as ctx:
            _ = make_client(common).uid
        self.assertIn("Access Denied", str(ctx.exception))

    def test_network_error_becomes_odoo_error(self):
        common = mock.Mock()
        common.version.side_effect = OSError("connection refused")
        with self.assertRaises(OdooError) as ctx:
            make_client(common).version()
        self.assertIn("erro de rede", str(ctx.exception))

    def test_rejected_credentials_raise_odoo_error(self):
        common = mock.Mock()
        common.authenticate.return_value = False
        with self.assertRaises(OdooError) as ctx:
            _ = make_client(common).uid
        self.assertIn("falha de autenticação", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

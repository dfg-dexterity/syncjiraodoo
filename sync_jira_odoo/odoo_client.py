"""Cliente XML-RPC do Odoo usando apenas a stdlib (xmlrpc.client)."""

from __future__ import annotations

import xmlrpc.client
from typing import Any, Callable


class OdooError(RuntimeError):
    pass


def _guarded(description: str, call: Callable[[], Any]) -> Any:
    """Converte falhas de transporte/servidor em OdooError legível."""
    try:
        return call()
    except xmlrpc.client.Fault as exc:
        raise OdooError(f"{description}: erro do servidor Odoo: {exc.faultString}") from exc
    except xmlrpc.client.ProtocolError as exc:
        raise OdooError(
            f"{description}: HTTP {exc.errcode} {exc.errmsg} em {exc.url} "
            "(verifique a URL e o acesso de rede ao Odoo)"
        ) from exc
    except OSError as exc:
        raise OdooError(f"{description}: erro de rede: {exc}") from exc


class OdooClient:
    def __init__(self, url: str, db: str, login: str, api_key: str):
        self._db = db
        self._login = login
        self._api_key = api_key
        self._common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common", allow_none=True)
        self._object = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object", allow_none=True)
        self._uid: int | None = None

    def version(self) -> dict:
        return _guarded("common.version()", self._common.version)

    @property
    def uid(self) -> int:
        if self._uid is None:
            uid = _guarded(
                "authenticate()",
                lambda: self._common.authenticate(self._db, self._login, self._api_key, {}),
            )
            if not uid:
                raise OdooError(
                    f"falha de autenticação no Odoo (db={self._db}, user={self._login})"
                )
            self._uid = uid
        return self._uid

    def execute(self, model: str, method: str, *args: Any, **kwargs: Any) -> Any:
        return _guarded(
            f"{model}.{method}",
            lambda: self._object.execute_kw(
                self._db, self.uid, self._api_key, model, method, list(args), kwargs
            ),
        )

    def search_read(
        self,
        model: str,
        domain: list,
        fields: list[str],
        limit: int | None = None,
        include_archived: bool = False,
    ) -> list[dict]:
        kwargs: dict[str, Any] = {"fields": fields}
        if limit is not None:
            kwargs["limit"] = limit
        if include_archived:
            kwargs["context"] = {"active_test": False}
        return self.execute(model, "search_read", domain, **kwargs)

    def create(self, model: str, vals: dict) -> int:
        return self.execute(model, "create", vals)

    def write(self, model: str, ids: list[int], vals: dict) -> bool:
        return self.execute(model, "write", ids, vals)

    def unlink(self, model: str, ids: list[int]) -> bool:
        return self.execute(model, "unlink", ids)

from __future__ import annotations

import hashlib
import json
from typing import Any

import httpx

from .config import Environment, ObjectType


class HelixError(RuntimeError):
    pass


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def fingerprint(values: dict[str, Any], ignore_fields: set[str] | None = None) -> str:
    ignore_fields = ignore_fields or set()
    payload = {k: v for k, v in values.items() if k not in ignore_fields}
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


class HelixClient:
    def __init__(self, env: Environment, timeout: float = 60.0):
        self.env = env
        self.client = httpx.AsyncClient(base_url=env.base_url.rstrip("/"), verify=env.verify_tls, timeout=timeout)
        self.token: str | None = None

    async def __aenter__(self) -> "HelixClient":
        await self.login()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.logout()
        await self.client.aclose()

    async def login(self) -> None:
        data = {"username": self.env.username, "password": self.env.password}
        if self.env.auth_string:
            data["authString"] = self.env.auth_string
        r = await self.client.post("/api/jwt/login", data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
        if r.status_code >= 400:
            raise HelixError(f"Login misslyckades för {self.env.name}: HTTP {r.status_code} {r.text[:300]}")
        self.token = r.text.strip().strip('"')

    async def logout(self) -> None:
        if not self.token:
            return
        try:
            await self.client.post("/api/jwt/logout", headers=self._headers())
        finally:
            self.token = None

    def _headers(self) -> dict[str, str]:
        if not self.token:
            raise HelixError("Inte inloggad")
        return {"Authorization": f"AR-JWT {self.token}", "Accept": "application/json"}

    async def fetch_entries(self, obj_type: ObjectType, q: str | None, limit: int = 500) -> list[dict[str, Any]]:
        fields_expr = "values(" + ",".join(obj_type.fields_for_api()) + ")"
        offset = 0
        entries: list[dict[str, Any]] = []
        while True:
            params = {"fields": fields_expr, "limit": str(limit), "offset": str(offset)}
            if q:
                params["q"] = q
            r = await self.client.get(f"/api/arsys/v1/entry/{obj_type.form}", params=params, headers=self._headers())
            if r.status_code >= 400:
                raise HelixError(f"Hämtning från {self.env.name}/{obj_type.form} misslyckades: HTTP {r.status_code} {r.text[:500]}")
            data = r.json()
            page = data.get("entries", [])
            entries.extend(page)
            if len(page) < limit:
                break
            offset += limit
        return entries


def _quote_value(value):
    if isinstance(value, int) or (isinstance(value, str) and value.isdigit()):
        return str(value)
    return f'"{str(value).replace(chr(34), chr(92)+chr(34))}"'


def _matches_type_filter(values: dict[str, Any], obj_type: ObjectType) -> bool:
    if not obj_type.type_field or not obj_type.type_values:
        return True
    current = values.get(obj_type.type_field)
    type_values = obj_type.type_values
    if len(type_values) == 1 and isinstance(type_values[0], str) and type_values[0].startswith("not:"):
        excluded = {x.strip() for x in type_values[0][4:].split(",") if x.strip()}
        return str(current) not in excluded
    allowed = {str(v) for v in type_values}
    return str(current) in allowed


def build_qualification(obj_type: ObjectType, prefix: str | None = None, contains: str | None = None) -> str | None:
    clauses: list[str] = []
    name = obj_type.name_field
    if obj_type.type_field and obj_type.type_values:
        vals = obj_type.type_values
        if len(vals) == 1 and isinstance(vals[0], str) and vals[0].startswith("not:"):
            # REST-kvalificeringen används inte för negativa containergrupper; vi filtrerar i Python.
            pass
        elif len(vals) == 1:
            clauses.append(f"'{obj_type.type_field}' = {_quote_value(vals[0])}")
        else:
            clauses.append("(" + " OR ".join(f"'{obj_type.type_field}' = {_quote_value(v)}" for v in vals) + ")")
    if prefix:
        safe = prefix.replace('"', '\\"')
        clauses.append(f"'{name}' LIKE \"{safe}%\"")
    if contains:
        safe = contains.replace('"', '\\"')
        search_fields = obj_type.search_fields or [name]
        parts = [f"'{field}' LIKE \"%{safe}%\"" for field in search_fields]
        clauses.append("(" + " OR ".join(parts) + ")")
    if not clauses:
        return None
    return " AND ".join(clauses)


def normalize_entries(env_name: str, obj_type: ObjectType, entries: list[dict[str, Any]], ignore_fields: set[str]) -> dict[str, dict[str, Any]]:
    result = {}
    for e in entries:
        values = e.get("values", {})
        if not _matches_type_filter(values, obj_type):
            continue
        name = values.get(obj_type.name_field)
        if not name:
            continue
        result[str(name)] = {
            "environment": env_name,
            "name": str(name),
            "values": values,
            "fingerprint": fingerprint(values, ignore_fields | {obj_type.name_field}),
        }
    return result

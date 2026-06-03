from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import os
from collections import defaultdict
from typing import Any

import httpx

from .config import Environment, ObjectType, RelatedForm


log = logging.getLogger("hlx.workflow_diff.helix")


class HelixError(RuntimeError):
    pass


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def fingerprint(values: dict[str, Any], ignore_fields: set[str] | None = None) -> str:
    ignore_fields = ignore_fields or set()
    payload = {k: v for k, v in values.items() if k not in ignore_fields}
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()




def _missing_field_from_error(text: str) -> str | None:
    # AR System REST returns e.g. "none exist field objProp, on schema ...".
    m = re.search(r"none exist field\s+([^,]+),", text, flags=re.IGNORECASE)
    if not m:
        return None
    return m.group(1).strip().strip("'\"")

class HelixClient:
    def __init__(self, env: Environment, timeout: float | None = None):
        self.env = env
        if timeout is None:
            timeout = float(os.getenv("HELIX_HTTP_TIMEOUT", "240") or "240")
        limits = httpx.Limits(
            max_connections=int(os.getenv("HELIX_HTTP_MAX_CONNECTIONS", "20") or "20"),
            max_keepalive_connections=int(os.getenv("HELIX_HTTP_MAX_KEEPALIVE", "10") or "10"),
        )
        self.client = httpx.AsyncClient(base_url=env.base_url.rstrip("/"), verify=env.verify_tls, timeout=timeout, limits=limits)
        self.token: str | None = None

    async def __aenter__(self) -> "HelixClient":
        await self.login()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.logout()
        await self.client.aclose()

    async def login(self) -> None:
        log.info("login start env=%s base_url=%s verify_tls=%s", self.env.name, self.env.base_url, self.env.verify_tls)
        start = time.perf_counter()
        data = {"username": self.env.username, "password": self.env.password}
        if self.env.auth_string:
            data["authString"] = self.env.auth_string
        r = await self.client.post("/api/jwt/login", data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
        elapsed = (time.perf_counter() - start) * 1000
        if r.status_code >= 400:
            log.error("login failed env=%s status=%s elapsed_ms=%.1f response=%s", self.env.name, r.status_code, elapsed, r.text[:300])
            raise HelixError(f"Login misslyckades för {self.env.name}: HTTP {r.status_code} {r.text[:300]}")
        self.token = r.text.strip().strip('"')
        log.info("login ok env=%s elapsed_ms=%.1f token_len=%s", self.env.name, elapsed, len(self.token or ""))

    async def logout(self) -> None:
        if not self.token:
            return
        try:
            log.debug("logout env=%s", self.env.name)
            await self.client.post("/api/jwt/logout", headers=self._headers())
        except Exception as exc:
            log.debug("logout failed env=%s error=%s", self.env.name, exc)
        finally:
            self.token = None

    def _headers(self) -> dict[str, str]:
        if not self.token:
            raise HelixError("Inte inloggad")
        return {"Authorization": f"AR-JWT {self.token}", "Accept": "application/json"}

    async def fetch_form_entries(self, form: str, fields: list[str], q: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        """Fetch entries with defensive field handling.

        AR metadata forms differ a little between versions/patch levels. If the
        server says that one requested field does not exist, we remove that field
        and retry instead of skipping the entire related form.
        """
        start = time.perf_counter()
        clean_fields = sorted({f for f in fields if f})
        removed_fields: list[str] = []

        for retry in range(25):
            fields_expr = "values(" + ",".join(clean_fields) + ")" if clean_fields else "values(*)"
            log.info("fetch form start env=%s form=%s fields=%s q=%r limit=%s retry=%s", self.env.name, form, len(clean_fields), q, limit, retry)
            offset = 0
            page_no = 0
            entries: list[dict[str, Any]] = []
            while True:
                params = {"fields": fields_expr, "limit": str(limit), "offset": str(offset)}
                if q:
                    params["q"] = q
                page_start = time.perf_counter()
                page_attempt = 0
                while True:
                    try:
                        r = await self.client.get(f"/api/arsys/v1/entry/{form}", params=params, headers=self._headers())
                        break
                    except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
                        page_attempt += 1
                        if page_attempt > int(os.getenv("HELIX_HTTP_PAGE_RETRIES", "2") or "2"):
                            log.error("fetch form page timeout env=%s form=%s offset=%s attempt=%s error=%s", self.env.name, form, offset, page_attempt, exc)
                            raise
                        log.warning("fetch form page timeout retry env=%s form=%s offset=%s attempt=%s error=%s", self.env.name, form, offset, page_attempt, exc)
                        await asyncio.sleep(min(2 * page_attempt, 10))
                page_ms = (time.perf_counter() - page_start) * 1000
                if r.status_code >= 400:
                    missing = _missing_field_from_error(r.text)
                    if r.status_code == 400 and missing and missing in clean_fields:
                        clean_fields.remove(missing)
                        removed_fields.append(missing)
                        log.warning("fetch form retry without missing field env=%s form=%s missing_field=%s remaining_fields=%s response=%s", self.env.name, form, missing, len(clean_fields), r.text[:300])
                        break
                    log.error("fetch form failed env=%s form=%s status=%s offset=%s elapsed_ms=%.1f response=%s", self.env.name, form, r.status_code, offset, page_ms, r.text[:500])
                    raise HelixError(f"Hämtning från {self.env.name}/{form} misslyckades: HTTP {r.status_code} {r.text[:500]}")
                data = r.json()
                page = data.get("entries", [])
                entries.extend(page)
                page_no += 1
                # INFO-logga varje sida för långa metadata-former så pod-loggen visar livstecken.
                log.info("fetch form page env=%s form=%s page=%s offset=%s page_count=%s total=%s elapsed_ms=%.1f", self.env.name, form, page_no, offset, len(page), len(entries), page_ms)
                if len(page) < limit:
                    total_ms = (time.perf_counter() - start) * 1000
                    if removed_fields:
                        log.warning("fetch form completed with skipped fields env=%s form=%s skipped=%s", self.env.name, form, removed_fields)
                    log.info("fetch form done env=%s form=%s entries=%s elapsed_ms=%.1f", self.env.name, form, len(entries), total_ms)
                    return entries
                offset += limit
            # break from inner loop means we removed a field and should retry from offset 0.
            continue

        raise HelixError(f"Hämtning från {self.env.name}/{form} misslyckades: för många saknade fält: {removed_fields}")

    async def fetch_entries(self, obj_type: ObjectType, q: str | None, limit: int = 500) -> list[dict[str, Any]]:
        return await self.fetch_form_entries(obj_type.form, obj_type.fields_for_api(), q=q, limit=limit)

    async def fetch_related_entries(self, rel: RelatedForm, limit: int = 1000, parent_ids: list[str] | None = None) -> list[dict[str, Any]]:
        """Fetch rows from a related metadata form.

        Parent-scoped, batched and parallel by default. This avoids the expensive
        unqualified reads of huge forms such as schema_group_ids and
        field_enum_values. It is intentionally closer to Migrator behaviour: use
        base metadata to decide what parents are relevant, then expand those
        parents only.
        """
        fields = ["Request ID", "Record ID", rel.parent_field] + rel.fields
        batch_size = int(os.getenv("HELIX_RELATED_PARENT_BATCH_SIZE", "25") or "25")
        batch_concurrency = max(1, int(os.getenv("HELIX_RELATED_BATCH_CONCURRENCY", "4") or "4"))

        if parent_ids is None:
            # Legacy fallback only. Normal sync always supplies an explicit parent
            # list. Never use this path for scoped sync, because unqualified reads
            # of forms such as filter_push/actlink_group_ids can pull tens of
            # thousands of rows and defeat cache_scope.
            if os.getenv("HELIX_ALLOW_UNSCOPED_RELATED_FETCH", "false").lower() in {"1", "true", "yes", "on"}:
                log.warning("fetch related without parent scope env=%s form=%s parent_field=%s scope=all slow_path=true", self.env.name, rel.form, rel.parent_field)
                return await self.fetch_form_entries(rel.form, fields, q=None, limit=limit)
            log.info("fetch related skipped env=%s form=%s reason=parent_scope_missing unscoped_fetch_disabled=true", self.env.name, rel.form)
            return []

        unique_ids = sorted({str(x) for x in parent_ids if x not in (None, "")})
        if not unique_ids:
            log.info("fetch related skipped env=%s form=%s reason=no_parent_ids", self.env.name, rel.form)
            return []

        sem = asyncio.Semaphore(batch_concurrency)
        batches = [unique_ids[idx:idx + max(1, batch_size)] for idx in range(0, len(unique_ids), max(1, batch_size))]
        log.info(
            "fetch related start env=%s form=%s parent_field=%s scope=parents parents=%s batches=%s batch_size=%s batch_concurrency=%s",
            self.env.name, rel.form, rel.parent_field, len(unique_ids), len(batches), batch_size, batch_concurrency,
        )

        async def fetch_batch(batch_no: int, batch: list[str]) -> list[dict[str, Any]]:
            async with sem:
                q = "(" + " OR ".join(f"'{rel.parent_field}' = {_quote_parent_value(rel.parent_field, v)}" for v in batch) + ")"
                rows = await self.fetch_form_entries(rel.form, fields, q=q, limit=limit)
                log.info("fetch related batch done env=%s form=%s batch=%s/%s parents=%s rows=%s", self.env.name, rel.form, batch_no, len(batches), len(batch), len(rows))
                return rows

        all_rows: list[dict[str, Any]] = []
        results = await asyncio.gather(*(fetch_batch(i + 1, b) for i, b in enumerate(batches)), return_exceptions=True)
        failures = []
        for result in results:
            if isinstance(result, Exception):
                failures.append(result)
            else:
                all_rows.extend(result)
        if failures:
            first = failures[0]
            log.error("fetch related failed env=%s form=%s failures=%s first_error=%s", self.env.name, rel.form, len(failures), first)
            raise first
        log.info("fetch related done env=%s form=%s parent_filter_count=%s rows=%s", self.env.name, rel.form, len(unique_ids), len(all_rows))
        return all_rows


def _quote_value(value):
    if isinstance(value, int) or (isinstance(value, str) and value.isdigit()):
        return str(value)
    return f'"{str(value).replace(chr(34), chr(92)+chr(34))}"'


def _quote_string_value(value):
    return f'"{str(value).replace(chr(34), chr(92)+chr(34))}"'


def _quote_type_value(obj_type: ObjectType, value):
    # Some metadata view forms expose numeric-looking discriminator fields as
    # character/citext fields. arcontainer.containerType is one of those in
    # several Helix versions. If we send containerType = 1 PostgreSQL may fail
    # with citext = integer. Quote it as a string instead.
    if obj_type.form == "AR System Metadata: arcontainer":
        return _quote_string_value(value)
    return _quote_value(value)


def _quote_parent_value(parent_field: str, value):
    """Quote related metadata parent ids safely.

    Many AR System metadata id fields such as filterId/actlinkId/containerId
    are character fields even when they look numeric. Unquoted numeric-looking
    values may make AR treat the value as an integer expression. That can make
    parent-scoped related queries unreliable and, in practice, lead to broad
    result sets. Only a small set of known integer parent fields are left
    unquoted.
    """
    numeric_parent_fields = {"schemaId", "fieldId", "groupId", "vuiId"}
    if parent_field in numeric_parent_fields and (isinstance(value, int) or (isinstance(value, str) and value.isdigit())):
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
            pass
        elif len(vals) == 1:
            clauses.append(f"'{obj_type.type_field}' = {_quote_type_value(obj_type, vals[0])}")
        else:
            clauses.append("(" + " OR ".join(f"'{obj_type.type_field}' = {_quote_type_value(obj_type, v)}" for v in vals) + ")")
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



def _normalize_schema_id(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if not text:
        return None
    if "-" in text and text.split("-", 1)[0].isdigit():
        return text.split("-", 1)[0]
    return text

def _id_for(values: dict[str, Any], obj_type: ObjectType) -> str | None:
    # Forms are special: related metadata uses the numeric schemaId, while the
    # REST entry id/request id is often exposed as e.g. "5008-1". Normalize that
    # to "5008" so fields, views, permissions and indexes are actually linked.
    if obj_type.key == "form":
        for field in ("schemaId", "schemaID", "schema_id", "Request ID", "Record ID"):
            sid = _normalize_schema_id(values.get(field))
            if sid:
                return sid
    for field in obj_type.id_fields:
        value = values.get(field)
        if value not in (None, ""):
            return str(value)
    # Vanliga fallbacknamn när REST returnerar databasfält i stället för display label.
    for field in ("actlinkId", "filterId", "escalationId", "containerId", "schemaId", "charMenuId", "Char Menu ID"):
        value = values.get(field)
        if value not in (None, ""):
            return str(value)
    return None


def _canonical_related_values(values: dict[str, Any], rel: RelatedForm, ignore_fields: set[str]) -> dict[str, Any]:
    # Strip environment-specific AR row ids. Parent ids are kept where useful for
    # grouping/identity before diff normalization, but Request ID/Record ID must
    # not make two equivalent environments differ.
    ignored = {"Request ID", "Record ID", "SystemID", "IntegerID", "Box1", "Box2"} | ignore_fields
    cleaned = {k: v for k, v in values.items() if k not in ignored}
    # Normalize schemaId values in related form metadata too. Some REST entries
    # expose schemaId-like ids as "5008-1" while dictionary relations use 5008.
    for key in ("schemaId", "schemaID", "schema_id"):
        if key in cleaned:
            norm = _normalize_schema_id(cleaned.get(key))
            if norm is not None:
                cleaned[key] = norm
    return cleaned


def group_related(rel: RelatedForm, entries: list[dict[str, Any]], ignore_fields: set[str]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in entries:
        values = entry.get("values", {})
        parent = values.get(rel.parent_field)
        if parent in (None, ""):
            continue
        grouped[str(parent)].append(_canonical_related_values(values, rel, ignore_fields))
    for parent, rows in grouped.items():
        grouped[parent] = sorted(rows, key=canonical_json)
    return dict(grouped)


def normalize_entries(
    env_name: str,
    obj_type: ObjectType,
    entries: list[dict[str, Any]],
    ignore_fields: set[str],
    deep_related: dict[str, dict[str, list[dict[str, Any]]]] | None = None,
) -> dict[str, dict[str, Any]]:
    result = {}
    deep_related = deep_related or {}
    for e in entries:
        values = e.get("values", {})
        if not _matches_type_filter(values, obj_type):
            continue
        name = values.get(obj_type.name_field)
        if not name:
            continue
        parent_id = _id_for(values, obj_type)
        comparable_values = dict(values)
        related_payload = {}
        if parent_id:
            for rel_form, grouped in deep_related.items():
                rows = grouped.get(parent_id, [])
                if rows:
                    related_payload[rel_form] = rows
        if related_payload:
            comparable_values["__deep_metadata"] = related_payload
        result[str(name)] = {
            "environment": env_name,
            "name": str(name),
            "id": parent_id,
            "values": comparable_values,
            "fingerprint": fingerprint(comparable_values, ignore_fields | {obj_type.name_field, "Request ID", "Record ID"}),
        }
    return result

from __future__ import annotations

import asyncio
import json
import logging
import fnmatch
import os
import sys
import time
from typing import Annotated, Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .cache import append_job, cache_status, load_cache, mark_cache_checked, objects_from_cache, save_cache, update_env_state, now_iso, has_complete_cache
from .config import Environment, ObjectType, load_config, load_cache_scope
from .diffing import compare_by_name, field_diffs
from .helix import HelixClient, HelixError, build_qualification, group_related, normalize_entries, fingerprint

def configure_logging() -> None:
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    fmt = os.getenv(
        "LOG_FORMAT",
        "%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    logging.basicConfig(level=level, format=fmt, stream=sys.stdout, force=True)
    logging.getLogger("httpx").setLevel(os.getenv("HTTPX_LOG_LEVEL", "WARNING").upper())
    logging.getLogger("httpcore").setLevel(os.getenv("HTTPCORE_LOG_LEVEL", "WARNING").upper())


configure_logging()
log = logging.getLogger("hlx.workflow_diff")

app = FastAPI(title="Helix Workflow Diff", version="1.6.0")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

_sync_lock = asyncio.Lock()
_last_sync_result: dict[str, Any] | None = None
_sync_task: asyncio.Task | None = None


def pretty(value):
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    return "" if value is None else str(value)


templates.env.filters["pretty"] = pretty

_SCOPE_FORM_IDS: dict[tuple[str, str], set[str]] = {}
WORKFLOW_FORM_SCOPED_TYPES = {"actlink", "filter", "escalation"}
GUIDE_FORM_SCOPED_TYPES = {"active_link_guide", "filter_guide"}
# These object types are global in AR System. They are not safely derivable from a
# form-prefix scope, so they are always cached even when cache_scope is active.
GLOBAL_ALWAYS_CACHE_TYPES = {"application", "packing_list", "web_service", "menu"}
FORM_SCOPE_SUPPORTED_TYPES = {"form"} | WORKFLOW_FORM_SCOPED_TYPES | GUIDE_FORM_SCOPED_TYPES | GLOBAL_ALWAYS_CACHE_TYPES


def _scope_patterns() -> tuple[list[str], list[str], str]:
    scope = load_cache_scope()
    normalized = scope.normalized()
    return normalized.get("include_form_prefixes", []), normalized.get("exclude_form_prefixes", []), scope.signature()


def _pattern_matches(pattern: str, value: str) -> bool:
    pattern = (pattern or "").strip().lower()
    value = str(value or "").lower()
    if not pattern:
        return False
    if "*" not in pattern and "?" not in pattern and "[" not in pattern:
        pattern = pattern + "*"
    return fnmatch.fnmatchcase(value, pattern)


def form_name_in_scope(name: str) -> bool:
    include, exclude, _sig = _scope_patterns()
    if not include and not exclude:
        return True
    value = str(name or "")
    included = True if not include else any(_pattern_matches(p, value) for p in include)
    excluded = any(_pattern_matches(p, value) for p in exclude)
    return included and not excluded


def scope_is_active() -> bool:
    include, exclude, _sig = _scope_patterns()
    return bool(include or exclude)




def scope_include_global_types() -> bool:
    """When a form prefix scope is active, avoid loading global object types by default.

    Forms and workflow can be related to the selected form prefixes. Global
    containers/menus/applications cannot be safely scoped to forms without first
    reading very broad reference metadata, which defeats the purpose of the
    scope. They can be explicitly re-enabled for advanced troubleshooting with
    HELIX_SCOPE_INCLUDE_GLOBAL_TYPES=true.
    """
    return (os.getenv("HELIX_SCOPE_INCLUDE_GLOBAL_TYPES", "false") or "false").strip().lower() in {"1", "true", "yes", "on"}


def object_type_enabled_by_scope(obj_type: ObjectType) -> bool:
    if not scope_is_active():
        return True
    if obj_type.key in GLOBAL_ALWAYS_CACHE_TYPES:
        return True
    if obj_type.key in FORM_SCOPE_SUPPORTED_TYPES:
        return True
    return scope_include_global_types()


async def empty_scoped_cache_row(env: Environment, obj_type: ObjectType, mode: str, reason: str = "not-form-scoped") -> dict[str, Any]:
    log.info("scope skip env=%s type=%s reason=%s include_global_types=%s", env.name, obj_type.key, reason, scope_include_global_types())
    row = save_cache(env.name, obj_type, True, {})
    row.update({"changed": False, "mode": mode, "skipped_by_scope": True, "skip_reason": reason})
    return row

def _mapping_related_only(obj_type: ObjectType) -> ObjectType:
    nt = ObjectType(**{**obj_type.__dict__})
    nt.related_forms = [r for r in obj_type.related_forms if "mapping" in r.form.lower()]
    return nt



def normalize_schema_id(value: Any) -> str | None:
    """Return the AR System data-dictionary schemaId.

    REST entries expose both the AR entry id (often like ``5008-1``) and the
    data-dictionary ``schemaId``. Workflow mapping tables use the numeric
    ``schemaId`` column, not the entry id. Older builds cached the entry id as
    the form object id, which made scoped workflow queries return zero rows and
    later forced broad metadata reads.
    """
    if value in (None, ""):
        return None
    text = str(value).strip()
    if not text:
        return None
    if "-" in text and text.split("-", 1)[0].isdigit():
        return text.split("-", 1)[0]
    return text


def form_schema_id_from_object(obj: dict[str, Any]) -> str | None:
    values = obj.get("values", {}) or {}
    for field in ("schemaId", "schemaID", "schema_id"):
        sid = normalize_schema_id(values.get(field))
        if sid:
            return sid
    return normalize_schema_id(obj.get("id"))


def _object_has_included_schema(obj: dict[str, Any], allowed_schema_ids: set[str]) -> bool:
    deep = (obj.get("values", {}) or {}).get("__deep_metadata", {}) or {}
    for rows in deep.values():
        if not isinstance(rows, list):
            continue
        for row in rows:
            schema_id = row.get("schemaId") or row.get("schemaID") or row.get("schema_id")
            sid = normalize_schema_id(schema_id)
            if sid is not None and sid in allowed_schema_ids:
                return True
    return False


async def allowed_schema_ids_for_env(env: Environment) -> set[str]:
    _include, _exclude, sig = _scope_patterns()
    key = (env.name, sig)
    if key in _SCOPE_FORM_IDS:
        return _SCOPE_FORM_IDS[key]
    config_envs, types = load_config()
    form_type = types.get("form")
    if form_type is None:
        _SCOPE_FORM_IDS[key] = set()
        return set()
    cached = load_cache(env.name, form_type, True)
    if cached and cached.get("objects") is not None:
        # Never trust a previous scoped form cache blindly. Older versions could
        # accidentally contain all forms with the same configured prefix scope.
        # Re-apply the current name filter before using schema ids to scope workflow.
        ids = {
            sid
            for name, obj in (cached.get("objects") or {}).items()
            for sid in [form_schema_id_from_object(obj)]
            if sid and form_name_in_scope(str(name))
        }
        log.info("allowed schema ids from cache env=%s scope_sig=%s count=%s", env.name, sig, len(ids))
        _SCOPE_FORM_IDS[key] = ids
        return ids
    async with HelixClient(env) as client:
        raw = await client.fetch_entries(form_type, q=scoped_base_qualification(form_type))
    _, forms = await collect(env, form_type, q=None, ignore_fields=set(), deep=False, raw_override=raw)
    ids = {sid for obj in forms.values() for sid in [form_schema_id_from_object(obj)] if sid}
    _SCOPE_FORM_IDS[key] = ids
    return ids


async def apply_cache_scope(env: Environment, obj_type: ObjectType, objects: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    if not scope_is_active():
        return objects
    if obj_type.key == "form":
        filtered = {name: obj for name, obj in objects.items() if form_name_in_scope(name)}
        log.info("cache scope applied env=%s type=%s before=%s after=%s", env.name, obj_type.key, len(objects), len(filtered))
        return filtered
    if obj_type.key in WORKFLOW_FORM_SCOPED_TYPES:
        allowed = await allowed_schema_ids_for_env(env)
        filtered = {name: obj for name, obj in objects.items() if _object_has_included_schema(obj, allowed)}
        log.info("cache scope applied env=%s type=%s before=%s after=%s allowed_forms=%s", env.name, obj_type.key, len(objects), len(filtered), len(allowed))
        return filtered
    return objects


IGNORE_GROUPS = {
    "timestamps": {
        "label": "Tidsstämplar",
        "fields": ["timestamp", "modifiedDate", "lastModifiedDate", "lastModifiedOn", "Last Modified Date"],
        "default": True,
    },
    "ownership": {
        "label": "Ägare/ändrad av",
        "fields": ["owner", "lastChanged", "lastModifiedBy", "modifiedBy"],
        "default": False,
    },
    "overlay": {
        "label": "Overlay/resolved metadata",
        "fields": ["overlayGroup", "overlayProp", "overlayExtended", "resolvedName", "resolvedfieldId", "resolvedVuiId"],
        "default": False,
    },
    "bundle": {
        "label": "Bundle/scope metadata",
        "fields": ["bundleScope", "bundleScopeEnabled"],
        "default": False,
    },
    "object_properties": {
        "label": "Objektegenskaper",
        "fields": ["objProp", "smObjProp"],
        "default": False,
    },
    "permissions": {
        "label": "Behörigheter",
        "fields": ["permission", "groupId", "groupList", "AR System Metadata: field_permissions", "AR System Metadata: schema_group_ids", "AR System Metadata: actlink_group_ids", "AR System Metadata: arctr_group_ids"],
        "default": False,
    },
    "field_definitions": {
        "label": "Fältdefinitioner",
        "fields": ["fieldId", "fieldName", "fieldType", "datatype", "fOption", "createMode", "defaultValue", "AR System Metadata: field"],
        "default": False,
    },
    "enum_values": {
        "label": "Enum-värden",
        "fields": ["enumItem", "enumValue", "enumLabel", "AR System Metadata: field_enum_values"],
        "default": False,
    },
    "views": {
        "label": "Vyer/VUI",
        "fields": ["vuiName", "vuiId", "vuiType", "locale", "AR System Metadata: vui", "AR System Metadata: view_mapping"],
        "default": False,
    },
    "indexes": {
        "label": "Index",
        "fields": ["indexName", "listIndex", "uniqueFlag", "numFields", "AR System Metadata: schema_index"],
        "default": False,
    },
    "guide_members": {
        "label": "Guide-medlemmar/referenser",
        "fields": ["referenceOrder", "referenceType", "referenceId", "referenceObjId", "AR System Metadata: arreference"],
        "default": False,
    },
    "workflow_actions": {
        "label": "Workflow actions",
        "fields": ["actionIndex", "assignShort", "assignLong", "command", "commandLong", "fieldMaplong", "fieldMapshort", "AR System Metadata: actlink_set", "AR System Metadata: actlink_push", "AR System Metadata: actlink_message", "AR System Metadata: actlink_process", "AR System Metadata: filter_set", "AR System Metadata: filter_push", "AR System Metadata: filter_message", "AR System Metadata: filter_process"],
        "default": False,
    },
}


def default_ignore_options() -> list[str]:
    return [key for key, item in IGNORE_GROUPS.items() if item.get("default")]


def ignore_fields_from_options(options: list[str] | None, custom: str | None = None) -> set[str]:
    selected = options if options is not None else default_ignore_options()
    fields: set[str] = set()
    for key in selected:
        fields.update(IGNORE_GROUPS.get(key, {}).get("fields", []))
    for value in (custom or "").split(","):
        value = value.strip()
        if value:
            fields.add(value)
    return fields



@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.perf_counter()
    log.debug("HTTP %s %s start", request.method, request.url.path)
    try:
        response = await call_next(request)
    except Exception:
        log.exception("HTTP %s %s failed", request.method, request.url.path)
        raise
    elapsed_ms = (time.perf_counter() - start) * 1000
    log.debug("HTTP %s %s -> %s %.1fms", request.method, request.url.path, response.status_code, elapsed_ms)
    return response


@app.on_event("startup")
async def startup_sync_scheduler():
    envs_preview, types_preview = load_config()
    log.info("hlx-workflow-diff starting: LOG_LEVEL=%s cache_dir=%s envs=%s object_types=%s", os.getenv("LOG_LEVEL", "INFO"), os.getenv("HELIX_CACHE_DIR") or os.getenv("CACHE_DIR") or "/tmp/cache", [e.name for e in envs_preview], list(types_preview.keys()))
    sync_on_start = os.getenv("HELIX_SYNC_ON_START", "true").lower() in {"1", "true", "yes", "on"}

    log.info("sync settings: HELIX_SYNC_ON_START=%s CACHE_MODE=deep-only SCHEDULER=disabled", sync_on_start)
    if sync_on_start:
        async def run_startup():
            envs, types = load_config()
            object_types = list(types.values())
            mode = "auto"
            cached = []
            missing = []
            for env in envs:
                for ot in object_types:
                    if load_cache(env.name, ot, True):
                        cached.append(f"{env.name}/{ot.key}")
                    else:
                        missing.append(f"{env.name}/{ot.key}")
            log.info("startup cache inventory: cached=%s missing=%s", len(cached), len(missing))
            if missing:
                log.info("startup will build missing deep cache entries: %s", missing[:30])
            else:
                log.info("startup complete cache found for all environments/object types; running incremental check only")
            log.info("queueing startup smart sync for environments=%s cache_mode=deep-only mode=%s", [e.name for e in envs], mode)
            await start_sync_job(envs, object_types, deep=True, mode=mode, source="startup")
        asyncio.create_task(run_startup())
    else:
        log.info("startup sync disabled")


def sync_running() -> bool:
    return _sync_task is not None and not _sync_task.done()


async def _run_sync_task(envs: list[Environment], object_types: list[ObjectType], deep: bool, mode: str, source: str) -> None:
    log.info("sync background task started: source=%s mode=%s deep=%s", source, mode, deep)
    try:
        result = await sync_environments(envs, object_types, deep=deep, mode=mode, source=source)
        log.info("sync background task finished: rows=%s errors=%s", len(result.get("rows", [])), len(result.get("errors", [])))
    except Exception as exc:
        log.exception("Sync job failed")
        for env in envs:
            update_env_state(env.name, status="error", message="Synkfel", current_object=None, last_error=str(exc))


async def start_sync_job(envs: list[Environment], object_types: list[ObjectType], deep: bool, mode: str, source: str) -> dict[str, Any]:
    global _sync_task
    log.info("sync request: source=%s mode=%s deep=%s envs=%s object_types=%s running=%s", source, mode, deep, [e.name for e in envs], [t.key for t in object_types], sync_running())
    if sync_running():
        log.warning("sync request ignored because another sync is already running")
        return {"started": False, "running": True, "message": "En synk pågår redan."}
    for env in envs:
        update_env_state(env.name, status="queued", message="Väntar på synk", progress_done=0, progress_total=len(object_types), current_object=None, last_error=None)
    _sync_task = asyncio.create_task(_run_sync_task(envs, object_types, deep, mode, source))
    log.info("sync background task created: %s", _sync_task)
    return {"started": True, "running": True, "mode": mode, "source": source, "deep": deep}


@app.get("/healthz")
async def healthz():
    return {"ok": True}


def parent_ids_from_raw(raw: list[dict[str, Any]], obj_type: ObjectType) -> list[str]:
    if obj_type.key == "form":
        ids: list[str] = []
        seen: set[str] = set()
        for entry in raw:
            values = entry.get("values", {}) or {}
            value = values.get("schemaId") or values.get("schemaID") or values.get("schema_id") or entry.get("id")
            sid = normalize_schema_id(value)
            if sid and sid not in seen:
                seen.add(sid)
                ids.append(sid)
        return ids

    fields = list(obj_type.id_fields) + [
        "Active Link ID", "Filter ID", "Escalation ID", "Container ID",
        "actlinkId", "filterId", "escalationId", "containerId",
        "schemaId", "charMenuId", "Char Menu ID", "Request ID", "Record ID",
    ]
    ids: list[str] = []
    seen: set[str] = set()
    for entry in raw:
        values = entry.get("values", {}) or {}
        for field in fields:
            value = values.get(field)
            if value not in (None, ""):
                sid = str(value)
                if sid not in seen:
                    seen.add(sid)
                    ids.append(sid)
                break
    return ids

async def collect(env: Environment, obj_type: ObjectType, q, ignore_fields, deep: bool = False, parent_ids: list[str] | None = None, raw_override: list[dict[str, Any]] | None = None):
    log.debug("collect start env=%s type=%s deep=%s q=%r parent_ids=%s raw_override=%s", env.name, obj_type.key, deep, q, len(parent_ids or []), raw_override is not None)
    max_related_parallel = int(os.getenv("HELIX_RELATED_FETCH_CONCURRENCY", "6") or "6")
    async with HelixClient(env) as client:
        raw = raw_override if raw_override is not None else await client.fetch_entries(obj_type, q=q)
        log.info("collect base done env=%s type=%s deep=%s entries=%s", env.name, obj_type.key, deep, len(raw))
        related = {}
        if deep and obj_type.related_forms:
            sem = asyncio.Semaphore(max(1, max_related_parallel))
            effective_parent_ids = parent_ids if parent_ids is not None else parent_ids_from_raw(raw, obj_type)
            if not effective_parent_ids:
                log.info("deep related fetch skipped env=%s type=%s reason=no_parent_ids raw_entries=%s scoped_parent_arg=%s", env.name, obj_type.key, len(raw), parent_ids is not None)

            async def fetch_one_related(rel):
                async with sem:
                    try:
                        scope = "changed-parents" if parent_ids is not None else "all-base-parents"
                        log.info("related fetch queued env=%s type=%s form=%s scope=%s parents=%s", env.name, obj_type.key, rel.form, scope, len(effective_parent_ids or []))
                        rel_raw = await client.fetch_related_entries(rel, parent_ids=effective_parent_ids)
                        grouped = group_related(rel, rel_raw, ignore_fields)
                        row_count = sum(len(v) for v in grouped.values())
                        log.info("related fetch done env=%s type=%s form=%s parent_objects=%s rows=%s", env.name, obj_type.key, rel.form, len(grouped), row_count)
                        return rel.form, grouped, None
                    except HelixError as exc:
                        log.warning("related metadata skipped env=%s type=%s related_form=%s error=%s", env.name, obj_type.key, rel.form, exc)
                        return rel.form, None, exc

            log.info("deep related fetch start env=%s type=%s related_forms=%s concurrency=%s parent_scope=%s", env.name, obj_type.key, len(obj_type.related_forms), max_related_parallel, len(effective_parent_ids or []))
            if effective_parent_ids:
                for form, grouped, exc in await asyncio.gather(*(fetch_one_related(rel) for rel in obj_type.related_forms)):
                    if grouped is not None:
                        related[form] = grouped
            log.info("deep related fetch finished env=%s type=%s successful_forms=%s/%s", env.name, obj_type.key, len(related), len(obj_type.related_forms))
        normalized = normalize_entries(env.name, obj_type, raw, ignore_fields, related)
        normalized = await apply_cache_scope(env, obj_type, normalized)
        log.info("collect normalized env=%s type=%s objects=%s related_forms=%s", env.name, obj_type.key, len(normalized), list(related.keys()))
        return env.name, normalized


def base_values_for_compare(values: dict[str, Any]) -> dict[str, Any]:
    data = dict(values or {})
    data.pop("__deep_metadata", None)
    return data


def base_fingerprint_for_object(obj: dict[str, Any], obj_type: ObjectType) -> str:
    return fingerprint(base_values_for_compare(obj.get("values", {}) or {}), {obj_type.name_field, "Request ID", "Record ID"})


def deep_verify_scoped_objects_by_default(obj_type: ObjectType) -> bool:
    """Return True when incremental sync should deep-refresh scoped objects.

    Some AR metadata changes, especially field permissions, enum values and
    certain workflow action details, do not always change the cheap/base fields
    we use for the fast dirty scan. When a form prefix scope is active the data
    set is normally intentionally small, so default to deep-verifying those
    scoped objects to preserve Migrator-like correctness while still avoiding
    server-wide related metadata reads.
    """
    if not scope_is_active():
        return False
    if obj_type.key == "form":
        return (os.getenv("HELIX_INCREMENTAL_VERIFY_SCOPED_FORMS", "true") or "true").lower() in {"1", "true", "yes", "on"}
    if obj_type.key in WORKFLOW_FORM_SCOPED_TYPES | GUIDE_FORM_SCOPED_TYPES:
        return (os.getenv("HELIX_INCREMENTAL_VERIFY_SCOPED_WORKFLOW", "true") or "true").lower() in {"1", "true", "yes", "on"}
    return False


async def collect_base_only(env: Environment, obj_type: ObjectType) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    if scope_is_active() and not object_type_enabled_by_scope(obj_type):
        log.info("collect base skipped by form scope env=%s type=%s", env.name, obj_type.key)
        return [], {}
    if scope_is_active() and obj_type.key in WORKFLOW_FORM_SCOPED_TYPES:
        raw, _parent_ids = await scoped_workflow_raw_and_parent_ids(env, obj_type)
        _, base_objects = await collect(env, obj_type, q=None, ignore_fields=set(), deep=False, raw_override=raw)
        return raw, base_objects
    if scope_is_active() and obj_type.key in GUIDE_FORM_SCOPED_TYPES:
        raw, _parent_ids = await scoped_guide_raw_and_parent_ids(env, obj_type)
        _, base_objects = await collect(env, obj_type, q=None, ignore_fields=set(), deep=False, raw_override=raw)
        return raw, base_objects

    q = scoped_base_qualification(obj_type)
    async with HelixClient(env) as client:
        raw = await client.fetch_entries(obj_type, q=q)
    _, base_objects = await collect(env, obj_type, q=None, ignore_fields=set(), deep=False, raw_override=raw)
    return raw, base_objects

def _and_qualification(*parts: str | None) -> str | None:
    cleaned = [p for p in parts if p]
    if not cleaned:
        return None
    return " AND ".join(f"({p})" for p in cleaned)


def _quote_ar_value(value: Any) -> str:
    if isinstance(value, int) or (isinstance(value, str) and value.isdigit()):
        return str(value)
    return _quote_ar_string(value)


def _quote_ar_string(value: Any) -> str:
    # Use this for character/citext metadata fields such as Request ID and Record ID.
    # Even when the underlying view column is numeric-looking, the AR metadata
    # form exposes these fields as character fields. Unquoted numeric values can
    # make PostgreSQL compare citext = integer and fail.
    return f'"{str(value).replace(chr(34), chr(92)+chr(34))}"'




def _workflow_id_query_field(obj_type: ObjectType) -> str:
    """Return the integer metadata id field for base workflow forms.

    The XML definitions show that the base workflow view forms expose numeric
    id fields in addition to Request ID / Record ID:
      - AR System Metadata: actlink  -> Active Link ID
      - AR System Metadata: filter   -> Filter ID
      - AR System Metadata: escalation -> Escalation ID

    Querying Request ID / Record ID with numeric-looking values can make the
    database compare citext = integer on some AR versions. The numeric display
    fields are the correct fields to use when the IDs come from *_mapping forms.
    """
    if obj_type.key == "actlink":
        return "Active Link ID"
    if obj_type.key == "filter":
        return "Filter ID"
    if obj_type.key == "escalation":
        return "Escalation ID"
    if obj_type.key in {"active_link_guide", "filter_guide", "application", "packing_list", "web_service"}:
        return "Container ID"
    return "Record ID"


def _workflow_base_id_qualification(obj_type: ObjectType, ids: list[str]) -> str:
    """Build a qualification for workflow base metadata forms using integer ids."""
    field = _workflow_id_query_field(obj_type)
    clauses: list[str] = []
    for value in ids:
        text = str(value).strip()
        if not text:
            continue
        # Mapping forms return integer ids. Keep numeric values unquoted when
        # querying the numeric display field, otherwise quote defensively.
        if text.isdigit():
            clauses.append(f"'{field}' = {text}")
        else:
            clauses.append(f"'{field}' = {_quote_ar_string(text)}")
    return "(" + " OR ".join(clauses) + ")" if clauses else "(1 = 0)"

def _wildcard_to_like(pattern: str) -> str:
    value = (pattern or "").strip().replace('"', '\"')
    if not value:
        return value
    value = value.replace("*", "%").replace("?", "_")
    if "%" not in value and "_" not in value:
        value += "%"
    return value


def form_scope_qualification(field: str = "name") -> str | None:
    include, exclude, _sig = _scope_patterns()
    clauses: list[str] = []
    include_parts = [f"'{field}' LIKE \"{_wildcard_to_like(p)}\"" for p in include if _wildcard_to_like(p)]
    exclude_parts = [f"'{field}' LIKE \"{_wildcard_to_like(p)}\"" for p in exclude if _wildcard_to_like(p)]
    if include_parts:
        clauses.append("(" + " OR ".join(include_parts) + ")")
    if exclude_parts:
        clauses.extend([f"NOT ({part})" for part in exclude_parts])
    return " AND ".join(clauses) if clauses else None


def scoped_base_qualification(obj_type: ObjectType) -> str | None:
    base_q = build_qualification(obj_type)
    if scope_is_active() and obj_type.key == "form":
        return _and_qualification(base_q, form_scope_qualification(obj_type.name_field))
    return base_q


def _mapping_related_form(obj_type: ObjectType):
    for rel in obj_type.related_forms:
        name = rel.form.lower()
        if "_mapping" in name or "escal_mapping" in name:
            return rel
    return None


async def scoped_workflow_raw_and_parent_ids(env: Environment, obj_type: ObjectType) -> tuple[list[dict[str, Any]], list[str]]:
    allowed = await allowed_schema_ids_for_env(env)
    mapping_rel = _mapping_related_form(obj_type)
    if not allowed or mapping_rel is None:
        log.info("workflow scope empty env=%s type=%s allowed_forms=%s mapping_rel=%s", env.name, obj_type.key, len(allowed), bool(mapping_rel))
        return [], []

    batch_size = max(1, int(os.getenv("HELIX_SCOPE_SCHEMA_BATCH_SIZE", "25") or "25"))
    schema_ids = sorted(allowed)
    mapping_rows: list[dict[str, Any]] = []
    fields = ["Request ID", "Record ID"] + mapping_rel.fields
    async with HelixClient(env) as client:
        for i in range(0, len(schema_ids), batch_size):
            batch = schema_ids[i:i + batch_size]
            q = "(" + " OR ".join(f"'schemaId' = {_quote_ar_value(v)}" for v in batch) + ")"
            rows = await client.fetch_form_entries(mapping_rel.form, fields, q=q, limit=1000)
            mapping_rows.extend(rows)
            log.info("scope mapping batch env=%s type=%s form=%s batch=%s/%s schemas=%s rows=%s total_rows=%s", env.name, obj_type.key, mapping_rel.form, (i//batch_size)+1, (len(schema_ids)+batch_size-1)//batch_size, len(batch), len(rows), len(mapping_rows))

        parent_ids = []
        seen = set()
        for entry in mapping_rows:
            values = entry.get("values", {}) or {}
            value = values.get(mapping_rel.parent_field)
            if value not in (None, ""):
                sid = str(value)
                if sid not in seen:
                    seen.add(sid)
                    parent_ids.append(sid)

        if not parent_ids:
            log.info("workflow scope found no parent workflow objects env=%s type=%s allowed_forms=%s", env.name, obj_type.key, len(allowed))
            return [], []

        raw: list[dict[str, Any]] = []
        # The workflow base metadata view-forms expose the underlying
        # filterId/actlinkId/escalationId through character fields. Query both
        # Request ID and Record ID as strings to avoid PostgreSQL citext=integer
        # failures and to handle version differences.
        for i in range(0, len(parent_ids), batch_size):
            batch = parent_ids[i:i + batch_size]
            id_q = _workflow_base_id_qualification(obj_type, [str(v) for v in batch])
            q = _and_qualification(build_qualification(obj_type), id_q)
            rows = await client.fetch_form_entries(obj_type.form, obj_type.fields_for_api(), q=q, limit=1000)
            raw.extend(rows)
            log.info("scope workflow base batch env=%s type=%s id_fields=numeric-display-id batch=%s/%s parents=%s rows=%s total_rows=%s", env.name, obj_type.key, (i//batch_size)+1, (len(parent_ids)+batch_size-1)//batch_size, len(batch), len(rows), len(raw))

    log.info("workflow scope applied before deep fetch env=%s type=%s allowed_forms=%s mappings=%s workflow_objects=%s", env.name, obj_type.key, len(allowed), len(mapping_rows), len(raw))
    return raw, parent_ids




async def scoped_guide_raw_and_parent_ids(env: Environment, obj_type: ObjectType) -> tuple[list[dict[str, Any]], list[str]]:
    """Scope Active Link Guides / Filter Guides to workflow mapped to scoped forms.

    Guides are containers and do not have a direct form mapping. We therefore
    look at arreference rows that point to scoped active links or filters, then
    fetch only those guide containers. If no references are found, the guide
    category is empty instead of falling back to all containers.
    """
    if obj_type.key == "active_link_guide":
        workflow_key = "actlink"
    elif obj_type.key == "filter_guide":
        workflow_key = "filter"
    else:
        return [], []

    _envs, types = load_config()
    workflow_type = types.get(workflow_key)
    if workflow_type is None:
        return [], []

    _workflow_raw, workflow_ids = await scoped_workflow_raw_and_parent_ids(env, workflow_type)
    if not workflow_ids:
        log.info("guide scope found no scoped workflow env=%s type=%s", env.name, obj_type.key)
        return [], []

    batch_size = max(1, int(os.getenv("HELIX_SCOPE_SCHEMA_BATCH_SIZE", "25") or "25"))
    reference_fields = ["Request ID", "Record ID", "containerId", "referenceId", "referenceObjId", "referenceType", "referenceOrder", "label"]
    container_ids: list[str] = []
    seen: set[str] = set()
    async with HelixClient(env) as client:
        for i in range(0, len(workflow_ids), batch_size):
            batch = workflow_ids[i:i + batch_size]
            q = "(" + " OR ".join(
                f"'referenceId' = {_quote_ar_string(v)} OR 'referenceObjId' = {_quote_ar_string(v)}"
                for v in batch
            ) + ")"
            rows = await client.fetch_form_entries("AR System Metadata: arreference", reference_fields, q=q, limit=1000)
            for entry in rows:
                values = entry.get("values", {}) or {}
                cid = values.get("containerId") or values.get("Record ID") or values.get("Request ID")
                if cid not in (None, "") and str(cid) not in seen:
                    seen.add(str(cid))
                    container_ids.append(str(cid))
            log.info(
                "guide scope reference batch env=%s type=%s batch=%s/%s workflow_ids=%s reference_rows=%s containers=%s",
                env.name, obj_type.key, (i // batch_size) + 1, (len(workflow_ids) + batch_size - 1) // batch_size,
                len(batch), len(rows), len(container_ids),
            )

        if not container_ids:
            log.info("guide scope found no guide containers env=%s type=%s scoped_workflow=%s", env.name, obj_type.key, len(workflow_ids))
            return [], []

        # Do not query AR System Metadata: arcontainer with id/type qualifications.
        # Several Helix/PostgreSQL versions expose arcontainer id/type fields as
        # citext in the REST view form even though the underlying dictionary names
        # look numeric. Qualifications such as containerType = 1 or Container ID = 123
        # then fail with "operator does not exist: citext = integer". Fetch the
        # relatively small container metadata form without qualification and filter
        # locally by container id and guide type. This is slower than a perfect indexed
        # lookup, but far faster and safer than falling back to all related workflow
        # action metadata.
        wanted_container_ids = {str(v) for v in container_ids if v not in (None, "")}
        wanted_types = {str(v) for v in (obj_type.type_values or [])}
        all_containers = await client.fetch_form_entries(obj_type.form, obj_type.fields_for_api(), q=None, limit=1000)
        raw = []
        for entry in all_containers:
            values = entry.get("values", {}) or {}
            candidate_ids = {
                str(values.get("containerId") or ""),
                str(values.get("Container ID") or ""),
                str(values.get("Request ID") or ""),
                str(values.get("Record ID") or ""),
                str(entry.get("id") or ""),
            }
            type_value = str(values.get(obj_type.type_field or "") or "")
            if candidate_ids.intersection(wanted_container_ids) and (not wanted_types or type_value in wanted_types):
                raw.append(entry)
        log.info(
            "guide scope base local-filter env=%s type=%s referenced_containers=%s fetched_containers=%s guide_objects=%s",
            env.name, obj_type.key, len(wanted_container_ids), len(all_containers), len(raw),
        )

    parent_ids = parent_ids_from_raw(raw, obj_type)
    log.info("guide scope applied before deep fetch env=%s type=%s scoped_workflow=%s guide_containers=%s guide_objects=%s", env.name, obj_type.key, len(workflow_ids), len(container_ids), len(raw))
    return raw, parent_ids

async def has_changes_since(env: Environment, obj_type: ObjectType, since_timestamp: int | None) -> bool:
    if since_timestamp is None:
        return True
    q = _and_qualification(build_qualification(obj_type), f"'timestamp' > {int(since_timestamp)}")
    fields = [obj_type.name_field, "timestamp"]
    if obj_type.type_field:
        fields.append(obj_type.type_field)
    async with HelixClient(env, timeout=30.0) as client:
        raw = await client.fetch_form_entries(obj_type.form, fields, q=q, limit=1)
    return bool(raw)


async def sync_one(env: Environment, obj_type: ObjectType, deep: bool, mode: str = "full") -> dict[str, Any]:
    log.info("sync_one start env=%s type=%s label=%s mode=%s deep=%s cache_model=migrator", env.name, obj_type.key, obj_type.label, mode, deep)
    update_env_state(env.name, phase="cache-check", phase_label="Kontrollerar cache", phase_current=0, phase_total=0)
    if scope_is_active() and not object_type_enabled_by_scope(obj_type):
        return await empty_scoped_cache_row(env, obj_type, mode)
    current = load_cache(env.name, obj_type, True)
    if current and current.get("objects") is not None:
        log.info("cache found env=%s type=%s count=%s synced_at=%s -> incremental/base scan only", env.name, obj_type.key, current.get("count"), current.get("synced_at"))
    elif current:
        log.warning("cache file exists but has no objects env=%s type=%s -> rebuilding deep cache", env.name, obj_type.key)
    else:
        log.info("cache missing env=%s type=%s -> building deep cache", env.name, obj_type.key)

    if mode in {"incremental", "auto"} and current and current.get("objects") is not None:
        update_env_state(env.name, phase="base-scan", phase_label=f"Läser basmetadata för {obj_type.label}", phase_current=0, phase_total=0)
        raw, base_objects = await collect_base_only(env, obj_type)
        update_env_state(env.name, phase="dirty-scan", phase_label=f"Identifierar ändringar i {obj_type.label}", phase_current=len(base_objects), phase_total=len(base_objects))
        cached_objects = current.get("objects", {}) or {}
        changed_names: list[str] = []
        deleted_names = sorted(set(cached_objects) - set(base_objects))

        if deep_verify_scoped_objects_by_default(obj_type):
            # Preserve exactness for scoped datasets: all currently scoped objects
            # are deep-refreshed, but deleted objects are still detected via the
            # base scan. This catches changes in fields/actions/permissions even
            # when the parent object's base timestamp/hash is unchanged.
            changed_names = sorted(base_objects.keys())
            log.info(
                "incremental scoped deep verification env=%s type=%s objects=%s reason=exact-scoped-cache",
                env.name, obj_type.key, len(changed_names),
            )
        else:
            for name, base_obj in base_objects.items():
                cached = cached_objects.get(name)
                if not cached:
                    changed_names.append(name)
                    continue
                if base_fingerprint_for_object(base_obj, obj_type) != base_fingerprint_for_object(cached, obj_type):
                    changed_names.append(name)

        mark_cache_checked(env.name, obj_type, True, changed=bool(changed_names or deleted_names))
        log.info(
            "incremental scan env=%s type=%s total=%s changed=%s deleted=%s reused=%s verify_scoped=%s",
            env.name, obj_type.key, len(base_objects), len(changed_names), len(deleted_names), max(0, len(base_objects) - len(changed_names)), deep_verify_scoped_objects_by_default(obj_type),
        )

        if not changed_names and not deleted_names:
            return {
                "env": env.name,
                "object_type": obj_type.key,
                "label": obj_type.label,
                "deep": True,
                "checked_at": now_iso(),
                "changed": False,
                "count": current.get("count", len(cached_objects)),
                "mode": mode,
                "reused": len(cached_objects),
            }

        changed_raw_names = set(changed_names)
        changed_raw = []
        for entry in raw:
            values = entry.get("values", {}) or {}
            name = values.get(obj_type.name_field)
            if str(name) in changed_raw_names:
                changed_raw.append(entry)
        
        if obj_type.key == "form":
            parent_ids = [sid for name, obj in base_objects.items() if name in changed_raw_names for sid in [form_schema_id_from_object(obj)] if sid]
        else:
            parent_ids = [obj.get("id") for name, obj in base_objects.items() if name in changed_raw_names and obj.get("id")]
        log.info("incremental deep expansion env=%s type=%s changed_objects=%s parent_ids=%s deleted=%s", env.name, obj_type.key, len(changed_raw), len(parent_ids), deleted_names)
        update_env_state(env.name, phase="deep-refresh", phase_label=f"Läser djupmetadata för {obj_type.label}", phase_current=0, phase_total=len(changed_raw))
        _, changed_deep = await collect(env, obj_type, q=None, ignore_fields=set(), deep=True, parent_ids=parent_ids, raw_override=changed_raw)
        update_env_state(env.name, phase="deep-refresh", phase_label=f"Läser djupmetadata för {obj_type.label}", phase_current=len(changed_raw), phase_total=len(changed_raw))

        merged = {name: obj for name, obj in cached_objects.items() if name not in set(deleted_names) and name in base_objects}
        for name, obj in base_objects.items():
            if name not in changed_raw_names and name in merged:
                # Update the cheap/base values while preserving already cached deep metadata.
                deep_metadata = (merged[name].get("values", {}) or {}).get("__deep_metadata")
                new_values = dict(obj.get("values", {}) or {})
                if deep_metadata:
                    new_values["__deep_metadata"] = deep_metadata
                merged[name] = {**obj, "values": new_values, "fingerprint": fingerprint(new_values, {obj_type.name_field, "Request ID", "Record ID"})}
        merged.update(changed_deep)
        row = save_cache(env.name, obj_type, True, merged)
        row.update({"changed": True, "mode": mode, "changed_objects": len(changed_names), "deleted_objects": len(deleted_names), "reused_objects": len(merged) - len(changed_deep)})
        return row

    # First cache, auto-missing cache or forced full sync: build deep snapshot.
    update_env_state(env.name, phase="full-snapshot", phase_label=f"Bygger cache för {obj_type.label}", phase_current=0, phase_total=0)
    full_reason = "missing-cache" if mode == "auto" and not current else "forced-full"
    log.info("full deep snapshot env=%s type=%s reason=%s", env.name, obj_type.key, full_reason)
    if scope_is_active() and obj_type.key in WORKFLOW_FORM_SCOPED_TYPES:
        raw, parent_ids = await scoped_workflow_raw_and_parent_ids(env, obj_type)
        _, objects = await collect(env, obj_type, q=None, ignore_fields=set(), deep=True, parent_ids=parent_ids, raw_override=raw)
    elif scope_is_active() and obj_type.key in GUIDE_FORM_SCOPED_TYPES:
        raw, parent_ids = await scoped_guide_raw_and_parent_ids(env, obj_type)
        _, objects = await collect(env, obj_type, q=None, ignore_fields=set(), deep=True, parent_ids=parent_ids, raw_override=raw)
    else:
        raw_q = scoped_base_qualification(obj_type)
        async with HelixClient(env) as client:
            raw = await client.fetch_entries(obj_type, q=raw_q)
        parent_ids = parent_ids_from_raw(raw, obj_type)
        _, objects = await collect(env, obj_type, q=None, ignore_fields=set(), deep=True, parent_ids=parent_ids, raw_override=raw)
    update_env_state(env.name, phase="save-cache", phase_label=f"Sparar cache för {obj_type.label}", phase_current=len(objects), phase_total=len(objects))
    row = save_cache(env.name, obj_type, True, objects)
    log.info("sync_one saved env=%s type=%s deep=%s count=%s max_timestamp=%s", env.name, obj_type.key, True, row.get("count"), row.get("max_timestamp"))
    row["changed"] = True
    row["mode"] = mode
    return row


async def sync_environments(envs: list[Environment], object_types: list[ObjectType], deep: bool = True, mode: str = "full", source: str = "manual") -> dict[str, Any]:
    global _last_sync_result
    async with _sync_lock:
        rows: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        job_started = now_iso()
        env_concurrency = 1  # deliberately sequential; easier to follow and gentler on AR Server
        obj_concurrency = max(1, int(os.getenv("HELIX_OBJECT_CONCURRENCY", "1") or "1"))
        log.info("sync_environments start mode=%s source=%s envs=%s object_types=%s env_concurrency=%s object_concurrency=%s", mode, source, [e.name for e in envs], [t.key for t in object_types], env_concurrency, obj_concurrency)
        async def sync_env(env: Environment) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
            env_rows: list[dict[str, Any]] = []
            env_errors: list[dict[str, Any]] = []
            total = len(object_types)
            done = 0
            obj_sem = asyncio.Semaphore(obj_concurrency)
            log.info("Starting %s sync for %s (%s object types, deep-only, object_concurrency=%s, env_sync=sequential)", mode, env.name, total, obj_concurrency)
            update_env_state(env.name, status="syncing", message=f"Synkar ({mode})", progress_done=0, progress_total=total, last_started_at=job_started, last_error=None, phase="start", phase_label="Startar synk", phase_current=0, phase_total=total)

            async def sync_obj(obj_type: ObjectType) -> None:
                nonlocal done
                async with obj_sem:
                    try:
                        update_env_state(env.name, status="syncing", current_object=obj_type.label, message=f"Hämtar {obj_type.label}", progress_done=done, progress_total=total, phase="object", phase_label=f"Synkar {obj_type.label}", phase_current=done, phase_total=total)
                        log.info("Sync %s/%s", env.name, obj_type.label)
                        row = await sync_one(env, obj_type, deep=True, mode=mode)
                        env_rows.append(row)
                    except Exception as exc:
                        log.exception("Sync failed for %s/%s", env.name, obj_type.label)
                        env_errors.append({"env": env.name, "object_type": obj_type.key, "label": obj_type.label, "error": str(exc)})
                    finally:
                        done += 1
                        update_env_state(env.name, status="syncing", progress_done=done, progress_total=total, phase="object", phase_label=f"Klar med {obj_type.label}", phase_current=done, phase_total=total)

            if obj_concurrency == 1:
                for t in object_types:
                    await sync_obj(t)
            else:
                await asyncio.gather(*(sync_obj(t) for t in object_types))

            update_env_state(
                env.name,
                status="error" if env_errors else "synced",
                message=f"{len(env_errors)} fel" if env_errors else "Synkad",
                current_object=None,
                progress_done=done,
                progress_total=total,
                last_sync_at=now_iso() if not env_errors else None,
                last_error="; ".join(e["error"] for e in env_errors[:3]) if env_errors else None,
            )
            return env_rows, env_errors

        # Sync one environment at a time. This avoids overloading the AR Server,
        # makes the pod log readable and keeps the GUI progress meaningful.
        for env in envs:
            env_rows, env_errors = await sync_env(env)
            rows.extend(env_rows)
            errors.extend(env_errors)
        _last_sync_result = {"rows": rows, "errors": errors, "mode": mode, "source": source, "started_at": job_started, "finished_at": now_iso(), "cache_model": "migrator-like-deep"}
        append_job(_last_sync_result)
        log.info("sync_environments finished rows=%s errors=%s", len(rows), len(errors))
        return _last_sync_result

def public_envs(envs: list[Environment]) -> list[dict[str, str]]:
    return [{"name": env.name, "base_url": env.base_url} for env in envs]


def get_env_pair(envs: list[Environment], source_name: str | None, target_name: str | None) -> list[Environment]:
    by_name = {env.name: env for env in envs}
    if not source_name or not target_name:
        raise ValueError("Välj både källmiljö och målmiljö.")
    if source_name == target_name:
        raise ValueError("Välj två olika miljöer att jämföra.")
    if source_name not in by_name or target_name not in by_name:
        raise ValueError("Vald miljö finns inte i serverns konfiguration.")
    return [by_name[source_name], by_name[target_name]]


async def compare_one_type(envs: list[Environment], obj_type: ObjectType, prefix: str | None, contains: str | None, ignore_fields: set[str]):
    # Endast komplett/djup cache används. Jämförelser gör inga livehämtningar.
    collected = [(e.name, objects_from_cache(e, obj_type, True, prefix, contains, ignore_fields)) for e in envs]
    result = compare_by_name(dict(collected))
    for row in result["rows"]:
        if row["status"] == "different":
            row["field_diffs"] = field_diffs(row["objects"], ignore_fields)
    return result


def default_form(envs: list[Environment]) -> dict[str, str]:
    return {
        "object_type": "all",
        "source_env": envs[0].name if len(envs) >= 1 else "",
        "target_env": envs[1].name if len(envs) >= 2 else "",
        "prefix": "",
        "contains": "",
        "ignore": "",
        "ignore_options": default_ignore_options(),
        "deep": "on",
        "use_cache": "on",
    }


def page_context(request: Request, form: dict[str, Any] | None = None, result_groups=None, error=None, message=None):
    envs, types = load_config()
    if len(envs) < 2 and not error:
        error = "Minst två miljöer måste definieras i serverns konfiguration innan du kan jämföra."
    return {
        "request": request,
        "envs": public_envs(envs),
        "types": types,
        "result_groups": result_groups,
        "error": error,
        "message": message,
        "form": form or default_form(envs),
        "cache_status": cache_status(envs, types),
        "last_sync_result": _last_sync_result,
        "sync_running": sync_running(),
        "deep_profile": os.getenv("HELIX_DEEP_PROFILE", "balanced"),
        "related_fetch_concurrency": os.getenv("HELIX_RELATED_FETCH_CONCURRENCY", "4"),
        "ignore_groups": IGNORE_GROUPS,
        "cache_scope": load_cache_scope().normalized(),
    }


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", page_context(request))


@app.post("/sync", response_class=HTMLResponse)
async def sync_from_ui(
    request: Request,
    source_env: Annotated[str | None, Form()] = None,
    target_env: Annotated[str | None, Form()] = None,
    object_type: Annotated[str, Form()] = "all",
    sync_mode: Annotated[str, Form()] = "incremental",
):
    config_envs, types = load_config()
    form = default_form(config_envs)
    form.update({"source_env": source_env or form["source_env"], "target_env": target_env or form["target_env"], "object_type": object_type, "deep": "on"})
    try:
        envs = get_env_pair(config_envs, source_env, target_env)
        selected_types = list(types.values()) if object_type == "all" else [types[object_type]]
        mode = "incremental"
        result = await start_sync_job(envs, selected_types, deep=True, mode=mode, source="manual-form")
        ctx = page_context(request, form=form, message=result.get("message") or "Synk startad i bakgrunden. Status uppdateras i miljöpanelen.")
    except (HelixError, KeyError, ValueError) as exc:
        ctx = page_context(request, form=form, error=str(exc))
    return templates.TemplateResponse("index.html", ctx)


@app.post("/compare", response_class=HTMLResponse)
async def compare(
    request: Request,
    object_type: Annotated[str, Form()] = "all",
    source_env: Annotated[str | None, Form()] = None,
    target_env: Annotated[str | None, Form()] = None,
    prefix: Annotated[str | None, Form()] = None,
    contains: Annotated[str | None, Form()] = None,
    ignore_custom: Annotated[str | None, Form()] = "",
    ignore_options: Annotated[list[str] | None, Form()] = None,

):
    config_envs, types = load_config()
    form = {
        "object_type": object_type,
        "source_env": source_env or "",
        "target_env": target_env or "",
        "prefix": prefix or "",
        "contains": contains or "",
        "ignore": ignore_custom or "",
        "ignore_options": ignore_options if ignore_options is not None else [],
        "deep": "on",
        "use_cache": "on",
    }

    try:
        envs = get_env_pair(config_envs, source_env, target_env)
        ignore_fields = ignore_fields_from_options(ignore_options, ignore_custom)
        selected_types = list(types.values()) if object_type == "all" else [types[object_type]]
        result_groups = []
        for obj_type in selected_types:
            result = await compare_one_type(envs, obj_type, prefix, contains, ignore_fields)
            result_groups.append({"type": obj_type, "result": result})
        ctx = page_context(request, form=form, result_groups=result_groups)
    except (HelixError, KeyError, ValueError, FileNotFoundError) as exc:
        ctx = page_context(request, form=form, error=str(exc))
    return templates.TemplateResponse("index.html", ctx)


@app.post("/api/sync/start")
async def api_sync_start(payload: dict):
    log.info("/api/sync/start payload=%s", {k: v for k, v in payload.items() if k not in {"password"}})
    config_envs, types = load_config()
    by_name = {e.name: e for e in config_envs}
    names = payload.get("environments")
    if not names:
        source = payload.get("source_env")
        target = payload.get("target_env")
        names = [n for n in [source, target] if n]
    if not names:
        names = [e.name for e in config_envs]
    envs = [by_name[n] for n in names if n in by_name]
    selected = payload.get("object_type", "all")
    object_types = list(types.values()) if selected == "all" else [types[selected]]
    res = await start_sync_job(envs, object_types, deep=True, mode=payload.get("mode", "incremental"), source="ui")
    return JSONResponse(res)


@app.post("/api/sync/{environment_name}")
async def api_sync_environment(environment_name: str, payload: dict | None = None):
    """Starta incremental sync för en specifik miljö som bakgrundsjobb."""
    payload = payload or {}
    config_envs, types = load_config()
    by_name = {e.name: e for e in config_envs}
    if environment_name not in by_name:
        return JSONResponse({"error": f"Miljön finns inte i konfigurationen: {environment_name}"}, status_code=404)
    selected = payload.get("object_type", "all")
    object_types = list(types.values()) if selected == "all" else [types[selected]]
    res = await start_sync_job([by_name[environment_name]], object_types, deep=True, mode="incremental", source="api-env")
    return JSONResponse(res)


@app.post("/api/sync")
async def api_sync(payload: dict):
    config_envs, types = load_config()
    env_names = payload.get("environments") or [e.name for e in config_envs]
    by_name = {e.name: e for e in config_envs}
    envs = [by_name[n] for n in env_names if n in by_name]
    selected = payload.get("object_type", "all")
    object_types = list(types.values()) if selected == "all" else [types[selected]]
    result = await sync_environments(envs, object_types, deep=True, mode=payload.get("mode", "incremental"), source="api")
    return JSONResponse(result)


@app.get("/api/cache/status")
async def api_cache_status():
    envs, types = load_config()
    return JSONResponse({"cache": cache_status(envs, types), "sync_running": sync_running(), "last_sync_result": _last_sync_result, "cache_scope": load_cache_scope().normalized()})


@app.post("/api/compare")
async def api_compare(payload: dict):
    config_envs, types = load_config()
    try:
        envs = get_env_pair(config_envs, payload.get("source_env"), payload.get("target_env"))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    ignore_fields = set(payload.get("ignore_fields", ["timestamp"]))
    object_type = payload.get("object_type", "all")
    selected_types = list(types.values()) if object_type == "all" else [types[object_type]]
    groups = []
    try:
        for obj_type in selected_types:
            result = await compare_one_type(envs, obj_type, payload.get("prefix"), payload.get("contains"), ignore_fields)
            groups.append({"object_type": obj_type.key, "label": obj_type.label, "result": result})
    except (HelixError, KeyError, FileNotFoundError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
    return JSONResponse({"groups": groups})

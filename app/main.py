from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from typing import Annotated, Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .cache import append_job, cache_status, load_cache, mark_cache_checked, objects_from_cache, periodic_sync, save_cache, update_env_state, now_iso, has_complete_cache
from .config import Environment, ObjectType, load_config
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

app = FastAPI(title="BMC HLX Workflow Diff", version="1.2.0")
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
    interval = int(os.getenv("HELIX_SYNC_INTERVAL_SECONDS", "0") or "0")
    sync_on_start = os.getenv("HELIX_SYNC_ON_START", "true").lower() in {"1", "true", "yes", "on"}

    async def run_incremental():
        envs, types = load_config()
        await sync_environments(envs, list(types.values()), deep=True, mode="incremental", source="interval")

    log.info("sync settings: HELIX_SYNC_ON_START=%s HELIX_SYNC_INTERVAL_SECONDS=%s CACHE_MODE=deep-only", sync_on_start, interval)
    if sync_on_start:
        async def run_startup():
            envs, types = load_config()
            object_types = list(types.values())
            mode = "auto"
            log.info("queueing startup auto sync for environments=%s cache_mode=deep-only", [e.name for e in envs])
            await start_sync_job(envs, object_types, deep=True, mode=mode, source="startup")
        asyncio.create_task(run_startup())
    else:
        log.info("startup sync disabled")

    if interval > 0:
        log.info("starting periodic incremental sync every %s seconds", interval)
        asyncio.create_task(periodic_sync(run_incremental, interval))


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
    fields = list(obj_type.id_fields) + [
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
            for form, grouped, exc in await asyncio.gather(*(fetch_one_related(rel) for rel in obj_type.related_forms)):
                if grouped is not None:
                    related[form] = grouped
            log.info("deep related fetch finished env=%s type=%s successful_forms=%s/%s", env.name, obj_type.key, len(related), len(obj_type.related_forms))
        normalized = normalize_entries(env.name, obj_type, raw, ignore_fields, related)
        log.info("collect normalized env=%s type=%s objects=%s related_forms=%s", env.name, obj_type.key, len(normalized), list(related.keys()))
        return env.name, normalized


def base_values_for_compare(values: dict[str, Any]) -> dict[str, Any]:
    data = dict(values or {})
    data.pop("__deep_metadata", None)
    return data


def base_fingerprint_for_object(obj: dict[str, Any], obj_type: ObjectType) -> str:
    return fingerprint(base_values_for_compare(obj.get("values", {}) or {}), {obj_type.name_field, "Request ID", "Record ID"})


async def collect_base_only(env: Environment, obj_type: ObjectType) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    async with HelixClient(env) as client:
        raw = await client.fetch_entries(obj_type, q=build_qualification(obj_type))
    _, base_objects = await collect(env, obj_type, q=None, ignore_fields=set(), deep=False, raw_override=raw)
    return raw, base_objects

def _and_qualification(*parts: str | None) -> str | None:
    cleaned = [p for p in parts if p]
    if not cleaned:
        return None
    return " AND ".join(f"({p})" for p in cleaned)


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
    current = load_cache(env.name, obj_type, True)

    if mode in {"incremental", "auto"} and current:
        raw, base_objects = await collect_base_only(env, obj_type)
        cached_objects = current.get("objects", {}) or {}
        changed_names: list[str] = []
        deleted_names = sorted(set(cached_objects) - set(base_objects))

        for name, base_obj in base_objects.items():
            cached = cached_objects.get(name)
            if not cached:
                changed_names.append(name)
                continue
            if base_fingerprint_for_object(base_obj, obj_type) != base_fingerprint_for_object(cached, obj_type):
                changed_names.append(name)

        mark_cache_checked(env.name, obj_type, True, changed=bool(changed_names or deleted_names))
        log.info(
            "incremental scan env=%s type=%s total=%s changed=%s deleted=%s reused=%s",
            env.name, obj_type.key, len(base_objects), len(changed_names), len(deleted_names), len(base_objects) - len(changed_names),
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
        parent_ids = [obj.get("id") for name, obj in base_objects.items() if name in changed_raw_names and obj.get("id")]
        log.info("incremental deep expansion env=%s type=%s changed_objects=%s parent_ids=%s deleted=%s", env.name, obj_type.key, len(changed_raw), len(parent_ids), deleted_names)
        _, changed_deep = await collect(env, obj_type, q=None, ignore_fields=set(), deep=True, parent_ids=parent_ids, raw_override=changed_raw)

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
    full_reason = "missing-cache" if mode == "auto" and not current else "forced-full"
    log.info("full deep snapshot env=%s type=%s reason=%s", env.name, obj_type.key, full_reason)
    _, objects = await collect(env, obj_type, q=build_qualification(obj_type), ignore_fields=set(), deep=True)
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
        env_concurrency = max(1, int(os.getenv("HELIX_ENV_CONCURRENCY", "3") or "3"))
        obj_concurrency = max(1, int(os.getenv("HELIX_OBJECT_CONCURRENCY", "4") or "4"))
        log.info("sync_environments start mode=%s source=%s envs=%s object_types=%s env_concurrency=%s object_concurrency=%s", mode, source, [e.name for e in envs], [t.key for t in object_types], env_concurrency, obj_concurrency)
        env_sem = asyncio.Semaphore(env_concurrency)

        async def sync_env(env: Environment) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
            async with env_sem:
                env_rows: list[dict[str, Any]] = []
                env_errors: list[dict[str, Any]] = []
                total = len(object_types)
                done = 0
                done_lock = asyncio.Lock()
                obj_sem = asyncio.Semaphore(obj_concurrency)
                log.info("Starting %s sync for %s (%s object types, deep-only, object_concurrency=%s)", mode, env.name, total, obj_concurrency)
                update_env_state(env.name, status="syncing", message=f"Synkar ({mode})", progress_done=0, progress_total=total, last_started_at=job_started, last_error=None)

                async def sync_obj(obj_type: ObjectType) -> None:
                    nonlocal done
                    async with obj_sem:
                        try:
                            update_env_state(env.name, status="syncing", current_object=obj_type.label, message=f"Hämtar {obj_type.label}", progress_done=done, progress_total=total)
                            log.info("Sync %s/%s", env.name, obj_type.label)
                            row = await sync_one(env, obj_type, deep=True, mode=mode)
                            env_rows.append(row)
                        except Exception as exc:
                            log.exception("Sync failed for %s/%s", env.name, obj_type.label)
                            env_errors.append({"env": env.name, "object_type": obj_type.key, "label": obj_type.label, "error": str(exc)})
                        finally:
                            async with done_lock:
                                done += 1
                                update_env_state(env.name, status="syncing" if done < total else "syncing", progress_done=done, progress_total=total)

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

        results = await asyncio.gather(*(sync_env(env) for env in envs))
        for env_rows, env_errors in results:
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
            row["field_diffs"] = field_diffs(row["objects"])
    return result


def default_form(envs: list[Environment]) -> dict[str, str]:
    return {
        "object_type": "all",
        "source_env": envs[0].name if len(envs) >= 1 else "",
        "target_env": envs[1].name if len(envs) >= 2 else "",
        "prefix": "",
        "contains": "",
        "ignore": "timestamp",
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
    sync_mode: Annotated[str, Form()] = "full",
):
    config_envs, types = load_config()
    form = default_form(config_envs)
    form.update({"source_env": source_env or form["source_env"], "target_env": target_env or form["target_env"], "object_type": object_type, "deep": "on"})
    try:
        envs = get_env_pair(config_envs, source_env, target_env)
        selected_types = list(types.values()) if object_type == "all" else [types[object_type]]
        mode = "incremental" if sync_mode == "incremental" else "full"
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
    ignore: Annotated[str | None, Form()] = "timestamp",

):
    config_envs, types = load_config()
    form = {
        "object_type": object_type,
        "source_env": source_env or "",
        "target_env": target_env or "",
        "prefix": prefix or "",
        "contains": contains or "",
        "ignore": ignore or "",
        "deep": "on",
        "use_cache": "on",
    }

    try:
        envs = get_env_pair(config_envs, source_env, target_env)
        ignore_fields = {x.strip() for x in (ignore or "").split(",") if x.strip()}
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
    res = await start_sync_job(envs, object_types, deep=True, mode=payload.get("mode", "full"), source="ui")
    return JSONResponse(res)


@app.post("/api/sync")
async def api_sync(payload: dict):
    config_envs, types = load_config()
    env_names = payload.get("environments") or [e.name for e in config_envs]
    by_name = {e.name: e for e in config_envs}
    envs = [by_name[n] for n in env_names if n in by_name]
    selected = payload.get("object_type", "all")
    object_types = list(types.values()) if selected == "all" else [types[selected]]
    result = await sync_environments(envs, object_types, deep=True, mode=payload.get("mode", "full"), source="api")
    return JSONResponse(result)


@app.get("/api/cache/status")
async def api_cache_status():
    envs, types = load_config()
    return JSONResponse({"cache": cache_status(envs, types), "sync_running": sync_running(), "last_sync_result": _last_sync_result})


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

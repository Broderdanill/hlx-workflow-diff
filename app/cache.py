from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import time

from .config import Environment, ObjectType
from .helix import fingerprint
from .diffing import comparable_values


log = logging.getLogger("hlx.workflow_diff.cache")

_PAYLOAD_CACHE: dict[tuple[str, str, bool], tuple[float, dict[str, Any]]] = {}
_INDEX_CACHE: dict[tuple[str, str, bool], tuple[float, dict[str, Any]]] = {}


def cache_dir() -> Path:
    path = Path(os.getenv("HELIX_CACHE_DIR") or os.getenv("CACHE_DIR") or "/tmp/cache")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _safe(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in value)


def cache_path(env_name: str, object_key: str, deep: bool) -> Path:
    suffix = "deep" if deep else "standard"
    return cache_dir() / f"{_safe(env_name)}__{_safe(object_key)}__{suffix}.json"


def index_path(env_name: str, object_key: str, deep: bool) -> Path:
    suffix = "deep" if deep else "standard"
    return cache_dir() / f"{_safe(env_name)}__{_safe(object_key)}__{suffix}.index.json"


def state_path() -> Path:
    return cache_dir() / "sync-state.json"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def active_scope_signature() -> str:
    return os.getenv("HELIX_CACHE_SCOPE_SIGNATURE", "") or ""


def active_scope_json() -> str:
    return os.getenv("HELIX_CACHE_SCOPE_JSON", "{}") or "{}"


def read_state() -> dict[str, Any]:
    path = state_path()
    if not path.exists():
        return {"environments": {}, "jobs": [], "updated_at": None}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"environments": {}, "jobs": [], "updated_at": None}


def write_state(state: dict[str, Any]) -> dict[str, Any]:
    state["updated_at"] = now_iso()
    path = state_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True, default=str), encoding="utf-8")
    tmp.replace(path)
    log.debug("state written path=%s envs=%s jobs=%s", path, len(state.get("environments", {})), len(state.get("jobs", [])))
    return state


def update_env_state(env_name: str, **changes: Any) -> dict[str, Any]:
    log.info("sync state update env=%s changes=%s", env_name, changes)
    state = read_state()
    env_state = state.setdefault("environments", {}).setdefault(env_name, {})
    env_state.update(changes)
    write_state(state)
    return env_state


def append_job(job: dict[str, Any], keep: int = 30) -> None:
    state = read_state()
    jobs = state.setdefault("jobs", [])
    jobs.insert(0, job)
    del jobs[keep:]
    write_state(state)



def _norm(value: Any) -> str:
    return str(value or "").casefold()


def _index_text(obj: dict[str, Any], obj_type: ObjectType) -> str:
    values = obj.get("values", {}) or {}
    parts = [obj.get("name", "")]
    for field in obj_type.search_fields or [obj_type.name_field]:
        val = values.get(field)
        if val is not None:
            parts.append(val)
    return "\n".join(_norm(x) for x in parts if x is not None)


def build_search_index(obj_type: ObjectType, objects: dict[str, dict[str, Any]]) -> dict[str, Any]:
    entries = []
    prefix_map: dict[str, list[str]] = {}
    for name, obj in objects.items():
        name_text = _norm(obj.get("name") or name)
        search_text = _index_text(obj, obj_type)
        entries.append({"name": name, "name_lower": name_text, "search_text": search_text})
        # Prefix acceleration. Index reasonably short prefixes; longer prefixes still scan entries.
        compact = name_text.replace(" ", "")
        for i in range(1, min(len(compact), 24) + 1):
            prefix_map.setdefault(compact[:i], []).append(name)
    return {"built_at": now_iso(), "count": len(entries), "entries": entries, "prefix_map": prefix_map}


def save_search_index(env_name: str, obj_type: ObjectType, deep: bool, objects: dict[str, dict[str, Any]]) -> None:
    path = index_path(env_name, obj_type.key, deep)
    payload = {
        "env": env_name,
        "object_type": obj_type.key,
        "deep": deep,
        "scope_signature": active_scope_signature(),
        "index": build_search_index(obj_type, objects),
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str), encoding="utf-8")
    tmp.replace(path)
    _INDEX_CACHE.pop((env_name, obj_type.key, deep), None)
    log.info("search index saved env=%s type=%s deep=%s path=%s count=%s", env_name, obj_type.key, deep, path, len(objects))


def load_search_index(env_name: str, obj_type: ObjectType, deep: bool) -> dict[str, Any] | None:
    path = index_path(env_name, obj_type.key, deep)
    if not path.exists():
        return None
    try:
        mtime = path.stat().st_mtime
        key = (env_name, obj_type.key, deep)
        cached = _INDEX_CACHE.get(key)
        if cached and cached[0] == mtime:
            return cached[1]
        payload = json.loads(path.read_text(encoding="utf-8"))
        if (payload.get("scope_signature") or "") != active_scope_signature():
            return None
        _INDEX_CACHE[key] = (mtime, payload)
        return payload
    except Exception as exc:
        log.warning("search index load failed env=%s type=%s: %s", env_name, obj_type.key, exc)
        return None


def _candidate_names_from_index(env_name: str, obj_type: ObjectType, deep: bool, prefix: str | None, contains: str | None) -> set[str] | None:
    idx_payload = load_search_index(env_name, obj_type, deep)
    if not idx_payload:
        return None
    idx = idx_payload.get("index") or {}
    entries = idx.get("entries") or []
    prefix_value = _norm(str(prefix or "").strip().rstrip("*"))
    contains_value = _norm(str(contains or "").strip())
    if not prefix_value and not contains_value:
        return None
    if prefix_value and not contains_value:
        names = idx.get("prefix_map", {}).get(prefix_value.replace(" ", ""))
        if names is not None:
            return set(names)
    result: set[str] = set()
    for row in entries:
        if prefix_value and not str(row.get("name_lower", "")).startswith(prefix_value):
            continue
        if contains_value and contains_value not in str(row.get("search_text", "")):
            continue
        result.add(row.get("name"))
    return {x for x in result if x}

def save_cache(env_name: str, obj_type: ObjectType, deep: bool, objects: dict[str, dict[str, Any]]) -> dict[str, Any]:
    log.info("cache save start env=%s type=%s deep=%s objects=%s", env_name, obj_type.key, deep, len(objects))
    payload = {
        "env": env_name,
        "object_type": obj_type.key,
        "label": obj_type.label,
        "deep": deep,
        "synced_at": now_iso(),
        "count": len(objects),
        "max_timestamp": max_timestamp(objects),
        "objects": objects,
        "scope_signature": active_scope_signature(),
        "scope": json.loads(active_scope_json()),
    }
    path = cache_path(env_name, obj_type.key, deep)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str), encoding="utf-8")
    tmp.replace(path)
    _PAYLOAD_CACHE.pop((env_name, obj_type.key, deep), None)
    try:
        save_search_index(env_name, obj_type, deep, objects)
    except Exception as exc:
        log.warning("search index save failed env=%s type=%s: %s", env_name, obj_type.key, exc)
    log.info("cache saved env=%s type=%s deep=%s path=%s count=%s max_timestamp=%s", env_name, obj_type.key, deep, path, len(objects), payload["max_timestamp"])
    return {
        "env": env_name,
        "object_type": obj_type.key,
        "label": obj_type.label,
        "deep": deep,
        "synced_at": payload["synced_at"],
        "count": len(objects),
        "max_timestamp": payload["max_timestamp"],
    }


def load_cache(env_name: str, obj_type: ObjectType, deep: bool) -> dict[str, Any] | None:
    path = cache_path(env_name, obj_type.key, deep)
    if not path.exists():
        log.debug("cache miss env=%s type=%s deep=%s path=%s", env_name, obj_type.key, deep, path)
        return None
    key = (env_name, obj_type.key, deep)
    mtime = path.stat().st_mtime
    cached = _PAYLOAD_CACHE.get(key)
    if cached and cached[0] == mtime:
        payload = cached[1]
    else:
        payload = json.loads(path.read_text(encoding="utf-8"))
        _PAYLOAD_CACHE[key] = (mtime, payload)
    current_sig = active_scope_signature()
    cached_sig = payload.get("scope_signature", "") or ""
    if current_sig and cached_sig != current_sig:
        log.info("cache ignored because scope changed env=%s type=%s deep=%s cached_scope=%s current_scope=%s path=%s", env_name, obj_type.key, deep, cached_sig, current_sig, path)
        return None
    log.debug("cache hit env=%s type=%s deep=%s count=%s path=%s", env_name, obj_type.key, deep, payload.get("count"), path)
    return payload




def has_complete_cache(envs: list[Environment], object_types: list[ObjectType]) -> bool:
    """Return True when every configured environment/object type has the complete deep snapshot."""
    for env in envs:
        for obj_type in object_types:
            if load_cache(env.name, obj_type, True) is None:
                log.info("complete cache missing env=%s type=%s", env.name, obj_type.key)
                return False
    return True

def _timestamp_value(obj: dict[str, Any]) -> int | None:
    values = obj.get("values", {}) or {}
    for key in ("timestamp", "Last Modified On", "Modified Date"):
        value = values.get(key)
        if value in (None, ""):
            continue
        try:
            return int(value)
        except Exception:
            continue
    return None


def max_timestamp(objects: dict[str, dict[str, Any]]) -> int | None:
    timestamps = [ts for obj in objects.values() if (ts := _timestamp_value(obj)) is not None]
    return max(timestamps) if timestamps else None


def environment_summary(env: Environment, types: dict[str, ObjectType]) -> dict[str, Any]:
    object_count = 0
    last_sync_values: list[str] = []
    last_check_values: list[str] = []
    cached_types = 0
    max_ts_values: list[int] = []
    for obj_type in types.values():
        payload = load_cache(env.name, obj_type, True)
        if payload:
            cached_types += 1
            object_count += int(payload.get("count") or len(payload.get("objects", {})))
            if payload.get("synced_at"):
                last_sync_values.append(payload["synced_at"])
            if payload.get("last_checked_at"):
                last_check_values.append(payload["last_checked_at"])
            if payload.get("max_timestamp") is not None:
                max_ts_values.append(int(payload["max_timestamp"]))
    runtime = read_state().get("environments", {}).get(env.name, {})
    return {
        "env": env.name,
        "status": runtime.get("status", "not_synced" if cached_types == 0 else "synced"),
        "message": runtime.get("message"),
        "current_object": runtime.get("current_object"),
        "phase": runtime.get("phase"),
        "phase_label": runtime.get("phase_label"),
        "phase_current": runtime.get("phase_current", 0),
        "phase_total": runtime.get("phase_total", 0),
        "progress_done": runtime.get("progress_done", 0),
        "progress_total": runtime.get("progress_total", 0),
        "last_error": runtime.get("last_error"),
        "last_started_at": runtime.get("last_started_at"),
        "last_sync_at": max(last_sync_values) if last_sync_values else runtime.get("last_sync_at"),
        "last_checked_at": max(last_check_values) if last_check_values else runtime.get("last_checked_at"),
        "object_count": object_count,
        "deep_count": object_count,
        "standard_count": 0,
        "cached_types": cached_types,
        "deep_types": cached_types,
        "total_types": len(types),
        "max_timestamp": max(max_ts_values) if max_ts_values else None,
    }

def cache_status(envs: list[Environment], types: dict[str, ObjectType]) -> dict[str, Any]:
    rows = []
    for env in envs:
        for obj_type in types.values():
            payload = load_cache(env.name, obj_type, True)
            row = {"env": env.name, "object_type": obj_type.key, "label": obj_type.label, "cache": None, "deep": None, "standard": None}
            if payload:
                row["cache"] = row["deep"] = {
                    "synced_at": payload.get("synced_at"),
                    "last_checked_at": payload.get("last_checked_at"),
                    "count": payload.get("count", len(payload.get("objects", {}))),
                    "max_timestamp": payload.get("max_timestamp"),
                }
            rows.append(row)
    return {
        "environments": [environment_summary(env, types) for env in envs],
        "objects": rows,
        "state": read_state(),
        "cache_mode": "deep-only",
        "scope": json.loads(active_scope_json()),
        "scope_signature": active_scope_signature(),
    }

def mark_cache_checked(env_name: str, obj_type: ObjectType, deep: bool, changed: bool, checked_at: str | None = None) -> None:
    payload = load_cache(env_name, obj_type, deep)
    if not payload:
        return
    payload["last_checked_at"] = checked_at or now_iso()
    payload["last_incremental_had_changes"] = bool(changed)
    path = cache_path(env_name, obj_type.key, deep)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str), encoding="utf-8")
    tmp.replace(path)


def _matches_cached(obj: dict[str, Any], obj_type: ObjectType, prefix: str | None, contains: str | None) -> bool:
    name = obj.get("name", "")
    values = obj.get("values", {}) or {}
    if prefix:
        # GUI prefix filtering should be user-friendly and case-insensitive.
        if not str(name).lower().startswith(str(prefix).strip().lower()):
            return False
    if contains:
        needle = str(contains).strip().lower()
        fields = obj_type.search_fields or [obj_type.name_field]
        haystack = [str(name)]
        for field in fields:
            val = values.get(field)
            if val is not None:
                haystack.append(str(val))
        if not any(needle in h.lower() for h in haystack):
            return False
    return True


def objects_from_cache(env: Environment, obj_type: ObjectType, deep: bool, prefix: str | None, contains: str | None, ignore_fields: set[str]) -> dict[str, dict[str, Any]]:
    payload = load_cache(env.name, obj_type, deep)
    if not payload:
        raise FileNotFoundError(f"Ingen komplett cache finns för {env.name}/{obj_type.label}. Synka miljön först.")
    objects = payload.get("objects") or {}
    candidates = _candidate_names_from_index(env.name, obj_type, deep, prefix, contains)
    if (prefix or contains) and candidates is None:
        # Older cache may not have an index yet. Build it lazily once so repeated GUI searches are fast.
        try:
            save_search_index(env.name, obj_type, deep, objects)
            candidates = _candidate_names_from_index(env.name, obj_type, deep, prefix, contains)
        except Exception as exc:
            log.warning("lazy search index build failed env=%s type=%s: %s", env.name, obj_type.key, exc)
    iterable = ((name, objects[name]) for name in candidates if name in objects) if candidates is not None else objects.items()
    result: dict[str, dict[str, Any]] = {}
    for name, obj in iterable:
        if candidates is None and not _matches_cached(obj, obj_type, prefix, contains):
            continue
        values = dict(obj.get("values", {}) or {})
        if not deep:
            values.pop("__deep_metadata", None)
        comparable = comparable_values({**obj, "values": values}, ignore_fields | {obj_type.name_field})
        result[name] = {
            **obj,
            "values": values,
            "fingerprint": fingerprint(comparable, set()),
        }
    return result

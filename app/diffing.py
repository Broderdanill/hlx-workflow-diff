from __future__ import annotations

from typing import Any


STATUS_LABELS = {
    "equal": "Lika",
    "different": "Olika",
    "missing": "Saknas",
}

# Environment/server specific identifiers. These are useful to fetch metadata,
# but they are not stable between environments and should not make objects differ.
VOLATILE_ID_FIELDS = {
    "Request ID", "Record ID", "SystemID", "IntegerID",
    "schemaId", "schemaID", "schema_id",
    "Active Link ID", "Filter ID", "Escalation ID", "Container ID",
    "actlinkId", "filterId", "escalationId", "containerId",
    "charMenuId", "Char Menu ID",
    "resolvedfieldId", "resolvedVuiId",
}

DISPLAY_ONLY_FIELDS = {"Box1", "Box2"}


def _clean_scalar(value: Any) -> Any:
    if isinstance(value, str):
        return value.strip()
    return value


def _row_identity(row: dict[str, Any], form: str) -> str:
    """Create a stable row identity inside a related metadata form.

    IDs that only identify the parent object are intentionally avoided. For
    forms, fields should be matched primarily by field name (and then field id),
    views by VUI name/id, permissions by target id + group, actions by action
    index/type, and guide members by reference order/type/name. The goal is a
    Migrator-like diff where you see which field/action/view/permission differs
    instead of only seeing that a related row count changed.
    """
    f = form.lower()

    candidates: list[list[str]] = []
    if "field_permissions" in f:
        candidates = [["fieldId", "groupId"], ["fieldName", "groupId"]]
    elif "field_enum_values" in f:
        candidates = [["fieldId", "enumItem"], ["fieldId", "enumValue"], ["fieldId", "enumLabel"]]
    elif f.endswith(": field") or " metadata: field" in f:
        candidates = [["fieldName"], ["fieldId"]]
    elif "schema_group_ids" in f or "group_ids" in f or "arctr_group_ids" in f:
        candidates = [["groupId"], ["permission", "groupId"]]
    elif "schema_index" in f:
        candidates = [["indexName"], ["listIndex"], ["f1", "f2", "f3", "f4", "f5"]]
    elif "view_mapping" in f:
        candidates = [["fieldId", "extField"], ["fieldName", "extField"]]
    elif f.endswith(": vui") or " metadata: vui" in f:
        candidates = [["vuiName", "locale"], ["vuiId"], ["resolvedName", "locale"]]
    elif "arreference" in f:
        candidates = [["referenceOrder"], ["referenceType", "label"], ["referenceType", "valueShort"], ["referenceId", "referenceObjId"]]
    elif "cntnr_ownr_obj" in f:
        candidates = [["ownerObjType", "ownerObjId"], ["objIndex"]]
    elif "_mapping" in f or "escal_mapping" in f:
        candidates = [["schemaId", "objIndex"], ["schemaId"]]
    elif "_call" in f:
        candidates = [["actionIndex", "guideName"], ["actionIndex"]]
    elif any(x in f for x in ("_set", "_push", "_message", "_process", "_notify", "_sql", "_goto", "_open", "_serviceaction", "_log")):
        candidates = [["actionIndex", "fieldId"], ["actionIndex", "label"], ["actionIndex"]]
    elif "char_menu" in f:
        candidates = [["path", "label"], ["path", "value"], ["arschema", "path"], ["server", "path"]]

    for fields in candidates:
        parts = []
        for key in fields:
            val = row.get(key)
            if val in (None, ""):
                parts = []
                break
            parts.append(f"{key}={val}")
        if parts:
            return " | ".join(parts)

    # Last resort: use a stable representation after volatile ids have been removed.
    stable = {k: row[k] for k in sorted(row) if k not in VOLATILE_ID_FIELDS | DISPLAY_ONLY_FIELDS}
    if stable:
        return repr(stable)[:240]
    return repr(row)[:240]


def normalize_for_compare(value: Any, ignore_fields: set[str] | None = None, *, field_name: str | None = None) -> Any:
    ignore_fields = ignore_fields or set()
    ignore = VOLATILE_ID_FIELDS | DISPLAY_ONLY_FIELDS | set(ignore_fields)

    if field_name in ignore:
        return None

    if field_name == "__deep_metadata" and isinstance(value, dict):
        result: dict[str, Any] = {}
        for form, rows in sorted(value.items()):
            if form in ignore:
                continue
            if not isinstance(rows, list):
                result[form] = normalize_for_compare(rows, ignore_fields)
                continue
            mapped: dict[str, Any] = {}
            for row in rows:
                if not isinstance(row, dict):
                    key = repr(row)
                    mapped[key] = normalize_for_compare(row, ignore_fields)
                    continue
                cleaned = {
                    k: normalize_for_compare(v, ignore_fields, field_name=k)
                    for k, v in row.items()
                    if k not in ignore
                }
                cleaned = {k: v for k, v in cleaned.items() if v is not None}
                mapped[_row_identity(row, form)] = cleaned
            result[form] = mapped
        return result

    if isinstance(value, dict):
        return {
            k: normalize_for_compare(v, ignore_fields, field_name=k)
            for k, v in sorted(value.items())
            if k not in ignore
        }
    if isinstance(value, list):
        return [normalize_for_compare(v, ignore_fields) for v in value]
    return _clean_scalar(value)


def comparable_values(obj: dict[str, Any] | None, ignore_fields: set[str] | None = None) -> dict[str, Any]:
    if not obj:
        return {}
    values = obj.get("values", {}) or {}
    out = {}
    for key, value in values.items():
        if key in (ignore_fields or set()) | VOLATILE_ID_FIELDS | DISPLAY_ONLY_FIELDS:
            continue
        norm = normalize_for_compare(value, ignore_fields, field_name=key)
        if norm is not None:
            out[key] = norm
    return out


def compare_by_name(env_data: dict[str, dict[str, dict[str, Any]]]) -> dict[str, Any]:
    envs = list(env_data.keys())
    all_names = sorted(set().union(*(set(v.keys()) for v in env_data.values()))) if env_data else []
    rows = []
    summary = {"equal": 0, "different": 0, "missing": 0, "total": len(all_names)}
    for name in all_names:
        present = {env: env_data[env].get(name) for env in envs}
        missing_envs = [env for env, obj in present.items() if obj is None]
        hashes = {obj["fingerprint"] for obj in present.values() if obj}
        if missing_envs:
            status = "missing"
            summary["missing"] += 1
        elif len(hashes) == 1:
            status = "equal"
            summary["equal"] += 1
        else:
            status = "different"
            summary["different"] += 1
        rows.append({
            "name": name,
            "status": status,
            "status_label": STATUS_LABELS.get(status, status),
            "missing_envs": missing_envs,
            "objects": present,
        })
    return {"environments": envs, "rows": rows, "summary": summary}


def _field_label(field: str) -> str:
    if field == "__deep_metadata":
        return "Djup metadata / relaterade actions"
    if field.startswith("__deep_metadata."):
        return field.replace("__deep_metadata.", "")
    return field


def _append_diff(diffs: list[dict[str, Any]], field: str, values: dict[str, Any], envs: list[str]) -> None:
    if len({repr(v) for v in values.values()}) > 1:
        diffs.append({
            "field": field,
            "field_label": _field_label(field),
            "values": values,
            "left": values.get(envs[0]) if len(envs) > 0 else None,
            "right": values.get(envs[1]) if len(envs) > 1 else None,
        })


def _deep_metadata_diffs(objects: dict[str, Any], ignore_fields: set[str] | None, envs: list[str]) -> list[dict[str, Any]]:
    normalized = {
        env: normalize_for_compare((obj.get("values", {}) if obj else {}).get("__deep_metadata", {}), ignore_fields, field_name="__deep_metadata")
        for env, obj in objects.items()
    }
    forms = sorted(set().union(*(set(v.keys()) for v in normalized.values() if isinstance(v, dict))))
    diffs: list[dict[str, Any]] = []
    for form in forms:
        per_env_form = {env: (normalized.get(env, {}) or {}).get(form, {}) for env in envs}
        row_keys = sorted(set().union(*(set(v.keys()) for v in per_env_form.values() if isinstance(v, dict))))
        for row_key in row_keys:
            row_values = {env: (per_env_form.get(env, {}) or {}).get(row_key) for env in envs}
            _append_diff(diffs, f"__deep_metadata.{form} / {row_key}", row_values, envs)
    return diffs


def field_diffs(objects: dict[str, Any], ignore_fields: set[str] | None = None) -> list[dict[str, Any]]:
    fields = set()
    envs = list(objects.keys())
    for obj in objects.values():
        if obj:
            fields.update(obj.get("values", {}).keys())
    diffs: list[dict[str, Any]] = []

    def sort_key(field: str):
        return (1 if field == "__deep_metadata" else 0, field)

    for field in sorted(fields, key=sort_key):
        if field == "__deep_metadata":
            diffs.extend(_deep_metadata_diffs(objects, ignore_fields, envs))
            continue
        values = {
            env: normalize_for_compare((obj.get("values", {}).get(field) if obj else None), ignore_fields, field_name=field)
            for env, obj in objects.items()
        }
        _append_diff(diffs, field, values, envs)
    return diffs

from __future__ import annotations
from typing import Any


STATUS_LABELS = {
    "equal": "Lika",
    "different": "Olika",
    "missing": "Saknas",
}


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


def field_diffs(objects: dict[str, Any]) -> list[dict[str, Any]]:
    fields = set()
    envs = list(objects.keys())
    for obj in objects.values():
        if obj:
            fields.update(obj.get("values", {}).keys())
    diffs: list[dict[str, Any]] = []
    for field in sorted(fields):
        values = {env: (obj.get("values", {}).get(field) if obj else None) for env, obj in objects.items()}
        if len({repr(v) for v in values.values()}) > 1:
            diffs.append({
                "field": field,
                "values": values,
                "left": values.get(envs[0]) if len(envs) > 0 else None,
                "right": values.get(envs[1]) if len(envs) > 1 else None,
            })
    return diffs

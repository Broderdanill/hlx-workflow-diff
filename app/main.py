from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import Environment, load_config
from .diffing import compare_by_name, field_diffs
from .helix import HelixClient, HelixError, build_qualification, normalize_entries

app = FastAPI(title="BMC HLX Workflow Diff", version="0.7.0")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


@app.get("/healthz")
async def healthz():
    return {"ok": True}


async def collect(env: Environment, obj_type, q, ignore_fields):
    async with HelixClient(env) as client:
        raw = await client.fetch_entries(obj_type, q=q)
        return env.name, normalize_entries(env.name, obj_type, raw, ignore_fields)


def public_envs(envs: list[Environment]) -> list[dict[str, str]]:
    """Return only display-safe environment data for templates/API."""
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


async def compare_one_type(envs: list[Environment], obj_type, prefix: str | None, contains: str | None, ignore_fields: set[str]):
    q = build_qualification(obj_type, prefix=prefix, contains=contains)
    collected = await asyncio.gather(*(collect(e, obj_type, q, ignore_fields) for e in envs))
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
    }


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    envs, types = load_config()
    error = None if len(envs) >= 2 else "Minst två miljöer måste definieras i serverns konfiguration innan du kan jämföra."
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "envs": public_envs(envs),
            "types": types,
            "result_groups": None,
            "error": error,
            "form": default_form(envs),
        },
    )


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
    }

    try:
        envs = get_env_pair(config_envs, source_env, target_env)
        ignore_fields = {x.strip() for x in (ignore or "").split(",") if x.strip()}
        selected_types = list(types.values()) if object_type == "all" else [types[object_type]]
        result_groups = []
        for obj_type in selected_types:
            result = await compare_one_type(envs, obj_type, prefix, contains, ignore_fields)
            result_groups.append({"type": obj_type, "result": result})
        ctx = {"request": request, "envs": public_envs(config_envs), "types": types, "result_groups": result_groups, "error": None, "form": form}
    except (HelixError, KeyError, ValueError) as exc:
        ctx = {"request": request, "envs": public_envs(config_envs), "types": types, "result_groups": None, "error": str(exc), "form": form}
    return templates.TemplateResponse("index.html", ctx)


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
    except (HelixError, KeyError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
    return JSONResponse({"groups": groups})

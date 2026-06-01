from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Environment:
    name: str
    base_url: str
    username: str
    password: str
    verify_tls: bool = True
    auth_string: str | None = None


@dataclass
class ObjectType:
    key: str
    label: str
    form: str
    name_field: str = "name"
    type_field: str | None = None
    type_values: list[int | str] = field(default_factory=list)
    compare_fields: list[str] = field(default_factory=list)
    search_fields: list[str] = field(default_factory=list)
    default_fields: list[str] = field(default_factory=list)

    def fields_for_api(self) -> list[str]:
        fields = {self.name_field}
        for f in (self.compare_fields + self.search_fields + self.default_fields):
            if f:
                fields.add(f)
        if self.type_field:
            fields.add(self.type_field)
        return sorted(fields)


COMMON_CONTAINER_FIELDS = [
    "containerType", "label", "description", "numReferences", "safeGuard", "objProp",
    "smObjProp", "version", "overlayGroup", "overlayProp", "resolvedName", "bundleScope",
    "bundleScopeEnabled",
]

# Defaultvärdena nedan är hämtade från användarens export av AR System Metadata-former.
# Viktigt: formnamnen är skiftlägeskänsliga i många installationer, t.ex. "actlink", inte "Actlink".
# Container-typer följer AR System API-konstanter i praktiken. Vanligt: ARCON_GUIDE=1,
# ARCON_FILTER_GUIDE=4. Om en miljö avviker kan type_values ändras i YAML.
DEFAULT_OBJECT_TYPES = [
    ObjectType(
        key="form",
        label="Forms",
        form="AR System Metadata: arschema",
        name_field="name",
        compare_fields=[
            "numFields", "schemaType", "numVuis", "coreVersion", "defaultVui", "nextFieldId",
            "safeGuard", "viewName", "objProp", "smObjProp", "version", "overlayGroup",
            "overlayProp", "resolvedName", "schemaRowIdentifier", "bundleScope", "bundleScopeEnabled",
        ],
        search_fields=["name", "resolvedName", "viewName"],
    ),
    ObjectType(
        key="actlink",
        label="Active Links",
        form="AR System Metadata: actlink",
        name_field="name",
        compare_fields=[
            "enable", "numActions", "numElses", "queryShort", "queryLong",
            "objProp", "smObjProp", "version", "alOrder", "controlfieldId",
            "errorActlinkId", "errorActlinkOptions", "executeMask", "safeGuard",
            "wkConnType", "overlayGroup", "overlayProp", "resolvedName", "bundleScope",
            "bundleScopeEnabled",
        ],
        search_fields=["name", "queryShort", "queryLong", "resolvedName"],
    ),
    ObjectType(
        key="filter",
        label="Filters",
        form="AR System Metadata: filter",
        name_field="name",
        compare_fields=[
            "enable", "numActions", "numElses", "queryShort", "queryLong",
            "objProp", "smObjProp", "version", "errorFilterId", "errorFilterOptions",
            "fOrder", "opSet", "safeGuard", "wkConnType", "overlayGroup", "overlayProp",
            "resolvedName", "bundleScope", "bundleScopeEnabled",
        ],
        search_fields=["name", "queryShort", "queryLong", "resolvedName"],
    ),
    ObjectType(
        key="escalation",
        label="Escalations",
        form="AR System Metadata: escalation",
        name_field="name",
        compare_fields=[
            "enable", "numActions", "numElses", "queryShort", "queryLong",
            "objProp", "smObjProp", "version", "firetmType", "hourmask", "minute",
            "monthday", "safeGuard", "tminterval", "weekday", "wkConnType",
            "overlayGroup", "overlayProp", "resolvedName", "bundleScope", "bundleScopeEnabled",
        ],
        search_fields=["name", "queryShort", "queryLong", "resolvedName"],
    ),
    ObjectType(
        key="active_link_guide",
        label="Active Link Guides",
        form="AR System Metadata: arcontainer",
        name_field="name",
        type_field="containerType",
        type_values=[1],
        compare_fields=COMMON_CONTAINER_FIELDS,
        search_fields=["name", "label", "description", "resolvedName"],
    ),
    ObjectType(
        key="filter_guide",
        label="Filter Guides",
        form="AR System Metadata: arcontainer",
        name_field="name",
        type_field="containerType",
        type_values=[4],
        compare_fields=COMMON_CONTAINER_FIELDS,
        search_fields=["name", "label", "description", "resolvedName"],
    ),
    ObjectType(
        key="application",
        label="Applications",
        form="AR System Metadata: arcontainer",
        name_field="name",
        type_field="containerType",
        type_values=[2],
        compare_fields=COMMON_CONTAINER_FIELDS,
        search_fields=["name", "label", "description", "resolvedName"],
    ),
    ObjectType(
        key="packing_list",
        label="Packing Lists",
        form="AR System Metadata: arcontainer",
        name_field="name",
        type_field="containerType",
        type_values=[3],
        compare_fields=COMMON_CONTAINER_FIELDS,
        search_fields=["name", "label", "description", "resolvedName"],
    ),
    ObjectType(
        key="web_service",
        label="Web Services",
        form="AR System Metadata: arcontainer",
        name_field="name",
        type_field="containerType",
        type_values=[5],
        compare_fields=COMMON_CONTAINER_FIELDS,
        search_fields=["name", "label", "description", "resolvedName"],
    ),
    ObjectType(
        key="menu",
        label="Menus",
        form="AR System Metadata: char_menu",
        name_field="name",
        compare_fields=[
            "menuType", "refreshCode", "safeGuard", "objProp", "smObjProp", "version",
            "overlayGroup", "overlayProp", "resolvedName", "bundleScope", "bundleScopeEnabled",
        ],
        search_fields=["name", "resolvedName"],
    ),
]


def _env_list_from_json() -> list[Environment]:
    raw = os.getenv("HELIX_ENVIRONMENTS_JSON")
    if not raw:
        return []
    data = json.loads(raw)
    return [Environment(**item) for item in data]


def _load_yaml(path: str | None) -> tuple[list[Environment], list[ObjectType]]:
    if not path or not Path(path).exists():
        return [], []
    doc = yaml.safe_load(Path(path).read_text()) or {}
    envs = [Environment(**item) for item in doc.get("environments", [])]
    types = [ObjectType(**item) for item in doc.get("object_types", [])]
    return envs, types


def load_config() -> tuple[list[Environment], dict[str, ObjectType]]:
    yaml_envs, yaml_types = _load_yaml(os.getenv("HELIX_CONFIG", "/config/hlx-diff.yaml"))
    envs = yaml_envs or _env_list_from_json()
    types = yaml_types or DEFAULT_OBJECT_TYPES
    return envs, {t.key: t for t in types}

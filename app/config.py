from __future__ import annotations

import hashlib
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
class CacheScope:
    include_form_prefixes: list[str] = field(default_factory=list)
    exclude_form_prefixes: list[str] = field(default_factory=list)

    def normalized(self) -> dict[str, list[str]]:
        return {
            "include_form_prefixes": [str(x).strip() for x in self.include_form_prefixes if str(x).strip()],
            "exclude_form_prefixes": [str(x).strip() for x in self.exclude_form_prefixes if str(x).strip()],
        }

    def signature(self) -> str:
        payload = self.normalized()
        # Bump this when scope semantics change so old PVC cache created with a
        # broader/buggy scope is not reused silently.
        payload["scope_model"] = "form-prefix-schemaid-int-workflowid-v11-migrator-deep-diff"
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        empty = {"exclude_form_prefixes": [], "include_form_prefixes": [], "scope_model": "form-prefix-schemaid-int-workflowid-v11-migrator-deep-diff"}
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16] if payload != empty else ""


@dataclass
class RelatedForm:
    form: str
    parent_field: str
    fields: list[str] = field(default_factory=list)
    label: str | None = None


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
    id_fields: list[str] = field(default_factory=lambda: ["Request ID", "Record ID"])
    related_forms: list[RelatedForm] = field(default_factory=list)

    def fields_for_api(self) -> list[str]:
        fields = {self.name_field, "timestamp"}
        for f in (self.id_fields + self.compare_fields + self.search_fields + self.default_fields):
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

ACTLINK_RELATED = [
    RelatedForm("AR System Metadata: actlink_mapping", "actlinkId", ["actlinkId", "schemaId", "objIndex", "overlayGroup", "overlayExtended"]),
    RelatedForm("AR System Metadata: actlink_group_ids", "actlinkId", ["actlinkId", "groupId", "overlayGroup", "overlayExtended"]),
    RelatedForm("AR System Metadata: actlink_call", "actlinkId", ["actlinkId", "actionIndex", "assignShort", "assignLong", "guideMode", "guideName", "guideTableId", "sampleGuide", "sampleServer", "serverName", "overlayGroup"]),
    RelatedForm("AR System Metadata: actlink_goto", "actlinkId", ["actlinkId", "actionIndex", "label", "overlayGroup"]),
    RelatedForm("AR System Metadata: actlink_message", "actlinkId", ["actlinkId", "actionIndex", "msgNum", "msgPane", "msgText", "msgType", "overlayGroup"]),
    RelatedForm("AR System Metadata: actlink_open", "actlinkId", ["actlinkId", "actionIndex", "assignShort", "assignLong", "serverName", "schemaName", "queryshort", "querylong", "sortlst", "targetLocation", "vuiLabel", "windowMode", "closeBox", "noMatchCtnu", "pollIntval", "reportstr", "supresEptyLst", "msgNum", "msgPane", "msgText", "msgType", "overlayGroup"]),
    RelatedForm("AR System Metadata: actlink_process", "actlinkId", ["actlinkId", "actionIndex", "command", "commandLong", "keywordList", "keywordListLong", "parameterList", "parameterListLong", "overlayGroup"]),
    RelatedForm("AR System Metadata: actlink_push", "actlinkId", ["actlinkId", "actionIndex", "fieldId", "assignShort", "assignLong", "sampleSchema", "sampleServer", "overlayGroup"]),
    RelatedForm("AR System Metadata: actlink_serviceaction", "actlinkId", ["actlinkId", "actionIndex", "fieldMaplong", "fieldMapshort", "requestIdMap", "sampleSchema", "sampleServer", "serverName", "serviceSchema", "overlayGroup"]),
    RelatedForm("AR System Metadata: actlink_set", "actlinkId", ["actlinkId", "actionIndex", "fieldId", "assignShort", "assignLong", "keywordList", "parameterList", "sampleSchema", "sampleServer", "overlayGroup"]),
    RelatedForm("AR System Metadata: actlink_set_char", "actlinkId", ["actlinkId", "actionIndex", "fieldId", "accessOpt", "charMenu", "focus", "options", "propLong", "propShort", "overlayGroup"]),
    RelatedForm("AR System Metadata: actlink_sql", "actlinkId", ["actlinkId", "actionIndex", "assignShort", "assignLong", "keywordList", "parameterList", "overlayGroup"]),
    RelatedForm("AR System Metadata: actlink_auto", "actlinkId", ["actlinkId", "actionIndex", "actionLong", "actionShort", "autoServerName", "clsId", "COMLong", "COMShort", "isVisible", "overlayGroup"]),
    RelatedForm("AR System Metadata: actlink_dde", "actlinkId", ["actlinkId", "actionIndex", "action", "command", "item", "path", "serviceName", "topic", "overlayGroup"]),
    RelatedForm("AR System Metadata: actlink_macro", "actlinkId", ["actlinkId", "actionIndex", "longText", "macroName", "shortText", "overlayGroup"]),
    RelatedForm("AR System Metadata: actlink_macro_parm", "actlinkId", ["actlinkId", "actionIndex", "name", "value", "overlayGroup"]),
    RelatedForm("AR System Metadata: actlink_wait", "actlinkId", ["actlinkId", "actionIndex", "buttonTitle", "overlayGroup"]),
]

FILTER_RELATED = [
    RelatedForm("AR System Metadata: filter_mapping", "filterId", ["filterId", "schemaId", "objIndex", "filterOverlayGroup", "overlayExtended"]),
    RelatedForm("AR System Metadata: filter_call", "filterId", ["filterId", "actionIndex", "assignShort", "assignLong", "guideMode", "guideName", "guideTableId", "sampleGuide", "sampleServer", "serverName", "overlayGroup"]),
    RelatedForm("AR System Metadata: filter_goto", "filterId", ["filterId", "actionIndex", "label", "overlayGroup"]),
    RelatedForm("AR System Metadata: filter_log", "filterId", ["filterId", "actionIndex", "logFile", "overlayGroup"]),
    RelatedForm("AR System Metadata: filter_message", "filterId", ["filterId", "actionIndex", "msgNum", "msgText", "msgType", "overlayGroup"]),
    RelatedForm("AR System Metadata: filter_notify", "filterId", ["filterId", "actionIndex", "bcc", "behavior", "cc", "contentTemplate", "fieldIdCode", "footerTemplate", "fromUser", "headerTemplate", "mailboxName", "mechanism", "mechXRef", "notifyText", "notifyTextLong", "organization", "permission", "priority", "replyTo", "subjectText", "userName", "overlayGroup"]),
    RelatedForm("AR System Metadata: filter_process", "filterId", ["filterId", "actionIndex", "command", "commandLong", "overlayGroup"]),
    RelatedForm("AR System Metadata: filter_push", "filterId", ["filterId", "actionIndex", "fieldId", "assignShort", "assignLong", "sampleSchema", "sampleServer", "overlayGroup"]),
    RelatedForm("AR System Metadata: filter_serviceaction", "filterId", ["filterId", "actionIndex", "fieldMaplong", "fieldMapshort", "requestIdMap", "sampleSchema", "sampleServer", "serverName", "serviceSchema", "overlayGroup"]),
    RelatedForm("AR System Metadata: filter_set", "filterId", ["filterId", "actionIndex", "fieldId", "assignShort", "assignLong", "sampleSchema", "sampleServer", "overlayGroup"]),
    RelatedForm("AR System Metadata: filter_sql", "filterId", ["filterId", "actionIndex", "assignShort", "assignLong", "overlayGroup"]),
]

CONTAINER_RELATED = [
    RelatedForm("AR System Metadata: arreference", "containerId", ["containerId", "referenceOrder", "referenceType", "referenceId", "referenceObjId", "label", "description", "dataType", "valueShort", "valueLong", "overlayGroup"]),
    RelatedForm("AR System Metadata: arctr_group_ids", "containerId", ["containerId", "groupId", "permission", "overlayGroup", "overlayExtended"]),
    RelatedForm("AR System Metadata: cntnr_ownr_obj", "containerId", ["containerId", "objIndex", "ownerObjId", "ownerObjType", "overlayGroup", "overlayExtended"]),
]

FORM_RELATED = [
    RelatedForm("AR System Metadata: field", "schemaId", ["schemaId", "fieldId", "fieldName", "fieldType", "datatype", "fOption", "createMode", "defaultValue", "helpText", "changeDiary", "overlayGroup", "overlayProp", "resolvedfieldId", "resolvedName", "sourceSchemaId"]),
    RelatedForm("AR System Metadata: field_permissions", "schemaId", ["schemaId", "fieldId", "groupId", "permission", "overlayGroup", "overlayExtended"]),
    RelatedForm("AR System Metadata: field_enum_values", "schemaId", ["schemaId", "fieldId", "enumItem", "enumValue", "enumLabel", "overlayGroup", "overlayExtended"]),
    RelatedForm("AR System Metadata: schema_group_ids", "schemaId", ["schemaId", "groupId", "permission", "overlayGroup", "overlayExtended"]),
    RelatedForm("AR System Metadata: schema_index", "schemaId", ["schemaId", "indexName", "listIndex", "uniqueFlag", "numFields", "f1", "f2", "f3", "f4", "f5", "f6", "f7", "f8", "f9", "f10", "overlayGroup", "overlayExtended"]),
    RelatedForm("AR System Metadata: vui", "schemaId", ["schemaId", "vuiName", "vuiId", "vuiType", "locale", "helpText", "changeDiary", "overlayGroup", "overlayProp", "resolvedName", "resolvedVuiId"]),
    RelatedForm("AR System Metadata: view_mapping", "schemaId", ["schemaId", "fieldId", "extField", "overlayGroup"]),
]

MENU_RELATED = [
    RelatedForm("AR System Metadata: char_menu_dd", "Char Menu ID", ["Char Menu ID", "arschema", "hiddenToo", "nameType", "path", "server", "structSubtype", "structType", "valueFormat", "overlayGroup"]),
    RelatedForm("AR System Metadata: char_menu_file", "Char Menu ID", ["Char Menu ID", "fileLocation", "filename", "path", "overlayGroup"]),
    RelatedForm("AR System Metadata: char_menu_list", "charMenuId", ["charMenuId", "childType", "label", "path", "value", "overlayGroup"]),
    RelatedForm("AR System Metadata: char_menu_query", "Char Menu ID", ["Char Menu ID", "arschema", "queryShort", "queryLong", "sampleSchema", "sampleServer", "externList", "keywordList", "labelField", "labelField2", "labelField3", "labelField4", "labelField5", "parameterList", "path", "server", "sortOnLabel", "valueField", "overlayGroup"]),
    RelatedForm("AR System Metadata: char_menu_sql", "Char Menu ID", ["Char Menu ID", "externList", "keywordList", "labelIndex", "labelIndex2", "labelIndex3", "labelIndex4", "labelIndex5", "parameterList", "path", "server", "sqlCmdLong", "sqlCmdShort", "valueIndex", "overlayGroup"]),
]

DEFAULT_OBJECT_TYPES = [
    ObjectType(
        key="form", label="Forms", form="AR System Metadata: arschema", name_field="name",
        id_fields=["schemaId", "Request ID", "Record ID"],
        compare_fields=["numFields", "schemaType", "numVuis", "coreVersion", "defaultVui", "nextFieldId", "safeGuard", "viewName", "objProp", "smObjProp", "version", "overlayGroup", "overlayProp", "resolvedName", "schemaRowIdentifier", "bundleScope", "bundleScopeEnabled"],
        search_fields=["name", "resolvedName", "viewName"], related_forms=FORM_RELATED,
    ),
    ObjectType(
        key="actlink", label="Active Links", form="AR System Metadata: actlink", name_field="name",
        id_fields=["Active Link ID", "Request ID", "Record ID"],
        compare_fields=["enable", "numActions", "numElses", "queryShort", "queryLong", "objProp", "smObjProp", "version", "alOrder", "controlfieldId", "errorActlinkId", "errorActlinkOptions", "executeMask", "safeGuard", "wkConnType", "overlayGroup", "overlayProp", "resolvedName", "bundleScope", "bundleScopeEnabled"],
        search_fields=["name", "queryShort", "queryLong", "resolvedName"], related_forms=ACTLINK_RELATED,
    ),
    ObjectType(
        key="filter", label="Filters", form="AR System Metadata: filter", name_field="name",
        id_fields=["Filter ID", "Request ID", "Record ID"],
        compare_fields=["enable", "numActions", "numElses", "queryShort", "queryLong", "objProp", "smObjProp", "version", "errorFilterId", "errorFilterOptions", "fOrder", "opSet", "safeGuard", "wkConnType", "overlayGroup", "overlayProp", "resolvedName", "bundleScope", "bundleScopeEnabled"],
        search_fields=["name", "queryShort", "queryLong", "resolvedName"], related_forms=FILTER_RELATED,
    ),
    ObjectType(
        key="escalation", label="Escalations", form="AR System Metadata: escalation", name_field="name",
        id_fields=["Escalation ID", "Request ID", "Record ID"],
        compare_fields=["enable", "numActions", "numElses", "queryShort", "queryLong", "objProp", "smObjProp", "version", "firetmType", "hourmask", "minute", "monthday", "safeGuard", "tminterval", "weekday", "wkConnType", "overlayGroup", "overlayProp", "resolvedName", "bundleScope", "bundleScopeEnabled"],
        search_fields=["name", "queryShort", "queryLong", "resolvedName"], related_forms=[RelatedForm("AR System Metadata: escal_mapping", "escalationId", ["escalationId", "schemaId", "objIndex", "escalationOverlayGroup", "overlayExtended"])],
    ),
    ObjectType(key="active_link_guide", label="Active Link Guides", form="AR System Metadata: arcontainer", name_field="name", type_field="containerType", type_values=[1], id_fields=["Container ID", "Request ID", "Record ID"], compare_fields=COMMON_CONTAINER_FIELDS, search_fields=["name", "label", "description", "resolvedName"], related_forms=CONTAINER_RELATED),
    ObjectType(key="filter_guide", label="Filter Guides", form="AR System Metadata: arcontainer", name_field="name", type_field="containerType", type_values=[4], id_fields=["Container ID", "Request ID", "Record ID"], compare_fields=COMMON_CONTAINER_FIELDS, search_fields=["name", "label", "description", "resolvedName"], related_forms=CONTAINER_RELATED),
    ObjectType(key="application", label="Applications", form="AR System Metadata: arcontainer", name_field="name", type_field="containerType", type_values=[2], id_fields=["Container ID", "Request ID", "Record ID"], compare_fields=COMMON_CONTAINER_FIELDS, search_fields=["name", "label", "description", "resolvedName"], related_forms=CONTAINER_RELATED),
    ObjectType(key="packing_list", label="Packing Lists", form="AR System Metadata: arcontainer", name_field="name", type_field="containerType", type_values=[3], id_fields=["Container ID", "Request ID", "Record ID"], compare_fields=COMMON_CONTAINER_FIELDS, search_fields=["name", "label", "description", "resolvedName"], related_forms=CONTAINER_RELATED),
    ObjectType(key="web_service", label="Web Services", form="AR System Metadata: arcontainer", name_field="name", type_field="containerType", type_values=[5], id_fields=["Container ID", "Request ID", "Record ID"], compare_fields=COMMON_CONTAINER_FIELDS, search_fields=["name", "label", "description", "resolvedName"], related_forms=CONTAINER_RELATED),
    ObjectType(key="menu", label="Menus", form="AR System Metadata: char_menu", name_field="name", compare_fields=["menuType", "refreshCode", "safeGuard", "objProp", "smObjProp", "version", "overlayGroup", "overlayProp", "resolvedName", "bundleScope", "bundleScopeEnabled"], search_fields=["name", "resolvedName"], related_forms=MENU_RELATED),
]


def _filter_related_by_profile(types: list[ObjectType]) -> list[ObjectType]:
    """Reduce expensive/legacy related metadata in deep mode unless explicitly requested.

    HELIX_DEEP_PROFILE:
      balanced (default) - includes common Migrator-like metadata: workflow actions, mappings, permissions, guides, fields, menus.
      full               - includes every known related metadata form, including legacy action types.
      minimal            - only base metadata + mapping/permissions/references.
    """
    profile = os.getenv("HELIX_DEEP_PROFILE", "balanced").strip().lower()
    if profile == "full":
        return types

    # Rare/legacy Active Link actions that can be very large and usually irrelevant in modern Helix systems.
    balanced_skip = {
        "AR System Metadata: actlink_auto",
        "AR System Metadata: actlink_dde",
        "AR System Metadata: actlink_macro",
        "AR System Metadata: actlink_macro_parm",
        "AR System Metadata: actlink_wait",
    }
    minimal_keep_keywords = (
        "_mapping", "_group_ids", "permissions", "arreference", "schema_group_ids",
        "field", "field_permissions", "field_enum_values", "schema_index",
        "vui", "view_mapping", "arctr_group_ids", "cntnr_ownr_obj",
    )

    filtered: list[ObjectType] = []
    for t in types:
        nt = ObjectType(**{**t.__dict__})
        if profile == "minimal":
            nt.related_forms = [r for r in t.related_forms if any(k in r.form for k in minimal_keep_keywords)]
        else:
            nt.related_forms = [r for r in t.related_forms if r.form not in balanced_skip]
        filtered.append(nt)
    return filtered


def _env_list_from_json() -> list[Environment]:
    raw = os.getenv("HELIX_ENVIRONMENTS_JSON")
    if not raw:
        return []
    data = json.loads(raw)
    return [Environment(**item) for item in data]


def _related_from_yaml(item: dict) -> RelatedForm:
    return RelatedForm(**item)


def _object_type_from_yaml(item: dict) -> ObjectType:
    related = item.get("related_forms", []) or []
    item = dict(item)
    item["related_forms"] = [_related_from_yaml(r) for r in related]
    return ObjectType(**item)


DEFAULT_CONFIG_PATHS = [
    "/etc/hlx-workflow-diff/config.yaml",
    "/config/hlx-diff.yaml",
    "/opt/hlx-workflow-diff/config/hlx-diff.yaml",
]


def resolve_config_path() -> str | None:
    """Resolve configuration file path.

    Priority:
    1. HELIX_CONFIG_PATH
    2. HELIX_CONFIG
    3. ConfigMap mount path: /etc/hlx-workflow-diff/config.yaml
    4. Legacy paths for backwards compatibility.
    """
    candidates: list[str | None] = [
        os.getenv("HELIX_CONFIG_PATH"),
        os.getenv("HELIX_CONFIG"),
        *DEFAULT_CONFIG_PATHS,
    ]
    seen: set[str] = set()
    for item in candidates:
        if not item:
            continue
        path = str(item)
        if path in seen:
            continue
        seen.add(path)
        if Path(path).exists():
            return path
    return None


def _credential_env_names(env_name: str, key: str) -> list[str]:
    safe = "".join(ch if ch.isalnum() else "_" for ch in env_name).upper()
    return [
        f"{safe}_{key.upper()}",
        f"HELIX_{safe}_{key.upper()}",
        f"HELIX_ENV_{safe}_{key.upper()}",
    ]


def _env_credential(env_name: str, key: str) -> str | None:
    for name in _credential_env_names(env_name, key):
        value = os.getenv(name)
        if value not in (None, ""):
            return value
    return None


def _environment_from_yaml(item: dict) -> Environment:
    item = dict(item)
    name = str(item.get("name", "")).strip()
    if not name:
        raise ValueError("Environment saknar name i konfigurationen")
    username = item.get("username") or _env_credential(name, "username")
    password = item.get("password") or _env_credential(name, "password")
    auth_string = item.get("auth_string") or _env_credential(name, "auth_string")
    if not username and not auth_string:
        raise ValueError(f"Environment {name} saknar username. Lägg det i Secret som { _credential_env_names(name, 'username')[0] }.")
    if not password and not auth_string:
        raise ValueError(f"Environment {name} saknar password. Lägg det i Secret som { _credential_env_names(name, 'password')[0] }.")
    item["username"] = username or ""
    item["password"] = password or ""
    if auth_string:
        item["auth_string"] = auth_string
    return Environment(**item)


def _load_yaml(path: str | None) -> tuple[list[Environment], list[ObjectType], CacheScope]:
    if not path or not Path(path).exists():
        return [], [], CacheScope()
    doc = yaml.safe_load(Path(path).read_text()) or {}
    envs = [_environment_from_yaml(item) for item in doc.get("environments", [])]
    types = [_object_type_from_yaml(item) for item in doc.get("object_types", [])]
    scope_doc = doc.get("cache_scope", {}) or {}
    scope = CacheScope(
        include_form_prefixes=scope_doc.get("include_form_prefixes", []) or [],
        exclude_form_prefixes=scope_doc.get("exclude_form_prefixes", []) or [],
    )
    return envs, types, scope


def load_cache_scope() -> CacheScope:
    _envs, _types, scope = _load_yaml(resolve_config_path())
    return scope


def load_config() -> tuple[list[Environment], dict[str, ObjectType]]:
    yaml_envs, yaml_types, scope = _load_yaml(resolve_config_path())
    os.environ["HELIX_CACHE_SCOPE_SIGNATURE"] = scope.signature()
    os.environ["HELIX_CACHE_SCOPE_JSON"] = json.dumps(scope.normalized(), ensure_ascii=False, sort_keys=True)
    envs = yaml_envs or _env_list_from_json()
    types = _filter_related_by_profile(yaml_types or DEFAULT_OBJECT_TYPES)
    return envs, {t.key: t for t in types}

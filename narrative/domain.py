"""Validated, portable narrative documents and isolated transactional storage."""
import base64
import binascii
import copy
import io
import json
import math
import re
import sqlite3
import uuid
import warnings
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, UnidentifiedImageError

MAX_DOCUMENT_BYTES = 32 * 1024 * 1024
MAX_ASSET_BYTES = 5 * 1024 * 1024
MAX_ASSETS_BYTES = 20 * 1024 * 1024
MAX_OWNER_BYTES = 256 * 1024 * 1024
MAX_PROJECTS = 100
ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")
PROJECT_ID = re.compile(r"^[a-f0-9]{32}$")
COLOR = re.compile(r"^#[a-fA-F0-9]{6}$")
MIME_FORMAT = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP", "image/gif": "GIF"}
AUDIO_MIMES = {"audio/mpeg", "audio/wav", "audio/ogg"}


class DomainError(Exception):
    def __init__(self, message, status=422):
        super().__init__(message)
        self.status = status


def fail(message):
    raise DomainError(message)


def obj(value, keys, context, required=None):
    if not isinstance(value, dict) or set(value) - set(keys):
        fail(f"{context}格式不正确，或包含不支持的字段。")
    if set(required if required is not None else keys) - set(value):
        fail(f"{context}缺少必要字段。")


def text(value, limit, context, allow_empty=True):
    if not isinstance(value, str) or len(value) > limit or (not allow_empty and not value.strip()):
        fail(f"{context}应为{'非空' if not allow_empty else ''}文本，长度不超过 {limit}。")
    if any(ord(c) < 32 and c not in "\n\r\t" for c in value):
        fail(f"{context}包含无效控制字符。")
    return value


def finite(value):
    return type(value) in (int, float) and abs(value) <= 1e15 and math.isfinite(value)


def collection(document, key, limit):
    value = document[key]
    if not isinstance(value, list) or len(value) > limit:
        fail(f"{key} 必须是列表，最多 {limit} 项。")
    seen = set()
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not ID.fullmatch(item["id"]):
            fail(f"{key} 包含无效 ID。")
        if item["id"] in seen:
            fail(f"{key} 包含重复 ID。")
        seen.add(item["id"])
    return value, seen


def literal(value, kind, context):
    okay = finite(value) if kind == "number" else type(value) is bool if kind == "boolean" else isinstance(value, str) and len(value) <= 4000
    if not okay:
        fail(f"{context}的值必须与变量类型 {kind} 一致。")
    if kind == "string":
        text(value, 4000, context)


def expressions(items, variables, condition=False):
    if not isinstance(items, list) or len(items) > 100:
        fail("单个节点或连线最多包含 100 项条件或变量操作。")
    for item in items:
        obj(item, {"variable", "op", "value"}, "条件或变量操作")
        if not isinstance(item["variable"], str) or item["variable"] not in variables:
            fail("条件或操作引用了不存在的变量。")
        kind = variables[item["variable"]]["type"]
        op = item["op"]
        if not isinstance(op, str) or op not in ({"eq", "ne", "gt", "gte", "lt", "lte"} if condition else {"set", "add"}):
            fail("不支持的条件或变量操作。")
        if (condition and op not in {"eq", "ne"} or not condition and op == "add") and kind != "number":
            fail("数值比较和加减操作只能用于数字变量。")
        literal(item["value"], kind, "条件或操作")


def asset_bytes(asset):
    obj(asset, {"id", "name", "type", "mime", "data"}, "素材")
    text(asset["name"], 200, "素材名称", False)
    mime = asset["mime"]
    if not isinstance(mime, str) or mime not in {*MIME_FORMAT, *AUDIO_MIMES}:
        fail("素材只支持 PNG、JPEG、WebP、GIF、MP3、WAV、OGG。")
    if asset["type"] != ("image" if mime in MIME_FORMAT else "audio"):
        fail("素材类型与 MIME 不一致。")
    data = asset["data"]
    prefix = f"data:{mime};base64,"
    if not isinstance(data, str) or not data.startswith(prefix) or len(data) > MAX_ASSET_BYTES * 4 // 3 + 100:
        fail("素材必须为有效的内嵌数据，单项不超过 5 MiB。")
    try:
        binary = base64.b64decode(data[len(prefix):], validate=True)
    except (ValueError, binascii.Error):
        fail("素材的 Base64 数据无效。")
    if not binary or len(binary) > MAX_ASSET_BYTES:
        fail("素材不能为空，单项不超过 5 MiB。")
    if mime in MIME_FORMAT:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(binary)) as image:
                    if image.format != MIME_FORMAT[mime] or image.width * image.height > 36_000_000:
                        fail("图片 MIME 与内容不一致，或超过 3600 万像素。")
                    image.verify()
        except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning):
            fail("图片数据损坏或过大。")
    elif mime == "audio/wav" and not (len(binary) >= 44 and binary[:4] == b"RIFF" and binary[8:12] == b"WAVE"):
        fail("WAV 文件标识无效。")
    elif mime == "audio/ogg" and not (len(binary) >= 27 and binary[:4] == b"OggS"):
        fail("OGG 文件标识无效。")
    elif mime == "audio/mpeg" and not (len(binary) >= 10 and (binary[:3] == b"ID3" or binary[0] == 255 and binary[1] & 0xE0 == 0xE0)):
        fail("MP3 文件标识无效。")
    return len(binary)


def _validate_document(document, legacy=False, outline_v2=False, depth=0, asset_budget=None):
    if depth > 3:
        fail("子剧情最多可嵌套 3 层。")
    if asset_budget is None:
        asset_budget = [0]
    keys = {"schema_version", "title", "description", "boards", "nodes", "edges", "characters", "assets", "start_node_id"}
    if legacy:
        keys.add("variables")
    obj(document, keys, "剧情文档")
    if type(document["schema_version"]) is not int or document["schema_version"] != (1 if legacy else 2 if outline_v2 else 3):
        fail("不支持的剧情文档版本。")
    text(document["title"], 120, "项目名称", False)
    text(document["description"], 4000, "项目简介")
    boards, board_ids = collection(document, "boards", 100)
    if not boards:
        fail("项目至少需要一个画板。")
    nodes, node_ids = collection(document, "nodes", 2000)
    edges, _ = collection(document, "edges", 5000)
    variables = collection(document, "variables", 1000)[0] if legacy else []
    characters, character_ids = collection(document, "characters", 500)
    assets, asset_ids = collection(document, "assets", 100)
    asset_map = {item["id"]: item for item in assets}
    asset_budget[0] += sum(asset_bytes(asset) for asset in assets)
    if asset_budget[0] > MAX_ASSETS_BYTES:
        fail("一个项目的素材总大小不能超过 20 MiB。")
    for board in boards:
        obj(board, {"id", "name"}, "画板")
        text(board["name"], 120, "画板名称", False)
    variable_map = {}
    variable_names = set()
    for variable in variables:
        obj(variable, {"id", "name", "type", "initial"}, "变量")
        text(variable["name"], 80, "变量名称", False)
        if variable["name"] in variable_names or "{{" in variable["name"] or "}}" in variable["name"]:
            fail("变量名称不能重复或包含插值分隔符。")
        variable_names.add(variable["name"])
        if not isinstance(variable["type"], str) or variable["type"] not in {"number", "boolean", "string"}:
            fail("变量类型应为 number、boolean 或 string。")
        literal(variable["initial"], variable["type"], "变量初始值")
        variable_map[variable["id"]] = variable
    for character in characters:
        obj(character, {"id", "name", "description", "color", "asset_id"}, "角色")
        text(character["name"], 120, "角色名称", False)
        text(character["description"], 8000, "角色描述")
        if not isinstance(character["color"], str) or not COLOR.fullmatch(character["color"]):
            fail("角色颜色应为六位十六进制颜色。")
        if not isinstance(character["asset_id"], str) or character["asset_id"] and (character["asset_id"] not in asset_ids or asset_map[character["asset_id"]]["type"] != "image"):
            fail("角色头像引用了不存在的图片。")
    node_map = {}
    for node in nodes:
        node_keys = {"id", "board_id", "type", "position", "title", "body", "speaker", "color", "asset_id"}
        if legacy:
            node_keys.add("actions")
        else:
            node_keys.add("substory")
            if not outline_v2:
                node_keys.update({"dialogue", "prerequisites"})
        obj(node, node_keys, "节点")
        if not isinstance(node["board_id"], str) or node["board_id"] not in board_ids:
            fail("节点引用了不存在的画板。")
        if not isinstance(node["type"], str) or node["type"] not in ({"scene", "dialogue", "branch", "note"} if legacy else {"scene", "dialogue", "branch", "note", "substory"}):
            fail("不支持的节点类型。")
        obj(node["position"], {"x", "y"}, "节点坐标")
        if any(not finite(value) or abs(value) > 1e7 for value in node["position"].values()):
            fail("节点坐标必须为有限数值，绝对值不超过 1000 万。")
        text(node["title"], 200, "节点标题")
        text(node["body"], 50000, "节点正文")
        if not isinstance(node["speaker"], str) or node["speaker"] and node["speaker"] not in character_ids:
            fail("节点引用了不存在的角色。")
        if not isinstance(node["color"], str) or not COLOR.fullmatch(node["color"]):
            fail("节点颜色应为六位十六进制颜色。")
        if not isinstance(node["asset_id"], str) or node["asset_id"] and node["asset_id"] not in asset_ids:
            fail("节点引用了不存在的素材。")
        if legacy:
            expressions(node["actions"], variable_map)
        elif node["type"] == "substory":
            _validate_document(node["substory"], depth=depth + 1, asset_budget=asset_budget)
        elif node["substory"] is not None:
            fail("只有子剧情节点可以包含子剧情文档。")
        if not legacy and not outline_v2:
            lines, _ = collection(node, "dialogue", 200)
            prerequisites = node["prerequisites"]
            if not isinstance(prerequisites, list) or len(prerequisites) > 50:
                fail("对话解锁前提必须是列表，最多 50 项。")
            if node["type"] != "dialogue" and (lines or prerequisites):
                fail("只有对话节点可以包含对话内容或解锁前提。")
            if node["type"] == "dialogue" and not lines:
                fail("一段对话至少需要一句对话内容。")
            for line in lines:
                obj(line, {"id", "speaker", "body"}, "对话内容")
                if not isinstance(line["speaker"], str) or line["speaker"] and line["speaker"] not in character_ids:
                    fail("对话内容引用了不存在的角色。")
                text(line["body"], 50000, "对话内容")
        node_map[node["id"]] = node
    if not legacy and not outline_v2:
        for node in nodes:
            referenced = set()
            for prerequisite in node["prerequisites"]:
                obj(prerequisite, {"node_id", "description"}, "对话解锁前提")
                target = prerequisite["node_id"]
                if not isinstance(target, str) or target not in node_map or target == node["id"] or node_map[target]["type"] == "note":
                    fail("对话解锁前提必须引用同一剧情中的其他非便笺节点。")
                if target in referenced:
                    fail("对话解锁前提不能重复引用同一节点。")
                referenced.add(target)
                text(prerequisite["description"], 4000, "对话解锁说明")
    start = document["start_node_id"]
    if not isinstance(start, str) or start and (start not in node_ids or node_map[start]["type"] == "note"):
        fail("剧情起点必须引用存在的非便笺节点，或留空。")
    for edge in edges:
        edge_keys = {"id", "source", "target", "label", "conditions", "actions"} if legacy else {"id", "source", "target", "label", "requirements"}
        if not legacy and not outline_v2:
            edge_keys.update({"source_handle", "target_handle"})
        obj(edge, edge_keys, "连线")
        if any(not isinstance(edge[key], str) or edge[key] not in node_ids for key in ("source", "target")):
            fail("连线引用了不存在的节点。")
        if any(node_map[edge[key]]["type"] == "note" for key in ("source", "target")):
            fail("便笺不能作为剧情连线的起点或终点。")
        text(edge["label"], 1000, "选项文字")
        if legacy:
            expressions(edge["conditions"], variable_map, condition=True)
            expressions(edge["actions"], variable_map)
        else:
            text(edge["requirements"], 4000, "剧情前提与衔接说明")
            if not outline_v2:
                for key in ("source_handle", "target_handle"):
                    if not isinstance(edge[key], str) or edge[key] not in {"left", "right", "top", "bottom"}:
                        fail("连线端口必须为 left、right、top 或 bottom。")
    try:
        encoded = json.dumps(document, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        size = len(encoded.encode("utf-8"))
    except (ValueError, UnicodeError, TypeError):
        fail("剧情文档包含无法保存的数据。")
    if size > MAX_DOCUMENT_BYTES:
        raise DomainError("剧情文档不能超过 32 MiB。", 413)
    return encoded, size


def validate_document(document):
    """Only complete v3 documents may be stored; old versions are normalized first."""
    return _validate_document(document)


def upgrade_document(document):
    """Convert a former playable graph into a plain outline without executing it."""
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        return document
    encoded, _ = _validate_document(document, legacy=True)
    from .seed import legacy_snow, snow
    # Exact complete comparison avoids replacing authored text, layouts or media.
    if document == legacy_snow():
        return snow(document["title"])
    migrated = json.loads(encoded)
    migrated["schema_version"] = 2
    variables = {item["id"]: item for item in migrated.pop("variables")}
    node_map = {item["id"]: item for item in migrated["nodes"]}
    for node in migrated["nodes"]:
        node.pop("actions")
        node["substory"] = None
    operators = {"eq": "等于", "ne": "不等于", "gt": "大于", "gte": "大于或等于", "lt": "小于", "lte": "小于或等于"}
    for edge in migrated["edges"]:
        conditions = edge.pop("conditions")
        edge.pop("actions")
        notes = [f"{variables[item['variable']]['name']} {operators[item['op']]} {json.dumps(item['value'], ensure_ascii=False)}" for item in conditions]
        requirements = "旧版条件转为文字备注（不再计算）：\n" + "\n".join(notes) if notes else ""
        if len(requirements) <= 4000:
            edge["requirements"] = requirements
        else:
            # Preserve long user-written condition values as plain notes rather
            # than truncate text to fit the smaller connection annotation field.
            source = node_map[edge["source"]]
            edge["requirements"] = "旧版条件已保留为同章节的“迁移备注”便笺，仅供梳理剧情。"
            for offset in range(0, len(requirements), 49000):
                migrated["nodes"].append({"id": "migration_" + uuid.uuid4().hex, "board_id": source["board_id"], "type": "note", "position": {"x": source["position"]["x"], "y": min(1e7, source["position"]["y"] + 320 + offset // 49000 * 280)}, "title": ("迁移备注：" + edge["label"])[:200], "body": requirements[offset:offset + 49000], "speaker": "", "color": "#d5af63", "asset_id": "", "substory": None})
    return migrated


def prepare_document(document):
    """Normalize prior exports, then validate one self-contained project tree."""
    try:
        normalized = copy.deepcopy(document)
    except RecursionError:
        fail("剧情文档嵌套过深。")

    def normalize(doc, depth=0):
        if depth > 3:
            fail("子剧情最多可嵌套 3 层。")
        doc = upgrade_document(doc)
        if isinstance(doc, dict) and doc.get("schema_version") in (2, 3) and isinstance(doc.get("nodes"), list):
            for node in doc["nodes"]:
                if not isinstance(node, dict):
                    continue
                if doc["schema_version"] == 2:
                    node.setdefault("substory", None)
                if node.get("substory") is not None:
                    node["substory"] = normalize(node["substory"], depth + 1)
            if doc["schema_version"] == 2:
                # Validate all legacy fields before translating, so unknown or
                # malformed input cannot be hidden by the migration defaults.
                _validate_document(doc, outline_v2=True, depth=depth)
                for node in doc["nodes"]:
                    node["dialogue"] = ([{"id": "line_1", "speaker": node["speaker"], "body": node["body"]}]
                                        if node["type"] == "dialogue" else [])
                    node["prerequisites"] = []
                for edge in doc["edges"]:
                    edge["source_handle"] = "right"
                    edge["target_handle"] = "left"
                doc["schema_version"] = 3
        return doc

    normalized = normalize(normalized)
    validate_document(normalized)
    return normalized


class ProjectStore:
    def __init__(self, directory, max_projects=MAX_PROJECTS, max_owner_bytes=MAX_OWNER_BYTES):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "narrative.sqlite3"
        self.max_projects = max_projects
        self.max_owner_bytes = max_owner_bytes
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, owner TEXT NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL, version INTEGER NOT NULL, updated_at TEXT NOT NULL, node_count INTEGER NOT NULL, bytes INTEGER NOT NULL, document TEXT NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS project_owner ON projects(owner, updated_at)")
            db.execute("BEGIN IMMEDIATE")
            # Storage revision 4 adds dialogue blocks, references and edge ports.
            if db.execute("PRAGMA user_version").fetchone()[0] < 4:
                for row in db.execute("SELECT id,document FROM projects").fetchall():
                    document = json.loads(row["document"])
                    migrated = prepare_document(document)
                    if migrated == document:
                        continue
                    encoded, size = validate_document(migrated)
                    db.execute("UPDATE projects SET title=?,description=?,version=version+1,node_count=?,bytes=?,document=? WHERE id=?", (migrated["title"], migrated["description"], len(migrated["nodes"]), size, encoded, row["id"]))
                db.execute("PRAGMA user_version=4")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.row_factory = sqlite3.Row
            # A project contains its complete media. Deletion overwrites SQLite cells;
            # DELETE journaling avoids leaving old inline assets in a persistent WAL.
            db.execute("PRAGMA secure_delete=ON")
            db.execute("PRAGMA journal_mode=DELETE")
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def envelope(row, viewer, admin=False):
        return {"id": row["id"], "owner_id": row["owner"], "read_only": row["owner"] != viewer and not admin,
                "version": row["version"], "updated_at": row["updated_at"], "document": json.loads(row["document"])}

    @staticmethod
    def check_id(project_id):
        if not isinstance(project_id, str) or not PROJECT_ID.fullmatch(project_id):
            raise DomainError("项目不存在。", 404)

    def owned(self, db, owner, project_id, *, admin=False):
        self.check_id(project_id)
        row = db.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            raise DomainError("项目不存在。", 404)
        if row["owner"] != owner and not admin:
            raise DomainError("仅创建者或管理员可以修改此项目。", 403)
        return row

    def list(self, owner, *, all_owners=True):
        with self.connect() as db:
            query = "SELECT id,owner AS owner_id,title,description,version,updated_at,node_count FROM projects"
            rows = db.execute(query + ("" if all_owners else " WHERE owner = ?") + " ORDER BY updated_at DESC,id", () if all_owners else (owner,)).fetchall()
            return [dict(row) for row in rows]

    def read(self, owner, project_id, *, all_owners=True, admin=False):
        with self.connect() as db:
            if all_owners:
                self.check_id(project_id)
                row = db.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
                if row is None:
                    raise DomainError("项目不存在。", 404)
            else:
                row = self.owned(db, owner, project_id)
            return self.envelope(row, owner, admin)

    def create(self, owner, document):
        document = prepare_document(document)
        encoded, size = validate_document(document)
        project_id = uuid.uuid4().hex
        updated = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            quota = db.execute("SELECT count(*) AS count,coalesce(sum(bytes),0) AS bytes FROM projects WHERE owner = ?", (owner,)).fetchone()
            if quota["count"] >= self.max_projects:
                raise DomainError(f"最多可保存 {self.max_projects} 个项目，请先导出并删除不再使用的项目。", 409)
            if quota["bytes"] + size > self.max_owner_bytes:
                raise DomainError("项目存储配额不足，请先导出并删除不再使用的项目。", 413)
            db.execute("INSERT INTO projects VALUES (?,?,?,?,?,?,?,?,?)", (project_id, owner, document["title"], document["description"], 1, updated, len(document["nodes"]), size, encoded))
            return self.envelope(self.owned(db, owner, project_id), owner)

    def update(self, owner, project_id, version, document, *, admin=False):
        if type(version) is not int or version < 1:
            fail("保存必须提供有效版本号。")
        document = prepare_document(document)
        encoded, size = validate_document(document)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = self.owned(db, owner, project_id, admin=admin)
            if old["version"] != version:
                raise DomainError("项目已在其他页面更新。请先导出当前修改，再重新载入最新版本。", 409)
            owner_bytes = db.execute("SELECT coalesce(sum(bytes),0) FROM projects WHERE owner = ?", (old["owner"],)).fetchone()[0]
            if owner_bytes - old["bytes"] + size > self.max_owner_bytes:
                raise DomainError("项目存储配额不足，请先导出并删除不再使用的项目。", 413)
            updated = datetime.now(timezone.utc).isoformat(timespec="microseconds")
            db.execute("UPDATE projects SET title=?,description=?,version=version+1,updated_at=?,node_count=?,bytes=?,document=? WHERE id=? AND owner=? AND version=?", (document["title"], document["description"], updated, len(document["nodes"]), size, encoded, project_id, old["owner"], version))
            return self.envelope(self.owned(db, owner, project_id, admin=admin), owner, admin)

    def delete(self, owner, project_id, version, *, admin=False):
        if type(version) is not int or version < 1:
            fail("删除必须提供有效版本号。")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.owned(db, owner, project_id, admin=admin)
            if row["version"] != version:
                raise DomainError("项目已更新，请重新载入后再删除。", 409)
            db.execute("DELETE FROM projects WHERE id=? AND owner=? AND version=?", (project_id, row["owner"], version))

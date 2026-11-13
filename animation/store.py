"""PostgreSQL is authoritative. Redis only accelerates wakeup, never owns jobs."""
from contextlib import contextmanager
import datetime
import hashlib
import json
import uuid

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
import redis

SCHEMA = """
CREATE TABLE IF NOT EXISTS characters (
 id text PRIMARY KEY, name text NOT NULL, approved boolean NOT NULL DEFAULT false,
 profile jsonb NOT NULL DEFAULT '{}', views jsonb NOT NULL DEFAULT '{}',
 revision integer NOT NULL DEFAULT 1, created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS motions (
 id text PRIMARY KEY, name text NOT NULL, description text NOT NULL DEFAULT '',
 video_path text NOT NULL, metadata jsonb NOT NULL DEFAULT '{}',
 created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS uploads (
 id text PRIMARY KEY, kind text NOT NULL, path text NOT NULL, sha256 text NOT NULL,
 metadata jsonb NOT NULL DEFAULT '{}', created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS jobs (
 id text PRIMARY KEY, idempotency_key text UNIQUE, request_hash text NOT NULL,
 task jsonb NOT NULL, snapshot jsonb NOT NULL, checkpoint jsonb NOT NULL DEFAULT '{}',
 status text NOT NULL DEFAULT 'queued', stage text NOT NULL DEFAULT 'queued',
 attempts integer NOT NULL DEFAULT 0, max_attempts integer NOT NULL DEFAULT 3,
 retryable boolean NOT NULL DEFAULT false, cancel_requested boolean NOT NULL DEFAULT false,
 preview boolean NOT NULL DEFAULT false, asset_id text, error text,
 next_attempt_at timestamptz NOT NULL DEFAULT now(),
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS jobs_claim ON jobs(status,next_attempt_at,created_at);
CREATE TABLE IF NOT EXISTS assets (
 id text PRIMARY KEY, job_id text UNIQUE NOT NULL REFERENCES jobs(id),
 preview boolean NOT NULL DEFAULT false, files jsonb NOT NULL,
 metadata jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS notifications (
 id text PRIMARY KEY, job_id text, title text NOT NULL, message text NOT NULL,
 level text NOT NULL DEFAULT 'info', created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS worker_state (
 id text PRIMARY KEY, state jsonb NOT NULL, updated_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS deleted_job_keys (
 owner_id text NOT NULL, idempotency_key text NOT NULL, request_hash text NOT NULL,
 PRIMARY KEY(owner_id,idempotency_key));
CREATE TABLE IF NOT EXISTS deletion_operations (
 id text PRIMARY KEY, resource_type text NOT NULL, resource_id text NOT NULL,
 actor_id text NOT NULL, summary jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS bootstrap_registry (
 id text PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now());
"""
TERMINAL = {"completed", "failed", "cancelled", "needs_attention"}
JOB_FIELDS = {"checkpoint", "status", "stage", "attempts", "retryable", "cancel_requested", "asset_id", "error"}
OWNED_TABLES = {"characters", "motions", "uploads", "jobs", "assets", "notifications"}
DEFAULT_OWNER = {"owner_id": "local-admin", "owner_name": "本地管理员"}


def owner_fields(owner=None):
    """Only authenticated callers supply owner; legacy/internal calls stay local."""
    value = DEFAULT_OWNER if owner is None else owner
    owner_id, owner_name = value.get("owner_id"), value.get("owner_name")
    if not isinstance(owner_id, str) or not owner_id.strip() or len(owner_id) > 200:
        raise ValueError("Invalid owner ID")
    if not isinstance(owner_name, str) or not owner_name.strip() or len(owner_name) > 200:
        raise ValueError("Invalid owner name")
    return owner_id, owner_name


def task_hash(task):
    semantic = {k: v for k, v in task.items() if k != "idempotency_key"}
    if not semantic.get("supplementary_description"):
        semantic.pop("supplementary_description", None)
    # Preserve legacy idempotency keys without rewriting their frozen prompts.
    if not semantic.get("use_character_description"):
        semantic.pop("use_character_description", None)
    for name in ("reference_id", "additional_reference_ids", "additional_reference_facings", "additional_reference_upload_ids"):
        if not semantic.get(name):
            semantic.pop(name, None)
    if semantic.get("prompt_mode", "assisted") == "assisted":
        semantic.pop("prompt_mode", None)
    if not semantic.get("video_prompt"):
        semantic.pop("video_prompt", None)
    return hashlib.sha256(json.dumps(semantic, sort_keys=True).encode()).hexdigest()


def check_idempotency(row, request_hash):
    if row and row["deleted_at"] is not None:
        raise Conflict("This idempotency key belongs to a deleted task; use a new key")
    if row and row["request_hash"] != request_hash:
        raise Conflict("Idempotency key already belongs to a different task")
    return row


def h3_unresolved(row):
    checkpoint = row["checkpoint"]
    return bool((checkpoint.get("h3_job_id") and not checkpoint.get("h3_terminal"))
                or ((row["stage"] == "submitting" or checkpoint.get("submission_started_at"))
                    and not checkpoint.get("h3_job_id") and not checkpoint.get("h3_terminal")))


class Conflict(ValueError):
    pass


class Store:
    def __init__(self, settings):
        self.settings = settings
        self.redis = redis.Redis.from_url(settings.redis_url, socket_connect_timeout=1, socket_timeout=3)

    @contextmanager
    def connection(self):
        with psycopg.connect(self.settings.database_url, row_factory=dict_row, connect_timeout=5) as conn:
            yield conn

    @contextmanager
    def resource_guard(self, table, object_id):
        if table not in OWNED_TABLES:
            raise ValueError("Unsupported table")
        with self.connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                         ("animation-resource:" + table + ":" + object_id,))
            yield

    def initialize(self):
        with self.connection() as conn:
            # API and worker commonly boot together; serialize first-time DDL.
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (73114418081,))
            conn.execute(SCHEMA)
            for table in sorted(OWNED_TABLES):
                conn.execute(f"""ALTER TABLE {table}
                    ADD COLUMN IF NOT EXISTS owner_id text NOT NULL DEFAULT 'local-admin',
                    ADD COLUMN IF NOT EXISTS owner_name text NOT NULL DEFAULT '本地管理员',
                    ADD COLUMN IF NOT EXISTS deleted_at timestamptz,
                    ADD COLUMN IF NOT EXISTS deleted_by text""")
                conn.execute(f"""CREATE INDEX IF NOT EXISTS {table}_visible_owner
                    ON {table}(owner_id,created_at DESC) WHERE deleted_at IS NULL""")
            # Keep a deleted task's key reserved: retries cannot resurrect it or
            # accidentally submit a second billable H3 generation.
            conn.execute("ALTER TABLE jobs DROP CONSTRAINT IF EXISTS jobs_idempotency_key_key")
            conn.execute("ALTER TABLE characters ADD COLUMN IF NOT EXISTS reference_labels jsonb NOT NULL DEFAULT '{}'")
            conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS jobs_owner_idempotency
                ON jobs(owner_id,idempotency_key)""")
            conn.execute("ALTER TABLE assets ALTER COLUMN job_id DROP NOT NULL")
            conn.execute("ALTER TABLE assets DROP CONSTRAINT IF EXISTS assets_job_id_fkey")
            conn.execute("""ALTER TABLE assets ADD CONSTRAINT assets_job_id_fkey
                FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE SET NULL""")

    def wake(self):
        try:
            self.redis.lpush("animation:wakeup", "1")
            self.redis.ltrim("animation:wakeup", 0, 100)
        except redis.RedisError:
            pass

    def wait(self):
        try:
            self.redis.brpop("animation:wakeup", timeout=2)
        except redis.RedisError:
            import time
            time.sleep(2)

    def get(self, table, object_id):
        if table not in OWNED_TABLES:
            raise ValueError("Unsupported table")
        with self.connection() as conn:
            return conn.execute(f"SELECT * FROM {table} WHERE id=%s AND deleted_at IS NULL", (object_id,)).fetchone()

    def list(self, table, limit=100, owner_id=None):
        if table not in OWNED_TABLES:
            raise ValueError("Unsupported table")
        with self.connection() as conn:
            scope = " AND owner_id=%s" if owner_id is not None else ""
            args = (owner_id, min(limit, 500)) if owner_id is not None else (min(limit, 500),)
            return conn.execute(f"SELECT * FROM {table} WHERE deleted_at IS NULL{scope} ORDER BY created_at DESC LIMIT %s", args).fetchall()

    def create_character(self, value, ignore_existing=False, owner=None):
        if value["id"] == "uploads":
            raise Conflict("Character ID is reserved for uploaded files")
        if owner is None and "owner_id" in value:
            owner = value
        owner_id, owner_name = owner_fields(owner)
        with self.connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (73114418082,))
            if ignore_existing:
                seeded = conn.execute("INSERT INTO bootstrap_registry(id) VALUES(%s) ON CONFLICT DO NOTHING RETURNING id",
                                      (value["id"],)).fetchone()
                if not seeded:
                    return None
            self._validate_reference_paths(value["views"].values())
            row = conn.execute("""INSERT INTO characters(id,name,profile,views,owner_id,owner_name,reference_labels) VALUES(%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(id) DO NOTHING RETURNING *""", (value["id"], value["name"],
                Jsonb(value.get("profile", {})), Jsonb(value["views"]), owner_id, owner_name,
                Jsonb(value.get("reference_labels", {})))).fetchone()
            if not row and not ignore_existing:
                raise Conflict("Character ID already exists")
            return row

    def save_profile(self, character_id, profile, approved):
        with self.connection() as conn:
            return conn.execute("""UPDATE characters SET profile=%s,approved=%s,revision=revision+1,
                updated_at=now() WHERE id=%s AND deleted_at IS NULL RETURNING *""", (Jsonb(profile), approved, character_id)).fetchone()

    def save_reference(self, character_id, facing, path):
        with self.connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (73114418082,))
            self._validate_reference_paths([path])
            return conn.execute("""UPDATE characters SET views=views||%s,approved=false,
                revision=revision+1,updated_at=now() WHERE id=%s AND deleted_at IS NULL RETURNING *""",
                (Jsonb({facing: path}), character_id)).fetchone()

    def add_reference(self, character_id, path, label):
        reference_id = uuid.uuid4().hex
        with self.connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (73114418082,))
            self._validate_reference_paths([path])
            row = conn.execute("SELECT * FROM characters WHERE id=%s AND deleted_at IS NULL FOR UPDATE", (character_id,)).fetchone()
            if not row:
                raise KeyError(character_id)
            if len(row["views"]) >= 200:
                raise Conflict("每个档案最多保存 200 张图片")
            return conn.execute("""UPDATE characters SET views=views||%s,reference_labels=reference_labels||%s,
                approved=false,revision=revision+1,updated_at=now() WHERE id=%s RETURNING *""",
                (Jsonb({reference_id: path}), Jsonb({reference_id: label}), character_id)).fetchone()

    def label_reference(self, character_id, reference_id, label):
        with self.connection() as conn:
            row = conn.execute("""UPDATE characters SET reference_labels=reference_labels||%s,revision=revision+1,
                updated_at=now() WHERE id=%s AND deleted_at IS NULL AND views ? %s RETURNING *""",
                (Jsonb({reference_id: label}), character_id, reference_id)).fetchone()
            if not row:
                raise KeyError(reference_id)
            return row

    def remove_reference(self, character_id, reference_id, actor_id):
        """Remove one image without touching the immutable copies in queued jobs."""
        from .deletion import FileRemoval, checked_path
        removal = None
        try:
            with self.connection() as conn:
                conn.execute("SELECT pg_advisory_xact_lock(%s)", (73114418082,))
                row = conn.execute("SELECT * FROM characters WHERE id=%s AND deleted_at IS NULL FOR UPDATE", (character_id,)).fetchone()
                if not row or reference_id not in row["views"]:
                    raise KeyError(reference_id)
                # Reuse the existing shared-reference protection, excluding only
                # this selected entry (other images in this same archive count).
                remaining = {key: value for key, value in row["views"].items() if key != reference_id}
                conn.execute("UPDATE characters SET views=%s,reference_labels=reference_labels-%s,approved=false,revision=revision+1,updated_at=now() WHERE id=%s",
                             (Jsonb(remaining), reference_id, character_id))
                files, _, retained = self._deletion_files(conn, "uploads", {"id": reference_id, "path": row["views"][reference_id], "owner_id": row["owner_id"]})
                removal = FileRemoval(self.settings.data_root, files)
                result = {"id": reference_id, "character_id": character_id, "deleted": True, "deleted_by": actor_id,
                          "operation_id": removal.operation_id, "physical_files_deleted": len(removal.entries),
                          "bytes_deleted": sum(entry["bytes"] for entry in removal.entries),
                          "retained_shared_paths": retained, "cleanup_pending": True}
                removal.prepare()
                removed_paths = [entry["source"] for entry in removal.entries]
                if removed_paths:
                    conn.execute("DELETE FROM uploads WHERE path=ANY(%s)", (removed_paths,))
                conn.execute("INSERT INTO deletion_operations(id,resource_type,resource_id,actor_id,summary) VALUES(%s,%s,%s,%s,%s)",
                             (removal.operation_id, "character_references", character_id + "/" + reference_id, actor_id, Jsonb(result)))
        except Exception:
            if removal is not None:
                try:
                    with self.connection() as conn:
                        committed = conn.execute("SELECT id FROM deletion_operations WHERE id=%s", (removal.operation_id,)).fetchone()
                    if not committed:
                        removal.rollback()
                except psycopg.Error:
                    pass
            raise
        with self.connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (73114418082,))
            try:
                removal.purge()
                result["cleanup_pending"] = False
            except OSError:
                pass
            conn.execute("UPDATE deletion_operations SET summary=%s WHERE id=%s", (Jsonb(result), removal.operation_id))
        return result

    def create_motion(self, name, video_path, description, metadata, owner=None):
        owner_id, owner_name = owner_fields(owner)
        with self.connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (73114418082,))
            self._validate_reference_paths([video_path])
            return conn.execute("""INSERT INTO motions(id,name,video_path,description,metadata,owner_id,owner_name)
                VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING *""", (uuid.uuid4().hex, name, video_path,
                description, Jsonb(metadata), owner_id, owner_name)).fetchone()

    def save_motion_analysis(self, motion_id, analysis):
        with self.connection() as conn:
            conn.execute("UPDATE motions SET metadata=metadata||%s WHERE id=%s AND deleted_at IS NULL",
                         (Jsonb({"analysis": analysis}), motion_id))

    def upload(self, upload_id, kind, path, digest, metadata, owner=None):
        owner_id, owner_name = owner_fields(owner)
        with self.connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (73114418082,))
            self._validate_reference_paths([path])
            return conn.execute("""INSERT INTO uploads(id,kind,path,sha256,metadata,owner_id,owner_name)
                VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                (upload_id, kind, path, digest, Jsonb(metadata), owner_id, owner_name)).fetchone()

    def enqueue(self, job_id, task, snapshot, owner=None, admin=False):
        request_hash = task_hash(task)
        owner_id, owner_name = owner_fields(owner)
        with self.connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (73114418082,))
            # Deletion takes FOR UPDATE on these same rows. This closes the gap
            # between Service.snapshot() and the committed job reference.
            self._lock_task_references(conn, task, owner=owner, admin=admin, snapshot=snapshot)
            if task.get("idempotency_key") and conn.execute(
                    "SELECT 1 FROM deleted_job_keys WHERE owner_id=%s AND idempotency_key=%s",
                    (owner_id, task["idempotency_key"])).fetchone():
                raise Conflict("This idempotency key belongs to a deleted task; use a new key")
            row = conn.execute("""INSERT INTO jobs(id,idempotency_key,request_hash,task,snapshot,preview,owner_id,owner_name)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(owner_id,idempotency_key) DO NOTHING RETURNING *""",
                (job_id, task.get("idempotency_key"), request_hash, Jsonb(task), Jsonb(snapshot), task["preview"], owner_id, owner_name)).fetchone()
            if not row:
                row = conn.execute("SELECT * FROM jobs WHERE owner_id=%s AND idempotency_key=%s",
                                   (owner_id, task["idempotency_key"])).fetchone()
                check_idempotency(row, request_hash)
        self.wake()
        return row

    def find_idempotency(self, key, task, owner=None):
        if not key:
            return None
        owner_id, _ = owner_fields(owner)
        with self.connection() as conn:
            if conn.execute("SELECT 1 FROM deleted_job_keys WHERE owner_id=%s AND idempotency_key=%s", (owner_id, key)).fetchone():
                raise Conflict("This idempotency key belongs to a deleted task; use a new key")
            row = conn.execute("SELECT * FROM jobs WHERE owner_id=%s AND idempotency_key=%s", (owner_id, key)).fetchone()
        return check_idempotency(row, task_hash(task))

    def _lock_task_references(self, conn, task, owner=None, admin=False, snapshot=None):
        from .files import safe_path, sha256
        from .models import additional_reference_selection
        extra_facings, upload_ids = additional_reference_selection(task)
        actor_id, _ = owner_fields(owner)
        character = conn.execute("SELECT * FROM characters WHERE id=%s AND deleted_at IS NULL FOR SHARE",
                                 (task["character_id"],)).fetchone()
        if not character:
            raise KeyError(task["character_id"])
        if not task["preview"] and not character["approved"]:
            raise Conflict("Character approval changed while the task was being queued")
        if snapshot is not None:
            reference = snapshot["reference"]
            selected = task.get("reference_id") or task.get("facing") or next(iter(character["views"]), None)
            if selected not in character["views"]:
                raise Conflict("Selected primary image no longer exists")
            path = safe_path(self.settings.data_root, character["views"][selected])
            if (reference.get("original_path", reference.get("path")) != str(path) or reference.get("sha256") != sha256(path)):
                raise Conflict("Primary reference changed while the task was being queued")
        frozen_additional = snapshot.get("additional_references", []) if snapshot is not None else None
        if frozen_additional is not None and len(frozen_additional) != len(extra_facings) + len(upload_ids):
            raise Conflict("Additional reference snapshot does not match the task")
        for index, facing in enumerate(extra_facings):
            if facing not in character["views"]:
                raise Conflict("Selected additional facing no longer exists")
            path = safe_path(self.settings.data_root, character["views"][facing])
            if frozen_additional is not None:
                reference = frozen_additional[index]
                if (reference.get("source") != "character_view" or (reference.get("reference_id") or reference.get("facing")) != facing
                        or reference.get("original_path", reference.get("path")) != str(path)
                        or reference.get("sha256") != sha256(path)):
                    raise Conflict("Additional facing reference changed while the task was being queued")
        for offset, upload_id in enumerate(upload_ids, len(extra_facings)):
            upload = conn.execute("SELECT * FROM uploads WHERE id=%s AND deleted_at IS NULL FOR SHARE", (upload_id,)).fetchone()
            if not upload or upload["kind"] != "character":
                raise KeyError("Additional reference upload")
            path = safe_path(self.settings.data_root, upload["path"])
            if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"} or sha256(path) != upload["sha256"]:
                raise Conflict("Additional reference upload file changed")
            if frozen_additional is not None:
                reference = frozen_additional[offset]
                if (reference.get("source") != "upload" or reference.get("upload_id") != upload_id
                        or reference.get("original_path", reference.get("path")) != str(path)
                        or reference.get("sha256") != upload["sha256"]):
                    raise Conflict("Additional upload snapshot does not match the registered reference")
        if task.get("motion_id"):
            motion = conn.execute("SELECT id FROM motions WHERE id=%s AND deleted_at IS NULL FOR SHARE",
                                  (task["motion_id"],)).fetchone()
            if not motion:
                raise KeyError(task["motion_id"])

    def _validate_reference_paths(self, paths):
        from .deletion import checked_path
        for value in paths:
            path = checked_path(self.settings.data_root, value)
            if not path.is_file():
                raise ValueError("Reference file no longer exists")

    def delete(self, table, object_id, actor_id, external_cleanup=None, *, admin=False):
        from .deletion import FileRemoval
        if table not in OWNED_TABLES:
            raise ValueError("Unsupported table")
        if not isinstance(actor_id, str) or not actor_id.strip() or len(actor_id) > 200:
            raise ValueError("Invalid deletion actor")
        removal = None
        try:
            with self.connection() as conn:
                conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                             ("animation-resource:" + table + ":" + object_id,))
                conn.execute("SELECT pg_advisory_xact_lock(%s)", (73114418082,))
                row = conn.execute(f"SELECT * FROM {table} WHERE id=%s AND deleted_at IS NULL FOR UPDATE",
                                   (object_id,)).fetchone()
                if not row:
                    previous = conn.execute("""SELECT summary FROM deletion_operations
                        WHERE resource_type=%s AND resource_id=%s ORDER BY created_at DESC LIMIT 1""",
                        (table, object_id)).fetchone()
                    if previous and previous["summary"].get("cleanup_pending"):
                        result = previous["summary"]
                        if not admin and result.get("owner_id", result.get("deleted_by")) != actor_id:
                            raise PermissionError("仅创建者或管理员可以删除此内容")
                        removal = FileRemoval.recover(self.settings.data_root, result["operation_id"])
                    else:
                        raise KeyError(object_id)
                else:
                    if row["owner_id"] != actor_id and not admin:
                        raise PermissionError("仅创建者或管理员可以删除此内容")
                    self._validate_deletion(conn, table, row)
                    files, directories, retained = self._deletion_files(conn, table, row)
                    removal = FileRemoval(self.settings.data_root, files, directories)
                    result = {"id": object_id, "deleted": True, "deleted_by": actor_id,
                              "owner_id": row["owner_id"],
                              "operation_id": removal.operation_id, "physical_files_deleted": len(removal.entries),
                              "bytes_deleted": sum(entry["bytes"] for entry in removal.entries),
                              "retained_shared_paths": retained, "cleanup_pending": True}
                    removal.prepare()
                    # Hide source frames transactionally before cleaning the
                    # host queue: a delayed POST cannot snapshot new batches
                    # after the parent cleanup. Any failure rolls sources back.
                    background = external_cleanup(table, row) if external_cleanup else None
                    if background is not None:
                        result["background_cleanup"] = background
                    if table == "assets":
                        conn.execute("UPDATE jobs SET asset_id=NULL,updated_at=now() WHERE asset_id=%s", (object_id,))
                    if table == "jobs" and row["idempotency_key"]:
                        conn.execute("""INSERT INTO deleted_job_keys(owner_id,idempotency_key,request_hash)
                            VALUES(%s,%s,%s) ON CONFLICT DO NOTHING""",
                            (row["owner_id"], row["idempotency_key"], row["request_hash"]))
                    if table == "characters":
                        conn.execute("INSERT INTO bootstrap_registry(id) VALUES(%s) ON CONFLICT DO NOTHING", (object_id,))
                    conn.execute(f"DELETE FROM {table} WHERE id=%s", (object_id,))
                    # An imported reference upload is an ingestion record of the
                    # same file, so remove it only when its last shared use ends.
                    removed_paths = [entry["source"] for entry in removal.entries]
                    if removed_paths:
                        conn.execute("DELETE FROM uploads WHERE path=ANY(%s)", (removed_paths,))
                    conn.execute("INSERT INTO deletion_operations(id,resource_type,resource_id,actor_id,summary) VALUES(%s,%s,%s,%s,%s)",
                                 (removal.operation_id, table, object_id, actor_id, Jsonb(result)))
        except Exception:
            if removal is not None:
                # A connection failure during COMMIT has an unknown outcome.
                # Restore only after a fresh connection confirms no deletion was
                # committed; otherwise the startup recovery handles the manifest.
                try:
                    with self.connection() as recovery_conn:
                        committed = recovery_conn.execute("SELECT id FROM deletion_operations WHERE id=%s",
                                                          (removal.operation_id,)).fetchone()
                    if not committed:
                        removal.rollback()
                except psycopg.Error:
                    pass
            raise
        with self.connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (73114418082,))
            try:
                removal.purge()
                result["cleanup_pending"] = False
            except OSError:
                # Original URLs are already inaccessible; private temporary
                # files remain pending deletion and can be retried explicitly.
                result["cleanup_pending"] = True
            conn.execute("UPDATE deletion_operations SET summary=%s WHERE id=%s", (Jsonb(result), removal.operation_id))
        return result

    def _validate_deletion(self, conn, table, row):
        if table == "jobs" and (row["status"] not in {"completed", "failed", "cancelled"} or h3_unresolved(row)):
            raise Conflict("Only terminal jobs with resolved H3 submissions can be deleted")
        if table in {"characters", "motions"}:
            field = "character_id" if table == "characters" else "motion_id"
            active = conn.execute(f"""SELECT * FROM jobs WHERE deleted_at IS NULL AND task->>'{field}'=%s""", (row["id"],)).fetchall()
            if any(job["status"] not in {"completed", "failed", "cancelled"} or h3_unresolved(job) for job in active):
                raise Conflict("Reference is used by an active job; finish or safely cancel the job first")

    def _deletion_files(self, conn, table, row):
        from .deletion import checked_path, directory_files, resource_directory
        root = self.settings.data_root.resolve()
        paths, directories = [], []
        source_job = row if table == "jobs" else None
        if table in {"characters", "motions", "jobs", "assets"}:
            folder = resource_directory(root, table, row["id"])
            directories.append(folder)
            paths.extend(directory_files(root, folder))
        if table == "characters":
            paths.extend(checked_path(root, value) for value in row["views"].values())
        if table == "motions":
            paths.append(checked_path(root, row["video_path"]))
        if table == "uploads":
            paths.append(checked_path(root, row["path"]))
        if table == "assets" and row["job_id"]:
            job = conn.execute("SELECT * FROM jobs WHERE id=%s FOR UPDATE", (row["job_id"],)).fetchone()
            if job and (job["status"] not in {"completed", "failed", "cancelled"} or h3_unresolved(job)):
                raise Conflict("Asset source files are still in use by an active job")
            if job:
                source_job = job
                folder = resource_directory(root, "jobs", row["job_id"])
                # API receipts can contain full base64 video/image payloads.
                # Delete the complete owned source directory, including those
                # receipts and per-frame caches, not only media extensions.
                directories.append(folder)
                paths.extend(directory_files(root, folder))
        shared = set()
        # Legacy catalogues can reference another member's uploaded bytes.
        # Deleting this catalogue must leave that member's original upload intact.
        for upload in conn.execute("SELECT id,path,owner_id FROM uploads WHERE deleted_at IS NULL").fetchall():
            if upload["owner_id"] != row.get("owner_id"):
                shared.add(checked_path(root, upload["path"]))
        for character in conn.execute("SELECT id,views FROM characters WHERE deleted_at IS NULL").fetchall():
            if table != "characters" or character["id"] != row["id"]:
                shared.update(checked_path(root, value) for value in character["views"].values())
        for motion in conn.execute("SELECT id,video_path FROM motions WHERE deleted_at IS NULL").fetchall():
            if table != "motions" or motion["id"] != row["id"]:
                shared.add(checked_path(root, motion["video_path"]))
        if table in {"characters", "motions", "uploads"}:
            live_dirs = {(name, record["id"]) for name in ("jobs", "assets")
                         for record in conn.execute(f"SELECT id FROM {name} WHERE deleted_at IS NULL").fetchall()}
            shared.update(path for path in paths if tuple(path.relative_to(root).parts[:2]) in live_dirs)
        # Asset/job directories are independently owned. A shared catalog record
        # cannot silently keep a deleted asset's media publicly downloadable.
        retained = sorted({p.relative_to(root).as_posix() for p in paths if p in shared})
        if retained and table in {"jobs", "assets"}:
            raise Conflict("Media is used as a shared catalog reference; remove that reference before deleting this resource")
        return [p for p in paths if p not in shared and p.exists()], directories, retained

    def recover_deletions(self):
        from .deletion import FileRemoval
        staging = self.settings.data_root.resolve() / ".delete-staging"
        with self.connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (73114418082,))
            if not staging.exists():
                return
            if staging.is_symlink():
                raise ValueError("Deletion staging must not be a symbolic link")
            for folder in sorted(staging.iterdir()):
                removal = FileRemoval.recover(self.settings.data_root, folder.name)
                operation = conn.execute("SELECT summary FROM deletion_operations WHERE id=%s", (folder.name,)).fetchone()
                if not operation:
                    removal.rollback()
                else:
                    try:
                        removal.purge()
                        summary = {**operation["summary"], "cleanup_pending": False}
                        conn.execute("UPDATE deletion_operations SET summary=%s WHERE id=%s", (Jsonb(summary), folder.name))
                    except OSError:
                        pass

    def update_job(self, job_id, **fields):
        if not fields or set(fields) - JOB_FIELDS:
            raise ValueError("Invalid job update")
        values = [Jsonb(v) if k == "checkpoint" else v for k, v in fields.items()]
        with self.connection() as conn:
            return conn.execute("UPDATE jobs SET " + ",".join(k + "=%s" for k in fields) +
                ",updated_at=now() WHERE id=%s AND deleted_at IS NULL RETURNING *", (*values, job_id)).fetchone()

    def claim(self):
        with self.connection() as conn:
            row = conn.execute("""SELECT * FROM jobs candidate WHERE deleted_at IS NULL AND status IN ('queued','retry_wait')
                AND next_attempt_at<=now() AND (
                  (checkpoint ? 'h3_job_id' AND NOT checkpoint ? 'h3_terminal') OR NOT EXISTS(
                    SELECT 1 FROM jobs active WHERE active.deleted_at IS NULL AND active.status IN ('running','queued','retry_wait')
                    AND active.checkpoint ? 'h3_job_id' AND NOT active.checkpoint ? 'h3_terminal'))
                ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1""").fetchone()
            if row:
                return conn.execute("""UPDATE jobs SET status='running',attempts=attempts+1,
                    updated_at=now() WHERE id=%s RETURNING *""", (row["id"],)).fetchone()

    def retry_later(self, job_id, error, delay):
        with self.connection() as conn:
            return conn.execute("""UPDATE jobs SET status='retry_wait',error=%s,retryable=true,
                next_attempt_at=now()+(%s * interval '1 second'),updated_at=now() WHERE id=%s AND deleted_at IS NULL RETURNING *""",
                (error, delay, job_id)).fetchone()

    def retry_manual(self, job_id, owner=None, admin=False):
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=%s AND deleted_at IS NULL FOR UPDATE", (job_id,)).fetchone()
            if not row:
                raise KeyError(job_id)
            if row["owner_id"] != owner_fields(owner)[0] and not admin:
                raise PermissionError("仅创建者或管理员可以重试此任务")
            if row["status"] != "failed" or not row["retryable"] or row["cancel_requested"]:
                raise Conflict("This job cannot be retried; ambiguous H3 submissions require operator investigation")
            self._lock_task_references(conn, row["task"], owner=owner, admin=admin)
            result = conn.execute("""UPDATE jobs SET status='queued',attempts=0,error=NULL,
                next_attempt_at=now(),updated_at=now() WHERE id=%s RETURNING *""", (job_id,)).fetchone()
        self.wake()
        return result

    def reprocess(self, job_id, verify_video, owner=None, admin=False):
        actor_id, _ = owner_fields(owner)
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=%s AND deleted_at IS NULL FOR UPDATE", (job_id,)).fetchone()
            if not row or (row["owner_id"] != actor_id and not admin):
                raise KeyError(job_id)
            checkpoint = row["checkpoint"]
            retries = checkpoint.get("postprocessing_retries", [])
            if retries and row["status"] in {"queued", "retry_wait", "running", "completed"}:
                return row  # Repeated clicks never enqueue another run.
            if (row["status"] != "failed" or row["stage"] != "postprocessing"
                    or row["cancel_requested"]):
                raise Conflict("仅已生成视频但背景处理失败的任务可以重新处理背景")
            verified = verify_video(row)
            checkpoint = {**checkpoint, "postprocessing_retries": [*retries, {
                "requested_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "requested_by": actor_id, "previous_error": row["error"],
                "previous_stage": row["stage"], **verified}]}
            result = conn.execute("""UPDATE jobs SET status='queued',attempts=0,error=NULL,retryable=false,
                checkpoint=%s,next_attempt_at=now(),updated_at=now() WHERE id=%s RETURNING *""",
                (Jsonb(checkpoint), job_id)).fetchone()
        self.wake()
        return result

    def cancel(self, job_id):
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=%s AND deleted_at IS NULL FOR UPDATE", (job_id,)).fetchone()
            if not row:
                raise KeyError(job_id)
            if row["status"] in TERMINAL:
                return row
            # Already submitted H3 work must be drained before the next GPU job.
            status = "cancelled" if row["status"] in {"queued", "retry_wait"} and not row["checkpoint"].get("h3_job_id") else row["status"]
            return conn.execute("""UPDATE jobs SET cancel_requested=true,status=%s,retryable=false,
                updated_at=now() WHERE id=%s RETURNING *""", (status, job_id)).fetchone()

    def recover(self):
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM jobs WHERE deleted_at IS NULL AND status='running' FOR UPDATE").fetchall()
            for row in rows:
                if row["checkpoint"].get("h3_terminal") == "rejected" and not row["checkpoint"].get("h3_job_id"):
                    conn.execute("""UPDATE jobs SET status='failed',stage='submission_rejected',retryable=false,
                        error=COALESCE(error,'H3 rejected request; inspect h3_rejection receipt'),
                        updated_at=now() WHERE id=%s""", (row["id"],))
                    continue
                ambiguous = row["stage"] == "submitting" and not row["checkpoint"].get("h3_job_id")
                conn.execute("""UPDATE jobs SET status=%s,error=%s,next_attempt_at=now(),
                    updated_at=now() WHERE id=%s""", (
                    "needs_attention" if ambiguous else "retry_wait",
                    "Worker restarted during H3 submission; do not resubmit without checking H3" if ambiguous else "Resuming durable checkpoint after worker restart",
                    row["id"]))

    def gpu_blocked(self):
        with self.connection() as conn:
            return conn.execute("""SELECT id FROM jobs WHERE deleted_at IS NULL AND status='needs_attention'
                AND (stage='submitting' OR (checkpoint ? 'h3_job_id' AND NOT checkpoint ? 'h3_terminal')) LIMIT 1""").fetchone()

    def finish(self, job_id, asset_id, files, metadata, preview):
        with self.connection() as conn:
            row = conn.execute("""SELECT cancel_requested,owner_id,owner_name FROM jobs
                WHERE id=%s AND deleted_at IS NULL FOR UPDATE""", (job_id,)).fetchone()
            if not row:
                raise KeyError(job_id)
            if row["cancel_requested"]:
                conn.execute("UPDATE jobs SET status='cancelled',stage='cancelled',retryable=false,updated_at=now() WHERE id=%s", (job_id,))
                return False
            conn.execute("""INSERT INTO assets(id,job_id,preview,files,metadata,owner_id,owner_name)
                VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(job_id) DO NOTHING""",
                (asset_id, job_id, preview, Jsonb(files), Jsonb(metadata), row["owner_id"], row["owner_name"]))
            conn.execute("UPDATE jobs SET status='completed',stage='completed',asset_id=%s,error=NULL,retryable=false,updated_at=now() WHERE id=%s", (asset_id, job_id))
            return True

    def notify(self, title, message, level="info", job_id=None, owner=None):
        with self.connection() as conn:
            if job_id:
                # Worker/service credentials must never redirect the notification
                # away from the human who owns this durable job.
                owner = conn.execute("SELECT owner_id,owner_name FROM jobs WHERE id=%s", (job_id,)).fetchone()
                if not owner:
                    raise KeyError(job_id)
            owner_id, owner_name = owner_fields(owner)
            return conn.execute("""INSERT INTO notifications(id,title,message,level,job_id,owner_id,owner_name)
                VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                (uuid.uuid4().hex, title, message, level, job_id, owner_id, owner_name)).fetchone()

    def heartbeat(self, value):
        with self.connection() as conn:
            conn.execute("""INSERT INTO worker_state(id,state) VALUES('main',%s) ON CONFLICT(id)
                DO UPDATE SET state=excluded.state,updated_at=now()""", (Jsonb(value),))

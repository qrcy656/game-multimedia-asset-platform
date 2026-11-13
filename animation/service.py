import json
from pathlib import Path
import shutil
import tempfile
import uuid

from PIL import Image

from .clients import ModelClients, PermanentError
from .compiler import compile_prompt
from .files import atomic_json, file_url, prose, relative_path, safe_path, sha256
from .models import FACINGS, additional_reference_selection, prompt_mode
from .store import Conflict


class Service:
    def __init__(self, settings, store, clients=None):
        self.settings, self.store = settings, store
        self.clients = clients or ModelClients(settings)
        self.root = settings.data_root.resolve()

    def bootstrap(self):
        self.store.recover_deletions()
        for folder in ("characters", "motions", "jobs", "assets"):
            (self.root / folder).mkdir(parents=True, exist_ok=True)
    def character(self, character_id):
        row = self.store.get("characters", character_id)
        if not row:
            raise KeyError(character_id)
        row["view_urls"] = {key: file_url(self.root, path) for key, path in row["views"].items()}
        labels = row.get("reference_labels") or {}
        row["images"] = [{"id": key, "label": labels.get(key, key), "path": path,
                          "url": row["view_urls"][key]} for key, path in row["views"].items()]
        return row

    def delete(self, table, object_id, actor_id, admin=False):
        return self.store.delete(table, object_id, actor_id, admin=admin)

    def snapshot(self, task, owner=None, admin=False):
        mode = prompt_mode(task)
        from .postprocess import validate_options
        options = {**task["postprocessing"], **task["target"]}
        if options["anchor"] == "bottom_center":
            options["anchor"] = "feet_center"
        validate_options(options)
        character = self.character(task["character_id"])
        if not character["approved"] and not task["preview"]:
            raise PermanentError("Character profile is a draft; explicit human approval is required for production. Use preview=true only for a marked preview.")
        reference_id = task.get("reference_id") or task.get("facing")
        if reference_id is None:
            # Match the first reference displayed by the character catalogue;
            # selecting a source image does not add a facing instruction.
            reference_id = next(iter(character["views"]), None)
        if reference_id not in character["views"]:
            raise PermanentError("所选主参考图片已不存在，请从角色档案中重新选择图片")
        reference = safe_path(self.root, character["views"][reference_id])
        labels = character.get("reference_labels") or {}
        extra_facings, upload_ids = additional_reference_selection(task)
        if reference_id in extra_facings:
            raise ValueError("补充参考图不能与当前主参考图重复")
        additional = []
        for extra_facing in extra_facings:
            if extra_facing not in character["views"]:
                raise PermanentError("所选档案补充参考图已不存在，请重新选择")
            try:
                path = safe_path(self.root, character["views"][extra_facing])
            except ValueError as exc:
                raise PermanentError("所选档案补充参考图文件已失效，请重新选择") from exc
            additional.append({"path": str(path), "sha256": sha256(path), "source": "character_view",
                               "reference_id": extra_facing, "label": labels.get(extra_facing, extra_facing),
                               "facing": extra_facing if extra_facing in task.get("additional_reference_facings", []) else None})
        actor_id = owner["owner_id"] if owner is not None else "local-admin"
        for upload_id in upload_ids:
            upload = self.store.get("uploads", upload_id)
            if not upload or upload["kind"] != "character":
                raise KeyError("Additional reference upload")
            try:
                path = safe_path(self.root, upload["path"])
            except ValueError as exc:
                raise PermanentError("上传的补充参考图文件已失效，请重新上传") from exc
            if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
                raise ValueError("补充参考图必须是 PNG、JPEG 或 WebP 图片")
            digest = sha256(path)
            if digest != upload["sha256"]:
                raise PermanentError("上传的补充参考图内容已变化，请重新上传")
            additional.append({"path": str(path), "sha256": digest, "source": "upload", "upload_id": upload_id})
        if mode != "direct" and task.get("use_character_description", False) and not prose(character["profile"].get("description", "")):
            raise PermanentError("Analyze or enter a character description first")
        motion = None
        if task.get("motion_id"):
            motion = self.store.get("motions", task["motion_id"])
            if not motion:
                raise KeyError(task["motion_id"])
            motion["video_path"] = str(safe_path(self.root, motion["video_path"]))
            motion["sha256"] = sha256(motion["video_path"])
        snapshot = {"character": {"id": character["id"], "name": character["name"], "approved": character["approved"],
                             "revision": character["revision"], "profile": character["profile"]},
                "reference": {"path": str(reference), "sha256": sha256(reference), "reference_id": reference_id,
                              "label": labels.get(reference_id, reference_id), "facing": task.get("facing")},
                "additional_references": additional,
                "motion": motion}
        self.validate_h3_references(snapshot)
        return snapshot

    @staticmethod
    def validate_h3_references(snapshot):
        """Catalogue images may be any shape; video API inputs must meet its API contract."""
        references = [snapshot["reference"], *snapshot.get("additional_references", [])]
        for index, reference in enumerate(references):
            try:
                with Image.open(reference["path"]) as picture:
                    width, height = picture.size
            except (OSError, ValueError) as exc:
                raise PermanentError(f"第 {index + 1} 张参考图片无法读取，请重新选择有效图片。") from exc
            if width * 4 < height or height * 4 < width:
                raise PermanentError(
                    f"第 {index + 1} 张参考图片尺寸为 {width}×{height}，宽高比超出 视频 API 支持的 1:4 至 4:1。"
                    "请在档案中选择单张角色图片，或先裁剪图集；本次尚未调用 LLM 或提交视频 API。")

    def motion_description(self, snapshot, output_dir):
        motion = snapshot.get("motion")
        if not motion:
            return ""
        # Human instructions are not a substitute for examining the reference.
        # Cache only a verified visual analysis of these exact video bytes.
        cached = motion.get("metadata", {}).get("analysis")
        if not cached or cached.get("video_sha256") != motion["sha256"]:
            current = self.store.get("motions", motion["id"])
            cached = current.get("metadata", {}).get("analysis") if current else None
        if cached and cached.get("video_sha256") == motion["sha256"]:
            analyzed = cached
        else:
            analyzed = self.clients.analyze_motion(motion["video_path"], output_dir)
            # Analysis belongs to this invocation; borrowing a shared motion
            # never changes its creator's original record or files.
        motion["analysis"] = analyzed
        manual = prose(motion.get("description", ""))
        return prose(analyzed["description"]) + (" Requested motion emphasis: " + manual if manual else "")

    def compile(self, task, owner=None, admin=False):
        if task.get("motion_id"):
            with self.store.resource_guard("motions", task["motion_id"]):
                return self._compile(task, owner=owner, admin=admin)
        return self._compile(task, owner=owner, admin=admin)

    def _compile(self, task, owner=None, admin=False):
        snapshot = self.snapshot(task, owner=owner, admin=admin)
        motion = snapshot.get("motion")
        (self.root / "jobs").mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="preview-", dir=self.root / "jobs") as folder:
            return self.compile_snapshot(task, snapshot, Path(folder), for_generation=False)

    def compile_snapshot(self, task, snapshot, analysis_dir, *, for_generation=True):
        """Preview and queued generation both use reference-grounded visual motion planning."""
        self.validate_h3_references(snapshot)
        motion = snapshot.get("motion")
        direct = prompt_mode(task) == "direct"
        if not direct:
            self.clients.require_visual_intent_ready()
        description = self.motion_description(snapshot, analysis_dir) if motion and not direct else ""
        translation = None
        if not direct:
            translation = self.clients.translate_animation(task, snapshot["character"]["profile"],
                snapshot["reference"]["path"], description, snapshot.get("additional_references", []))
        compiled = compile_prompt(task, snapshot["character"]["profile"], snapshot["reference"]["path"],
                              description, snapshot["character"]["revision"], motion_reference=motion,
                              additional_references=snapshot.get("additional_references", []),
                              translation=translation, reference_metadata=snapshot["reference"])
        try:
            profile = self.clients.video_profile()
        except PermanentError:
            if for_generation:
                raise
            profile = None
        if profile:
            compiled["h3_request"]["model"] = profile["model"]
            compiled["video_provider"] = {key: profile.get(key) for key in ("id", "name", "model", "protocol", "base_url", "scope")}
        else:
            compiled["video_provider"] = None
            compiled["warnings"].append('当前为提示词预览；生成动画时再配置视频 API 与透明背景图片 API。')
        compiled["motion_description"] = description
        return compiled

    def enqueue(self, task, owner=None, admin=False):
        existing = self.store.find_idempotency(task.get("idempotency_key"), task, owner=owner)
        if existing:
            return existing
        snapshot = self.snapshot(task, owner=owner, admin=admin)
        from providers import capture_model_context
        snapshot["model_context"] = capture_model_context()
        self.require_generation_ready(task)
        job_id = uuid.uuid4().hex
        folder = self.root / "jobs" / job_id
        try:
            self._freeze_snapshot(task, snapshot, folder)
            result = self.store.enqueue(job_id, task, json.loads(json.dumps(snapshot, default=str)), owner=owner, admin=admin)
        except Exception:
            # Confirm absence in a fresh transaction before removing a directory:
            # a connection error at COMMIT may have occurred after persistence.
            try:
                if not self.store.get("jobs", job_id):
                    self._discard_unqueued_folder(folder)
            except Exception:
                pass  # Preserve bytes when commit status cannot be established.
            raise
        if result["id"] != job_id:
            self._discard_unqueued_folder(folder)
        return result

    def require_generation_ready(self, task):
        mode = prompt_mode(task)
        if mode == "assisted":
            return self.clients.require_generation_ready(prompt_mode=mode)
        return self.clients.require_generation_ready()

    def reprocess(self, job_id, owner=None, admin=False):
        def verify_video(row):
            checkpoint = row["checkpoint"]
            compiled = checkpoint.get("compiled")
            receipt = checkpoint.get("video")
            if (not checkpoint.get("h3_job_id") or checkpoint.get("h3_terminal") != "completed"
                    or not isinstance(compiled, dict) or not isinstance(compiled.get("h3_request"), dict)
                    or not isinstance(compiled.get("prompt"), str) or not isinstance(receipt, dict)):
                raise Conflict("已完成的视频及生成检查点不完整，不能仅重新处理背景")
            try:
                video = safe_path(self.root, Path("jobs") / row["id"] / "generation.mp4")
                size = video.stat().st_size
                digest = sha256(video)
            except (OSError, ValueError) as exc:
                raise Conflict("已生成的视频文件不存在或无法读取，不能重新处理背景") from exc
            if not size or digest != receipt.get("sha256") or size != receipt.get("bytes"):
                raise Conflict("已生成的视频与下载校验记录不一致，不能重新处理背景")
            return {"video_sha256": digest, "video_bytes": size}

        # The store holds the job row lock while verifying the existing video.
        # No model readiness probe or reference re-analysis is needed here.
        return self.store.reprocess(job_id, verify_video, owner=owner, admin=admin)

    def _freeze_snapshot(self, task, snapshot, folder):
        refs = folder / "refs"
        refs.mkdir(parents=True, exist_ok=False)
        source = Path(snapshot["reference"]["path"])
        dest = refs / ((task.get("facing") or "primary") + source.suffix.lower())
        if sha256(source) != snapshot["reference"]["sha256"]:
            raise PermanentError("Reference changed while task snapshot was being created")
        shutil.copyfile(source, dest)
        if sha256(dest) != snapshot["reference"]["sha256"]:
            raise PermanentError("Reference changed while task snapshot was being created")
        snapshot["reference"]["original_path"] = str(source)
        snapshot["reference"]["path"] = str(dest)
        for index, reference in enumerate(snapshot.get("additional_references", []), 2):
            source = Path(reference["path"])
            if sha256(source) != reference["sha256"]:
                raise PermanentError("Additional reference changed before snapshot freeze")
            dest = refs / (f"picture_{index}" + source.suffix.lower())
            shutil.copyfile(source, dest)
            if sha256(dest) != reference["sha256"]:
                raise PermanentError("Additional reference changed during snapshot freeze")
            reference["original_path"], reference["path"] = str(source), str(dest)
        if snapshot.get("motion"):
            motion = snapshot["motion"]
            source = Path(motion["video_path"])
            dest = refs / ("motion" + source.suffix.lower())
            shutil.copyfile(source, dest)
            if sha256(dest) != motion["sha256"]:
                raise PermanentError("Motion reference changed during snapshot")
            motion["original_path"] = str(source)
            motion["video_path"] = str(dest)
        atomic_json(folder / "task.json", task)
        atomic_json(folder / "profile.json", snapshot["character"])
        atomic_json(folder / "references.json", snapshot)

    def _discard_unqueued_folder(self, folder):
        from .deletion import FileRemoval, directory_files
        removal = FileRemoval(self.root, directory_files(self.root, folder), [folder])
        removal.prepare()
        removal.purge()

    def approve(self, character_id, profile):
        self.character(character_id)
        if not prose(profile.get("description", "")):
            raise PermanentError("Approval requires an explicit reviewed description")
        result = self.store.save_profile(character_id, profile, True)
        atomic_json(self.root / "characters" / character_id / "profile.json", result)
        return self.character(character_id)

    def analyze(self, character_id):
        character = self.character(character_id)
        paths = [safe_path(self.root, value) for value in list(character["views"].values())[:4]]
        if not paths:
            raise PermanentError("Character has no reference images")
        profile = self.clients.analyze_character(paths)
        from .privacy import public_metadata
        profile = public_metadata(profile)
        self.store.save_profile(character_id, profile, False)
        atomic_json(self.root / "characters" / character_id / "profile.json", profile)
        return self.character(character_id)

    def asset(self, asset_id):
        asset = self.store.get("assets", asset_id)
        if not asset:
            raise KeyError(asset_id)
        asset["export_url"] = f"/animation/api/assets/{asset_id}/export.zip"
        asset["quality"] = asset.get("metadata", {}).get("postprocessing", {}).get("quality", {})
        asset["task"] = asset.get("metadata", {}).get("task", {})
        for field in ("character_id", "action", "facing"):
            asset[field] = asset["task"].get(field)
        return asset

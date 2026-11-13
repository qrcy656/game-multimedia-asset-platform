"""Single H3 consumer with durable phase checkpoints and bounded safe retries."""
import datetime
import json
import os
from pathlib import Path
import shutil
import signal
import time
import traceback
import uuid

import psycopg

from . import __version__
from .clients import Cancelled, H3NotSubmitted, H3Rejected, ModelClients, NeedsAttention, PermanentError, TransientError
from .compiler import VERSION, compile_prompt
from .config import settings as default_settings
from .files import atomic_json, file_url, sha256
from .models import prompt_mode
from .service import Service
from .store import Store

ADVISORY_LOCK_ID = 73114418080


class Worker:
    def __init__(self, settings, store, clients=None, postprocessor=None, sleep=time.sleep, clock=time.time):
        self.settings, self.store = settings, store
        self.clients = clients or ModelClients(settings)
        self.service = Service(settings, store, self.clients)
        self.postprocessor = postprocessor
        self.sleep, self.clock = sleep, clock

    def checkpoint(self, job, stage, **values):
        job["checkpoint"].update(values)
        job["stage"] = stage
        self.store.update_job(job["id"], stage=stage, checkpoint=job["checkpoint"])
        atomic_json(self.settings.data_root / "jobs" / job["id"] / "checkpoint.json", job["checkpoint"])

    def check_cancel(self, job):
        current = self.store.get("jobs", job["id"])
        if current and current["cancel_requested"]:
            raise Cancelled("Cancellation requested")

    def process(self, job):
        task, snapshot, checkpoint = job["task"], job["snapshot"], job["checkpoint"]
        folder = self.settings.data_root / "jobs" / job["id"]
        folder.mkdir(parents=True, exist_ok=True)
        from .matting import freeze_provider, matte_provider
        freeze_provider(self, job)
        video = folder / "generation.mp4"
        if not checkpoint.get("h3_job_id"):
            if checkpoint.get("h3_terminal") == "rejected":
                raise PermanentError("H3 已拒绝此请求；请修正参数并创建新任务，不会重复提交原请求。")
            self.check_cancel(job)
            current_character = self.store.get("characters", task["character_id"])
            if not task["preview"] and (not current_character or not current_character["approved"]):
                raise PermanentError("Character profile is not approved; production task will not run")
            # Recheck on every pre-submit attempt, including an already compiled
            # checkpoint recovered after a known-unsent connection failure.
            if sha256(snapshot["reference"]["path"]) != snapshot["reference"]["sha256"]:
                raise PermanentError("Frozen character reference checksum mismatch")
            for reference in snapshot.get("additional_references", []):
                if sha256(reference["path"]) != reference["sha256"]:
                    raise PermanentError("Frozen additional reference checksum mismatch")
            if snapshot.get("motion") and sha256(snapshot["motion"]["video_path"]) != snapshot["motion"]["sha256"]:
                raise PermanentError("Frozen motion reference checksum mismatch")
            self.service.validate_h3_references(snapshot)
        if not checkpoint.get("compiled"):
            self.checkpoint(job, "compiling")
            compiled = self.service.compile_snapshot(task, snapshot, folder / "motion_analysis")
            motion = compiled["motion_description"]
            atomic_json(folder / "compiled.json", compiled)
            (folder / "prompt.txt").write_text(compiled["prompt"], encoding="utf-8")
            atomic_json(folder / "h3_request.json", compiled["h3_request"])
            self.checkpoint(job, "compiled", compiled=compiled, motion_description=motion,
                            motion_analysis=snapshot.get("motion", {}).get("analysis") if snapshot.get("motion") else None)
        if not checkpoint.get("h3_job_id"):
            self.check_cancel(job)
            # No submission checkpoint/POST when a dependency stopped after enqueue.
            self.service.require_generation_ready(task)
            checkpoint.pop("h3_submission_not_sent", None)
            # Durable state is committed BEFORE POST. A crash here is intentionally
            # conservative; no replacement process may blindly issue another POST.
            self.checkpoint(job, "submitting", submission_started_at=self.clock())
            try:
                submitted = self.clients.submit_h3(checkpoint["compiled"]["h3_request"], provider=checkpoint["compiled"]["video_provider"])
            except H3NotSubmitted as exc:
                # Only a proved pre-send transport failure may clear ambiguity.
                # Persist the rollback before the outer handler schedules retry;
                # a failed DB write leaves the durable submitting gate intact.
                checkpoint.pop("submission_started_at", None)
                self.checkpoint(job, "compiled", h3_submission_not_sent={
                    "transport_error": exc.transport_error, "confirmed_at": self.clock()})
                raise
            except H3Rejected as exc:
                # Explicit rejection is terminal even if the worker exits before
                # the outer failure handler runs. Keep the original POST time.
                self.checkpoint(job, "submission_rejected", h3_terminal="rejected",
                    h3_rejection={"status_code": exc.status_code, "response": exc.response_text,
                                  "confirmed_at": self.clock()})
                raise
            self.checkpoint(job, "polling", h3_job_id=submitted["id"], submit_response=submitted,
                            poll_started_at=self.clock())
            atomic_json(folder / "h3_submit_response.json", submitted)
        if not checkpoint.get("h3_terminal") and checkpoint.get("submit_response", {}).get("status") == "completed":
            self.checkpoint(job, "generated", h3_terminal="completed", generation_response=checkpoint["submit_response"])
        if not checkpoint.get("h3_terminal"):
            self.checkpoint(job, "polling")
            while True:
                state = self.clients.h3_status(checkpoint["h3_job_id"], provider=checkpoint["compiled"]["video_provider"])
                atomic_json(folder / "h3_status.json", state)
                self.store.heartbeat({"status": "polling", "job_id": job["id"], "h3_status": state.get("status")})
                status = state.get("status")
                if status in {"completed", "failed", "cancelled", "canceled", "deleted"}:
                    self.checkpoint(job, "generated", h3_terminal=status, generation_response=state)
                    break
                if status not in {"queued", "pending", "processing", "in_progress", "running"}:
                    raise NeedsAttention("Unknown H3 job status: " + str(status))
                if self.clock() - checkpoint["poll_started_at"] > self.settings.max_poll_seconds:
                    raise NeedsAttention("H3 polling deadline elapsed; job may still run, so no replacement POST is allowed")
                # Cancellation does not release the GPU gate while H3 is still running.
                self.sleep(self.settings.poll_seconds)
        if checkpoint["h3_terminal"] in {"cancelled", "canceled", "deleted"}:
            raise Cancelled("H3 generation was cancelled")
        if checkpoint["h3_terminal"] != "completed":
            raise PermanentError("视频 API 生成失败；请在供应商处核对本任务，工作台不会公开供应商的原始错误回包。")
        self.check_cancel(job)
        if not checkpoint.get("video") or not video.is_file():
            self.checkpoint(job, "downloading")
            info = self.clients.download_h3(checkpoint["h3_job_id"], video, provider=checkpoint["compiled"]["video_provider"], state=checkpoint.get("generation_response"))
            self.checkpoint(job, "downloaded", video=info)
        if sha256(video) != checkpoint["video"]["sha256"]:
            raise PermanentError("Stored H3 video checksum does not match the download receipt")
        self.check_cancel(job)
        output = self.settings.data_root / "assets" / job["id"]
        if not checkpoint.get("postprocess_result"):
            self.checkpoint(job, "postprocessing")
            temporary = folder / ("postprocess-" + uuid.uuid4().hex)
            options = {**task["postprocessing"], **task["target"]}
            options["anchor"] = "feet_center" if options["anchor"] == "bottom_center" else options["anchor"]
            processor = self.postprocessor
            if processor is None:
                from .postprocess import process_video
                processor = process_video
            try:
                provider = matte_provider(self, job)
                result = processor(video, temporary, options, matte_provider=provider) if provider else processor(video, temporary, options)
            except (Cancelled, NeedsAttention, TransientError, PermanentError):
                raise
            except (ValueError, RuntimeError) as exc:
                raise PermanentError("Postprocessing rejected output: " + str(exc)) from exc
            self.checkpoint(job, "postprocessed", postprocess_result=result, postprocess_dir=str(temporary))
        self.check_cancel(job)
        if not output.exists():
            output.parent.mkdir(parents=True, exist_ok=True)
            os.replace(checkpoint["postprocess_dir"], output)
        result = checkpoint["postprocess_result"]
        compiled_version = checkpoint["compiled"].get("compiler_version", VERSION)
        versions = {"animation": __version__, "compiler": compiled_version,
                    "translator_model": (checkpoint["compiled"].get("llm_translation") or {}).get("model"),
                    "prompt_mode": task.get("prompt_mode", "assisted"),
                    "video_provider": checkpoint["compiled"].get("video_provider"),
                    "background_removal": result.get("background_model", {}).get("version", "cpu-border-connected-color-distance")}
        from .privacy import public_metadata, public_document
        public_snapshot = public_metadata(snapshot)
        metadata = {"job_id": job["id"], "asset_id": job["id"], "preview": task["preview"],
                    "owner_id": job["owner_id"], "owner_name": job["owner_name"],
                    "task": task, "character": snapshot["character"], "references": public_snapshot,
                    "seed": task["seed"], "versions": versions, "h3_job_id": checkpoint["h3_job_id"],
                    "video": checkpoint["video"], "compiler_version": compiled_version,
                    "postprocessing": result, "motion_description": checkpoint.get("motion_description", ""),
                    "llm_translation": checkpoint["compiled"].get("llm_translation"),
                    "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
        metadata = public_metadata(metadata)
        for name, value in (("task.json", task), ("profile.json", snapshot["character"]),
                            ("references.json", public_snapshot), ("versions.json", versions),
                            ("seed.json", {"seed": task["seed"]}), ("h3_request.json", checkpoint["compiled"]["h3_request"]),
                            ("metadata.json", metadata)):
            atomic_json(output / name, public_document(value, name))
        (output / "prompt.txt").write_text(checkpoint["compiled"]["prompt"], encoding="utf-8")
        refs = output / "refs"
        refs.mkdir(exist_ok=True)
        for source in (folder / "refs").glob("*"):
            if source.is_file() and not (refs / source.name).exists():
                shutil.copyfile(source, refs / source.name)
        files = {"video": file_url(self.settings.data_root, output / result.get("source_video", "source/video.mp4")),
                 "spritesheet": file_url(self.settings.data_root, output / result["spritesheet"]),
                 "preview": file_url(self.settings.data_root, output / result["preview"]),
                 "manifest": file_url(self.settings.data_root, output / result["manifest"]),
                 "metadata": file_url(self.settings.data_root, output / "metadata.json"),
                 "spritesheet_json": file_url(self.settings.data_root, output / result["spritesheet_json"]),
                 "frames": [file_url(self.settings.data_root, p) for p in sorted((output / result["frames_dir"]).glob("*.png"))]}
        if not files["frames"]:
            raise PermanentError("Postprocessor produced no PNG frames")
        atomic_json(output / "checksums.json", {p.relative_to(output).as_posix(): sha256(p)
                    for p in sorted(output.rglob("*")) if p.is_file() and p.name != "checksums.json"})
        if self.store.finish(job["id"], job["id"], files, metadata, task["preview"]):
            label = " / ".join(str(value) for value in (task["character_id"], task.get("action"), task.get("facing")) if value)
            self.store.notify("动画生成完成", label, "success", job["id"])

    def handle(self, job):
        try:
            from providers import restore_model_context
            with restore_model_context(job.get("snapshot", {}).get("model_context"), owner_id=job["owner_id"]):
                self.process(job)
        except Cancelled as exc:
            self.store.update_job(job["id"], status="cancelled", stage="cancelled", error=str(exc), retryable=False)
        except NeedsAttention as exc:
            self.store.update_job(job["id"], status="needs_attention", error=str(exc), retryable=False)
            self.store.notify("任务需要人工检查", str(exc), "error", job["id"])
        except TransientError as exc:
            if job["attempts"] < job["max_attempts"]:
                self.store.retry_later(job["id"], str(exc), min(5 * 2 ** (job["attempts"] - 1), 60))
            else:
                unresolved_h3 = job["checkpoint"].get("h3_job_id") and not job["checkpoint"].get("h3_terminal")
                self.store.update_job(job["id"], status="needs_attention" if unresolved_h3 else "failed",
                                      error=str(exc), retryable=not bool(unresolved_h3))
                self.store.notify("任务重试已用尽", str(exc), "error", job["id"])
        except (PermanentError, ValueError, FileNotFoundError) as exc:
            self.store.update_job(job["id"], status="failed", error=str(exc), retryable=False)
            self.store.notify("动画任务失败", str(exc), "error", job["id"])

    def run(self):
        self.store.initialize()
        self.service.bootstrap()
        # Hold on a dedicated session. No other process can generate with H3 while
        # this worker owns this lock, even if Redis restarts or disappears.
        with psycopg.connect(self.settings.database_url, autocommit=True) as leader:
            if not leader.execute("SELECT pg_try_advisory_lock(%s)", (ADVISORY_LOCK_ID,)).fetchone()[0]:
                raise RuntimeError("Another animation worker already owns the H3 generation lock")
            self.store.recover()
            while True:
                # Validate the lock-holding DB connection before touching a GPU job.
                leader.execute("SELECT 1")
                blocked = self.store.gpu_blocked()
                if blocked:
                    self.store.heartbeat({"status": "needs_attention", "blocked_by": blocked["id"]})
                    self.store.wait()
                    continue
                job = self.store.claim()
                self.store.heartbeat({"status": "running" if job else "idle", "job_id": job["id"] if job else None})
                if job:
                    self.handle(job)
                else:
                    self.store.wait()


def main():
    # PID 1 must explicitly handle SIGTERM. Checkpoints make immediate shutdown
    # recoverable, including conservative treatment of an interrupted POST.
    def terminate(signum, frame):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, terminate)
    worker = Worker(default_settings, Store(default_settings))
    try:
        worker.run()
    except Exception:
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()

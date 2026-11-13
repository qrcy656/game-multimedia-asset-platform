"""Bounded ComfyUI API workflow execution, independent of the local GPU queue.

The configured ComfyUI server owns node/model execution. This client never calls
/interrupt or /free: both affect other users of the shared ComfyUI service.
"""
from __future__ import annotations

import copy
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid


class ComfyError(ValueError):
    pass


class SubmissionUncertain(ComfyError):
    pass


# Standard image pipelines only. Administrators can explicitly enable installed
# custom nodes through MEDIA_COMFY_ALLOWED_NODES; browser input cannot do so.
STANDARD_NODES = set("""CheckpointLoaderSimple CheckpointLoader DiffusersLoader
CLIPLoader DualCLIPLoader TripleCLIPLoader UNETLoader VAELoader LoraLoader LoraLoaderModelOnly
CLIPTextEncode CLIPSetLastLayer ConditioningCombine ConditioningAverage ConditioningConcat
ConditioningSetArea ConditioningSetAreaPercentage ConditioningSetMask ConditioningZeroOut
ConditioningSetTimestepRange ControlNetLoader ControlNetApply ControlNetApplyAdvanced
EmptyLatentImage EmptySD3LatentImage EmptyFlux2LatentImage KSampler KSamplerAdvanced
VAEDecode VAEDecodeTiled VAEEncode VAEEncodeTiled VAEEncodeForInpaint InpaintModelConditioning
SetLatentNoiseMask LatentUpscale LatentUpscaleBy LatentComposite LatentBlend RepeatLatentBatch
LoadImage ImageScale ImageScaleBy ImageInvert ImageBatch ImageCrop ImagePadForOutpaint
EmptyImage ImageCompositeMasked ImageToMask MaskToImage SolidMask InvertMask MaskComposite
GrowMask FeatherMask CropMask ThresholdMask JoinImageWithAlpha SplitImageWithAlpha
SaveImage PreviewImage UpscaleModelLoader ImageUpscaleWithModel
FluxGuidance FluxDisableGuidance ModelSamplingFlux ModelSamplingAuraFlow ModelSamplingSD3
ModelSamplingDiscrete ModelSamplingContinuousEDM ModelSamplingContinuousV
BasicGuider CFGGuider DualCFGGuider RandomNoise DisableNoise KSamplerSelect
BasicScheduler KarrasScheduler ExponentialScheduler PolyexponentialScheduler VPScheduler
SplitSigmas FlipSigmas SetFirstSigma SamplerCustom SamplerCustomAdvanced
TextEncodeQwenImageEdit TextEncodeQwenImageEditPlus TextEncodeQwenImage TextEncodeFlux2
CLIPTextEncodeSDXL CLIPTextEncodeSDXLRefiner CLIPTextEncodeFlux
RescaleCFG DifferentialDiffusion ImageColorToMask ImageQuantize ImageBlur ImageSharpen""".split())
BINDINGS = {"prompt", "negative_prompt", "seed", "width", "height", "image"}
NODE_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
FIELD = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,79}$")


def relative_name(value, *, empty=False):
    if empty and value == "":
        return ""
    if (not isinstance(value, str) or not value or len(value) > 250 or "\\" in value
            or any(ord(c) < 32 for c in value) or ":" in value
            or value.startswith("/") or any(p in {".", "..", ""} for p in value.split("/"))):
        raise ComfyError("ComfyUI 返回或工作流包含无效的文件路径。")
    return value


def parse_json(value, label, limit):
    if not isinstance(value, str) or len(value) > limit:
        raise ComfyError(label + "格式无效或过大。")
    def unique(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ComfyError(label + "不允许重复键。")
            result[key] = item
        return result
    try:
        result = json.loads(value, object_pairs_hook=unique,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, RecursionError):
        raise ComfyError(label + "必须为有效 JSON 对象。") from None
    if not isinstance(result, dict):
        raise ComfyError(label + "必须为 JSON 对象。")
    return result


def validate_workflow(params, *, has_image=False, allowed_nodes=None):
    workflow = parse_json(params["workflow"], "工作流", 65536)
    bindings = parse_json(params["bindings"], "参数绑定", 2048)
    if not 1 <= len(workflow) <= 100 or set(bindings) - BINDINGS:
        raise ComfyError("工作流须包含 1–100 个节点，参数绑定仅支持 prompt、negative_prompt、seed、width、height、image。")
    allowed = STANDARD_NODES | {item for item in (allowed_nodes or []) if isinstance(item, str)}
    outputs = 0
    for identifier, node in workflow.items():
        if not NODE_ID.fullmatch(identifier) or not isinstance(node, dict) or set(node) - {"class_type", "inputs", "_meta"}:
            raise ComfyError("请使用 ComfyUI 导出的 API 格式工作流，不能使用可视化界面格式。")
        kind, inputs = node.get("class_type"), node.get("inputs")
        if not isinstance(kind, str) or kind not in allowed or not isinstance(inputs, dict) or len(inputs) > 100:
            raise ComfyError("工作流含有未启用的节点；请使用标准图片节点，或由管理员审核并配置自定义节点。")
        outputs += kind == "SaveImage"
        for key, value in inputs.items():
            if not FIELD.fullmatch(key):
                raise ComfyError("工作流输入名称无效。")
            if isinstance(value, list):
                if len(value) != 2 or not isinstance(value[0], str) or value[0] not in workflow or type(value[1]) is not int or not 0 <= value[1] <= 32:
                    raise ComfyError("工作流节点连接无效。")
            elif value is not None and not isinstance(value, (str, int, float, bool)):
                raise ComfyError("工作流输入只能是基本值或节点连接。")
            if isinstance(value, float) and not math.isfinite(value):
                raise ComfyError("工作流数值必须为有限数字。")
            if isinstance(value, str) and ("\x00" in value or len(value) > 16000):
                raise ComfyError("工作流文本输入无效或过长。")
            if isinstance(value, str) and (key.endswith(("_name", "_path", "_file", "_directory")) or key in {"path", "directory", "file"}):
                relative_name(value)
            if key in {"batch_size", "amount"} and (type(value) is not int or not 1 <= value <= 8):
                raise ComfyError("单次工作流批量最多 8 张图片。")
            if key in {"width", "height"} and not isinstance(value, list) and (type(value) is not int or not 1 <= value <= 4096):
                raise ComfyError("工作流图片宽高最多 4096 像素。")
            if key == "steps" and not isinstance(value, list) and (type(value) is not int or not 1 <= value <= 150):
                raise ComfyError("工作流采样步数范围为 1–150。")
    if not 1 <= outputs <= 8:
        raise ComfyError("工作流需要 1–8 个 SaveImage 输出节点。")
    targets = set()
    for name, binding in bindings.items():
        if (not isinstance(binding, dict) or set(binding) != {"node", "field"}
                or not isinstance(binding["node"], str) or binding["node"] not in workflow
                or not isinstance(binding["field"], str) or binding["field"] not in workflow[binding["node"]]["inputs"]):
            raise ComfyError("参数绑定必须指定存在的节点 node 和输入 field。")
        target = (binding["node"], binding["field"])
        if target in targets:
            raise ComfyError("不同参数不能绑定到同一个节点输入。")
        targets.add(target)
        if isinstance(workflow[target[0]]["inputs"][target[1]], list):
            raise ComfyError("参数只能绑定字面值输入，不能覆盖节点连接。")
        if name == "image" and (workflow[target[0]]["class_type"] != "LoadImage" or target[1] != "image"):
            raise ComfyError("参考图片必须绑定 LoadImage 节点的 image 输入。")
    # EmptyImage is a useful CPU-only connectivity workflow, hence a prompt
    # binding is required only when the user actually supplies a prompt.
    for name in ("prompt", "negative_prompt"):
        if params.get(name) and name not in bindings:
            raise ComfyError(name + "已填写，但没有对应的工作流参数绑定。")
    if has_image is not None and has_image != ("image" in bindings):
        raise ComfyError("上传参考图时必须配置 image 绑定；配置 image 绑定时必须选择参考图。")
    image_target = bindings.get("image", {}).get("node")
    if any(node["class_type"] == "LoadImage" and key != image_target for key, node in workflow.items()):
        raise ComfyError("LoadImage 只允许读取本次任务上传并绑定的参考图片。")
    return workflow, bindings


def prepare_workflow(params, job_id, image_name=None, *, allowed_nodes=None):
    workflow, bindings = validate_workflow(params, has_image=bool(image_name), allowed_nodes=allowed_nodes)
    workflow = copy.deepcopy(workflow)
    for name, binding in bindings.items():
        value = relative_name(image_name) if name == "image" else params[name]
        workflow[binding["node"]]["inputs"][binding["field"]] = value
    for identifier, node in workflow.items():
        if node["class_type"] == "SaveImage":
            node["inputs"]["filename_prefix"] = "fantasy-" + job_id + "-" + identifier
    return workflow


class ComfyClient:
    def __init__(self, base=None):
        self.base = (base or "").rstrip("/")

    def request(self, path, body=None, *, raw=False, content_type=None, maximum=8 * 1024 * 1024, timeout=30):
        if not path.startswith("/") or path.startswith("//"):
            raise ComfyError("无效 ComfyUI API 路径。")
        data = body if isinstance(body, bytes) else None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.base + path, data=data,
            headers={"Content-Type": content_type or "application/json"})
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=timeout) as response:
                result = response.read(maximum + 1)
                if len(result) > maximum:
                    if path == "/prompt":
                        raise SubmissionUncertain("ComfyUI 提交后返回内容过大，结果不明确，不会自动重发。")
                    raise ComfyError("ComfyUI 返回内容超过单次限制。")
                if not result and body is not None and path in {"/queue", "/history"}:
                    return {}
                return result if raw else json.loads(result)
        except urllib.error.HTTPError as exc:
            if path == "/prompt" and (exc.code in {408, 409, 429} or exc.code >= 500):
                raise SubmissionUncertain("ComfyUI 提交结果不明确，已保留任务供核对，未自动重发。") from None
            raise ComfyError("ComfyUI 拒绝请求（HTTP %s），请检查工作流和已安装节点。" % exc.code) from None
        except (OSError, ValueError, urllib.error.URLError) as exc:
            if isinstance(exc, ComfyError):
                raise
            if path == "/prompt":
                raise SubmissionUncertain("ComfyUI 提交结果不明确，已保留任务供核对，未自动重发。") from None
            raise ComfyError("无法连接 ComfyUI 或返回格式无效，请在主页检查服务。") from None

    def ready(self):
        try:
            result = self.request("/system_stats", timeout=3)
            return isinstance(result, dict) and isinstance(result.get("system"), dict) and bool(result["system"]) and isinstance(result.get("devices"), list)
        except ComfyError:
            return False

    def upload(self, source, job_id):
        source = Path(source)
        size = source.stat().st_size
        if not 0 < size <= 200 * 1024 * 1024:
            raise ComfyError("ComfyUI 参考图为空或超过 200 MiB。")
        boundary = "Fantasy" + uuid.uuid4().hex
        name = "fantasy-" + job_id + source.suffix.lower()
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{name}"\r\nContent-Type: application/octet-stream\r\n\r\n').encode()
        body += source.read_bytes() + (f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="type"\r\n\r\ninput\r\n--{boundary}--\r\n').encode()
        result = self.request("/upload/image", body, content_type="multipart/form-data; boundary=" + boundary, timeout=120)
        if not isinstance(result, dict) or result.get("type") != "input":
            raise ComfyError("ComfyUI 未确认参考图上传。")
        filename = relative_name(result.get("name"))
        subfolder = relative_name(result.get("subfolder", ""), empty=True)
        return (subfolder + "/" if subfolder else "") + filename

    def submit(self, workflow, client_id):
        result = self.request("/prompt", {"prompt": workflow, "client_id": client_id,
                                         "extra_data": {"fantasy_job_id": client_id}}, timeout=120)
        identifier = result.get("prompt_id") if isinstance(result, dict) else None
        if not isinstance(identifier, str) or not NODE_ID.fullmatch(identifier):
            raise SubmissionUncertain("ComfyUI 未返回有效任务编号，请核对服务记录；本任务不会自动重发。")
        return identifier

    def history(self, prompt_id):
        result = self.request("/history/" + urllib.parse.quote(prompt_id, safe=""))
        if not isinstance(result, dict):
            raise ComfyError("ComfyUI 任务历史格式无效。")
        return result.get(prompt_id)

    def recover_submission(self, client_id):
        queue = self.request("/queue")
        if not isinstance(queue, dict):
            raise ComfyError("ComfyUI 队列格式无效。")
        candidates = list(queue.get("queue_running", [])) + list(queue.get("queue_pending", []))
        history = self.request("/history?max_items=200")
        if not isinstance(history, dict):
            raise ComfyError("ComfyUI 历史格式无效。")
        candidates.extend(row.get("prompt", []) for row in history.values() if isinstance(row, dict))
        matches = []
        for row in candidates:
            if isinstance(row, list) and len(row) >= 4 and isinstance(row[3], dict) and (row[3].get("client_id") == client_id or row[3].get("fantasy_job_id") == client_id):
                if isinstance(row[1], str) and NODE_ID.fullmatch(row[1]):
                    matches.append(row[1])
        found = set(matches)
        if len(found) > 1:
            raise ComfyError("ComfyUI 返回多个关联任务，请管理员核对；不会再次提交。")
        return next(iter(found), None)

    def cancel_pending(self, prompt_id, client_id):
        state = self.request("/queue")
        if not isinstance(state, dict):
            raise ComfyError("ComfyUI 队列格式无效。")
        for row in state.get("queue_pending", []):
            if (isinstance(row, list) and len(row) >= 4 and row[1] == prompt_id and isinstance(row[3], dict)
                    and (row[3].get("client_id") == client_id or row[3].get("fantasy_job_id") == client_id)):
                self.request("/queue", {"delete": [prompt_id]})
                # The workflow may have started between GET and POST. Let the
                # polling worker observe completion rather than interrupt it.
                after = self.request("/queue")
                active = after.get("queue_running", []) + after.get("queue_pending", [])
                return not any(isinstance(item, list) and len(item) > 1 and item[1] == prompt_id for item in active)
        return False

    def download(self, history, output):
        outputs = history.get("outputs")
        if not isinstance(outputs, dict):
            raise ComfyError("ComfyUI 未返回图片输出。")
        artifacts, total = [], 0
        for node in outputs.values():
            for item in node.get("images", []) if isinstance(node, dict) else []:
                if not isinstance(item, dict) or item.get("type") != "output":
                    continue
                filename = relative_name(item.get("filename"))
                if "/" in filename:
                    raise ComfyError("ComfyUI 图片文件名无效。")
                subfolder = relative_name(item.get("subfolder", ""), empty=True)
                extension = PurePosixPath(filename).suffix.lower()
                if extension not in {".png", ".jpg", ".jpeg", ".webp"} or len(artifacts) >= 16:
                    raise ComfyError("ComfyUI 最多返回 16 张 PNG、JPEG 或 WebP 图片。")
                data = self.request("/view?" + urllib.parse.urlencode({"filename": filename, "subfolder": subfolder, "type": "output"}), raw=True, maximum=64 * 1024 * 1024, timeout=120)
                valid = (extension == ".png" and data.startswith(b"\x89PNG\r\n\x1a\n") or extension in {".jpg", ".jpeg"} and data.startswith(b"\xff\xd8\xff") or extension == ".webp" and data[:4] == b"RIFF" and data[8:12] == b"WEBP")
                total += len(data)
                if not valid or total > 256 * 1024 * 1024:
                    raise ComfyError("ComfyUI 图片内容无效或总大小超过 256 MiB。")
                name = "comfy-%02d%s" % (len(artifacts) + 1, extension)
                (Path(output) / name).write_bytes(data)
                artifacts.append({"name": name, "kind": "image", "size": len(data)})
        if not artifacts:
            raise ComfyError("工作流已结束，但没有 SaveImage 输出图片。")
        return artifacts

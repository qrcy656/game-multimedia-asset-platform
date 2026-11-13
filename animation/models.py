from typing import Any, Literal
import re
from pydantic import BaseModel, Field, field_validator, model_validator

FACINGS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
# Application limit, not a claim about the model's maximum supported inputs.
MAX_CHARACTER_REFERENCES = 9


def prompt_mode(task):
    mode = task.get("prompt_mode", "assisted")
    if mode not in {"assisted", "direct"}:
        raise ValueError("提示词模式必须为 assisted 或 direct")
    if mode == "direct":
        value = task.get("video_prompt", "")
        if not isinstance(value, str) or not value.strip() or len(value) > 12000 or "\x00" in value:
            raise ValueError("直接生成模式需要填写视频提示词，最多 12000 字符")
    return mode


def additional_reference_selection(task):
    facings = task.get("additional_reference_facings", [])
    references = task.get("additional_reference_ids", [])
    uploads = task.get("additional_reference_upload_ids", [])
    if not all(isinstance(value, list) for value in (facings, references, uploads)):
        raise ValueError("补充参考图必须以列表提交")
    if len(facings) + len(references) + len(uploads) > MAX_CHARACTER_REFERENCES - 1:
        raise ValueError(f"角色参考图总数最多为 {MAX_CHARACTER_REFERENCES} 张（包含主参考图）")
    if any(not isinstance(f, str) or f not in FACINGS for f in facings):
        raise ValueError("补充参考图的朝向无效")
    if any(not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", value) for value in references):
        raise ValueError("档案图片 ID 无效")
    selected = facings + references
    if len(set(selected)) != len(selected) or (task.get("reference_id") or task.get("facing")) in selected:
        raise ValueError("补充参考图不能重复，也不能与主参考图相同")
    if any(not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", value) for value in uploads):
        raise ValueError("请先上传补充参考图，再提交上传记录 ID；不支持直接填写路径或网址")
    if len(set(uploads)) != len(uploads):
        raise ValueError("同一张上传的补充参考图不能重复选择")
    return selected, uploads
ACTIONS = {
    "idle": ("待机", "A restrained repeating idle cycle with gentle breathing and subtle cloth movement."),
    "walk": ("行走", "An even in-place walk cycle with alternating planted steps and opposing arm swings."),
    "run": ("奔跑", "A continuous in-place running cycle, alternating knee lift, foot contact, rearward push and recovery; arms swing opposite the legs."),
    "attack": ("攻击", "A single controlled attack motion using only equipment already visible in the reference, then recovery to the opening pose."),
    "jump": ("跳跃", "A compact jump in place: bend knees, push upward, brief airborne pose, soft landing and return to the opening pose."),
    "hurt": ("受击", "A brief readable recoil at a stationary root, followed by recovery to the original stance."),
    "death": ("倒下", "A single controlled collapse at the same ground position, ending in a still fallen pose."),
}


class Target(BaseModel):
    fps: int = Field(12, ge=1, le=30)
    frame_width: int = Field(512, ge=32, le=2048)
    frame_height: int = Field(512, ge=32, le=2048)
    anchor: Literal["bottom_center", "feet_center"] = "feet_center"


class Camera(BaseModel):
    mode: Literal["static"] = "static"


class AnimationTask(BaseModel):
    character_id: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
    # Legacy tasks may still provide explicit presets. New tasks choose neither.
    action: str | None = None
    facing: str | None = None
    reference_id: str | None = Field(None, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
    prompt_mode: Literal["assisted", "direct"] = "assisted"
    video_prompt: str = Field("", max_length=12000)
    duration: int = Field(4, ge=4, le=10)
    seed: int = Field(0, ge=0, le=2147483647)
    use_character_description: bool = False
    additional_reference_facings: list[str] = Field(default_factory=list, max_length=MAX_CHARACTER_REFERENCES - 1)
    additional_reference_ids: list[str] = Field(default_factory=list, max_length=MAX_CHARACTER_REFERENCES - 1)
    additional_reference_upload_ids: list[str] = Field(default_factory=list, max_length=MAX_CHARACTER_REFERENCES - 1)
    supplementary_description: str = Field("", max_length=3000)
    motion_id: str | None = Field(None, max_length=80)
    root_motion: Literal[False] = False
    camera: Camera = Field(default_factory=Camera)
    loop: Literal[True] = True
    target: Target = Field(default_factory=Target)
    postprocessing: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(None, max_length=200)
    preview: bool = False

    @field_validator("action")
    @classmethod
    def valid_action(cls, value):
        if value is None:
            return value
        if value not in ACTIONS:
            raise ValueError("Unsupported action")
        return value

    @field_validator("facing")
    @classmethod
    def valid_facing(cls, value):
        if value is None:
            return value
        value = value.upper()
        if value not in FACINGS:
            raise ValueError("Unsupported facing")
        return value

    @field_validator("additional_reference_facings")
    @classmethod
    def valid_additional_facings(cls, value):
        return [f.upper() for f in value]

    @model_validator(mode="after")
    def valid_reference_selection(self):
        prompt_mode({"prompt_mode": self.prompt_mode, "video_prompt": self.video_prompt})
        if self.reference_id and self.facing and self.reference_id != self.facing:
            raise ValueError("主参考图片 ID 与旧朝向字段冲突，请只提交 reference_id")
        additional_reference_selection({"facing": self.facing,
            "reference_id": self.reference_id,
            "additional_reference_ids": self.additional_reference_ids,
            "additional_reference_facings": self.additional_reference_facings,
            "additional_reference_upload_ids": self.additional_reference_upload_ids})
        return self

    @field_validator("postprocessing")
    @classmethod
    def alpha_required(cls, value):
        if value.get("remove_background") is False:
            raise ValueError("Generated MP4 has no alpha; remove_background must remain enabled for transparent sprite export")
        return value


class CharacterLabelInput(BaseModel):
    label: str = Field("", max_length=100)

    @field_validator("label")
    @classmethod
    def clean_label(cls, value):
        if "\x00" in value:
            raise ValueError("图片标签包含无效字符")
        return value.strip()


class CharacterImageInput(CharacterLabelInput):
    path: str = Field(min_length=1, max_length=1000)


class CharacterInput(BaseModel):
    id: str | None = Field(None, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
    name: str = Field(min_length=1, max_length=100)
    views: dict[str, str] = Field(default_factory=dict)
    images: list[CharacterImageInput] = Field(default_factory=list, max_length=200)
    profile: dict[str, Any] = Field(default_factory=dict)


class MotionInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    upload_id: str | None = None
    video_path: str | None = None
    description: str = Field("", max_length=3000)


class Approval(BaseModel):
    profile: dict[str, Any]


class CharacterReferenceInput(CharacterImageInput):
    # Old clients can still explicitly replace a legacy facing entry. The new
    # UI submits only a label, so equal labels always create independent images.
    facing: str | None = None

    @field_validator("facing")
    @classmethod
    def validate_facing(cls, value):
        if value is None:
            return None
        value = value.upper()
        if value not in FACINGS:
            raise ValueError("Unsupported facing")
        return value


class NotificationInput(BaseModel):
    title: str = Field(max_length=200)
    message: str = Field(max_length=5000)
    level: Literal["info", "success", "error"] = "info"
    job_id: str | None = None

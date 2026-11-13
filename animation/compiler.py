"""Compile animation prompts without an LLM in direct mode."""
import re
from pathlib import Path

from .files import prose, sha256
from .models import ACTIONS, additional_reference_selection, prompt_mode

VERSION = "visual-motion-instructions-3.0.0"
DIRECT_VERSION = "direct-video-1.1.0"
DIRECT_SYSTEM_PROMPT = """Animation workbench requirements for transparent sprite extraction:
Generate one continuous shot of the single complete character in <Picture 1> performing the user's requested action.
Keep the character's identity, rendering style, clothing, colors, proportions and existing equipment unchanged.
Keep the exact viewing angle of <Picture 1>; the camera remains fixed with no pan, zoom, tracking, rotation, cuts or scene changes.
Show one complete character, including hair, hands, weapons and feet, with generous empty margins throughout the entire movement. Keep the root in place and a stable screen scale.
Replace all reference-image scenery with a perfectly uniform, flat neutral-gray background, RGB 128, 128, 128 (#808080), across every frame. No background texture, gradient, floor, horizon, cast shadows, lighting changes, scenery, text or extra subjects.
The gray background must be visually distinct from the foreground, including the character and any explicitly requested effects, so it can be removed after video generation. Do not paint gray over the character, weapon or effects.
Only the user's animation request determines the action. These fixed background, framing and identity requirements remain mandatory when the request describes a scene or camera change.
The output is a normal video; the workbench removes its background and exports transparent PNG frames afterwards."""
HEADINGS = ["subject_definitions", "summary", "retention_analysis", "detailed_description", "overall_soundscape", "non_diegetic_music"]


def validate_prompt(prompt):
    actual = re.findall(r"(?m)^([a-z_]+):\s*$", prompt)
    if actual != HEADINGS:
        raise ValueError("Compiled prompt must contain exactly the six official sections")
    for text in ("<Picture 1>", "<Subject 1>", "[Shot 1]", "RGB 128, 128, 128", "camera remains fixed", "one complete character", "exact viewing angle"):
        if text not in prompt:
            raise ValueError("Missing deterministic constraint: " + text)


def compile_prompt(task, profile, reference, motion_description="", profile_revision=1, motion_reference=None,
                   additional_references=None, translation=None, reference_metadata=None):
    mode = prompt_mode(task)
    reference = Path(reference).resolve()
    if not reference.is_file():
        raise ValueError("Selected reference image is missing")
    extra_facings, upload_ids = additional_reference_selection(task)
    additional_references = additional_references or []
    if len(additional_references) != len(extra_facings) + len(upload_ids):
        raise ValueError("Additional reference snapshot does not match the task selection")
    for index, extra in enumerate(additional_references):
        source = "character_view" if index < len(extra_facings) else "upload"
        expected = extra_facings[index] if source == "character_view" else upload_ids[index - len(extra_facings)]
        selected = (extra.get("reference_id") or extra.get("facing")) if source == "character_view" else extra.get("upload_id")
        if extra.get("source") != source or selected != expected:
            raise ValueError("Additional reference order or source does not match the task")
        path = Path(extra["path"]).resolve()
        if not path.is_file() or sha256(path) != extra["sha256"]:
            raise ValueError("Additional reference changed during compilation")
    if mode == "direct":
        # H3's video API exposes one prompt string, not chat role messages.
        # Preserve user text separately and wrap it in deterministic production
        # requirements; this does not invoke an LLM or add action presets.
        system_prompt = DIRECT_SYSTEM_PROMPT
        if additional_references:
            system_prompt += "\nAdditional pictures clarify the same character's appearance only; <Picture 1> alone determines the viewing angle."
        if motion_reference:
            system_prompt += "\n<Video 1> supplies motion timing and limb mechanics only; do not copy its identity, scenery, camera or audio."
        user_prompt = task["video_prompt"]
        prompt = system_prompt + "\n\nUser animation request (verbatim):\n<animation_request>\n" + user_prompt + "\n</animation_request>"
        request, refs = reference_request(task, prompt, reference, additional_references, motion_reference, reference_metadata)
        return {"prompt": prompt, "h3_request": request, "compiler_version": DIRECT_VERSION,
                "system_prompt": system_prompt, "user_prompt": user_prompt,
                "prompt_mode": "direct", "use_character_description": False,
                "profile_revision": profile_revision, "preview": task["preview"], "refs": refs,
                "motion_analysis": None, "supplementary_description": "",
                "llm_translation": None,
                "warnings": ["直接模式不调用 LLM；动作描述原文保留，工作台自动附加纯灰背景、固定镜头与完整主体等透明动画要求。"]}
    use_description = task.get("use_character_description", False)
    description = prose(profile.get("description", "")) if use_description else ""
    if use_description and not description:
        raise ValueError("Character profile needs a description and human confirmation")
    identity_text = f" Identity description: {description}" if use_description else ""
    visibility_priority = (" The visible surfaces and occlusion in <Picture 1> take priority over the identity description. "
        "Identity details describe the character, not a requirement to display every feature. Features hidden by this viewpoint remain hidden.") if use_description else ""
    visibility_retention = (" Preserve which identity features are visible and which are occluded; do not reveal hidden facial or costume details.") if use_description else ""
    visibility_action = (" If the eyes, face or chest details are hidden in <Picture 1>, keep them hidden throughout the action. "
        "If they are visible, preserve their visibility from the same viewpoint. Never turn the head or torso, change the camera, "
        "or invent visible features to display details mentioned only in the identity description.") if use_description else ""
    selected_action = task.get("action")
    action = (ACTIONS[selected_action][1] if selected_action else
              "Follow only the movement described below or demonstrated by the supplied motion reference.")
    motion = prose(motion_description)
    supplementary = prose(task.get("supplementary_description", ""))
    translated_action = ""
    if translation is not None:
        if not isinstance(translation, dict) or not isinstance(translation.get("description"), str):
            raise ValueError("LLM translation must include a description string")
        translated_action = prose(translation["description"])
        if not translated_action:
            raise ValueError("LLM translation is empty")
        # The translation replaces raw user prose in the H3 prompt. It cannot
        # replace the compiler's fixed framing, identity and gray-background rules.
        action = "Image-grounded animation instructions: " + translated_action
    supplementary_text = ("Additional requested animation detail and visual effects: " + supplementary +
        f" Apply these details only within the {'selected action' if selected_action else 'requested animation'} and the fixed identity, viewing angle, stationary root and framing. "
        "Effects may animate around the character, but must not replace or conceal its identity, change its costume, rotate its body toward the camera, move it across the canvas or alter the neutral-gray background. "
        "Keep the full character and effects inside the canvas margins. Any conflicting supplementary request is subordinate to these fixed constraints.") if supplementary and translation is None else ""
    motion_text = ("Motion timing and limb mechanics only: " + motion +
                   " The motion description does not define identity, clothing, camera position or facing.") if motion and translation is None else ""
    ending = ("Hold the final fallen pose." if selected_action == "death" else
              "Keep a regular rhythm until the ending; approach the opening phase for a repeatable game-animation loop.")
    if translation is not None:
        ending = "Preserve the requested action's ending and timing; use a repeatable cycle only when the translated animation intent calls for one."
    sections = [
        f"<Subject 1> is the single game character in <Picture 1>.{identity_text}\n"
        f"<Picture 1> is the exact viewing-angle and body-orientation reference for [Shot 1]. It defines which surfaces of the character are visible. The image alone supplies orientation; preserve its apparent proportions.{visibility_priority}",
        f"[reference generation] A {task['duration']}-second single-shot game animation shows <Subject 1> performing the requested {selected_action + ' action' if selected_action else 'animation'} in place. Keep the exact viewing angle of <Picture 1>, a stationary root, stable screen scale, fixed framing and a uniform neutral-gray background.",
        "<Subject 1> (appears in [Shot 1]): fully_preserved - preserve the visible character features, costume, colors, silhouette, equipment actually present and compact proportions. Only pose and limb motion change.\n"
        f"<Picture 1> ([Shot 1] orientation reference): fully_preserved - retain the same camera elevation, azimuth, visible surfaces, head angle and torso orientation throughout.{visibility_retention}",
        "The target video preserves the rendering style, shaded game-sprite appearance, crisp edges and restrained palette of <Picture 1>. Lighting stays even against a uniform neutral-gray background, RGB 128, 128, 128.\n"
        f"[Shot 1] Show one complete character at the center of a square canvas. Keep clear empty margins around the hair, hands, clothing and feet. The ground-projected root position stays fixed. Preserve the exact viewing angle and the pictured relationship between head, shoulders, torso, hips and boots. The same surfaces remain visible throughout; the character does not rotate toward the camera.{visibility_action}\n"
        + action + " " + motion_text + "\n" + supplementary_text + "\n"
        "Animate naturally foreshortened limbs from the reference viewpoint. Preserve the nearer and farther limb relationships. Keep restrained secondary cloth and hair movement, without changing their shapes, lengths, materials or colors. Begin the requested action in the opening frame without an introductory idle. Keep the whole character visible at all times.\n"
        "The camera remains fixed: no translation, tracking, panning, zooming, cuts or rotation. There is no movement across the canvas, no scene change and no scale change. No additional characters, invented weapons, unrequested props, text, shadows on the background or scenery appear. Add visual effects or temporary effect elements only when explicitly requested in the animation intent. Keep the costume and identity unchanged. " + ending,
        "Quiet incidental foot contacts and faint clothing movement follow the action. The character does not speak. Audio will be discarded during sprite extraction.",
        "N/A",
    ]
    if additional_references:
        for index in range(2, len(additional_references) + 2):
            sections[0] += f"\n<Picture {index}> is an additional appearance reference for the same <Subject 1>, not a viewing-angle, orientation or pose reference."
            sections[2] += f"\n<Picture {index}> (appearance details only): weak_reference - borrow only consistent appearance details visible from <Picture 1>; never reveal surfaces hidden in <Picture 1>."
        sections[3] += "\n<Picture 1> remains the sole viewing-angle and body-orientation reference. Additional pictures may clarify consistent costume, colors and materials but must not rotate the character or change which surfaces are visible."
    if motion_reference:
        sections[0] += "\n<Video 1> is the motion reference. It supplies only limb mechanics and action timing, never the character's identity, face, hair, costume, camera angle, body facing or background."
        sections[1] += " <Subject 1> uses the action rhythm of <Video 1> while retaining the exact identity and viewing angle of <Picture 1>."
        sections[2] += "\n<Video 1> (motion timing and limb-mechanics reference): weak_reference - borrow only the action phases, rhythm and coordinated limb movement. Discard the source actor's identity, clothing, body facing, camera movement, framing, environment and audio."
        sections[3] += "\nUse <Video 1> only for motion timing, limb coordination and action phases, adapted to the fixed body orientation shown in <Picture 1>. Identity, visible body surfaces and camera orientation remain fully defined by <Picture 1>."
        sections[4] += " Do not reuse or copy the audio from <Video 1>."
    prompt = "\n\n".join(f"{heading}:\n{value}" for heading, value in zip(HEADINGS, sections)) + "\n"
    validate_prompt(prompt)
    request, refs = reference_request(task, prompt, reference, additional_references, motion_reference, reference_metadata)
    warnings = ["Draft character preview; human approval is still required for production"] if task["preview"] else []
    if selected_action == "death":
        warnings.append("Death animation ends in a held fallen pose; it is not a seamless repeating loop.")
    if supplementary:
        warnings.append("Supplementary effects require visual review. Glow, smoke and semitransparent effects may need background-keying and alpha adjustments during postprocessing.")
    return {"prompt": prompt, "h3_request": request, "compiler_version": VERSION,
            "prompt_mode": "assisted", "use_character_description": use_description,
            "profile_revision": profile_revision, "preview": task["preview"],
            "refs": refs,
            "motion_analysis": motion_reference.get("analysis") if motion_reference else None,
            "supplementary_description": supplementary,
            "llm_translation": translation,
            "warnings": warnings}


def reference_request(task, prompt, reference, additional_references, motion_reference, reference_metadata=None):
    """Preserve the verified reference order and request parameters in both modes."""
    request = {"prompt": prompt, "seconds": task["duration"],
        "task": "ref2va", "conditions": [{"type": "image", "uri": reference.as_uri(), "role": "reference"}],
        "target": {"short_edge": 768, "aspect_ratio": "1:1", "duration_seconds": float(task["duration"])},
        "num_outputs_per_prompt": 1, "num_inference_steps": 50, "flow_shift": 12.0,
        "audio_flow_shift": 3.0, "seed": task["seed"]}
    refs = [{"path": str(reference), "sha256": sha256(reference), "facing": task.get("facing")}]
    refs[0].update({key: value for key, value in (reference_metadata or {}).items() if key in {"reference_id", "label"}})
    if task.get("reference_id"):
        refs[0]["reference_id"] = task["reference_id"]
    for index, extra in enumerate(additional_references, 2):
        path = Path(extra["path"]).resolve()
        request["conditions"].append({"type": "image", "uri": path.as_uri(), "role": "reference"})
        refs.append({"path": str(path), "sha256": extra["sha256"], "source": extra["source"],
                     "role": "appearance", "picture_index": index,
                     **{key: extra[key] for key in ("reference_id", "label", "facing", "upload_id") if key in extra}})
    if motion_reference:
        video_path = Path(motion_reference["video_path"]).resolve()
        if not video_path.is_file() or sha256(video_path) != motion_reference["sha256"]:
            raise ValueError("Motion video changed during compilation")
        request["conditions"].append({"type": "video", "uri": video_path.as_uri(), "role": "reference"})
        refs.append({"path": motion_reference["video_path"], "sha256": motion_reference["sha256"], "role": "motion_semantics"})
    return request, refs

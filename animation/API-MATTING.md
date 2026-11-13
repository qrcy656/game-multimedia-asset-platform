# API 背景去除（必需）

在主页「AI 模型配置」中，为动画模块的 `image` 用途绑定已通过检测的图片 API，即可在视频生成后逐帧调用抠图。未绑定时就绪检查不通过，不提交视频生成；透明背景处理始终使用图像模型。解绑会阻止新任务；已开始的任务会冻结当时的选择。

支持 OpenAI 兼容图片编辑和 Fantasy REST 图片服务。图片类型检测只证明图片能力，所选服务还必须支持参考图编辑、透明背景及原图尺寸；普通纯文生图服务不适合这个用途。API 可能逐帧计费，实际请求数量等于抽帧选择后保留的帧数，最多 120 帧。可先在批次任务 JSON 设置 `postprocessing.max_frames: 4` 做少帧预览，验证轮廓与边缘。

- OpenAI：`POST /images/edits`，multipart `image` 上传原帧，`prompt` 要求仅去除背景、保持人物和像素位置，`size` 为原帧尺寸，`n=1`、`background=transparent`、`output_format=png`。服务须支持这些编辑参数；不会自动缩放不支持的尺寸。
- Fantasy REST：`POST /generate`，`kind=image`，`input` 包含 `operation=remove_background`、`prompt`、`width`、`height`、`background=transparent`、`output_format=png` 和 `image={mime_type,data_base64}`。模型字段来自已绑定配置。可按通用契约同步返回一张 `artifacts` 图片，也可返回任务 ID 后使用 `GET /jobs/{id}` 查询。
- 输出必须只有一张原尺寸的 RGBA PNG/WebP，同时含透明背景与不透明前景。只采用 API 返回的 alpha，颜色始终使用原视频帧的 RGB，合成为 PNG；不接受全不透明、全透明、尺寸改变或丢失 alpha 的结果。轮廓与半透明边缘仍需人工查看。

首次处理时冻结配置 ID 和端点、模型、参数指纹；API Key 不写入任务或成果，可以单独轮换。每帧在提交前记录状态，完成帧保存在该任务的 `matting/` 缓存。任务恢复会复用已完成帧，或查询原远程任务 ID，不重新生成视频或重复提交帧。端点和参数变更、提交后断网、缺失回执或不可追踪的远程响应会进入「需要人工检查」，禁止自动重发；请先在供应商处核对费用和结果。恢复原配置不会自动清除需要人工检查的状态。

本模块不启动本地模型，ComfyUI 工作流可在图片生成模块使用。

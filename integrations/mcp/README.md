# Fantasy Workflow MCP

将 Fantasy Main 和 Fantasy Community 的现有 HTTP 接口封装为本地 stdio MCP。私人版经 GitLab 统一登录；社区版默认使用 GitLab 个人访问令牌（PAT），也兼容明确保留的本地账号；不需要读取网关签名密钥或关闭 TLS 验证。

0.3.0 支持私人版与社区版，两者均提供 25 个工具并分别使用独立配置和账号。社区版的本机连接、模型 ID、特效制作和可选远程配置说明见 [README.community.md](README.community.md)。

## 安装与运行

需要 Python 3.10 或更新版本。依赖固定使用官方 MCP Python SDK 2.3.0，安装在独立环境。

```console
python -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m fantasy_mcp --config config.local.json
```

Windows 将 `.venv/bin/python` 替换为 `.venv\Scripts\python.exe`。也可用 `uv venv` 和 `uv pip install -e .`。

社区版请使用 `config.community.example.json`，复制为 `config.community.local.json`，并将运行参数改为 `--config config.community.local.json`。首次安装完成后，按 [社区版本机连接步骤](README.community.md#本机平台默认)进行检查和启动；其中分别列出了 Windows 和 Linux 的完整命令。

私人版复制 `config.example.json` 为本机的 `config.local.json`，配置 HTTPS 主页、账号文件、允许上传的目录与产物目录。私人版的默认端口为：主页18443、GitLab18444、动画18445、3D18449、图片18450、语音18451、音乐18452、特效18454。不同端口/域名可用 `module_urls` 覆盖。社区版使用前述社区模板，特效默认经主页同源的 `/effects` 访问。

账号文件可使用 JSON `{"username":"...","password":"..."}`，或现有的 `账户: ...` / `密码: ...` 文本格式；也支持 `FANTASY_USERNAME`、`FANTASY_PASSWORD` 环境变量。不要把真实账号文件打包或提交到代码库。上传只接受配置根目录内的图片、视频、音频与训练ZIP，凭据文件不会作为素材上传。

系统信任库用于验证 HTTPS；可设置 `ca_bundle` 追加内部CA证书。适配器不提供跳过证书校验选项。会话保存在进程内存中，明确失效时重新登录；不会自动修改密码、批准OAuth新增权限或批准角色档案。

只读连通性检查：

```console
fantasy-mcp --config config.local.json --check
```

正式 stdio 模式只在 stdout 输出 MCP 协议。日志写 stderr，不记录密码或会话值。

## 在 Codex 注册

将下列路径替换为实际绝对路径：

```console
codex mcp add fantasy-workflow -- /absolute/path/.venv/bin/python -m fantasy_mcp --config /absolute/path/config.local.json
```

建议在对应 `[mcp_servers.fantasy-workflow]` 中设置 `tool_timeout_sec = 360`。媒体提示词辅助可能需要较长时间；真正的生成任务提交后立即返回任务ID，后续调用查询进度。新的MCP配置通常在新聊天或客户端重新加载连接后生效。

官方参考：[Codex MCP配置](https://learn.chatgpt.com/docs/extend/mcp)、[MCP Python SDK](https://py.sdk.modelcontextprotocol.io/run/)。

## 工具

| 工具 | 用途 |
| --- | --- |
| `list_capabilities` | 各模块模型、参数schema及健康状态 |
| `list_animation_references` | 角色、参考图片和动作视频目录 |
| `list_jobs` / `get_job_status` / `list_assets` | 当前账号可见任务、状态与资产 |
| `upload_animation_reference` | 上传角色图或动作视频 |
| `create_character` / `create_motion` | 从上传结果建立档案；角色保持草稿 |
| `preview_animation_prompt` | 参数/提示词预览；assisted模式会调用模型 |
| `submit_model_job` | 单图建模；私人版默认TRELLIS.2，社区版使用已配置模型ID |
| `submit_media_job` | 按已发现的模型和参数提交3D/图片/语音/音乐任务 |
| `submit_animation_job` | 提交动画并返回幂等键 |
| `submit_animation_batch` | 1–10个任务的稳定批次提交 |
| `list_downloads` / `download_artifacts` | 从任务/资产解析产物，下载并生成SHA256清单 |
| `get_effects_guide` / `list_effects` / `read_effect` | 特效格式、当前账号工程与可验证SHA256版本 |
| `validate_effect` / `save_effect` | 校验、新建或带版本冲突保护地修改特效工程 |
| `export_effect` | 便捷提交、等待并下载单帧或图集PNG |
| `start_effect_export` / `get_effect_export` / `download_effect_export` | 分步处理无总时限的后台导出，按同一任务ID查询和恢复 |
| `cancel_effect_export` | 明确取消本账号不再需要的渲染 |

资源 `fantasy://usage` 提供使用顺序，`fantasy://animation-schema` 提供所选版本的动画OpenAPI快照，包括建档与任务字段。`fantasy://effects-usage` 说明特效保存、版本校验和异步导出；详细格式与限制每次从 `get_effects_guide` 获取。模型参数在运行时通过 `list_capabilities` 获取。

社区版动画的 LLM 辅助模式需要 `animation/vision` 绑定通过验证的视觉模型 API。平台会同时发送任务文字和参考图，请根据 `list_capabilities` 中已有配置与就绪状态使用；纯文本 LLM 不满足该模式。`prompt_mode="direct"` 跳过提示词视觉模型，视频生成与透明背景图片处理仍使用对应的 API。

特效使用当前平台账号隔离保存，不调用生成模型。首次访问特效会绑定该连接的所有者，重新登录不会静默切换所有者。新建默认拒绝覆盖，修改需明确提供 `overwrite=true` 和 `read_effect` 返回的 `expected_revision`。长导出使用 start/get/download 三步，保存完成后释放临时服务器缓存；单次写请求失败不会自动重放。下载清单根据配置记录 `private` 或 `community` 版本。

不提供删除资产、修改账号、管理模型服务或批准角色工具。遇到模型未启用，返回状态供调用者处理，不自动启停GPU服务。

## 制作流程

1. 查询能力与参考目录，检查 `enabled`、`ready` 及健康状态。
2. 选择模型与参考素材，提交一次并保存任务ID。动画重试应重用同一幂等键；三维接口没有已确认的幂等支持，网络结果不明时先查询任务，不能盲目重发。
3. 大约每5秒查询状态。工具调用不会阻塞等待完整GPU生成。
4. 下载服务实际返回的产物；默认动画优先打包ZIP，下载不会覆盖已有文件，也不会自动解压ZIP。
5. 输出位于配置的 `output_dir`，每次下载包含 `fantasy-download-manifest.json` 与文件SHA256。下载响应若跳转到登录页会停止，避免把网页保存为资产。

三维模块输出GLB；动画模块输出透明帧和精灵图。导入当前Godot工程时，GLB先经Blender检查单位、朝向、原点与材质；人物帧动画需保存方向、FPS、循环、脚底锚点与技能时点。适配器只下载到暂存目录，项目内的资源接入仍由具体制作任务执行。

## 开发与验证

```console
python -m pip install -e '.[test]'
python -m pytest
```

离线测试使用MockTransport与假后端，覆盖认证、CSRF、提交结果不明时不重发、目录边界、下载大小/散列/覆盖保护及工具参数。实际只读协议检查可使用 `scripts/smoke_mcp.py --config config.local.json`。

服务器源码目录中的 `integrations/mcp` 是相同适配器的可安装副本，不更改或重启原有服务。服务器运行时应自行配置适合该机器的账号路径、HTTPS地址和上传/下载目录。

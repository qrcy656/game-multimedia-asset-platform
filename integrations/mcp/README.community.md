# Fantasy Community MCP

社区版默认使用 GitLab 统一账号与同源模块接口。适配器提供能力发现、模型/图片/语音/音乐任务、角色和动作档案、帧动画任务、批次，以及特效工程的读取、校验、保存和 PNG 导出。MCP 使用该 GitLab 用户的个人访问令牌（PAT）登录平台，继承同一账号 ID、作品与权限。

## 配置和启动

复制 `config.community.example.json`，设置社区地址、账号文件、允许上传目录及产物目录。

### GitLab 托管账号（默认）

1. 使用 AI 的专属 GitLab 账号，在 GitLab 用户设置中创建个人访问令牌，只勾选 **`read_user`** 并设置到期日。
2. 复制 `community.credentials.example.json` 为自己的 `community.credentials.json`，填入用户名与 PAT：

```json
{"credential_type":"gitlab_pat","username":"你的GitLab用户名","token":"你的PAT"}
```

`username` 可省略，也可使用该 GitLab 身份已经明确绑定的平台账号名，例如初始管理员的 `admin`。它不能指向其他同名账号。也可在配置中设置 `credential_type: "gitlab_pat"`，通过 `FANTASY_GITLAB_TOKEN` 环境变量提供令牌。

浏览器仍走 GitLab OAuth；MCP 将 PAT 交给平台验证上游身份，再获取正常平台会话及 CSRF。GitLab 停用、角色调整、PAT 撤销或过期会在平台复验时生效，缓存最长 60 秒。PAT 不会出现在工具结果中，退出 MCP/平台不会撤销该 PAT；在 GitLab 中管理它的轮换和撤销。不要把真实凭据文件放入分享包。

### 本地账号模式

明确保留的非托管本地账号继续使用原配置。账号文件为：

```json
{"credential_type":"local","username":"你的本地账号","password":"你的密码"}
```

旧版本省略 `credential_type` 的本地账号配置继续兼容。已由 GitLab 接管的账号使用上述 PAT，保留的旧本地密码不会绕过上游认证。

### 本机平台（默认）

平台由本机 Docker 启动后，适配器直接运行在同一台电脑时，示例使用 `portal_url=http://localhost:18082`、`origin=http://localhost:18082`，无需 SSH。如果更换平台端口，同时调整这两个字段；`origin` 必须与平台 `.env` 中的 `APP_ORIGIN` 一致。

在 `integrations/mcp/` 目录按 [MCP 安装与运行](README.md#安装与运行)创建 `.venv` 并安装适配器后，复制 `config.community.example.json` 为 `config.community.local.json`，设置自己的账号文件和素材目录。

Windows PowerShell：

```powershell
.\.venv\Scripts\python.exe -m fantasy_mcp --config config.community.local.json --check
.\.venv\Scripts\python.exe -m fantasy_mcp --config config.community.local.json
```

Linux：

```bash
.venv/bin/python -m fantasy_mcp --config config.community.local.json --check
.venv/bin/python -m fantasy_mcp --config config.community.local.json
```

第一条检查登录和接口连通性，第二条启动 stdio MCP。上述命令直接使用已安装适配器的虚拟环境，无需额外激活。账号文件与输入、下载目录使用自己电脑上的路径；平台浏览器部署本身无需安装 MCP。

若将 MCP 放入另一个容器，`portal_url` 应使用该容器能访问的平台地址，`origin` 仍填写平台的 `APP_ORIGIN`，不把 MCP 容器自己的 `localhost` 当作平台。

### 远程平台（可选）

只有平台在另一台机器时才需要配置远程连接。已配置 HTTPS 入口的安装，直接填写该入口地址和对应 `origin`。需要 SSH 隧道时，可在自己的配置中增加：

```json
{
  "portal_url": "http://127.0.0.1:28082",
  "origin": "http://localhost:18082",
  "ssh_tunnel": {
    "target": "your-user@your-server",
    "local_port": 28082,
    "remote_port": 18082,
    "ssh_port": 22
  }
}
```

这是对完整配置的地址字段补充。示例假定远程平台 `APP_ORIGIN=http://localhost:18082`，转发 `127.0.0.1:28082` 到远端回环端口 `18082`。适配器使用现有 SSH 密钥与 known_hosts，启动和关闭自己创建的隧道；多个实例使用不同本地端口。普通本机部署保留默认示例即可。

## 工具使用要点

- 社区模型 ID 来自已经验证的模型 API 配置。先调用 `list_capabilities`，使用返回的配置 `id` 与字段 schema；具体模型名在平台「AI 模型配置」中设置。3D 未指定模型时仅在唯一可用模型或唯一默认模型存在时自动选择。
- 社区服务自行排队。MCP 不启停模型、GPU、Docker 或其他工作台，也不会创建模型供应商配置。
- 动画仍生成透明帧和精灵图。LLM 辅助模式（`prompt_mode="assisted"`）需要在平台中为 `animation/vision` 绑定通过验证的视觉模型 API；平台将任务文字与角色参考图一起交给它理解并整理视频提示词，纯文本 LLM 不满足这个模式。提交前通过 `list_capabilities` 查看已有配置与就绪状态。直接模式（`direct`）跳过这一步，不需要该提示词视觉模型；两种模式均需配置视频 API 和透明背景编辑图片 API。角色新建为草稿，适配器不自动批准；可按服务契约使用 `preview=true`。
- 能力发现返回的 `modules.animation.health` 是直接模式就绪状态，`assisted_health` 是辅助模式就绪状态；辅助模式缺少视觉 API 时，后者会说明缺失项。提交前查看所选模式的 `generation_ready` 与 `generation_missing_services`。模块 `available=true` 表示接口可访问，不代表两种模式均可生成。
- 动画工具支持素材引用、创建角色与动作档案、提交 AnimationTask 和下载 ZIP。社区下载路径包含 `/animation/` 或对应媒体模块前缀。
- 音乐训练在供应商明确验证 `music_train` 且当前账号有权使用该 API 时可用，普通成员也可提交自己的训练任务。
- 媒体提交在超时后不自动重复。先查询任务；动画使用并保留同一幂等键或批次 ID。

## 特效制作

特效模块默认地址为 `portal_url + /effects`，与主页使用同一社区账号。`list_capabilities` 的 `effects` 项返回可用状态和渲染限制；制作特效由 CPU 渲染，无需配置模型 API。

1. 调用 `get_effects_guide` 读取当前 Arcadia JSON 格式与限制。
2. 用 `list_effects` 查看本人及共享工程；用列表中的 `owner_key` 调用 `read_effect` 可读取对应作者的工程，新建可直接准备 JSON。
3. 调用 `validate_effect(data)`，检查 `valid` 与错误信息。
4. `save_effect(name, data)` 默认只新建；修改已有工程需传 `overwrite=true` 和最近 `read_effect` 返回的 `expected_revision`。冲突后先重新读取、合并，再保存。
5. 调用 `start_effect_export(file, format="atlas")`，保留 `job.id`；用 `get_effect_export(job_id)` 查询，完成后用 `download_effect_export(job_id, subdirectory)` 下载透明 PNG 和 SHA256 清单。单帧格式为 `frame`，`time` 单位为秒。

读取、导出或管理员更新共享原件时传入列表的 `owner_key`；只有该作者或管理员可修改原件。成员复用共享工程时，以新名称调用 `save_effect` 并省略 `owner_key`，保存为当前账号的作品。

渲染无总时长截止。长任务可跨 MCP 调用继续查询同一 `job_id`，不用重新提交。`export_effect` 是提交、等待、下载的便捷工具；客户端取消该调用时会尽力取消相应渲染。`cancel_effect_export` 用于明确取消不再需要的任务。成功下载后自动释放服务器临时缓存，后续查询该 job 可能返回 404，工程和本地结果仍保留。

社区写请求同时带门户 `X-Community-CSRF`、特效 `X-Effects-CSRF` 和账号 `X-Effects-Owner`。一条 MCP 连接首次访问特效后即绑定该账号，后续重新登录也不能静默切换特效所有者；需要换账号时显式重启连接。清单中的 `edition` 为 `community`，输出只写入配置的 `output_dir`，不会覆盖已有文件。

资源 `fantasy://effects-usage` 包含同一流程，供外部智能体读取。MCP 不提供特效删除工具。

## 验证与分发

`tests/test_community.py` 与 `tests/test_community_effects.py` 使用模拟 HTTP 验证本地登录、两层 CSRF、会话恢复、提交不重放、模型发现、特效账号绑定、并发编辑保护及受限下载。真实服务只读检查可通过 `--check`，它不会提交生成。完整提交链路的隔离测试必须使用临时数据和模拟供应商，不挂载正式 `data/`。

适配器源码位于 `integrations/mcp/`。分享时保留源码、测试、示例和文档；自己的 `*.local.json`、`*.credentials.json`、下载文件和虚拟环境留在本机。

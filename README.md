本项目由个人自用版本改造而来，个人自用版本的AI服务均采用本地部署的形式，因此该版本的AI工作流未经操作验证。如发现问题或者有修改意见欢迎向"zhenghaoran200504@gmail.com"进行反馈。

# Fantasy Community 社区创作平台

## 一、项目介绍

Fantasy Community 是面向游戏开发的自托管工作平台，将世界设定、剧情结构、代码与创作资产集中到一个网页入口，并通过AI模型 API 制作图片、语音、音乐、三维模型和角色动画。外部智能体也可通过 MCP 使用制作工具、查询任务并下载成果。

### 作者与许可

- **第一作者：[qrcy656](https://github.com/qrcy656)**，项目发起者与仓库所有者。
- **第二作者：ChatGPT**，参与 AI 辅助开发、测试与文档编写。

本项目原创代码采用 [MIT License](LICENSE)。集成项目及第三方组件保留各自的许可证和版权声明。

### 集成项目

| 开源项目 | 在平台中的用途 | 原仓库 |
| --- | --- | --- |
| Arcadia Effects | 特效编辑器、粒子与时间线编辑、特效渲染和导出 | [xaidan777/Arcadia-Effects](https://github.com/xaidan777/Arcadia-Effects) |
| GitLab Community Edition | 代码仓库、版本协作及平台统一账号管理 | [gitlab-org/gitlab](https://gitlab.com/gitlab-org/gitlab) |
| ComfyUI | 图片模块通过 API 接入节点工作流，使用已部署的 ComfyUI 服务生成图片 | [Comfy-Org/ComfyUI](https://github.com/Comfy-Org/ComfyUI) |

平台环境由 Docker Compose 管理，包含应用、动画 worker、特效渲染器、PostgreSQL、Redis 和随附 GitLab。模型服务通过可配置的 API 接入。

### 默认端口与修改位置

下表为宿主机占用的端口，默认绑定本机回环地址。

| 端口 | 用途 | 源码目录 `.env` 中的配置 |
| --- | --- | --- |
| `18082` | 平台网页及各工作模块 | 同时修改 `PORT` 和 `APP_ORIGIN` |
| `18083` | GitLab 网页与登录 | 同时修改 `GITLAB_HTTP_PORT` 和 `COMMUNITY_GITLAB_URL` |
| `12223` | GitLab SSH 克隆与推送 | 修改 `GITLAB_SSH_PORT` |

`.env` 由首次启动生成，字段参考见 [`.env.example`](.env.example)，端口映射定义在 [`compose.yaml`](compose.yaml)。修改后在源码目录运行 `docker compose up -d`，等待服务就绪；随附 GitLab 会更新登录回调配置。使用已有 GitLab 时，同步修改其 OAuth 回调地址。更换平台地址后，MCP 的 `portal_url` 与 `origin` 也需同步更新。

## 二、部署说明

### Windows / Linux 本机部署

克隆仓库并进入包含 `compose.yaml` 的仓库根目录；也可下载源码 ZIP，解压后在该目录打开终端。

```console
git clone https://github.com/qrcy656/game-multimedia-asset-platform.git
cd game-multimedia-asset-platform
```

**Windows：**安装并打开 Docker Desktop，使用 Linux 容器模式，在本 README 所在目录打开 PowerShell：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start.ps1
```

**Linux：**安装 Docker Engine 和 Docker Compose 插件，确保当前用户能运行 Docker，然后执行：

```bash
bash scripts/start.sh
```

启动脚本会初始化配置、构建环境并启动服务。首次 GitLab 初始化需要几分钟，使用 `docker compose ps` 查看状态。就绪后打开 **[http://localhost:18082](http://localhost:18082)**；后续在源码目录执行 `docker compose up -d` 即可启动。

平台内容保存在 `data/`，安装配置保存在 `.env`，GitLab 内容保存在 Docker 命名卷。备份、迁移及停止步骤见[操作手册](./操作手册.md)。

### 初始管理员与账号管理

默认选择“使用 GitLab 统一登录”：GitLab 初始管理员用户名为 **`root`**，随机初始密码位于源码目录 `.env` 的 **`COMMUNITY_GITLAB_ROOT_PASSWORD`**。首次登录后可在 GitLab 修改密码。该身份自动关联平台的初始 `admin`，使用同一账号归属管理作品。

平台采用 **GitLab 账号系统**。进入“代码仓库”模块，在 GitLab 的管理区域创建用户、设置管理员、停用账号和管理团队；用户在 GitLab 管理自己的资料、密码与个人访问令牌。平台同步用户身份及角色，仓库成员、分支和可见性由 GitLab 管理。

### 外部智能体操作平台

适配器位于 [`integrations/mcp/`](integrations/mcp/)，提供 **stdio MCP**，由 Codex、Claude Code 等客户端启动。它使用专属账号上传参考素材、创建动画档案、提交媒体与动画任务、编辑特效工程、查询进度和下载成果。

1. 在 GitLab 为智能体创建专属账号，并用该账号生成具有 **`read_user`** 权限的个人访问令牌（PAT）。平台按这个账号的角色和作品权限执行操作。
2. 按 [MCP 安装说明](./integrations/mcp/README.md#安装与运行)在客户端电脑准备 Python 3.10+ 独立环境并安装适配器。
3. 复制 `config.community.example.json` 为 `config.community.local.json`，填写平台地址、凭据文件、允许上传目录 `upload_roots` 和下载目录 `output_dir`。本机默认 `portal_url`、`origin` 均为 `http://localhost:18082`。
4. 根据 `community.credentials.example.json` 创建自己的凭据文件，填入 `credential_type: "gitlab_pat"`、GitLab 用户名及 `token`。先按 [MCP 社区版手册](./integrations/mcp/README.community.md)运行 `--check` 验证连接，再注册到客户端。

下文的 `MCP_PYTHON` 指已安装适配器的 Python **绝对路径**：Windows 通常为 `.venv\Scripts\python.exe`，Linux 为 `.venv/bin/python`；`MCP_CONFIG` 指 `config.community.local.json` 的绝对路径。将示例中的这两个占位值替换为自己的路径，Windows 的 JSON 路径可使用 `/`。路径以客户端实际运行的环境为准；适配器在容器中运行时，`portal_url` 使用容器可达的平台地址，`origin` 保持为平台的 `APP_ORIGIN`。

**命令行客户端：**安装对应客户端后，选择自己使用的工具执行一条注册命令。

```console
codex mcp add fantasy-community -- "MCP_PYTHON" -m fantasy_mcp --config "MCP_CONFIG"
claude mcp add --transport stdio --scope user fantasy-community -- "MCP_PYTHON" -m fantasy_mcp --config "MCP_CONFIG"
gemini mcp add --scope user --transport stdio fantasy-community "MCP_PYTHON" -- -m fantasy_mcp --config "MCP_CONFIG"
opencode mcp add fantasy-community --global -- "MCP_PYTHON" -m fantasy_mcp --config "MCP_CONFIG"
copilot mcp add fantasy-community -- "MCP_PYTHON" -m fantasy_mcp --config "MCP_CONFIG"
qoder mcp add -s user -t stdio fantasy-community -- "MCP_PYTHON" -m fantasy_mcp --config "MCP_CONFIG"
```

分别对应 [Codex](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)、[Claude Code](https://code.claude.com/docs/en/mcp)、[Gemini CLI](https://geminicli.com/docs/tools/mcp-server/)、[OpenCode](https://opencode.ai/docs/mcp-servers/)、[GitHub Copilot CLI](https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-mcp-servers) 和 [Qoder CLI](https://docs.qoder.com/cli/mcp-servers)。客户端会保存启动配置，并在连接时运行适配器。

**图形客户端与编辑器：**通过下列入口配置同一条启动命令，或合并 JSON 中的 `fantasy-community` 项到已有配置。

```json
{
  "mcpServers": {
    "fantasy-community": {
      "command": "MCP_PYTHON",
      "args": ["-m", "fantasy_mcp", "--config", "MCP_CONFIG"]
    }
  }
}
```

| 客户端 | 配置方式 |
| --- | --- |
| [Cursor](https://cursor.com/docs/mcp) | 写入项目 `.cursor/mcp.json` 或用户 `~/.cursor/mcp.json`；在上面的服务对象中补充 `"type": "stdio"`，通过 Customize 管理连接。 |
| [VS Code / GitHub Copilot](https://code.visualstudio.com/docs/agents/reference/mcp-configuration) | 打开 Copilot 会话，运行 **Chat: Open Customizations → MCP Servers → Add Server → Command (stdio)**，填写上述程序与参数。当前 Copilot 配置使用 `mcpServers`，项目文件为 `.mcp.json`；个人文件默认 `~/.copilot/mcp-config.json`。若使用旧版 `.vscode/mcp.json`，顶层改为 `servers` 并加 `"type": "stdio"`。 |
| [Cline](https://docs.cline.bot/mcp/mcp-overview) | 在 Cline 面板进入 **MCP Servers → Configure → Configure MCP Servers**，将上面的服务项写入打开的 `cline_mcp_settings.json`。 |
| [Windsurf / Devin](https://docs.devin.ai/desktop/devin-local/customizations.md) | 旧 Windsurf 使用 `~/.codeium/windsurf/mcp_config.json`；当前 Devin 在 **Customize → MCPs → This computer** 中打开配置文件，使用上面的 `mcpServers` 格式。[当前配置位置](https://docs.devin.ai/cli/extensibility/mcp/configuration.md) |
| [ZCode](https://zcode.z.ai/cn/docs/mcp-services) | **设置 → MCP 服务器 → 新建 MCP 服务器**，选择用户或工作区作用域、类型 `stdio`，填写上述程序与参数；也可切到“完整配置”模式粘贴通用 JSON。 |
| [WorkBuddy](https://www.workbuddy.cn/docs/workbuddy/From-Beginner-to-Expert-Guide/Function-Description/Connector) | **专家·技能·连接器 → 连接器 → 自定义连接器**，添加通用 JSON，并在服务对象中补充 `"type": "stdio"`。[MCP 配置规格](https://open.workbuddy.cn/en/docs/connector) |
| [TRAE](https://docs.trae.cn/ide_add-mcp-servers) | **设置 → MCP → 本地 → 添加 → 手动添加**，粘贴通用 JSON；项目级配置可在启用后写入 `.trae/mcp.json`。按其字段要求，Python 可执行文件路径使用不含空格的位置。 |
| [Qoder IDE](https://docs.qoder.com/user-guide/chat/model-context-protocol) | **Settings → MCP → My Servers → + Add**，在打开的 JSON 中加入通用配置，保存后进入 Agent 模式使用。CLI 注册命令见上方。 |
| [Google Antigravity](https://antigravity.google/docs/mcp) | 代理侧栏 **… → MCP Servers → Manage MCP Servers → View raw config**，加入通用 JSON。当前全局文件为 `~/.gemini/config/mcp_config.json`，项目文件为 `.agents/mcp_config.json`；Antigravity CLI 使用同一配置，并可通过 `/mcp` 查看和刷新连接。 |
| [Grok Bot](https://docs.x.ai/grok-bot/computer-and-apps#connect-an-app) | **Connect apps → Add a custom connector → Type: Command**，填写 `MCP_PYTHON` 和上述参数，再点 **Add plugin**。命令在 Bot 的云电脑上执行：先在该电脑安装适配器、准备配置与凭据，并将 `portal_url` 指向云电脑可访问的平台地址；`origin` 对应平台的 `APP_ORIGIN`。 |

**DSH（DeepSeek Harness）：**使用官方 [`@deepseek-ai/dsh-mcp-client`](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/mcp/mcp-client/README.md) 插件。将下面的用户覆盖配置保存为 `fantasy-mcp.yml`，替换两个绝对路径：

```yaml
- insert:
    - id: mcp-fantasy-community
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: fantasy-community
        transport: stdio
        command: 'MCP_PYTHON'
        args: ['-m', 'fantasy_mcp', '--config', 'MCP_CONFIG']
```

使用 `dsh web --patch ./fantasy-mcp.yml` 启动，也可将该覆盖配置加入正在使用的 profile。详见[官方 overlay 教程](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/user/develop/basic/index.md)。

**Antigravity 与 Gemini CLI：**Antigravity 是包含 App、IDE 和 CLI 的产品体系。Google 已提供从 Gemini CLI 迁往 Antigravity CLI 的路线，两者使用各自的命令和配置；本文分别列出接入方式。[官方迁移说明](https://developers.googleblog.com/an-important-update-transitioning-gemini-cli-to-antigravity-cli/)
## 三、功能介绍

### 仪表盘、模型与协作

- **统一入口与主题：**仪表盘展示各模块及模型能力状态；白天、黑夜主题可作为模块默认值，每个模块也可独立选择。
- **模型 API 配置：**支持多组模型、接口地址和密钥，检测模型类型与能力。管理员配置全局 API，所有成员可用；每个用户也可配置仅本人可见、可用的个人 API。
- **独立选型：**各模块可从全局及个人配置中选择模型，选择按账号保存在当前浏览器，也可跟随管理员设定的默认值。
- **协作权限：**成员管理自己的文档和作品，查看、引用与复用他人内容；复用后的新成果归当前账号。管理员可编辑他人内容并保留原作者。
- **按功能启用：**文档、档案、剧情与特效编辑可直接使用；配置相应模型后启用问答、生成和辅助功能。制作任务提供队列、进度、历史成果与下载。

### 各工作模块

| 模块 | 主要功能 |
| --- | --- |
| **代码仓库** | 集成 [GitLab](https://gitlab.com/gitlab-org/gitlab)，管理代码、提交、分支、合并请求及团队成员，并作为平台的统一登录和账号管理入口。 |
| **设定资料室 / RAG** | 嵌套目录管理、Markdown 编辑与导入、文件移动与删除、图片素材查看；LLM 可按名称或关键词查找资料，按需读取原始文档，并在回答中标注来源文件。配置视觉模型后可理解图片；配置 Embedding 后增加向量入库与检索，配置 Reranker 后增加重排序。每组 Embedding 配置分别维护索引。 |
| **动画资产工作台** | 管理带标签的多图角色档案、主参考图、辅助图和动作参考视频；预览动作指令、批量生成动画、逐帧去背景，导出透明 PNG 帧、精灵图、预览与相关元数据。生成使用视频 API 和支持透明背景编辑的图片 API。 |
| **图片生成** | 根据文字或参考图生成、编辑图片，导入并管理带标签的图片档案；可接入 ComfyUI，提交 API 格式工作流 JSON，绑定提示词、参考图、尺寸和种子等参数，并保存生成成果。 |
| **语音制作** | 根据朗读文本、音色、情绪和表演要求生成语音，管理任务并试听、下载音频。 |
| **音乐制作** | 根据描述、风格标签及歌词制作歌曲、纯音乐、配乐、氛围和音效，试听并下载成果；对接通过训练能力检测的 API 后，可上传音频 ZIP 数据集，设置训练参数并下载训练成果。 |
| **3D 模型生成** | 将参考图片及描述提交给三维生成 API，在网页预览模型并下载 GLB 资产。 |
| **剧情设计** | 在节点画布中组织场景、角色、对白和条件分支；一段多轮对话可共用节点，支持嵌套子剧情、图片与音频素材、共享项目复用、JSON 工程导入导出，以及 TXT 剧情大纲导出。 |
| **特效制作** | 集成 [Arcadia Effects](https://github.com/xaidan777/Arcadia-Effects)，编辑粒子、图层、运动路径和时间线，保存并复用特效工程，导出透明 PNG、帧序列和图集；同时提供智能体可调用的格式说明、工程校验、保存和后台导出工具。 |

### LLM 辅助模式与原文问答

**动画的 LLM 辅助模式**会同时读取角色参考图和动作意图，把简短描述细化为适合生成模型执行的动作指令。例如输入“让图中的人物行走”，视觉 LLM 会结合人物朝向、可见肢体和持物，描述交替迈步、重心移动、手臂摆动及动作节奏，再交给视频模型。该模式使用具有识图能力的模型，并支持先预览整理后的指令。

**图片、语音、音乐的可选 LLM 辅助**会把制作描述翻译为英文提示词。图片处理画面或编辑要求；语音处理声音、情绪与表演要求并保留朗读原文；音乐处理描述和风格标签并保留歌词。

**资料室的问答支持原文与向量两条检索路径。**配置 LLM 后，即使尚未向量化，也可查找文件、搜索原文并按需读取内容，回答附带来源文件以便核查。增加 Embedding 后可使用语义向量检索，Reranker 可进一步调整检索结果顺序；这些能力按各自配置启用。

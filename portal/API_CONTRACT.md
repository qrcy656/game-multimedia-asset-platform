# 社区版模型 API 契约

模型配置只在服务器保存。API Key 使用独立 Fernet 密钥加密；浏览器只得到 `has_api_key` 和固定掩码。迁移时须同时保留模型配置数据库与 `model-key.fernet`，不要把运行数据放进源码发布包。

## 配置与检测

配置字段：`name`、`kind`、`protocol`、`base_url`、`model`、`api_key`、`options`、`scope`。`id` 由系统产生，`owner_id` 由服务器根据当前账号写入，不接受客户端指定。`scope` 为 `personal`（个人，HTTP 新建默认值）或 `global`（全局）；创建后不能修改范围。个人配置仅本人可见、管理和调用，管理员也不能访问其他人的个人配置；全局配置由管理员管理，所有登录用户可使用。升级时原有配置迁移为全局配置，保留原 ID、加密密钥和检测记录。`options` 可为 `{"timeout":180,"parameters":{"temperature":0.2}}`。编辑 API Key 时省略该字段保留原值，传空字符串清除。

类型：`llm`、`vision`、`embedding`、`rerank`、`image`、`speech`、`music`、`model3d`、`video`、`workflow`。协议：`openai`、`ollama`、`rerank`、`comfyui`、`videos`、`rest`。`vision` 通过视觉和聊天兼容检测后也可绑定 `llm`，反向不允许。图片、语音、音乐的翻译官使用各模块的 `llm` 绑定。动画 LLM 辅助与参考图理解统一使用 `animation/vision`，要求已验证的识图能力；直接提示词模式无需此绑定。旧版 `animation/llm` 绑定及对应模型资料会保留，但新动画流程使用 `vision` 配置。

任何配置修改都使旧检测结果失效，无法再调用或重新绑定，直到再次检测通过。检测验证真实协议与返回结构，不从模型名字推断类型。视觉检测发送一张微型蓝色方块图片，并检查识别结果。检测会发出少量推理请求；无法通过探测不等同于模型绝对没有该能力，但系统会拒绝启用未确认的能力。

| 协议 | 基础地址示例 | 检测与调用 |
| --- | --- | --- |
| OpenAI 兼容 | `https://api.example.com/v1` | LLM/视觉：`POST /chat/completions`；向量：`POST /embeddings` |
| Ollama | `http://localhost:11434` | `/api/show` 声明检查，加 `/api/chat` 或 `/api/embed` 真实探测 |
| Rerank | `https://api.example.com/v1` | `POST /rerank`；校验 `results[].index/relevance_score` |
| ComfyUI | `http://localhost:8188` | `/system_stats` 与 `/object_info` 检测，`POST /prompt` 提交 |
| Videos API | `https://api.example.com/v1` | `/models` 必须明确声明配置模型的 `capabilities:["video"]`，且 `/videos?limit=1` 返回任务列表对象 |
| Fantasy REST | `https://api.example.com` | 以下明确类型的通用生成协议 |

OpenAI 图片和语音 API 没有统一、无需实际生成的类型探测端点。默认使用下面的 `/capabilities` 与 `/validate` 契约；若普通兼容服务不提供它们，可在界面显式勾选“允许生成一次用于检测（可能计费）”。此时图片生成一张简单画面并验证可解码图片，语音生成短句 `Test.` 并验证音频格式，不保存检测文件。普通 API 用户也可通过此方式完成检测。图片结果 URL 下载不会携带 API Key、不跟随重定向。检测后分别调用 `/images/generations` 与 `/audio/speech`。不能只凭模型名称或 HTTP 200 判定能力。

## Fantasy REST：同步或异步生成

所有路径相对于配置的 `base_url`，API Key 非空时发送 `Authorization: Bearer <key>`。不跟随重定向，不把 API Key 返回给浏览器。服务应对这些端点使用同一鉴权。

`GET /capabilities` 返回当前所选模型的契约：

```json
{"api_version":"fantasy-v1","model":"provider-model-id","capabilities":["image"],"input_schema":{"type":"object","properties":{"prompt":{"type":"string"}}},"output_schema":{"type":"object"}}
```

`POST /validate` 是**不执行生成、不写任务**的能力检测：

```json
{"model":"provider-model-id","kind":"image","validate_only":true,"input":{"prompt":"connection test"}}
```

成功返回 `{"valid":true,"kind":"image","model":"provider-model-id"}`。服务必须检查模型确实支持这个 `kind`，不可无条件返回成功。不支持则返回 4xx。Schema 与验证响应须为 JSON 对象。

`POST /generate` 接收：

```json
{"model":"provider-model-id","kind":"image","input":{"prompt":"一座云端城堡","negative_prompt":"","width":1024,"height":1024,"seed":42}}
```

`input` 保存模块任务参数；图像、音频或3D输入使用 `{"mime_type":"image/png","data_base64":"..."}` 内联文件，不能使用本平台服务器的绝对文件路径。服务端可按模块定义扩展参数，但必须在 `input_schema` 中说明。`model` 与 `kind` 由平台选定的配置强制写入，调用者不能覆盖。

同步完成可返回：

```json
{"status":"succeeded","artifacts":[{"name":"result.png","mime_type":"image/png","data_base64":"..."}],"text":"可选的说明"}
```

异步接受返回 `{"id":"job_abc123","status":"queued"}`（兼容 `job_id`）。平台使用同一个配置轮询 `GET /jobs/job_abc123`。状态为 `queued`、`running`、`succeeded`、`failed`、`cancelled`；成功时含上述 `artifacts`，失败时含不包含凭据的 `error` 文本。轮询返回的 `id` 应与请求一致。任务 ID 仅允许字母、数字、下划线和连字符。客户端不能提供任意回调或轮询 URL。文件也可用 `url` 替代 `data_base64`，由模块执行限大小下载，跨来源 URL 不得获得配置 API Key；优先使用同源文件接口。

若 REST 声明 `llm`/`vision`/`embedding`/`rerank`，`/generate` 使用下文同用途 payload，返回对应 OpenAI/Rerank 结构，而不是媒体 artifacts。REST 视觉配置要用于文本聊天，需要声明和验证 `llm` 能力。

### 音乐训练

音乐 REST 服务可额外声明 `capabilities: ["music", "music_train"]`。检测时平台分别调用 `/validate` 验证 `music` 和 `music_train`，训练检测请求为 `{"model":"provider-model-id","kind":"music_train","validate_only":true,"input":{}}`，仅返回匹配模型与类型且 `valid:true` 才记录训练能力。此检测不得运行训练或生成。音乐生成和训练继续使用同一个音乐配置，未验证训练能力的配置不能创建训练任务。

成员可通过自己有权使用的全局或个人 API 提交训练，任务与结果归提交者。`POST /generate` 的任务仍为 `kind:"music"`，`input` 包含 `task:"train"`、`prompt`、`epochs`（1–1000）、`rank`（8/16/32/64）、`learning_rate`（0.000001–0.001）、`duration`（不超过 240 秒）及 `dataset:{mime_type:"application/zip",data_base64:"..."}`。数据集 ZIP 不超过 256 MiB，条目只能是 WAV、MP3 或 FLAC 音频；平台检查文件名和内容，不在宿主机解压用户压缩包。

训练结果沿用上述同步 `artifacts` 或异步任务查询契约，成果只接受通过内容校验的 ZIP 或 safetensors。平台保存训练任务和成果，删除任务时同时删除其本地完整资产；远端服务管理自己的训练计算资源。

## Python 内部调用

```python
from portal import create_app, authenticate
from portal.registry import Registry
from providers import model_context, parse_model_selection, fingerprint

portal_app = create_app(data_dir)
auth = portal_app.state.auth
registry = portal_app.state.registry
# 在根应用保护中间件里：
user = authenticate(request, auth=auth)  # 用户对象或 None；同时写入 request.state.user
valid = auth.validate_csrf(request)      # 支持 X-Community-CSRF / X-CSRF-Token

selections = parse_model_selection(request.headers.get("X-Fantasy-Model-Selection"))
with model_context(user, selections):  # user 必须来自服务端已验证的登录会话
    profile = registry.resolve("image", "llm")  # 仅供服务器，包含解密 api_key
    result = await registry.invoke("image", "llm", {
        "messages": [{"role":"user", "content":"Turn this creative intent into a precise production instruction."}],
        "temperature":0.2, "max_tokens":1000,
        "response_format":{"type":"json_object"}
    }, profile_id=profile["id"], expected_fingerprint=fingerprint(profile))
```

`invoke(module, role, payload, profile_id=None, *, expected_fingerprint=None)` 可明确选择当前账号有权使用、已验证且用途兼容的配置；没有显式 `profile_id` 时先使用当前请求的浏览器选择，再使用全局默认绑定。每次解析、调用及轮询都会检查配置归属和模型用途。`expected_fingerprint` 可固定端点、模型与参数，在实际调用同一个已解析配置前校验；配置发生变化时拒绝调用。`providers.invoke` 和 `providers.invoke_sync` 提供相同参数。

后台任务使用 `capture_model_context()` 保存当前账号和已合并全局默认的模型选择，通过 `restore_model_context(snapshot, owner_id=任务作者ID)` 恢复执行；恢复时核对任务作者，快照不包含 API Key。输入/输出约定：

- LLM/视觉：OpenAI `messages`，图像使用 `image_url.url=data:image/...;base64,...`；输出 `choices[0].message.content`。Ollama 自动转换图片、采样参数和 JSON 输出格式，不支持远程图片 URL。
- Embedding：`{"input":["text"]}`；输出 `data[].embedding`，Ollama 结果也转换为该结构。
- Rerank：`{"query":"...","documents":["..."],"top_n":5}`；输出 `results[].index/relevance_score`。
- OpenAI 图片：标准 `/images/generations` 参数与结果；语音：标准 `/audio/speech` 参数，输出 `{"data_base64":"...","mime_type":"audio/mpeg"}`。
- REST 生成：`{"input":{...}}`，返回上述同步/异步 JSON。
- ComfyUI：`{"prompt":{...API格式节点图...},"client_id":"可选"}`，返回原始 `/prompt` JSON（`prompt_id` 等）。`await registry.poll(module,role,prompt_id,profile_id)` 返回 `/history/{prompt_id}`；文件读取和保存由模块负责，必须限制在 ComfyUI 的文件接口。
- REST 异步：`await registry.poll(module,role,job_id,profile_id)`，返回 `/jobs/{job_id}` JSON。提交时应记录 `profile_id`，确保轮询同一配置。
- Videos API：`POST /videos` 接收 `{model,prompt,seconds,conditions:[{type:"image",uri:"data:...",role:"reference"}],...}`，返回 `{id,status}`；`poll` 查询 `/videos/{id}`，生成模块从同源 `/videos/{id}/content` 获取视频。

## 门户 HTTP 接口

- `POST /api/login {username,password}` 用于非托管本地账号；也可明确传 `credential_type:"local"`。GitLab 托管账号的非浏览器客户端（例如 MCP）使用 `POST /api/login {credential_type:"gitlab_pat",token,username?}`，其中 `token` 为该用户的 GitLab `read_user` PAT。可选 `username` 必须是上游用户名或该数字身份已明确绑定的平台用户名。两种成功响应均为 `{user,csrf}`，并设置平台会话 Cookie；浏览器默认使用下述 OAuth 跳转登录。
- `GET /api/session` 返回 `{user,csrf}`；`POST /api/logout` 撤销当前平台会话。OAuth 登录尝试撤销对应上游令牌；PAT 登录不会撤销用户自行管理的 PAT。
- 本地账号使用 `POST /api/password {old_password,new_password}` 更新密码并使其他会话失效，返回新 `csrf`。GitLab 托管账号在 GitLab 修改密码，该本地接口拒绝修改托管账号。
- 管理员：`GET /api/users` 返回 `{users,management}`；`management` 含 `provider`、`url`、`profile_url`。启用统一登录时 `provider:"gitlab"`，`url` 指向 GitLab 原生用户管理，`users` 是已在平台建立身份的用户。GitLab 负责创建、停用及管理员角色；`POST /api/users` 在该模式下返回 403。本地账号模式可通过此 POST 新增 `{username,name,password,admin:false}`。
- 管理员管理本地账号：`PATCH /api/users/{id} {name?,admin?,active?}` 修改显示名称、权限和启用状态；`POST /api/users/{id}/password {new_password}` 重置他人密码；`DELETE /api/users/{id}` 删除他人账号。上述接口拒绝修改 GitLab 托管目标。对本地账号，停用、权限变更、重置密码和删除会立即撤销其会话；禁止停用、降权或删除当前管理员本人，且必须保留有效管理员。
- `GET /api/profiles` 返回 `{profiles,bindings,default_bindings,module_roles,protocol_kinds}`。`profiles` 仅包含全局配置与当前用户自己的个人配置；各项含 `scope`、`owner_id`、`can_manage`、`has_api_key` 和固定掩码，不返回 API Key。`bindings` 为当前请求选型后的有效绑定，`default_bindings` 为全局默认绑定。
- `POST /api/profiles` 新建配置，成功返回 201；`PUT/DELETE /api/profiles/{id}` 修改或删除；`POST /api/profiles/{id}/verify {allow_generation_probe:false}` 检测能力（可省略字段，默认不生成图片或语音）。所有登录用户可管理自己的个人配置，只有管理员可管理全局配置；其他人的个人配置始终不可访问。
- `GET /api/bindings` 返回 `{bindings,module_roles}`，反映当前请求的有效选择。`PUT /api/bindings/{module}/{role} {profile_id}` 仅管理员可调用，用于设置全局默认；只能绑定检测通过且用途兼容的全局配置，传 `null` 解除该全局默认。个人配置通过下述请求头选择。
- 仪表盘的模块选择保存在当前浏览器的 `localStorage`，按登录用户 ID 分开；选择不会写入服务器的全局默认。浏览器为状态、读取及制作请求发送 `X-Fantasy-Model-Selection`，格式如 `{"knowledge":{"llm":"配置ID","embedding":null},"animation":{"vision":"配置ID"}}`，最多 8 KiB。未出现的用途沿用全局默认；显式 `null` 表示该用途不使用默认模型。模块和用途须匹配 `module_roles`，格式错误返回 400；配置 ID 的归属、能力和检测状态在实际使用时再次校验。MCP 等 API 客户端也可发送此请求头选择自己有权使用的配置。
- 浏览器同时发送 `X-Fantasy-Model-Account`，值为当前会话用户 ID。该头用于检测旧标签页的账号变化，与当前登录用户不匹配时返回 409，提示刷新页面；它不能代替 Cookie 登录或指定调用身份。API 客户端可省略此头，调用身份始终来自服务端验证的会话。
- 根入口提供 `GET /api/workspace` 返回九个模块的状态及代码仓库公开配置，`PUT /api/repository` 由管理员保存 GitLab 地址和 OAuth 配置。`{mode:"managed",use_managed_defaults:true}` 使用随附 GitLab 的签名自动配置；外部或手动配置保留原字段。OAuth 密钥只写入，不返回浏览器；`clear_client_secret:true` 清除密钥并关闭统一登录。重复保存等价配置保持 revision 和会话；真正变更服务地址、应用或密钥后需要重新登录。
- 特效制作位于 `/effects/`，沿用社区会话。`GET /effects/api/session` 返回当前用户、特效 CSRF 和个人草稿命名空间；其余特效 API 带 `X-Effects-Owner`，写操作同时带 `X-Community-CSRF`、`X-Effects-CSRF` 和当前 `Origin`。完整数据格式与导出接口见受认证的 `/effects/api/guide`。特效渲染为本地 CPU 工作，不需要模型 API 绑定。
- 匿名 `GET /api/auth/options` 返回 `gitlab_login`、`preferred_login`、`gitlab_initializing`、`gitlab_setup_error`、`gitlab_url`、`local_login`。`local_login` 仅在存在可用本地账号时为真。`GET /auth/gitlab/start` 开始 GitLab 登录。已有本地账号通过带 CSRF 的 `POST /auth/gitlab/link-start` 显式关联自己的 GitLab 账号；响应 `authorization_url` 用于跳转。`/auth/gitlab/callback` 由根入口完成服务端验证和会话签发。
- 登录入口的主机或端口与 `APP_ORIGIN` 不同时，先 303 到配置地址的 `/auth/gitlab/start`，该跳不创建 state 或 Cookie；随后在统一主机发起授权。跳转目的地不采用请求提供的回调或转发主机。非统一地址的已认证 `link-start` 关联请求返回 409，提示在统一入口重新登录，不跨主机重放 POST。
- 除登录外，修改请求必须带当前会话的 `X-Community-CSRF`；浏览器跨来源请求会被拒绝。

`AuthStore` 仅在账号表为空时初始化 `admin / 12345678`。默认随附 GitLab 的签名初始化完成后，将该账号与受信 GitLab 数字身份关联，保留平台 UUID 和作品，并设为 `auth_provider:"gitlab"`；原本地密码不再用于登录。升级不重置密码、不重建账号。现有安装通过已保存的安装标识或明确的 `COMMUNITY_GITLAB_ADMIN_ACCOUNT_ID` 识别原管理员，拒绝冲突关联。首次接管撤销旧平台会话一次，后续同一身份初始化保持会话。

本地密码为 PBKDF2-SHA256 加盐哈希，Cookie 为 HttpOnly/SameSite=Lax（HTTPS 下 Secure），服务端 session 可撤销，最长 8 小时。保留的旧本地哈希仅供维护者通过 `scripts/restore-local-admin.py --account-id UUID --disable-gitlab` 明确关闭统一登录后恢复原本地账号模式。删除账号不删除作品，作品保留原来的独立账号 ID；重新创建同名账号不会继承作品。需要之后恢复访问时可在 GitLab 停用并恢复同一账号。

GitLab 身份按实例和数字用户 ID 独立映射，不按用户名自动合并。成功 OAuth 或 PAT 登录使用上游 `is_admin` 同步平台管理员角色；GitLab 普通用户对应平台成员。显式关联后从 GitLab 登录，保留原用户 ID 和作品，并由 GitLab 接管该账号的角色与密码登录。一个 GitLab 身份不能关联不同账号，已有绑定不能替换其他身份。

OAuth/PAT 凭据加密保存，平台每隔最多 60 秒复核上游身份与角色；账号停用、删除或令牌撤销后阻止继续访问。OAuth 到期时刷新，并在共享数据库中串行化刷新与登出，保留已轮换的凭据；临时上游故障返回 503，不把它当成用户撤权。下游模块使用复验后的最新平台角色。PAT 不需要 OAuth password grant，也不会作为模块凭据、API Key 或工具结果返回。关闭平台会话后，GitLab 网页自己的会话需单独退出。

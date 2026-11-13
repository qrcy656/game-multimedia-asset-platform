# 特效制作：智能体使用说明

本模块使用 Arcadia Effects 的 JSON 数据生成 2D 特效。智能体通过平台已有的 MCP 和 AI 专属账号访问；每个账号有独立特效库。不要在服务器目录直接写文件，也不要借用管理员登录。所有操作都应使用当前账号的受认证接口。

## 推荐流程

1. 读取本说明、格式文档或当前账号已有特效，先明确用途、大小、时长与风格。
2. 创建新的 JSON，显式使用稳定的 `doc.id` 和图层 `id`，不要擅自覆盖用户文件。
3. 调用验证接口检查数据，再保存到个人特效库。
4. 导出 `frame` PNG 检查关键时刻，导出 `atlas` PNG 查看整段动画。查看真实图片后修改 JSON，再验证和保存。
5. 返回文件名和可打开的编辑器链接。需要时提供 PNG 与 atlas 元数据，便于游戏引擎导入。

## 接口

MCP 负责认证与账号绑定。若直接通过受认证 HTTP 使用，每个 `/api/` 请求带 `X-Effects-Owner`，写请求另带服务器发出的 `X-Effects-CSRF`。不要自行生成或猜测凭据。

- `GET /api/guide`：返回 `guide`（本说明）、`format`（完整字段格式），以及 `usage`、`upstream`、`limits`。通过 MCP 或该认证接口读取这些字段即可，不要请求服务器内部文档路径。
- `GET /api/list`：列出本人及共享作品，包含 `{file,name,id,thumb,mtime,owner_key,owner_name,is_own,editable}`。
- `GET /api/effect?f=<file>&owner_key=<owner_key>`：读取指定作者的 JSON；省略 `owner_key` 时读取自己的库。
- `POST /api/save`，JSON `{name,data,thumb?,create_only?,expected_sha256?}`：保存特效文件，返回 `{ok:true,file,revision}`。`data` 是下面的 effect 包装结构。新建时使用 `create_only:true`，防止意外覆盖同名文件；修改现有文件时先读取并把 ETag 中的 SHA256 作为 `expected_sha256`，冲突时重新读取并合并，不要强行覆盖。
- `POST /api/delete`，JSON `{file,owner_key?}`：仅作者或管理员可删除原件。仅在用户要求删除时使用。
- `POST /api/validate`，JSON `{data}`：提交特效数据进行格式验证。
- `POST /api/exports`，JSON `{file,format:"frame",time:0.3}` 或 `{file,format:"atlas"}`：开始后台导出，返回 `202 {job:{id,status,...}}`。渲染没有固定时间上限。
- `GET /api/exports/{id}`：查询有确切 ID 的共享导出任务，状态为 `running`、`done`、`failed` 或 `cancelled`。
- `GET /api/exports/{id}/result`：完成后下载 PNG；`X-Effects-Width/Height/Frames/Fps` 提供尺寸、帧数和 FPS。取消或释放任务仅限其创建者或管理员。
- `POST /api/exports/{id}/cancel`：取消自己的运行中任务。
- `POST /api/exports/{id}/release`：下载保存成功后释放已完成任务的临时缓存。MCP 会自动释放；缓存不会修改原始 JSON 特效库。未释放的已完成缓存保留一小时，运行中的任务不按时间清理。
- `POST /api/export`：保留原同步 PNG 接口用于兼容；长时间导出请用上述后台接口，避免网关和客户端的单次连接超时。

认证失效或账号已切换时必须停止操作。不要把失败的保存描述为成功。

`save`、`export` 和 `exports` 的 JSON 也接受列表返回的 `owner_key`。成员可读取和导出他人作品；修改共享原件需 `editable=true`。复用他人内容时，以新名称保存并省略 `owner_key`，作品即归当前账号。403 内容只读错误表示该操作没有权限，不表示登录失效。

## 最小特效示例（作为数据，不是代码）

```json
{
  "app": "arcadia-effects", "v": 2,
  "doc": {
    "v": 2, "id": "fx_my_sparks", "name": "My Sparks",
    "comp": {"w": 256, "h": 256, "dur": 0.8, "fps": 30},
    "exp": {"cols": 4, "rows": 2, "frames": 8, "cellW": 128, "cellH": 128, "ss": 1},
    "layers": [
      {"id": "lr_my_sparks_1", "type": "emitter", "name": "Sparks",
       "em": {"bursts": [{"t": 0, "n": 30}], "rate": 0, "speed": 180, "seed": 7},
       "pt": {"life": 0.5, "size": 12, "sprite": {"kind": "shape", "id": "streak", "p": {}},
              "alignVel": true, "gravY": 80}}
    ]
  }, "tex": {}
}
```

缺省字段由上游模型补全。连续喷射必须显式设置 `bursts:[]`，否则会继承默认 24 粒子爆发。坐标原点在画面中心，Y 轴向下；时间以秒计；图层数组第一个元素位于最上层。关键帧时间从图层起点计算。`start` 可以是小幅负值，用于预热。

支持粒子 emitter、精灵 sprite、图集 atlas、后处理 postfx、力场 force。除粒子和闪光外，可结合运动路径、子发射器、发射器动画、位移/模糊后处理、纹理图集，构成大中小三种视觉尺度。详细字段说明请读取认证 `/api/guide` 响应中的 `format`。上游创作技能文件仅保留为服务器维护参考，不通过接口提供；不要请求内部源码、内部文件路径或其他账号资料。

## 导出限制

服务端导出只解释 JSON 数据，不执行脚本、不下载远程素材。纹理需内嵌 PNG，单张不超过 2048×2048。渲染没有固定时间上限，`limits.render.timeoutMs` 为 `null`。最多 24 层、8 个发射器、8000 个同时存活粒子、15 秒动画时长、64 帧；画面最大 1024×1024，图集最长边 4096。超出资源限额时调整分辨率、粒子预算或导出范围。浏览器的原有 JSON / PNG / ZIP 导出仍可使用。

MCP 的 `export_effect` 自动提交、轮询并下载。若 AI 客户端自身设有工具调用时限，使用 `start_effect_export`、`get_effect_export`、`download_effect_export` 分步操作；客户端每次只需等待一次短请求。需要取消时调用 `cancel_effect_export`。

浏览器链接支持 `?open=<file>&t=0.3` 或 `?open=<file>&tab=atlas`。不要默认添加 `fresh=1`，因为它会丢弃未保存的浏览器工作副本。

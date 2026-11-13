# 特效制作 · 社区版

从社区工作空间打开“特效制作”，地址为 `/effects/`。使用社区平台当前账号，作品与浏览器草稿按账号独立保存。支持粒子、精灵、运动路径、后处理、纹理图集及透明 PNG / 序列帧图集导出。

## 使用

1. 打开内置 Factory 示例，或导入 Arcadia Effects JSON 创建特效。
2. 调整图层、时间轴和导出尺寸，保存到“个人特效库”。
3. 浏览器可导出当前画面或图集；外部智能体可通过社区 MCP 读取格式说明、编辑项目、验证并保存，再启动后台 PNG 导出。
4. 保存已有项目时携带读取时的 SHA256 版本，防止覆盖其他窗口的新修改。退出或切换账号后，旧编辑页会锁定；浏览器草稿继续属于原账号。

## 服务与数据

根目录 `compose.yaml` 启动独立的 `effects` 容器。容器仅通过内部 Docker 网络向社区入口提供服务，浏览器和 MCP 统一访问社区端口的 `/effects/` 路径。特效作品保存在 `data/effects/users/`；运行中的导出和已完成的短期结果保存在内存，MCP 下载校验后释放缓存。

`scripts/start.sh` 会补齐 `EFFECTS_UID` / `EFFECTS_GID`，默认使用安装用户身份。它通过 Docker 辅助容器兼容原有 root 归属的 `data/`，只为新建的 `data/effects/` 设置归属；已有目录及其他模块数据保持不变，并拒绝符号链接路径。迁移数据时保持 `.env` 中这两个值与 `data/effects` 的所有者一致。全新安装的个人特效库为空，内置 Factory 示例由编辑器程序提供。

门户每次请求验证当前会话并签发短期特效身份令牌。特效写操作同时检查社区 CSRF、特效 CSRF、请求来源和当前账号标识。共享特效库列出各作者作品：成员可编辑本人作品，使用他人作品时打开自己的工作副本；管理员可管理他人原始作品，保留作者归属。

## 智能体

社区 MCP 使用既有社区登录配置即可。推荐流程：

`get_effects_guide` → `list_effects` / `read_effect` → `validate_effect` → `save_effect` → `start_effect_export` → `get_effect_export` → `download_effect_export`。

渲染没有单次运行时限。导出查询与下载分开进行，客户端可稍后使用同一任务 ID 继续，也可以取消当前账号的任务。编辑器与渲染器负责执行特效数据；智能体负责按照说明构造这些数据。

社区 `list_effects` 返回 `owner_key`、`owner_name` 和 `editable`。读取或导出共享作品时，将其 `owner_key` 传给 `read_effect`、`export_effect` 或 `start_effect_export`；只有作者或管理员可指定该归属更新原件。保存到自己的特效库时省略 `owner_key`，使用新名称和 `create_only` 防止覆盖。

上游来源和 MIT 许可证见 `UPSTREAM.md` 与 `LICENSE`。

# 短视频编导运营体系

本目录是现有 `short-video-director-system` 技能的文件快照，含技能入口、14 份内置参考、Agent 配置及 19 个课程处理脚本。技能正文、参考与脚本保留原版本；打包时排除了 Python 缓存等运行产物。

## 安装与调用

将整个目录放到 `~/.codex/skills/short-video-director-system/`，在 Codex 中调用 `$short-video-director-system`。先读 [SKILL.md](SKILL.md)，再按任务读取 `references/`。

## 两种使用方式

1. **离线参考**：未连接 Obsidian 时，普通编导执行任务回退至内置参考。内置资料不包含个人动态知识库中所有新增卡片、方法地图或案例。
2. **连接自有知识库**：配置 `~/.codex/knowledge-sources/short-video-director-system.json`，然后运行：

```bash
python3 ~/.codex/skills/short-video-director-system/scripts/resolve_kb.py
```

定位文件需要 `collection_id`、`vault_id`、`vault_path`、`entry` 和 `schema_version`；字段须与自有知识库入口匹配。入口须为 canonical，带有效的 `kb_revision`，并通过一级路由校验。详细协议见 [Obsidian 集成](references/obsidian-integration.md)。没有配套知识库时，不要虚构这些字段或把原检查点当作已同步状态。

## 脚本使用前检查

- `sync_skill_kb.py` 的 `KB_ROOT` 保留原机器配置；换机时先改为自己的知识库路径。
- `prepare_asset_learning.py` 的缓存工具及默认数据库路径依赖本地 `research-capture` 环境。
- 文档中的 `/Users/lipeiyu/...`、外接盘和 Python 环境路径是原环境示例，需要按实际环境调整。
- 转写、文档提取、OCR 和关键帧工具按各自 `--help` 与源码配置依赖；本仓库不附带模型或运行环境。

本目录未附带个人 Obsidian 知识库、课程原件、转写全文、账户配置或缓存。课程学习与入库仍按技能原有的证据、核读和版本校验流程执行。

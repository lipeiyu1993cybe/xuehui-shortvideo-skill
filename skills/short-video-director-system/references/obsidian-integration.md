# Obsidian 动态知识源

## 目的

把 Obsidian 作为唯一可编辑的动态知识真源。技能内置参考资料只作为离线基线，不与 Obsidian 竞争“最新版本”。

## Skill—Obsidian 同步检查

Skill 是同一套知识的执行入口，不是第二个独立知识库。短视频编导与巨量本地推共用本库，但按执行任务分 Skill。每次正式合并课程、真实复盘或稳定规则时：

1. 先完成 Obsidian 的来源、去重/冲突、方法、应用转译和日志，并升版 `kb_revision`。
2. 更新受影响 `SKILL.md` 的同步检查点；如果规则影响执行流程，同时更新对应 Skill 内容。全库使用 `kb_revision`；投放领域另使用 `local_promotion_revision`，只有本地推知识、模板或共享字段变化时才增加它。
3. 再运行 `resolve_kb.py`，确认解析结果与 Skill 检查点一致。

对应 revision 不一致时，只能报告“待同步”，不能报告“已完成学习”。不要把整个知识库复制进 Skill；用唯一动态真源、显式版本检查和同一执行规则避免双份文本漂移。

## 定位与读取

1. 运行 `python3 ~/.codex/skills/short-video-director-system/scripts/resolve_kb.py`；校验器会同时检查入口中的一级任务路由是否存在、唯一且状态可用。脚本一律使用该绝对路径调用，不依赖当前工作目录。
2. 若返回 `ok: true`，读取返回的 `entry`，按入口中的任务路由读取相关方法地图。
3. 在 `04-知识卡片` 中检索任务同义词，只采用 `status: canonical` 的卡片。
4. 核对课程原词时读取来源档案；不要把来源证据直接当成当前执行口径。
5. 记录本次读取到的 `kb_revision`。需要修改知识库时，它是乐观锁基线。

全盘课程审计时，将本节解析出的 `kb_root` 传给 `audit_course_archive.py --kb-root`。完整转写只证明 `transcript_status: complete`；只有稳定来源档案明确引用的原件路径或 SHA-256 才能证明 `learning_status: learned`，精确重复副本可以继承该证据。

用户本次明确提供知识库路径时，以用户指定路径为准，但仍校验 `collection_id`、`vault_id`、`schema_version` 和 `status`。不要遍历多个 `.obsidian` 后自动猜测。

连接校验失败时：

- 普通执行任务可回退到技能内置 `references/`，并把它视为离线基线。
- 用户明确询问“刚收录的新知识”或要求更新知识库时，不要静默回退；报告连接问题。

## 新知识入库

只有用户明确说“收录、记住、更新体系、加入知识库”时持久化。

1. 读取入口的 `kb_revision`。
2. 在 `01-收件箱/待合并` 保存原始内容和来源，标记 `status: draft` 与 `base_kb_revision`。
3. 将知识分类为 `course_fact`、`execution_rule`、`platform_fact`、`user_preference` 或 `content_material`。
4. 对照现有方法地图、知识卡片和口径决策，判断补充、重复、修正或冲突。
5. 原始证据进入 `02-来源档案`，分配稳定 `source_id`；旧证据不覆盖。
6. 提炼为原子知识卡片并链接到方法地图。一张卡只表达一个核心结论。
   - `content_material` 另按动态知识库的 `TPL-内容素材记录` 建立 `MAT-XXXX`；先保持 `draft`，完成来源、授权与三筛后再升为 canonical 并加入素材索引。
7. 若存在冲突，先在口径决策中记录双方来源和采用理由，不用“最后写入者覆盖”。
8. 每节课程额外完成应用转译：记录“能解决的账号问题、需要的真实输入、可执行动作、验证信号、适用边界”。没有真实账号与数据时标为待验证，不能伪称已跑通。
9. 写入规范笔记前重新读取入口；若 `kb_revision` 已变化，停止覆盖，将候选留在 `待合并`。
10. 合并后将全库 `kb_revision` 加一，同步增加被修改笔记的 `revision`，更新 `updated_at` 和更新日志。若改变巨量本地推知识、实验模板或 `content_id/ad_variant_id/experiment_id` 共享字段，同时增加 `local_promotion_revision`。
11. 再次运行 `python3 ~/.codex/skills/short-video-director-system/scripts/resolve_kb.py` 验证全库连接；投放领域同时运行 `$douyin-local-promotion-ads` 的 `scripts/resolve_shared_kb.py` 验证领域 revision。

普通知识内容仍以 Obsidian 为唯一真源，但每次正式合并都必须同步受影响 `SKILL.md` 的版本检查点。触发条件、入库协议、目录 schema、跨 Skill 字段或执行工作流发生变化时，还必须更新对应 Skill 规则并重新运行技能校验。

## 时效与事实边界

- 课程原词：对应版本的来源证据优先。
- 执行归纳：当前 `status: canonical` 的方法地图与口径决策优先。
- 平台事实：最新官方资料优先，必须记录 `checked_at` 和适用时间。
- 用户偏好：只适用于明确的账号、人物或项目，不外推成通用规律。

## 禁止事项

- 不从收件箱或 `draft` 卡片直接生成确定性结论。
- 不把用户一次性的临时要求自动写成长期知识。
- 不用同步脚本覆盖经过人工归纳的方法地图。
- 不删除旧来源来消除冲突。
- 不把“可能爆”写成“保证爆”。

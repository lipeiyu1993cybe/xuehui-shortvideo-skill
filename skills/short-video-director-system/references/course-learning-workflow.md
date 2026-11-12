# 课程审计、全量学习与证据准备

仅在用户要求课程审计、转写、蒸馏、批量学习或云端学习时读取。

`<SCRIPTS>` 固定指 `/Users/lipeiyu/.codex/skills/short-video-director-system/scripts/`；知识库路径从 `resolve_kb.py` 获取。完整转写与证据准备不等于课程学习完成。

用户要求检查一个课程目录是否全部学习、是否遗漏或重复时，先运行 `python3 <SCRIPTS>/audit_course_archive.py`，并把 `resolve_kb.py` 返回的 `kb_root` 传给 `--kb-root`；区分发现、完整下载、唯一内容、转写/OCR、学习与入库状态。完整转写只能标记转写完成；视频学习必须由稳定来源档案中的精确原件路径或 SHA-256 证明，字节级重复再继承该证据。重复只学习一次，不删除原件，审计报告不能把目录发现、文件完整或已有转写误写成已学习。

### 全量课程学习队列

当用户要求“全部学习”“剩余课程全量蒸馏”或“不要重复学习”时，先将审计 JSON/CSV 固化为唯一队列。默认队列包含 `download_status: complete`、完整性可核验且 `learning_status: not_learned` 的唯一视频、文档、图片和本地 HTML；需要只转写视频时显式使用 `--videos-only`/`--kind video`。不因相近标题、课程版本相同或讲义相似擅自去重。已有稳定来源档案路径或 SHA-256 证据的内容从队列排除，但保留其来源 ID。

全量不只指视频。审计 schema v2 还必须把 PDF/Office、图片、HTML 与文本作为独立资产登记：每个资产保留 `asset_id`、`sha256`、全部别名路径、实际 MIME/容器、`integrity_status`、`evidence_status`、`external_dependency_status` 与阻塞原因。`.downloading`、`._*`、`.DS_Store`、`.accelerate` 等技术/未完成项单列，不计入可学习内容分母，也不得静默标为已学。

视频容器以 ffprobe 为准：扩展名为 `.mp4` 但实际是 MPEG-TS 时保留原路径并记录真实 `container_format`；无音频视频只能以视觉时间轴和画面证据完成有限范围学习，不伪造语音转写。完整可读内容与整个资料库分两层验收：前者仍须列出缺口/外链边界，后者还要求未完成下载、损坏原件、课程编号缺口和外部依赖均已处理。

按媒介路由证据：视频用完整时间轴转写、五点分位关键帧和必要定点帧；PDF/Office 用逐页/逐表/逐幻灯片抽取与原页核验（损坏 OOXML 只能保留 `partial/blocked_integrity`）；图片用 OCR 与原图视觉核验；HTML 用 `extract_course_html.py` 记录标题、日期、正文、链接和外链状态，不自动抓取外部资源。机器转写、OCR、抽取和关键帧均只是 evidence，不会自行提升 `learning_status`。

验收时分开报告“可学习完整唯一内容已完成”和“整个资料库已完成”。只有唯一内容的人工核读、原画面/原页核验、去重、应用转译、稳定 `SRC` 与版本同步全部完成，且阻塞项另列，才可使用前者；存在未完成下载、损坏文件、缺失编号或未核验外链时，不得使用后者。HTML 的 `links_only/external_unverified` 只说明外部图片/链接未核验：本地标题与正文证据完整时可完成本地内容学习，但外链仍须单列，不能升级为平台事实。

对于用户已拥有的本地完整原件，优先使用 `<SCRIPTS>/prepare_course_learning.py`：先以 `audit` 生成当前审计，再以 `prepare --dry-run` 查看唯一批次，确认后才写入 `evidence_pending_review` 草稿，并按需加 `--transcribe --keyframes`。该工具防止重复 ASR、排除未完成文件，且只准备证据；它绝不自行创建 `SRC`、提升 `learning_status`、修改 canonical 方法或同步版本。详细调用和边界读取 [[本地课程蒸馏流水线]]。

按课程版本和主题将队列切成可恢复小批次。每批开始前记录基准 `kb_revision`、文件范围、原件哈希和预期方法地图；完成后立刻更新来源、canonical 内容、索引、更新日志与 Skill 检查点，再重新审计。发生中断时从“尚无稳定学习证据”的下一项续跑，绝不把已完成转写、抽帧或草稿蒸馏当作已学习。

当用户明确要求“只蒸馏增量”时，先执行轻量语义去重：读取课程标题、完整可回查文字或转写、关键段和少量关键画面，与现有 canonical 方法地图及已学来源逐项对照。若没有新增规则、边界、案例机制、应用转译或可纠正的冲突，只建立独立来源/去重证据，标记为 `semantic_duplicate`，不再完整蒸馏、不重复修改方法地图；不同 SHA-256 仍保留原件和独立证据，不能写成字节级重复。只有发现实质增量，才进入完整学习门槛。

只有用户在当前任务中明确排除某类课程时，才可将该类资产标记为 `user_excluded`，并保留原始审计记录、不把排除误写成已学习；排除范围不得从旧任务自动继承。用户要求“所有课程/所有文件全量学习”时默认没有类别排除，简历、剪辑、案例、笔记、HTML 等均按各自媒介证据门槛进入队列。

只有审计同时满足“所有可学习完整唯一资产（视频、文档、图片及本地 HTML）均已进入终态”“无下载未完成或完整性阻塞”“课程编号缺口已明确处理”“Skill 检查点与知识库版本一致”时，才可称全量课程已完成。`semantic_duplicate` 与 `user_excluded` 需单独计数，不能冒充已学习；`external_unverified` 需列出未核验外部依赖。面对数十小时以上的课程，应明确报告剩余时长和当前批次，不把尚未逐段核读的内容压缩成泛化总结或虚构已掌握。

### 版本化验收与队列复核

长队列完成或中断后，使用独立的只读校验器复核账本，而不是根据转写文件数量推断学习完成：

```bash
python3 <SCRIPTS>/validate_course_learning.py \
  --ledger <全量资产账本.json> \
  --kb-root <Obsidian知识库根目录> \
  --skill /Users/lipeiyu/.codex/skills/short-video-director-system/SKILL.md
```

校验器至少检查：`unique_assets` 与汇总数量、`learned` 是否有可回查 `source_ids`、完整性/下载阻塞是否被误标为 learned、证据路径是否存在、批次 manifest 的状态和 `base_kb_revision → completed_kb_revision`、`_index.md` 与 Skill 检查点版本。`semantic_duplicate` 与 `user_excluded` 是终态但不计入“已学习”数量；`evidence_preparation`、`planned`、`repair_queue` 和 `superseded` 只表示队列/证据状态。

只有报告中的 `actionable_pending = 0`、`blocked = 0`、无过期 `completed_learning` manifest 且版本一致时，才运行 `--strict` 作为最终门槛。默认模式允许长队列存在待处理项并返回结构性问题；`--strict` 失败时应报告剩余项和阻塞原因，不改写状态。外部依赖只在本地正文不足或用户明确要求外链核验时进入阻塞，不得因 `links_only` 自动抬高或降低学习状态。

需要本地批量转写时使用 `python3 <SCRIPTS>/transcribe_course.py`，输出同名 `.transcript.txt` 与 `.transcript.json`；脚本支持跳过已完成文件、按序号分批、长视频 `--chunk-seconds` 分段读取和失败后续跑。长视频默认用有界窗口合并时间轴，避免一次性加载整段音频；转写文件只是逐段核读与画面核验的输入，不能单独提升 `learning_status`。

需要为一批课程建立可回查的视觉证据时，使用 `python3 <SCRIPTS>/extract_course_keyframes.py` 从资产审计 JSON 按相对时间抽帧，并保留 `keyframes.manifest.json`；只看抽帧不能替代逐段核读，界面字段、数字和争议段落仍须回到精确时间点核验。

### 云端不落地蒸馏

当原件只在百度网盘等已登录云盘中可用，且用户要求不下载、不保存视频文件时，可使用平台在线播放及其云端字幕、摘要、分段总结作为学习输入。每节先在 Obsidian 的 `01-收件箱/待合并` 建立 `course_cloud_distillation` 草稿，至少记录课程版本、文件名、时长、云端文字、时间戳、证据限制、去重初判和应用转译。

云端 AI 摘要不是完整转写，也不等于已学习：不得直接写入 canonical 方法地图、不得提升 `learning_status`、不得因摘要中的案例数字或平台判断制造结果承诺。只有补齐可回查的云端文字或字幕、关键段与原视频上下文核验、来源/去重/应用转译，并完成 Skill—Obsidian 同步检查后，才能正式合并。云端流式播放产生的服务缓存不视为用户保存的视频文件，但不得主动下载媒体原件。


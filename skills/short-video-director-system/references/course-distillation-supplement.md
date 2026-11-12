---
type: reference_supplement
scope: 课程蒸馏与知识库互通
status: canonical
added_at: 2026-08-30
supersedes: none
---

# 课程蒸馏补充规范

本文是 `short-video-director-system` 的**增量补充**，不替换既有规则。与既有规则冲突时，
以[[知识入库规则]]与[[本地课程蒸馏流水线]]为准，本文只补它们未覆盖的工程细节。

关联：`$research-capture`、`$douyin-local-promotion-ads`

## 一、补充定位

本 skill 是**执行入口**，Obsidian 是**唯一可编辑的动态真源**。补充内容只涉及三类：

1. 工程管线：如何低成本、可恢复地把课程变成证据。
2. 队列与缓存：如何避免重复计算（哈希、转写、OCR）。
3. 互通边界：什么进 Obsidian，什么只进 Skill。

不复制整套课程原文进 Skill。Skill 只保留：稳定任务路由、判断规则、质量门槛、版本检查点。

## 二、工具链（本机实测可用）

### ffmpeg

系统未安装 ffmpeg。两处内置副本中：

- `/Applications/VideoFusion-macOS.app/Contents/Resources/ffmpeg`
  依赖 `Contents/Frameworks/` 下的 `libavcodec.dylib` 等，且链接加密库 `libvecryptor`；
  即便设置 `DYLD_LIBRARY_PATH` 也静默失败（无输出、不产出文件）。**不可用。**

- `/Applications/ChatCut.app/Contents/Resources/app.asar.unpacked/node_modules/ffmpeg-ffprobe-static/ffmpeg`
  静态编译 ffmpeg 6.1.1，可直接运行。**使用这一个。**

已软链至 `~/.local/bin/ffmpeg`、`~/.local/bin/ffprobe`（`/usr/local/bin` 无写入权限）。
任何调用前先执行：

```bash
export PATH="$HOME/.local/bin:$PATH"
```

### ASR

- 运行环境：`/Users/lipeiyu/.codex/runtime/course-transcription-py312/bin/python3`
- 模型：`mlx-community/whisper-large-v3-turbo`（已缓存于 `~/.cache/huggingface/hub`）
- 后端：mlx，GPU 加速；实测约 10x 实时
- mlx-whisper 内部同样调用 ffmpeg，PATH 必须先设好，否则 `load_audio` 抛 `FileNotFoundError`

### 并发约束

ASR 是 GPU 独占资源，**多批次必须串行**。同时启动两个转写进程会争抢显存并可能双双失败。
用接续脚本排队，不要并行。

## 三、低算力增量缓存

借鉴 `$research-capture` 的做法，避免重复劳动。缓存库：
`/Users/lipeiyu/.codex/research-capture/cache.sqlite3`

```bash
CAP=~/.codex/skills/research-capture/scripts/capture_cache.py
PY=/Users/lipeiyu/.workbuddy/binaries/python/versions/3.13.12/bin/python3

$PY "$CAP" stats                                  # 查看覆盖情况
$PY "$CAP" scan-local <目录>                       # 增量扫描，未变文件不重算 SHA-256
$PY "$CAP" import-audit <审计.json>                # 导入审计已算好的哈希，避免二次全盘读盘
$PY "$CAP" attach-artifact ...                    # 把转写/OCR 产物挂到原件哈希
$PY "$CAP" lookup --source <文件>                  # 查是否已有可复用证据
```

要点：

- **审计完成后立刻 `import-audit`**，缓存里已有哈希，后续扫描不必再读一遍 1TB。
- 转写/OCR 产出后 `attach-artifact`，下次遇到同一原件直接复用，不重复 ASR。
- 缓存命中**只说明计算产物可复用**，不代表事实仍有效，更不提升 `learning_status`。

## 四、队列机制

用软链接队列精确圈定待转写/抽取范围，避免重复处理已学内容，也不复制原件。
队列工具固定在 `$short-video-director-system/scripts/build_queue.py`，必须提供完整审计账本；
它按 SHA-256 选择唯一代表、保留别名路径，并把未完成/损坏/外链依赖另列为阻塞项。

```bash
PY=/Users/lipeiyu/.codex/runtime/course-transcription-py312/bin/python3
QUEUE=/Users/lipeiyu/.codex/skills/short-video-director-system/scripts/build_queue.py

# 默认：完整、未学习的唯一视频/文档/图片（只创建指向原件的软链接）
$PY "$QUEUE" --ledger <账本.json> --archive-root <课程根> --queue-dir <队列目录>

# 只做视频转写队列，或限定一个可恢复批次
$PY "$QUEUE" --ledger <账本.json> --archive-root <课程根> --queue-dir <队列目录> \
  --videos-only --max-hours 8

# 中断后从确定的唯一序号续跑
$PY "$QUEUE" --ledger <账本.json> --archive-root <课程根> --queue-dir <下一批队列> \
  --videos-only --start-index 9 --max-items 3

# 单独列出未完成/损坏/外链依赖的修复队列；这些项仍不得标记已学习
$PY "$QUEUE" --ledger <账本.json> --archive-root <课程根> --queue-dir <修复队列> \
  --blocked-only
```

完成一个批次或准备交接时，运行独立的只读校验器。它不修改账本、来源档案或原件，专门检查“证据已生成”与“已学习”之间是否被误合并：

```bash
python3 /Users/lipeiyu/.codex/skills/short-video-director-system/scripts/validate_course_learning.py \
  --ledger <账本.json> \
  --kb-root <Obsidian知识库根目录> \
  --skill /Users/lipeiyu/.codex/skills/short-video-director-system/SKILL.md
```

默认模式允许队列仍有待处理项，但会列出 `actionable_pending`、`blocked`、来源/证据缺口和过期批次；最终验收才加 `--strict`。`links_only/external_unverified` 仅是 HTML 外部资源边界，本地正文证据完整时不自动视为阻塞；`blocked_integrity`、未完成下载和损坏原件仍必须留在修复队列。

队列文件以序号加原名命名，规避跨目录重名。转写脚本按队列目录批量处理：

```bash
export PATH="$HOME/.local/bin:$PATH"
~/.codex/runtime/course-transcription-py312/bin/python3 \
  ~/.codex/skills/short-video-director-system/scripts/transcribe_course.py \
  <队列目录> --output-dir <输出目录> --language zh --chunk-seconds 1800
```

- `--chunk-seconds 1800` 按 30 分钟分段读取长音频，避免一次性加载导致内存溢出。
- 脚本跳过已完成文件，中断后重跑安全，不必从头开始。

### 多媒体资产路由（全量审计）

`scripts/audit_course_archive.py` 的 schema v2 会同时登记视频、PDF/Office、图片、HTML
和文本，并把 `asset_id`、SHA-256 别名、MIME/容器探测、`integrity_status`、
`evidence_status`、技术元数据排除项写入 JSON/CSV/Markdown。视频优先使用 `ffprobe`
取时长并保留容器名，因此 `.mp4` 伪装的 MPEG-TS 不会丢失时长；`.downloading` 文件
保留为 `course_video + incomplete`，不进入完整内容队列。

文档证据：

```bash
PY=/Users/lipeiyu/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3
export PYTHONPATH=/Users/lipeiyu/.cache/codex-runtimes/codex-primary-runtime/dependencies/python
$PY scripts/extract_course_documents.py <PDF/DOCX/PPTX/XLSX目录> --output-dir <证据目录>
$PY scripts/extract_course_html.py <HTML目录> --output-dir <证据目录>
```

`extract_course_documents.py` 会先按文件头识别 OLE/OOXML、检查 ZIP/CRC，并对损坏
DOCX 尝试读取 XML 文本；输出只能标作 `complete/partial/needs_ocr` 证据，不能单凭
抽取结果提升学习状态。`extract_course_html.py` 只解析本地快照的标题、正文、日期
提示、链接和图片引用，外链统一标为 `links_only/external_unverified`，不会自动访问
或下载外部资源。图片/PDF 页面仍需 Vision OCR 与原页视觉核验。

## 五、三模式门槛

沿用 `$research-capture` 的三种模式，防止把中间态当成果：

| 模式 | 允许动作 | 禁止 |
|---|---|---|
| 预览 | 读取、盘点、展示计划 | 写入知识库、下载文件 |
| 整理 | 建来源清单、摘要、`draft/pending_evidence` 草稿 | 宣称已学习或已验证 |
| 入库 | 来源记录、去重、冲突判断、可回查链接、应用转译 | 缺证据时写入 canonical |

## 六、证据等级（不得越级）

1. `扫描发现` — 目录里看到文件。不等于完整、不等于已转写。
2. `完整下载` — 文件字节完整（非 `.downloading`）。
3. `唯一内容` — SHA-256 去重后的独立内容。
4. `转写/OCR 完成` — 有可回查文字。**仍只是证据，不提升学习状态。**
5. `已学习` — 有稳定来源档案，且完成应用转译。
6. `canonical` — 通过核读、去重、冲突判断，进入方法地图。

转写产出落在第 4 级。要升到 5、6 级，必须人工逐段核读、关键画面核验、语义去重、应用转译。

## 七、与 Obsidian 互通

写入方向：

1. 原始内容先进 `01-收件箱/待合并`，记录来源、时间、原话、适用范围。
2. 核验后移入 `02-来源档案`，只追加新版本，不覆盖旧证据。
3. 提炼为 `04-知识卡片` 原子卡，一张卡只表达一个可判断真假的核心结论。
4. 链接到 `03-方法地图`；若改变稳定方法，修订方法地图并升 `revision`。
5. 更新[[更新日志]]，全库 `kb_revision` 加一。
6. 同步本 Skill 的检查点至该 `kb_revision`，并重跑 `resolve_kb.py` 校验。

回流方向：Skill 实战中产生的新知识，同样走上述流程进入 Obsidian，不在 Skill 内另存一份。

课程原词与执行归纳必须分开写，不把归纳伪装成讲师原话。
课程中的案例数字、平台规则、收益说法只作历史口径，不升级为当前事实或效果承诺。
安先生与薛辉两套课程分别归因，相似主题不等于相同方法。

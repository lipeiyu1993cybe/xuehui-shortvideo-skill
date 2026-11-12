# 短视频编导与薛辉体系 Skills

同一个仓库保存两套独立技能：现有的短视频编导运营执行体系，以及薛辉短视频体系 v3.4 参考资料。

| 技能 | 用途 | 入口 |
| --- | --- | --- |
| `short-video-director-system` | 产品与定位、选题、四类脚本、采访拍摄、剪辑、获客承接、经营复盘及课程蒸馏；可连接自己的 Obsidian 知识库 | [SKILL.md](skills/short-video-director-system/SKILL.md) |
| `xuehui-shortvideo` | 薛辉体系按主题编排的 13 册资料，配套总索引与冲突索引 | [SKILL.md](skills/xuehui-shortvideo/SKILL.md) · [原说明](skills/xuehui-shortvideo/README.md) |

## 目录

```text
skills/
├── short-video-director-system/
│   ├── SKILL.md
│   ├── README.md
│   ├── agents/
│   ├── references/
│   └── scripts/
└── xuehui-shortvideo/
    ├── SKILL.md
    ├── README.md
    ├── references/       # 13 册与 2 份索引
    └── assets/
```

原先仓库根目录的薛辉技能已整体移至 `skills/xuehui-shortvideo/`，历史提交保留。

## 安装到 Codex

```bash
git clone https://github.com/lipeiyu1993cybe/xuehui-shortvideo-skill.git
cd xuehui-shortvideo-skill
mkdir -p ~/.codex/skills
cp -R skills/short-video-director-system ~/.codex/skills/
# 需要独立使用薛辉参考技能时，再安装这一套：
cp -R skills/xuehui-shortvideo ~/.codex/skills/
```

安装前先检查目标目录；已有自定义内容时手动合并，避免覆盖。其他支持 Skills 的 Agent 可将对应技能目录复制到其技能目录；编导技能内部分命令固定使用 `~/.codex/skills/short-video-director-system/`，换用其他安装位置时需要同步调整路径。

## 使用

- 完成编导或账号运营任务：调用 `$short-video-director-system`。
- 专门检索薛辉资料：调用 `$xuehui-shortvideo`，先查总索引，按小节读取；涉及不同版本口径时先查冲突索引。
- 同时参考两套资料时，明确当前主技能，并分别标明课程原话、整理者归纳与项目建议。编导技能包含安先生和薛辉两套来源，课程归因需要分别保留。

## Obsidian 与脚本依赖

`short-video-director-system` 上传的是技能入口、内置离线参考、Agent 配置和课程处理脚本。个人 Obsidian 知识库、课程原件、完整转写、缓存、运行环境和知识源定位文件均不随仓库发布。技能里的知识版本号是原本地检查点，不代表安装后自动具备对应的动态知识库。

未连接知识库时，普通编导任务使用内置 `references/` 作为离线基线；动态知识问答、入库及同步需要配置自己的知识库。详情见[编导技能安装说明](skills/short-video-director-system/README.md)。

脚本保留现有版本，部分文档与辅助脚本含原机器的绝对路径，课程批处理工具还可能依赖 FFmpeg、OCR、语音识别环境或 `research-capture`；换机使用前按实际环境调整。阅读 Markdown 参考资料无需运行这些脚本。

## 来源与版本

打包日期：2026-11-12。来源、版权和版本边界见 [SOURCES.md](SOURCES.md)。薛辉资料中的覆盖率、时长和课程数据沿用原整理者声明，本次仅核对文件完整性。原技能正文保留，各自独立维护。

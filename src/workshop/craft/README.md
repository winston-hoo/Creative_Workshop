# 技法库

给正文生成与审稿提供写作技法储备。`craft.py` 只做**检索与预算控制**（按章号与本章指令挑几篇，
超预算的点名说明），不改写原文。

## 两个来源，外部优先

| 来源 | 位置 | 入库 | 进 exe |
|---|---|---|---|
| **内置**（本项目原创，12 篇） | 本目录 `src/workshop/craft/` | ✅ | ✅ |
| **外部包**（你自己收的） | 数据根下的 `craft/` | ❌ | ❌ |

数据根：源码方式是项目根目录（`workshop/craft/`），exe 是 `%LOCALAPPDATA%\创作工坊\craft`。
**外部包里的同名 `.md` 覆盖内置同名篇目**，所以想接别的技法库，放文件进去就行，不用改代码。

文件不在时 `read()` 返回空、生成与审稿少一段技法参考，其余功能不受影响。

## 内置 12 篇

| 文件 | 篇目 | 什么时候带上 |
|---|---|---|
| `chapter-guide.md` | 章节写作指南 | 每一章 |
| `hook-techniques.md` | 悬念钩子十法 | 每一章 |
| `emotion-curve.md` | 情绪曲线 | 每一章 |
| `plot-structures.md` | 爽文情节结构 | 每一章 |
| `golden-opening.md` | 黄金开篇 | 第 1 章、每卷第 1 章 |
| `dialogue-writing.md` | 对话写作规范 | 本章指令里出现对话迹象时 |
| `content-expansion.md` | 内容扩充技巧 | 目标字数写不够时 |
| `continuity.md` | 连贯性机制 | 有「需承接」内容时 |
| `character-building.md` | 人物塑造原则 | 本章有新人物登场时 |
| `prompt-guide.md` | 提示词完善指南 | 助手备用，不给正文生成 |
| `quality-checklist.md` | 质量检查清单 | 审稿时 |
| `review-dimensions.md` | 章节审查维度 | 审稿时 |

`CATALOG` 里的 `always` / `first_chapter` / `first_of_volume` / `on_dialogue` / `on_carry_over` /
`on_new_character` / `review_only` 决定选哪几篇，改选法看 `src/workshop/craft.py`。

**篇目少给会被点名。** 预算 10000 字（`DEFAULT_CRAFT_BUDGET`）装不下的篇目会在提示词末尾列出——
「这一章不需要对话规范」和「技法库超预算没给」是两回事，混成一句作者会去调高上限，
而调多高都改变不了「这一章没有对话」这个事实。上限取值只需满足一条：**最长的一次选择
（4 篇 `always` + 黄金开篇 + 对话规范 + 承接）必须整整齐齐装得下**，`tests/test_craft.py` 有断言兜住。

## 接外部技法包

把 `.md` 按上表的文件名放进数据根的 `craft/` 即可，本机生效、不进 git。

⚠️ **从别处收来的技法内容，版权归原作者。** 自己本机参考没问题；要是随项目分发或公开，
先确认许可——换一个来源不解决问题，SkillHub 上每个 Skill 页面都挂着同一句
「内容版权归原作者所有」。详见 `SOURCES.md`。

## 加自己的篇目

`CATALOG` 里加一条（`label` 必填，加个 `always` 或某个触发条件），再放同名 `.md`。就这么两步。

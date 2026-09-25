# 技法库（本目录的文件不入库）

这个目录是给**你自己**放写作技法参考的地方。程序按文件名读取（见
`src/workshop/craft.py`），文件不在就返回空字符串——功能降级，但不会报错。

## 为什么仓库里是空的

本目录原本收录的 14 份技法文档（章节写作指南、黄金开篇、悬念钩子、爽文结构、
情绪曲线、对话规范、人物塑造、质量清单、审查维度等）**不是本项目的原创内容**，
是从外部技能包原样收录的，来源与版权说明见本目录的 `SOURCES.md`。

那份技能的页面声明「内容版权归原作者所有」，本项目的定位是**本机自用参考**。
把第三方版权内容放进公开仓库需要先取得原作者许可，所以在 `v0.3.0` 发布时
把这些文件从仓库里排除了（`.gitignore` 里那条 `src/workshop/craft/*`）。

## 没有它们会怎样

| 功能 | 影响 |
|---|---|
| 逐章创作指令生成（`draft.py`） | 少一段「技法参考」前缀，其余照常 |
| 助手起草 / 审稿（`setting_assist.py`） | 少「审查维度与质量清单」那一段 |
| 校验、表单、标注、知识库、大纲、报告 | **不受影响** |

也就是说：没有技法库，工具仍然完整可用，只是生成正文时少一层写作参考。

## 想自己补上

按 `craft.py` 里认的文件名放 Markdown 即可，常用的几个：

```
chapter-guide.md      章节写作指南
golden-opening.md     黄金开篇
hook-techniques.md    悬念钩子
plot-structures.md    结构公式
emotion-curve.md      情绪曲线
dialogue-writing.md   对话规范
character-building.md 人物塑造
content-expansion.md  内容扩充
continuity.md         连贯性
prompt-guide.md       提示词指南
quality-checklist.md  质量检查清单
review-dimensions.md  审查维度
```

放进来就是本机生效，**不会被提交**（`craft/*` 已在 `.gitignore` 里）。
用别人写的材料前，请自行确认许可。

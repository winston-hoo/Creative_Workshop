# 开发日志 · 2026-09-24

> 本文件记录 2026-09-24 当天在 `workshop/` 目录完成的所有改动。
> 对应 git 提交：`5e4fc92` → `c170967` → `a38df58`（已推送 `origin/main`）。

---

## 一、设置页：第三方服务商 + 限速（提交 `5e4fc92`）

之前已推送，此处回归摘要：

- 服务商 CRUD：设置页可新增/编辑/删除 OpenAI 兼容服务商（中转站等），
  配置落盘 `custom-providers.yaml`（已加入 .gitignore），不重写带注释的 `providers.yaml`
- 「默认模型」绑定：任务没单独指定模型时回退到设置页选择的 provider/model
- 每分钟限速：滑动窗口限速器（`src/workshop/ratelimit.py`），按 RPM/TPM 在客户端排队放行，
  超时明确报错；429 读取 `Retry-After` 提示等待
- 实体页修复：失败块显示具体错误原因，可只重试失败的块；
  对不认 `response_format=json_object` 的中转站降级重试
- 实体生成接口补上 model 参数，实体信息区增加模型下拉选择

## 二、六大功能模块（提交 `c170967`）

| 模块 | 文件 | 说明 |
|---|---|---|
| 标注台（UI-P3） | `server/static/app.js`、`server/main.py`、`server/services.py` | 三栏布局（章节/正文/字段）、J/K 翻章、1-5 打分、Tab 切字段、空格确认、Ctrl+S 保存、? 帮助；置信度分级（高绿/中黄/低红）；人工修正保存接口 |
| 知识库 | `src/workshop/kb.py` | K1 实体卡片 / K2 人物·支线·时间线台账 / K3 作品指纹；纯本地计算，不调模型；`GET/POST /api/works/{name}/kb*` |
| 改写台（UI-P4） | `src/workshop/rewrite.py` | M3 重写（五类指令）+ M4 六类校验（长度/人称/风格/结构/锚点/伏笔）+ G2 输出闸门（有阻断问题拒绝接受）；原稿永存，改写出新版本 |
| 题材库（M6） | `src/workshop/genres.py` | 作品登记题材；同题材 ≥3 部才聚合规则，3-9 部置信度最高「中」、≥10 部可到「高」；`genres/` 运行时数据已加入 .gitignore |
| 对比分析（M8） | `src/workshop/compare.py` | 同题材 / 跨题材 / 作品 vs 基线偏离度三份报告，落盘 `compare/`（已忽略） |
| 融合器（M5） | `src/workshop/fusion.py` | 用自有素材（K1 实体）拼新故事骨架，每条素材带来源可追溯；落盘 `workspaces/_fusion/` |

对应测试：`tests/test_annotator.py`、`tests/test_kb.py`、`tests/test_rewrite.py`、`tests/test_genre_lab.py`。

## 三、UI 优化 / 归档恢复 / 数据依赖指引（提交 `a38df58`）

### 3.1 全站视觉统一（`server/static/style.css` 全量重写）
- 统一色彩/阴影/间距网格；卡片轻投影 + hover 微抬升
- 顶栏分组导航、表格斑马纹、按钮/表单统一交互态
- 进度条渐变、弹窗模糊遮罩 + 淡入动画

### 3.2 信息架构与使用说明
- 顶栏新增「使用说明」（`#/help`）：数据产出顺序 + 8 张功能卡（干什么/花不花钱/依赖/入口）+ 费用提示
- 书架空状态：图标 + 引导 + 主按钮
- 作品页新增「数据依赖指引」面板：标注→实体→大纲→知识库 各层状态与入口实时显示
- 所有子页面顶部返回键升级为按钮式胶囊（纯 CSS，箭头统一渲染）；标注数据表梗概不再截断

### 3.3 交互修复
- 修复 `askConfirm` 先移除 DOM 再 resolve 导致「点击确定后读不到表单值」的组件级 bug
- 「连已标注的也重跑」复选框直接内嵌进提示弹窗，未勾选时明确提示「没有发起调用」

### 3.4 归档恢复
- 书架页新增「归档区」：移出书架的作品可一键恢复（搬回 `workspaces/`，非复制）
- 同名保护：书架上已有同名时拒绝恢复而非覆盖
- 重名副本（`作品名.时间戳` 后缀）恢复时还原为原名
- 接口：`GET /api/archive`、`POST /api/archive/restore`；测试 `test_archive_restore_via_api`
- 修复：归档时间戳后缀是 `20260924T151222` 形态（含字母 T），`isdigit()` 误判，改正则匹配

### 3.5 脱敏
- `.gitignore` 追加 `custom-providers.yaml`、`genres/`、`compare/`
- 提交前扫描确认：密钥（.env/secrets.json）、真实作品数据（workspaces/）、源码本地路径均未入库

## 四、测试

提交前运行全部关键套件通过：`test_annotator`、`test_kb`、`test_rewrite`、`test_genre_lab`、
`test_server`、`test_ui_smoke`、`test_settings`、`test_batch`、`test_invariants`、`test_analysis`、
`test_ingest`、`test_ledger`、`test_outline`、`test_annotate`、`test_provider_admin`。

## 五、服务

- 开发验证：`python run_server.py --port 8800`（8765/8766 被本机其他服务占用，故换端口）
- 本机数据状态（在 `workspaces/` 下，不入库）：已完成逐章标注；实体统计与大纲尚未生成
  （需在作品页手动触发）
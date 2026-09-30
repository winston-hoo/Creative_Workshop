"""界面冒烟测试：用无头浏览器真实点一遍导入流程。

    python tests/test_ui_smoke.py

## 为什么需要它

接口测试打的是 HTTP 层，**抓不到前端状态机的 bug**。真实发生过两次「按钮点了没反应」：

  · 第一次：上传成功后全局忙碌标记没复位 → 「确认导入」被无声吞掉
  · 第二次：同一个标记还卡着 → 刷新前「开始切分预览」也跟着失效

两次的接口全对、页面看起来也正常（按钮是亮的、没有报错），只有真人点一下才会发现。

所以这个测试做三件接口测试做不到的事：

  1. **真的点按钮**，而不是直接调接口
  2. **在同一个页面会话里连做两遍完整流程**——全局状态没复位的 bug 只在第二遍暴露
  3. **收集页面上的 JS 错误**，任何未捕获异常都算失败

## ⚠️ 它驱动的是真实服务器 —— 两条铁律

**真实事故**：测试里「移出书架」点的是 `[data-archive]`（页面上第一个匹配），
某天书架上有用户的真实作品，第一个匹配就是**用户的作品**——测试把别人的书
移出了书架。（归档是移动不是删除，已恢复，但事故就是事故。）

所以本目录的测试遵守两条铁律，缺一不可：

  1. **不许假设书架为空** —— 用户随时可能有真实作品。能守的约定只有
     「测试自己造的作品，导入前不许出现」
  2. **UI 操作必须按测试作品的名字定位** —— `[data-archive="{测试作品}"]`、
     `locator(".work-card", has_text=...)`，绝不许「第一个」这类泛选器

另外整套跑在一块**临时根目录**上（`server_main.PATHS`），从根上不碰真实 `workspaces/`。
即便如此，第 2 条依然不能省：位置判据本身就是脆的，隔离只是多一层保险。
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import shutil  # noqa: E402
import tempfile  # noqa: E402

import uvicorn  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from server import main as server_main  # noqa: E402
from server import services  # noqa: E402
from workshop.creation import create_original_work, load_work_setting  # noqa: E402
from workshop.samples import build_synthetic_novel  # noqa: E402

app = server_main.app

TMP_ROOT = Path(tempfile.mkdtemp(prefix="workshop-ui-test-"))
server_main.PATHS = services.Paths(root=TMP_ROOT)
WS = TMP_ROOT / "workspaces"
for _name in ("providers.yaml", "primitives.yaml"):
    shutil.copy(ROOT / _name, TMP_ROOT / _name)

TEST_WORK = "界面冒烟作品"

_failures: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"  [通过] {label}")
    else:
        print(f"  [失败] {label}")
        _failures.append(label)


def launch_browser(pw) -> object:
    """起浏览器。

    playwright 升级后自带的 chromium 版本号会变，而下载一个 Chromium 很慢。
    这里先试默认，找不到就用本机上已有的 Chromium / Edge / Chrome——
    冒烟测试要验证的是界面逻辑，不是「playwright 装得对不对」。
    """
    try:
        return pw.chromium.launch()
    except Exception:  # noqa: BLE001
        pass

    candidates: list[str] = []
    root = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "") / ""
    bases = [
        Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")),
        Path.home() / "AppData" / "Local" / "ms-playwright",
    ]
    for base in bases:
        if base and base.exists():
            candidates += [str(p) for p in base.glob("chromium-*/chrome-win*/chrome.exe")]
            candidates += [str(p) for p in base.glob("chromium-*/chrome-win/chrome.exe")]
    candidates += [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for exe in candidates:
        if Path(exe).exists():
            try:
                return pw.chromium.launch(executable_path=exe)
            except Exception:  # noqa: BLE001
                continue
    raise RuntimeError("找不到可用的 Chromium，请先执行 playwright install chromium")


def close_browser(browser) -> None:
    """关浏览器，**不等它**。

    playwright 的收尾有两处会挂死，而且都跟被测代码无关：
      · 用 executable_path 起老版本 Chromium 时，`browser.close()` 不返回
      · `sync_playwright()` 上下文退出时会等浏览器进程死掉，同样可能一直等

    断言已经跑完了，不该让收尾把整个测试挂住。所以关的动作扔进守护线程，
    主流程立刻往下走；进程最后由 `os._exit()` 直接退出，浏览器由系统回收。
    """
    threading.Thread(target=_close_quietly, args=(browser,), daemon=True).start()


def _close_quietly(browser) -> None:
    try:
        browser.close()
    except Exception:  # noqa: BLE001
        pass


# Chromium 会拒连一批「不安全端口」（X11、FTP、IRC 那些老协议），6000 就在名单里。
# 随机取端口时踩到它，浏览器报 ERR_UNSAFE_PORT，而现象看起来像是服务没起来——
# 排查方向完全错。所以随机取完之后要避开这些端口。
_UNSAFE_PORTS = {
    1, 7, 9, 11, 13, 15, 17, 19, 20, 21, 22, 23, 25, 37, 42, 43, 53, 69, 77, 79,
    87, 95, 101, 102, 103, 104, 109, 110, 111, 113, 115, 117, 119, 123, 135, 137,
    139, 143, 161, 179, 389, 427, 465, 512, 513, 514, 515, 526, 530, 531, 532,
    540, 548, 554, 556, 563, 587, 601, 636, 989, 990, 993, 995, 1719, 1720, 1723,
    2049, 3659, 4045, 5060, 5061, 6000, 6566, 6665, 6666, 6667, 6668, 6669, 6697,
    10080,
}


def free_port() -> int:
    for _ in range(50):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if port not in _UNSAFE_PORTS:
            return port
    raise RuntimeError("连取 50 次端口都撞上了浏览器禁用端口")


def start_server() -> tuple[str, uvicorn.Server]:
    port = free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.time() + 20
    while not server.started and time.time() < deadline:
        time.sleep(0.1)
    if not server.started:
        raise RuntimeError("服务未能启动")
    return f"http://127.0.0.1:{port}", server


def cleanup() -> None:
    shutil.rmtree(WS / TEST_WORK, ignore_errors=True)
    archive = WS / "_archive"
    if archive.exists():
        for p in archive.glob(f"{TEST_WORK}*"):
            shutil.rmtree(p, ignore_errors=True)


def run_import_flow(page, sample_file: Path, tag: str) -> None:
    """走一遍完整导入。tag 用于区分第一遍和第二遍的日志。"""
    page.click('a[href="#/import"]')

    # 停在 #/import 时再点同一个标签不会触发 hashchange，所以这里等的是「新鲜的第 1 步」：
    # 文件选择区在、作品名输入框是空的。上一步若是完成页，这两条都不成立。
    page.wait_for_selector("#drop", timeout=20000)
    check(
        page.input_value("#workname") == "",
        f"[{tag}] 进入的是全新的第 1 步（作品名为空）",
    )

    # 选文件：直接塞给 file input。真实用户是拖拽或点选，但落到代码里都是这一条
    page.set_input_files("#file", str(sample_file))
    page.wait_for_function("() => !document.getElementById('go').disabled", timeout=20000)
    check(True, f"[{tag}] 选文件后按钮变为可用")

    # 作品名会自动填成文件名，改成固定值方便断言
    page.fill("#workname", TEST_WORK)

    page.click("#go")
    # 第 2 步：切分预览。
    # 不能用 text=切分预览 等待——步骤条上第 1 步就写着这三个字，会立刻误命中。
    # #commit 只在第 2 步存在，是唯一可靠的信号。
    page.wait_for_selector("#commit", timeout=60000)
    body = page.inner_text("#view")
    check("识别章节" in body, f"[{tag}] 进入切分预览页")
    check("完整性核验通过" in body, f"[{tag}] 显示完整性核验通过")

    # 确认导入 → 第 3 步
    page.click("#commit")
    page.wait_for_selector("text=导入完成", timeout=60000)
    check("导入完成" in page.inner_text("#view"), f"[{tag}] 进入导入完成页")


def test_ui() -> None:
    base, server = start_server()
    sample = Path(tempfile.mkdtemp(prefix="smoke-")) / "合成测试.txt"
    sample.write_text(build_synthetic_novel(), encoding="utf-8")

    cleanup()
    console_errors: list[str] = []
    page_errors: list[str] = []

    try:
        # 不用 `with sync_playwright()`：它的退出会等浏览器进程死掉，
        # 老版本 Chromium 上这一步会一直等不到。连接句柄不主动释放，
        # 反正进程最后是 os._exit() 走的。
        pw = sync_playwright().start()
        browser = launch_browser(pw)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        # 带上出错的 URL。只记文本的话，「favicon 404」看起来和真错误一模一样，
        # 因为消息里根本不含地址——过滤条件写不出来。
        page.on(
            "console",
            lambda m: console_errors.append(
                f"{m.text} @ {(m.location or {}).get('url', '')}"
            )
            if m.type == "error"
            else None,
        )
        page.on(
            "pageerror",
            lambda e: page_errors.append(str(e)),
        )

        print("打开工作台")
        page.goto(base, wait_until="networkidle")
        check("小说创作工坊" in page.title(), "页面标题正确")
        check("服务运行中" in page.inner_text("#conn"), "顶栏显示服务运行中")
        # 书架上可能有用户正在使用的真实作品——不许假设它是空的。
        # 能守的约定是：测试作品在导入前绝不能出现。
        check(
            TEST_WORK not in page.inner_text("#view"),
            "导入前书架上没有测试作品",
        )

        print("\n第一遍导入")
        run_import_flow(page, sample, "第一遍")

        print("\n确认作品出现在书架")
        page.click('a[href="#/shelf"]')
        page.wait_for_selector(".work-card", timeout=20000)
        card = page.inner_text(".work-grid")
        check(TEST_WORK in card, "书架出现新作品")
        check("8 章" in card, "章数正确显示")

        print("\n第二遍导入（同一个页面会话）")
        # 这是关键回归：全局忙碌标记没复位的 bug 只在第二遍暴露——
        # 第一遍成功后标记卡在 true，之后所有按钮都被静默吞掉。
        run_import_flow(page, sample, "第二遍")

        print("\n作者工作区")
        page.click('a[href="#/shelf"]')
        page.wait_for_selector(".work-card", timeout=20000)
        # 必须点测试作品自己的卡片。书架上还有真实作品，
        # 点「第一张卡片」等于在用户的书架上乱翻——这个错真犯过。
        page.locator(".work-card", has_text=TEST_WORK).locator(
            'a[href^="#/work/"]'
        ).click()
        page.wait_for_selector("text=完整性核验", timeout=20000)
        detail = page.inner_text("#view")
        check("逐字一致" in detail, "作品概览显示重建校验通过")
        check("章节列表" in detail, "概览含章节列表")
        # 作品页上不止一个 .table-wrap（上面那张「完整性核验」的问题表也是），
        # 直接读第一个会读到那张表——「章节列表有数据」就是这么间歇性红的。
        # 所以要等**带章节链接的那张**表，并且只读它。
        chapter_table = page.locator(".table-wrap", has=page.locator('a[href*="/chapter/"]'))
        chapter_table.wait_for(timeout=20000)
        # 失败时把看到的原文带出来：只写「没数据」等于让人再去复现一次
        rows = chapter_table.inner_text()
        check("v001-c0001" in rows, f"章节列表有数据（实际 {rows[:60]!r}）")

        print("\n章节阅读")
        # 点章节标题进阅读页。这一步验证的是「列表和正文是通的」——
        # 接口单独测过了，这里要确认的是前端真的把链接接上了。
        page.click('.table-wrap a[href*="/chapter/"]')
        page.wait_for_selector(".prose", timeout=20000)
        chapter = page.inner_text("#view")
        check("正文" in chapter, "进入章节阅读页")
        check("夜行" in chapter or len(page.inner_text(".prose")) > 50, "正文渲染出来了")
        check("下一章" in chapter, "有翻章按钮")
        check("还没有标注" in chapter, "没标注时明说了还没标注")

        # 方向键翻章：看整本书时点按钮太慢
        page.keyboard.press("ArrowRight")
        page.wait_for_function(
            "() => location.hash.includes('c0002')", timeout=20000
        )
        check("c0002" in page.url, "方向键翻到下一章")

        print("\n返回作品页")
        page.goto(base + "#/work/" + TEST_WORK, wait_until="networkidle")
        page.wait_for_selector("#task-body", timeout=20000)

        print("\n模型任务区")
        # 这一块最容易出的错是「进页面就发起调用」。它必须只出计划。
        page.wait_for_selector("#task-body", timeout=20000)
        page.wait_for_function(
            "() => document.getElementById('task-body')"
            " && document.getElementById('task-body').innerText.includes('待跑')",
            timeout=20000,
        )
        task = page.inner_text("#task-body")
        check("待跑" in task, "任务区显示了待跑章数")
        check("成本估算" in task, "任务区有成本估算")
        check("试跑 20 章" in task, "有试跑按钮")
        check("全书大纲" in task, "任务区有全书大纲入口")
        check(
            (WS / TEST_WORK / "10-annotations").exists() is False,
            "进页面不产生任何标注（模型调用必须显式）",
        )

        # 没跑过标注时点「生成大纲」不该发起调用，而要明说缺原料
        page.click("#outline-run")
        page.wait_for_selector(".modal", timeout=10000)
        check("先跑标注" in page.inner_text(".modal"), "没梗概时明说缺原料，不硬跑")
        page.click("#m-ok")
        page.wait_for_selector(".modal", state="detached", timeout=10000)

        print("\n大纲页（还没生成过）")
        page.goto(base + "#/work/" + TEST_WORK + "/outline", wait_until="domcontentloaded")
        page.wait_for_selector("text=还没有生成过大纲", timeout=20000)
        check(True, "没生成过时给出明确指引，而不是空白页")
        page.goto(base + "#/work/" + TEST_WORK, wait_until="domcontentloaded")
        page.wait_for_selector("#task-body", timeout=20000)

        print("\n确认框必须是页面内的，不能用原生 confirm()")
        # 「点了没反应」的回归：sandbox 里没有 allow-modals 的 iframe 会把原生
        # confirm() **静默变成 false** —— 不弹窗、不报错、不发请求。
        # 内置预览面板就是这么渲染页面的，用户点「试跑 20 章」才会毫无反馈。
        dialog_types: list[str] = []
        page.on("dialog", lambda d: (dialog_types.append(d.type), d.dismiss()))

        page.click("#trial")
        page.wait_for_selector(".modal", timeout=10000)
        modal = page.inner_text(".modal")
        check("服务商" in modal, "确认框里给出了服务商与模型")
        check("范围" in modal, "确认框里给出了范围")
        check("预估费用" in modal or "未填写单价" in modal, "确认框里给出了成本")
        check("不能撤销" in modal, "确认框里写明了不可撤销")
        check(not dialog_types, "没有使用原生对话框")

        page.click("#m-cancel")
        page.wait_for_selector(".modal", state="detached", timeout=10000)
        check(True, "取消后确认框关闭")
        check(
            not (WS / TEST_WORK / "10-annotations").exists(),
            "取消后确实没有发起调用、没有产生标注",
        )

        print("\n同一个页面放进 sandbox iframe 里也必须能弹确认框")
        # 直接复现用户遇到的环境：内置预览面板用 iframe 渲染页面，
        # sandbox 不带 allow-modals。原生 confirm() 在这里会静默返回 false——
        # 这就是「点击试跑 20 章没有任何反馈」的根因。
        try:
            page.evaluate(
                """(url) => {
                    const f = document.createElement('iframe');
                    f.id = 'sandbox-probe';
                    f.setAttribute('sandbox', 'allow-scripts allow-same-origin');
                    f.src = url;
                    f.style.cssText = 'width:1270px;height:900px;border:0';
                    document.body.appendChild(f);
                }""",
                f"{base}/#/work/{TEST_WORK}",
            )
            inner = page.frame_locator("#sandbox-probe")
            inner.locator("#task-body").wait_for(timeout=20000)
            inner.locator("#trial").click()
            inner.locator(".modal").wait_for(timeout=10000)
            text = inner.locator(".modal").inner_text()
            check("不能撤销" in text, "沙箱 iframe 里确认框照常出现（不再被静默吃掉）")
            inner.locator("#m-cancel").click()
            inner.locator(".modal").wait_for(state="detached", timeout=10000)
        except Exception as exc:  # noqa: BLE001
            check(False, f"沙箱 iframe 里点试跑无反馈：{type(exc).__name__}: {exc}")
        finally:
            # 探针 iframe 得拆掉。它挂着一个长连接，留着的话后面所有
            # `wait_until="networkidle"` 都永远等不到——排查起来很费时间，
            # 因为现象是「跳转超时」，跟 iframe 看起来毫无关系。
            page.evaluate("() => document.getElementById('sandbox-probe')?.remove()")

        print("\n体检报告页")
        page.goto(base + "#/work/" + TEST_WORK, wait_until="domcontentloaded")
        page.wait_for_selector('a[href$="/report"]', timeout=20000)
        page.click('a[href$="/report"]')
        page.wait_for_selector("text=结构体检报告", timeout=30000)
        report = page.inner_text("#view")
        check("章节长度" in report, "报告含章节长度")
        check("伏笔追踪" in report, "报告含伏笔追踪")
        check("还没有任何标注" in report, "零标注时明说了还没有标注")
        check(page.locator("#view svg").count() > 0, "报告画出了 SVG 图表")

        print("\n设置页")
        page.click('a[href="#/settings"]')
        page.wait_for_selector("text=模型服务商", timeout=20000)
        page.wait_for_selector("[data-test]", timeout=20000)
        st = page.inner_text("#view")
        check("DeepSeek" in st, "列出了服务商")
        check("测试连接" in st, "有测试连接按钮")
        check("预算闸门" in st, "显示了预算闸门")
        check("定价与实测" in st, "显示了定价区")
        check("添加服务商" in st, "有添加服务商入口")
        check("默认模型" in st, "有默认模型选择")

        # 密钥输入默认收起，展开后必须是 password 且永远是空的——绝不回显已存的密钥
        page.click('[data-toggle-key="deepseek"]')
        page.wait_for_selector("#key-value-deepseek", timeout=10000)
        check(
            page.get_attribute("#key-value-deepseek", "type") == "password",
            "密钥输入框是密码类型",
        )
        check(page.input_value("#key-value-deepseek") == "", "输入框不回显已有密钥")
        page.click('[data-toggle-key="deepseek"]')

        print("\n创作台 · 设定集表单的下拉")
        # 创作台原来没有一个浏览器级的检查，于是「select 忘了写 options」这种错
        # 一直没人拦：下拉点开是空的，而代码路径、接口、保存全都正常。
        # 作者报的就是这个——设定集的「视角」下拉空白，选不出任何东西（2026-09-30）。
        CREATION_WORK = "界面冒烟原创"
        create_original_work(WS, CREATION_WORK, "科幻", protagonist="李默", core_motive="找出事故的真相")
        page.goto(f"{base}/#/work/{CREATION_WORK}/creation/setting", wait_until="networkidle")
        page.wait_for_selector('select[data-set="style.perspective"]', timeout=20000)
        options = page.eval_on_selector_all(
            'select[data-set="style.perspective"] option', "els => els.map((e) => e.textContent)"
        )
        check(options[1:] == ["第一人称", "第三限知", "第三全知"],
              f"视角下拉有三个选项（实际 {options}）")
        page.select_option('select[data-set="style.perspective"]', "第三限知")
        page.click('button:has-text("保存设定集")')
        # 选了能存下去：校验器只认这三个词，选项与它一旦不一致，保存就会被拦住
        page.wait_for_timeout(1200)
        check(
            (load_work_setting(WS / CREATION_WORK) or {}).get("style", {}).get("perspective")
            == "第三限知",
            "选中的视角存进了 setting.yaml",
        )

        print("\n创作台 · 指令没落盘，写不成正文")
        # 作者报「无法进行创作」（2026-09-30）：逐章指令的表单是满的——标题、一句话概要、
        # 核心情节都在——因为那是后端拿卷表和设定集**拼**出来的骨架，磁盘上根本没有这个文件。
        # 点「写这一章」被拦下，报「还没有 v001-c0001 的创作任务指令」。
        # 两边都不算错，错的是没人把「这份还没落盘」说出来。
        (WS / CREATION_WORK / "70-volume" / "volume-001.yaml").write_text(
            "schema_version: volume-v1\n"
            f"work: {CREATION_WORK}\n"
            "vol:\n"
            "  vol: 1\n"
            "  title: 第一卷\n"
            "  start_chapter: 1\n"
            "  end_chapter: 2\n"
            "  chapters:\n"
            "    - chapter_no: 1\n"
            "      title: 第一次上机\n"
            "      gist: 李默第一次进试验舱做兼容性测试\n"
            "      characters: [李默]\n"
            "    - chapter_no: 2\n"
            "      title: 隔着一层雾\n"
            "      gist: 别人看得很清楚，他总觉得隔着一层雾\n"
            "      characters: [李默]\n",
            encoding="utf-8",
        )
        brief_file = WS / CREATION_WORK / "80-brief" / "v001-c0001.yaml"
        check(not brief_file.exists(), "这一章本来没有指令文件")
        page.goto(f"{base}/#/work/{CREATION_WORK}/creation/brief/v001-c0001",
                  wait_until="networkidle")
        page.wait_for_selector("#creation-draft-host", timeout=20000)
        # 计划是异步取的，卡片先写「正在算计划…」。不等它落地就断言，读到的是上一次的状态。
        _settled = ("() => !document.querySelector('#creation-draft-host')"
                    ".innerText.includes('正在算计划')")
        page.wait_for_function(_settled, timeout=20000)
        check("还没建文件" in page.inner_text("#draft-flag"), "工具栏明说这份还没落盘")
        check(page.is_disabled("#draft-go"), "没落盘时「写这一章」是灰的")
        check(page.is_visible("#draft-save-brief"), "卡片里就地给了「保存本章指令」")
        page.click("#draft-save-brief")
        page.wait_for_selector("#draft-save-brief", state="detached", timeout=20000)
        page.wait_for_function(_settled, timeout=20000)
        check("还没有" not in page.inner_text("#creation-draft-host"), "存完不再报缺指令")
        check(not page.is_disabled("#draft-go"), "存完「写这一章」能点了")
        check(brief_file.exists(), "指令文件真的落了盘")

        print("\n移出书架（只许移测试作品自己）")
        page.click('a[href="#/shelf"]')
        page.wait_for_selector(f'[data-archive="{TEST_WORK}"]', timeout=20000)
        page.click(f'[data-archive="{TEST_WORK}"]')
        page.wait_for_selector(".modal", timeout=20000)
        check("不是被删除" in page.inner_text(".modal"), "确认框说清了是移动不是删除")
        page.click("#m-ok")
        # 书架上可能还有别人的作品，不能等「还没有导入作品」；
        # 等的是测试作品自己的按钮消失。
        page.wait_for_selector(
            f'[data-archive="{TEST_WORK}"]', state="detached", timeout=20000
        )
        check(True, "确认后作品被移出书架")
        check(
            (WS / "_archive" / TEST_WORK).exists(),
            "归档目录里能找到它（移动而非删除）",
        )

        close_browser(browser)
    finally:
        server.should_exit = True
        time.sleep(0.5)
        cleanup()
        shutil.rmtree(TMP_ROOT, ignore_errors=True)

    print("\n页面错误检查")
    check(not page_errors, f"没有未捕获的 JS 异常（{page_errors[:2]}）")
    # favicon 404 之类的噪音不算错误
    real_console = [e for e in console_errors if "favicon" not in e.lower()]
    check(not real_console, f"没有 console 错误（{real_console[:2]}）")


def main() -> int:
    print("=" * 58)
    print("界面冒烟测试（无头浏览器）")
    print("=" * 58)
    print()

    try:
        test_ui()
    except Exception as exc:  # noqa: BLE001
        print(f"\n测试执行中断：{type(exc).__name__}: {exc}")
        _failures.append(f"执行中断：{exc}")

    print()
    print("=" * 58)
    if _failures:
        print(f"未通过 {len(_failures)} 项：")
        for item in _failures:
            print(f"  · {item}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    # 直接退出进程，不走常规的解释器收尾。
    # 原因有两个，都是 playwright 的收尾噪音而不是本项目的错误：
    #   · 用 executable_path 起的老版本 Chromium 时 browser.close() 会卡住不返回
    #   · 即便返回了，也会抛 "Task was destroyed but it is pending" 把一份
    #     **全部通过**的报告弄得像崩了一样
    # 断言早已跑完，临时目录也已显式清理，这里没必要再等它。
    os._exit(code)

"""设定集助手自检。不需要密钥、不联网。

直接跑：python tests/test_setting_assist.py

重点是 `apply_proposals`——这条链上唯一会写设定集的地方。
模型说什么都不能直接信，所以每条断言都对应一种「模型会胡说」的形态：

  · 动了白名单以外的小节（往设定集里塞别的东西）
  · add 一个已经存在的角色（换个写法再加一遍）
  · update 一个不存在的角色（凭空改）
  · 缺主键 / 条目不是对象 / value 是空的
  · 人物缺 name·role·motive（半个人物进了设定集，后面全歪）
  · 多塞了白名单外的字段（悄悄污染设定集）
  · 一次提一大堆（作者只要三个，它给二十个）

以及「不能静默」：没勾的那几条必须出现在逐条结果里，并说明是没勾，不是没生效。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from workshop.setting_assist import (  # noqa: E402
    ASSIST_SYSTEM,
    LAYER_SPECS,
    SECTION_SPECS,
    apply_proposals,
    assist_system,
    build_assist_plan,
    build_refs_block,
    build_user_prompt,
    describe_proposal,
    kb_proposals,
    kb_relations_proposals,
)

_failures: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"  [通过] {label}")
    else:
        print(f"  [失败] {label}")
        _failures.append(label)


def _base() -> dict:
    return {
        "schema_version": "setting-v1",
        "work": "关山灯",
        "genre": "玄幻",
        "logline": "一句话",
        "core_motive": "活着回去",
        "style": {"perspective": "第三限知", "tone": "", "taboo": ["不用破折号"]},
        "world": {"era": "", "places": [{"name": "青云宗", "note": "第一大宗"}], "rules": []},
        "power": {"system": "修为", "tiers": ["炼气"]},
        "factions": [{"name": "青云宗", "stance": "表面中立", "leader": "玄阳真人"}],
        "characters": [
            {"name": "李默", "role": "主角", "motive": "活着回去", "identity": "穿越者"},
        ],
        "terms": [{"term": "数值之眼", "meaning": "能看到修为数值"}],
        "themes": [],
    }


def test_add_is_whitelisted() -> None:
    print("提案应用 · 只会动白名单里的位置")

    setting, results = apply_proposals(
        _base(),
        [
            {"section": "characters", "op": "add",
             "entry": {"name": "苏清月", "role": "重要配角", "motive": "守住宗门",
                       "identity": "大师姐", "abilities": ["水龙虚影"]}},
            {"section": "secrets", "op": "add", "entry": {"name": "不要的东西"}},
            {"section": "characters", "op": "add", "entry": {"name": "无角色定位"}},
        ],
    )
    check(results[0]["ok"], "合法人物被接受")
    check(any(c["name"] == "苏清月" for c in setting["characters"]), "进了设定集")
    check(setting["characters"][1]["abilities"] == ["水龙虚影"], "数组字段保留")

    check(not results[1]["ok"], "白名单外的小节被拒")
    check("不认识的小节" in results[1]["reason"], f"并说明了原因（{results[1]['reason']}）")
    check("secrets" not in setting, "被拒的小节没有进设定集")

    check(not results[2]["ok"], "缺 role/motive 的人物被拒")
    check("缺必填字段" in results[2]["reason"], f"原因是缺必填（{results[2]['reason']}）")
    check(len(setting["characters"]) == 2, "只进了一个人")


def test_duplicate_and_update() -> None:
    print("提案应用 · 重名与就地修改")

    setting, results = apply_proposals(
        _base(),
        [
            {"section": "characters", "op": "add", "entry": {"name": "李默", "role": "主角", "motive": "x"}},
            {"section": "characters", "op": "update", "entry": {"name": "李默", "personality": "嘴上认怂"}},
            {"section": "characters", "op": "update", "entry": {"name": "查无此人", "personality": "x"}},
        ],
    )
    check(not results[0]["ok"] and "已经存在" in results[0]["reason"], "重名 add 被拒并提示用 update")
    check(len(setting["characters"]) == 1, "重名没有产生第二个人")

    check(results[1]["ok"], "update 成功")
    check(setting["characters"][0]["personality"] == "嘴上认怂", "字段被改到")
    check(setting["characters"][0]["identity"] == "穿越者", "没写的字段保持原样，没被清空")
    check(not results[2]["ok"] and "找不到" in results[2]["reason"], "update 不存在的条目被拒")

    # remove 单独一批：同一个批次里删除再断言前面的改动，会读到删除后的状态
    after, results2 = apply_proposals(
        setting, [{"section": "characters", "op": "remove", "entry": {"name": "李默"}}]
    )
    check(results2[0]["ok"], "remove 成功")
    check(not after["characters"], "删除后人物列表为空")


def test_strip_unknown_fields() -> None:
    """模型多塞的字段不能进设定集。

    「多给了一个字段」看着无害，但它是静默污染：等发现时已经写了几十章，
    而设定集里那个字段谁也没定义过。
    """
    print("提案应用 · 白名单外的字段一律丢掉")

    setting, results = apply_proposals(
        _base(),
        [
            {"section": "factions", "op": "add",
             "entry": {"name": "魔宗", "stance": "邪道", "leader": "魔头",
                       "secret_plan": "统治世界", "成员": ["甲", "乙"]}},
        ],
    )
    check(results[0]["ok"], "势力被接受")
    added = next(f for f in setting["factions"] if f["name"] == "魔宗")
    check("secret_plan" not in added, "白名单外的字段没进去")
    check("成员" not in added, "中文杂字段也没进去")
    check(added["stance"] == "邪道", "白名单内的字段正常")


def test_strlist_and_scalar() -> None:
    print("提案应用 · 字符串数组与单个字段")

    setting, results = apply_proposals(
        _base(),
        [
            {"section": "rules", "op": "add", "value": "查克拉用尽会昏迷"},
            {"section": "rules", "op": "add", "value": "查克拉用尽会昏迷"},
            {"section": "rules", "op": "remove", "value": "本来就没有这条"},
            {"section": "tiers", "op": "add", "value": "筑基"},
            {"section": "logline", "op": "set", "value": "新的前提"},
            {"section": "core_motive", "op": "add", "value": "数组动作用在单字段上"},
            {"section": "themes", "op": "add", "value": ""},
            {"section": "terms", "op": "add", "entry": {"meaning": "没写术语名"}},
        ],
    )
    check(results[0]["ok"], "规则 add 成功")
    check(not results[1]["ok"] and "已经在里面" in results[1]["reason"], "重复规则被拒")
    check(not results[2]["ok"] and "本来就不在" in results[2]["reason"], "remove 不存在的条目被拒")
    check(setting["power"]["tiers"] == ["炼气", "筑基"], "等级按顺序追加")
    check(setting["logline"] == "新的前提", "单字段 set 成功")
    check(not results[5]["ok"] and "只能用 set" in results[5]["reason"], "数组动作用在单字段上被拒")
    check(not results[6]["ok"] and "value 是空的" in results[6]["reason"], "空值被拒")
    check(not results[7]["ok"] and "缺主键" in results[7]["reason"], "术语缺 term 被拒")
    check(all(r["ok"] or r["reason"] for r in results), "每一条都有结果或原因，没有静默")


def test_unchecked_are_reported() -> None:
    """作者只勾了两条，其余必须出现在结果里并说明「没勾」。"""
    print("提案应用 · 没勾的条目也要留痕")

    setting, results = apply_proposals(
        _base(),
        [
            {"section": "themes", "op": "add", "value": "活着本身就是本事"},
            {"section": "themes", "op": "add", "value": "不要这条"},
        ],
        accepted=[0],
    )
    check(setting["themes"] == ["活着本身就是本事"], "只应用勾中的那条")
    check(len(results) == 2, "两条都出现在结果里")
    check(results[0]["ok"], "勾中的那条 ok")
    check(not results[1]["ok"] and "没勾选" in results[1]["reason"], f"没勾的说明原因（{results[1]['reason']}）")


def test_proposals_do_not_mutate_input() -> None:
    print("提案应用 · 不改原对象")

    original = _base()
    import copy

    snapshot = copy.deepcopy(original)
    apply_proposals(original, [{"section": "themes", "op": "add", "value": "x"}])
    check(original == snapshot, "原设定集没被就地改掉（界面草稿才该变）")


def test_plan_is_free_and_honest() -> None:
    print("助手的计划：先给数字再花钱")

    plan = build_assist_plan(
        work="关山灯", setting=_base(), refs_block="", refs=[],
        provider_id="deepseek", model_id="deepseek-flash",
        price_input_per_mtok=1.0, price_output_per_mtok=4.0,
    )
    check(plan.est_input_tokens > 0, "算出了输入 token")
    check(plan.est_cost_cny is not None and plan.est_cost_cny >= 0, "算出了费用")
    check(any("不注入任何已入库作品的素材" in w for w in plan.warnings),
          f"没选参照时明确说明是纯原创（{plan.warnings}）")
    check(plan.to_dict()["ready"], "有计划就 ready")

    no_price = build_assist_plan(
        work="x", setting=_base(), refs_block="", refs=[], provider_id="p", model_id="m",
    )
    check(no_price.est_cost_cny is None and "无法计算" in no_price.price_note,
          "没填单价时明说算不出，不给假数字")


def test_refs_block_carries_real_material() -> None:
    """参照块要给**素材**，不是只给名字。

    只喂一串人名等于没喂：作者要的是「这个人什么角色、这个势力什么立场、
    这套能力怎么分层」，那些才借得动。名字本身借不了任何东西。
    """
    print("参照素材 · 给的是素材不是名字")

    refs = [{
        "work": "某书",
        "k3": ["章末钩子强度 均值 3.42（545 章）"],
        "k1": {
            "characters": [
                {"name": "路人甲", "role": "路人", "identity": "打酱油的", "first_chapter": 300},
                {"name": "李默", "role": "主角", "identity": "穿越者，外门杂役", "first_chapter": 1},
                {"name": "苏清月", "role": "重要配角", "identity": "内门大师姐", "first_chapter": 5},
            ],
            "factions": [{"name": "青云宗", "stance": "表面中立，内部按修为分层"}],
            "abilities": [{"name": "数值之眼", "effect": "能看到目标的修为数值"}],
            "locations": [{"name": "柴房"}],
            "relations": [{"type": "上下级"}, {"type": "上下级"}, {"type": "敌对"}],
        },
    }]
    block = build_refs_block(refs, level="full")
    check("李默（主角）：穿越者，外门杂役" in block, f"人物带定位与身份（{block[:80]}…）")
    check("青云宗：表面中立，内部按修为分层" in block, "势力带立场")
    check("数值之眼：能看到目标的修为数值" in block, "能力带效果")
    check("钩子强度" in block, "结构指纹在里面")
    check("上下级 2" in block, "关系格局按类型统计")
    # 主角要排在路人前面——按角色优先级排，别让一个路人在第 41 条被截掉而撑不住重点
    check(block.index("李默（主角）") < block.index("路人甲"), "主角排在路人前面")


def _volume() -> dict:
    return {
        "schema_version": "volume-v1",
        "vol": {
            "vol": 1, "title": "灯下黑", "start_chapter": 1, "end_chapter": 6,
            "era": "天元三百年", "core_conflict": "零层杂役怎么活下来",
            "goal": "在柴房里站住脚", "closing": "",
            "parts": [{"title": "入宗", "start_chapter": 1, "end_chapter": 3, "gist": "被塞进柴房"}],
            "chapters": [
                {"chapter_no": 1, "title": "数值", "gist": "看到别人的修为数值",
                 "characters": ["李默"], "foreshadow": "埋设 v001-c0004-f01"},
                {"chapter_no": 2, "title": "零层", "gist": "零层意味着什么"},
            ],
        },
    }


def test_volume_layer() -> None:
    """分卷目录这一层：章号必须是整数，这是最容易悄悄坏掉的地方。

    章号被 str() 掉变成 "7"，卷表的连号校验当场就崩，而报出来的错会是
    「缺 start_chapter」这类看着毫不相干的话——很难查回真正的原因。
    """
    print("分卷目录层 · 章号必须是整数")

    setting, results = apply_proposals(
        _volume(),
        [
            {"section": "chapters", "op": "add",
             "entry": {"chapter_no": 3, "title": "规矩", "gist": "柴房的名册与月俸",
                       "characters": ["李默", "周砚"]}},
            {"section": "chapters", "op": "add", "entry": {"chapter_no": "4", "title": "苏清月"}},
            {"section": "chapters", "op": "add", "entry": {"title": "没写章号"}},
            {"section": "chapters", "op": "add", "entry": {"chapter_no": 1, "title": "章号重复"}},
            {"section": "core_conflict", "op": "set", "value": "零层杂役在名册与月俸之间活下来"},
            {"section": "characters", "op": "add", "entry": {"name": "周砚", "role": "导师", "motive": "x"}},
        ],
        layer="volume",
    )
    chapters = setting["vol"]["chapters"]

    check(results[0]["ok"], "新增一章成功")
    check(chapters[2]["chapter_no"] == 3, f"chapter_no 是整数 3（实际 {chapters[2]['chapter_no']!r}）")
    check(isinstance(chapters[2]["chapter_no"], int), "类型真的是 int，不是 str")
    check(chapters[2]["characters"] == ["李默", "周砚"], "出场人物数组保留")

    check(results[1]["ok"] and chapters[3]["chapter_no"] == 4,
          f"字符串章号「4」被转成整数（实际 {chapters[3]['chapter_no']!r}）")
    check(isinstance(chapters[3]["chapter_no"], int), "转过来的也是 int")

    check(not results[2]["ok"] and "chapter_no" in results[2]["reason"],
          f"没写章号被拒（{results[2]['reason']}）")
    check(not results[3]["ok"] and "已经存在" in results[3]["reason"], "章号重复被拒")
    check(results[4]["ok"] and setting["vol"]["core_conflict"].startswith("零层杂役在名册"),
          "卷头字段可以 set")
    check(not results[5]["ok"] and "不认识的小节" in results[5]["reason"],
          "把设定集的小节名用到卷层上会被拒（两层的白名单是分开的）")
    check(len(chapters) == 4, f"只进了两章（实际 {len(chapters)}）")


def test_brief_layer_foreshadow_enum() -> None:
    """逐章指令这一层：伏笔动作只能是埋设/推进/回收。

    注意「回收」不是改这一章里已有的那条，而是**新加一行**指向前面埋设过的 id。
    所以本章的伏笔表里不该有同一个 id 两次——那才是真的重复登记。
    """
    print("逐章指令层 · 伏笔动作是枚举")

    brief = {
        "schema_version": "brief-v1", "chapter_id": "v001-c0007", "title": "登门",
        "one_line": "", "core_plot": ["李默第一次进内门"],
        "narrative": {"perspective": "第三限知", "tone": "", "focus": []},
        "settings": {"characters": [], "factions": [], "terms": []},
        "foreshadow": [{"action": "埋设", "id": "v001-c0007-f01", "desc": "内门的名册是假的"}],
        "carry_over": [], "must_not": [],
    }
    out, results = apply_proposals(
        brief,
        [
            # 回收第 1 章埋的那条：本章第一次出现，所以是 add
            {"section": "foreshadow", "op": "add",
             "entry": {"action": "回收", "id": "v001-c0001-f01", "desc": "玉佩原来是她母亲的"}},
            {"section": "foreshadow", "op": "add",
             "entry": {"action": "埋伏", "id": "v001-c0008-f01", "desc": "动作名乱写"}},
            {"section": "foreshadow", "op": "add",
             "entry": {"action": "埋设", "id": "v001-c0007-f01", "desc": "同一个 id 再来一次"}},
            {"section": "core_plot", "op": "add", "value": "李默第一次进内门"},
            {"section": "characters", "op": "add", "value": "李默"},
            {"section": "one_line", "op": "set", "value": "李默替人送名册，第一次踏进内门"},
        ],
        layer="brief",
    )
    check(results[0]["ok"], f"回收一条前面埋过的伏笔成功（{results[0]['reason']}）")
    check(not results[1]["ok"] and "不在允许值里" in results[1]["reason"],
          f"动作写成「埋伏」被拒（{results[1]['reason']}）")
    check(not results[2]["ok"] and "已经存在" in results[2]["reason"],
          f"同一章里同一个伏笔 id 重复登记被拒（{results[2]['reason']}）")
    check(len(out["foreshadow"]) == 2, f"只进了回收那一条（实际 {len(out['foreshadow'])}）")
    check(not results[3]["ok"] and "已经在里面" in results[3]["reason"], "核心情节重复被拒")
    check(results[4]["ok"] and out["settings"]["characters"] == ["李默"], "出场人物进对了位置")
    check(results[5]["ok"] and out["one_line"].startswith("李默替人送名册"), "一句话概要 set 成功")

    # 两层同名的小节形状不同，靠 layer 分开——这正是「分层」的意义
    check(LAYER_SPECS["setting"]["characters"]["kind"] == "objlist"
          and LAYER_SPECS["brief"]["characters"]["kind"] == "strlist",
          "setting 的 characters 是条目表，brief 的 characters 是字符串数组")
    check("chapters" not in LAYER_SPECS["setting"], "setting 层没有卷层的 chapters")
    check("chapters" not in LAYER_SPECS["brief"], "brief 层也没有 chapters")


def test_layer_prompts_differ() -> None:
    """三层的提示词要各说各的话，不能拿设定集那套去写章表。"""
    print("分层提示词")

    s, v, b = assist_system("setting"), assist_system("volume"), assist_system("brief")
    check("设定集" in s and "人物" in s, "设定集提示词讲人物")
    check("分卷目录" in v and "chapters" in v, "卷层提示词讲章表")
    check("chapter_no" in v and "整数" in v, "卷层提示词说明了章号是整数")
    check("创作任务指令" in b and "foreshadow" in b, "指令层提示词讲伏笔")
    check("埋设" in b and "回收" in b, "指令层提示词写了伏笔动作的允许值")
    check("章表" not in s and "chapters" not in s, "设定集提示词里不出现章表那套")
    check(assist_system("乱写") == assist_system("setting"), "认不出的层名退回设定集，不报错")


def test_refs_levels() -> None:
    """三档素材：不注入 / 只结构指纹（默认）/ 全素材。

    实测（同一问题、贫瘠设定集、各一次）：全素材 2648 输入 token，产出与「不注入」
    同等可用；不注入 636。所以默认给中间档——借节奏不借设定。
    这一条钉住的是「默认档不能悄悄变回全素材」，那会让每次对话贵 4 倍而没人察觉。
    """
    print("素材三档")

    refs = [{
        "work": "某书",
        "k3": ["章末钩子强度 均值 3.42（545 章）"],
        "k1": {
            "characters": [{"name": "李默", "role": "主角", "identity": "穿越者"}],
            "factions": [{"name": "青云宗", "stance": "表面中立"}],
            "abilities": [{"name": "数值之眼", "effect": "看到修为数值"}],
            "locations": [{"name": "柴房"}],
            "relations": [{"type": "上下级"}],
        },
    }]

    none_block = build_refs_block(refs, level="none")
    check(none_block == "", "none 档什么都不注入")

    k3_block = build_refs_block(refs, level="k3")
    check("钩子强度" in k3_block, "k3 档有结构指纹")
    check("李默" not in k3_block, "k3 档没有人名")
    check("青云宗" not in k3_block, "k3 档没有势力")
    check("数值之眼" not in k3_block, "k3 档没有能力")
    check(len(k3_block) < len(build_refs_block(refs, level="full")) / 2,
          f"k3 档明显更短（{len(k3_block)} vs {len(build_refs_block(refs, level='full'))}）")

    full_block = build_refs_block(refs, level="full")
    for needle in ("李默（主角）：穿越者", "青云宗：表面中立", "数值之眼：看到修为数值", "上下级 1"):
        check(needle in full_block, f"full 档有「{needle}」")

    check(build_refs_block(refs, level="乱写") == "", "认不出的档位当作不注入，不猜")

    # 计划里要如实反映档位与素材量
    for level, expect in (("none", None), ("k3", "结构指纹"), ("full", "人物")):
        plan = build_assist_plan(
            work="x", setting=_base(), refs_block=build_refs_block(refs, level=level),
            refs=["某书"], provider_id="p", model_id="m", level=level,
        )
        told = plan.to_dict()
        check(told["refs_level"] == level, f"{level} 档在计划里如实标出")
        check(bool(told["refs_preview"]) == (expect is not None),
              f"{level} 档的素材预览{'有' if expect else '为空'}")
        if expect:
            check(expect in told["refs_preview"], f"{level} 档预览里有「{expect}」")

    default_plan = build_assist_plan(
        work="x", setting=_base(), refs_block=build_refs_block(refs),
        refs=["某书"], provider_id="p", model_id="m",
    )
    check(default_plan.level == "full", f"不传档位时默认全素材（实际 {default_plan.level}）")
    check(build_refs_block(refs) != build_refs_block(refs, level="k3"), "默认档给的是全素材，不是中间档")


def test_refs_block_never_truncates_silently() -> None:
    print("参照作品块 · 超预算要点名")

    refs = [
        {"work": f"作品{i}", "k3": ["章末钩子强度 均值 3.4（545 章）"],
         "k1": {"characters": [{"name": f"人物{j}"} for j in range(40)]}}
        for i in range(5)
    ]
    full = build_refs_block(refs, level="full", max_chars=100000)
    check("作品0" in full and "作品4" in full, "预算够时全都在")
    check("⚠️" not in full, "不超预算时不出现告警")

    tiny = build_refs_block(refs, level="full", max_chars=300)
    check("作品0" in tiny, "第一条保住")
    check("⚠️" in tiny and "没有注入" in tiny, "超预算时点名丢了哪些")
    check(build_refs_block([]) == "", "没有参照时返回空串")


def test_prompt_carries_current_state_and_refs() -> None:
    print("提示词组装")

    prompt = build_user_prompt(
        setting=_base(), message="给我三个配角",
        history=[{"role": "user", "content": "先想个主角"}, {"role": "assistant", "content": "好的"}],
        refs_block="【参照作品】某书",
    )
    for needle in ("当前设定集", "李默", "参照作品", "作者这次的要求", "给我三个配角", "作者：先想个主角"):
        check(needle in prompt, f"提示词里有「{needle}」")
    check("作者要多少就给多少" in ASSIST_SYSTEM, "系统提示明确要求：作者要多少给多少，一次给足")
    check("没点名" in ASSIST_SYSTEM, "但没点名的类别不许顺手加")
    check("不要重复已有的条目" in ASSIST_SYSTEM, "系统提示明确要求不要重复登记")


def test_describe_proposal_survives_garbage() -> None:
    print("提案摘要 · 认不出来的也要能显示")

    check("人物" in describe_proposal({"section": "characters", "op": "add", "entry": {"name": "甲"}}),
          "正常条目显示小节名与主键")
    check("未知小节" in describe_proposal({"section": "乱写", "op": "add"}),
          "未知小节显示成「未知小节」而不是空白")
    check(describe_proposal({}) != "", "空提案也能显示出一行")


def main() -> int:
    print("=" * 58)
    print("设定集助手自检（不联网、不花钱）")
    print("=" * 58)

    for fn in (
        test_add_is_whitelisted,
        test_duplicate_and_update,
        test_strip_unknown_fields,
        test_strlist_and_scalar,
        test_unchecked_are_reported,
        test_proposals_do_not_mutate_input,
        test_plan_is_free_and_honest,
        test_refs_block_carries_real_material,
        test_volume_layer,
        test_brief_layer_foreshadow_enum,
        test_layer_prompts_differ,
        test_refs_levels,
        test_refs_block_never_truncates_silently,
        test_prompt_carries_current_state_and_refs,
        test_describe_proposal_survives_garbage,
        test_kb_import_mapping,
        test_kb_import_goes_through_the_same_gate,
        test_abilities_in_injection,
    ):
        fn()
        print()

    print("=" * 58)
    if _failures:
        print(f"未通过 {len(_failures)} 项：")
        for item in _failures:
            print(f"  · {item}")
        return 1
    print("全部通过")
    return 0




def test_kb_import_mapping() -> None:
    """知识库导入：字段对得上才映射，对不上的留空，不编。

    K1 的人物有名字和身份，**没有动机**——动机是作者的事。
    编一个占位的动机比留空坏得多：作者会以为那是从原作里读出来的。
    """
    print("知识库导入 · 映射与留空")

    k1 = {
        "characters": [
            {"name": "王铁柱", "role": "主角", "identity": "星闪大学异能系学生"},
            {"name": "张老师", "role": "重要配角", "identity": "组织星首"},
        ],
        "factions": [{"name": "异能协会", "stance": "官方组织"}],
        "abilities": [{"name": "数值之眼", "effect": "看到修为数值"}],
        "locations": [{"name": "柴房", "note": "外门最末等的住处"}],
        "relations": [
            {"from": "王铁柱", "to": "张老师", "type": "师生"},
            {"from": "王铁柱", "to": "赵富", "type": "同学"},
        ],
    }
    rows = kb_proposals(k1, section="characters", keys=["王铁柱"])
    check(len(rows) == 1, f"按主键筛出 1 条（实际 {len(rows)}）")
    entry = rows[0]["entry"]
    check(entry["name"] == "王铁柱" and entry["identity"] == "星闪大学异能系学生", "名字与身份都带过来了")
    check("motive" not in entry, "没有的字段不编（motive 不在里面）")

    fac = kb_proposals(k1, section="factions", keys=None)
    check(fac[0]["entry"] == {"name": "异能协会", "stance": "官方组织"}, "势力带立场")

    ab = kb_proposals(k1, section="abilities", keys=["数值之眼"])
    check(ab[0]["section"] == "abilities" and ab[0]["entry"]["effect"] == "看到修为数值",
          "能力带效果，落到新加的 abilities 小节")

    loc = kb_proposals(k1, section="locations", keys=["柴房"])
    check(loc[0]["section"] == "places" and loc[0]["entry"]["note"] == "外门最末等的住处",
          "地点映射到 world.places")

    check(kb_proposals(k1, section="没有这类") == [], "认不出的小节返回空，不报错")
    check(kb_proposals(k1, section="characters", keys=["查无此人"]) == [], "勾了不存在的名字返回空")

    rel = kb_relations_proposals(k1, subject="王铁柱")
    check(len(rel) == 1 and rel[0]["op"] == "update", "关系是 update 那一个人")
    check(len(rel[0]["entry"]["relations"]) == 2, "这个人的两条关系都带上了")
    dup_k1 = {"relations": [{"from": "甲", "to": "乙", "type": "师生"},
                            {"from": "甲", "to": "乙", "type": "师生"},
                            {"from": "甲", "to": "乙", "type": "师徒"},
                            {"from": "甲", "to": "乙", "type": "师生"}]}
    dup = kb_relations_proposals(dup_k1, subject="甲")
    got = len(dup[0]["entry"]["relations"])
    check(got == 2, f"K1 里按章累积的重复关系要去重（4 条 → {got} 条）")
    check(kb_relations_proposals(k1, subject="查无此人") == [], "没有关系记录的人返回空")


def test_kb_import_goes_through_the_same_gate() -> None:
    """导入走的判断和模型提案一模一样：白名单、主键去重、逐条原因。

    导入路径如果比提案路径松，模型塞不进来的东西就能从这儿塞进来——
    两条路一个口子，等于没有口子。
    """
    print("知识库导入 · 同一道闸")

    k1 = {"characters": [{"name": "李默", "role": "主角", "identity": "穿越者"},
                         {"name": "苏清月", "role": "重要配角", "identity": "内门大师姐"}],
          "factions": [{"name": "青云宗", "stance": "中立"}]}
    doc = {"characters": [{"name": "李默", "role": "主角", "motive": "活着回去"}]}

    rows = kb_proposals(k1, section="characters", keys=["李默"])
    out, results = apply_proposals(doc, rows, accepted=None, layer="setting", allow_partial=True)
    check(not results[0]["ok"] and "已经存在" in results[0]["reason"], "重名照样被去重挡住")
    check(len(out["characters"]) == 1, "没有产生第二个李默")

    # 允许部分：动机还没有也让进，但要说出来
    out2, results2 = apply_proposals(
        doc, kb_proposals(k1, section="factions"), accepted=None,
        layer="setting", allow_partial=True,
    )
    check(results2[0]["ok"] and len(out2["factions"]) == 1, "新势力进来了")

    rows3 = kb_proposals(k1, section="characters", keys=["苏清月"])
    _, strict = apply_proposals({"characters": []}, rows3, layer="setting")
    check(not strict[0]["ok"] and "缺必填" in strict[0]["reason"],
          "不允许部分时，缺动机照样拒（模型提案走这条）")
    _, loose = apply_proposals({"characters": []}, rows3, layer="setting", allow_partial=True)
    check(loose[0]["ok"], "允许部分时进得来（知识库导入走这条）")
    check("必填字段还没写" in loose[0].get("warning", ""),
          f"并且明确说了要作者自己补（{loose[0].get('warning')}）")


def test_abilities_in_injection() -> None:
    """能力升成一等小节之后，必须真的出现在给模型看的设定卡里。

    加了个字段但注入的时候不带上，等于这个字段不存在——
    作者填了、模型看不见、生成结果当然不认，然后作者会以为是模型不听话。
    """
    print("能力小节 · 要进设定卡")

    from workshop.creation import render_injection_block

    block = render_injection_block({
        "work": "关山灯", "abilities": [
            {"name": "数值之眼", "effect": "看到目标修为数值", "tier": "天赋", "holder": "李默"},
        ],
    })
    check("【能力体系】" in block, f"设定卡里有能力小节（{block[:60]}…）")
    check("数值之眼" in block and "看到目标修为数值" in block, "名称与效果都在")
    check("等级：天赋" in block and "持有者：李默" in block, "等级与持有者在")

    empty = render_injection_block({"work": "x"})
    check("【能力体系】" not in empty, "没有能力时不出现空小节")

if __name__ == "__main__":
    raise SystemExit(main())

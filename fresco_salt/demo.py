"""端到端演示：直接运行 `python -m fresco_salt.demo`（内存库，不落盘）。

场景 A：近地基毛细返潮，证据闭合 —— 剔除污染样本、留理由、封版、产物校验。
场景 B：返潮与渗漏证据并列 + 电荷账/时序门控演示 —— 不输出唯一盐源，给补样建议。
"""
from __future__ import annotations

import json

from .analysis import review
from .app import SaltApiApp
from .db import Store
from .recompute import verify

WALL = {
    "name": "西配殿北壁（演示墙）",
    "width_mm": 3000, "height_mm": 4000, "thickness_mm": 80,
    "layers": [
        {"layer_id": "L1_pigment", "d_start_mm": 0, "d_end_mm": 5, "name": "白粉/颜料层"},
        {"layer_id": "L2_plaster", "d_start_mm": 5, "d_end_mm": 40, "name": "细泥地杖"},
        {"layer_id": "L3_mud", "d_start_mm": 40, "d_end_mm": 70, "name": "粗泥层"},
        {"layer_id": "L4_masonry", "d_start_mm": 70, "d_end_mm": 80, "name": "砌体"},
    ],
}

RAINS = [
    {"rain_id": "rain_0609", "ts": "2026-06-09T02:00:00", "rain_mm": 18.2,
     "note": "屋面排水槽溢漏"},
]
REPAIRS = [
    {"repair_id": "rp_1", "date": "2023-09-10", "material": "水泥砂浆",
     "layer_id": "L2_plaster", "x_mm": 500, "y_mm": 2000, "radius_mm": 300},
]


def _ion(na=0.0, ca=0.0, detected=True, lod=0.5):
    def pair(v):
        entry = {"unit": "mmol/kg", "value": v if detected else None, "lod": lod}
        return entry
    return {"Na+": pair(na), "Cl-": pair(na), "Ca2+": pair(ca), "SO42-": pair(ca)}


def _sample(sid, x, y, depth, ts, na, ca, moisture, stage=None, note=None):
    layer = ("L1_pigment" if depth <= 5 else "L2_plaster" if depth <= 40
             else "L3_mud" if depth <= 70 else "L4_masonry")
    s = {"sample_id": sid, "layer_id": layer, "x_mm": x, "y_mm": y, "z_mm": depth,
         "depth_mm": depth, "ts": ts, "moisture_wt": moisture,
         "temp_c": 22.0, "rh_percent": 60, "ions": _ion(na, ca)}
    if stage:
        s["stage"] = stage
    if note:
        s["note"] = note
    return s


def rising_dataset():
    """低带富集、随高度下降、雨后低带含水率抬升；修补点旁不富集。"""
    samples = []
    # 低带竖孔 x=500,y=200：干季(06-01) 与雨后(06-10) 各三个深度
    plan = [(3, 38, 7), (20, 50, 10), (55, 62, 13)]
    for d, na, ca in plan:
        samples.append(_sample(f"A_low_{d}_dry", 500, 200, d,
                               "2026-06-01T09:00:00", na, ca, 5.5))
        samples.append(_sample(f"A_low_{d}_wet", 500, 200, d,
                               "2026-06-10T10:00:00", na, ca, 10.0))
    # 中带（水泥修补点旁），总盐 70
    for d, na, ca in [(3, 30, 5), (20, 30, 5), (55, 30, 5)]:
        samples.append(_sample(f"A_mid_{d}_dry", 500, 2000, d,
                               "2026-06-01T10:00:00", na, ca, 4.0))
    # 高带（真实剖面）：干季 75、雨后 42，雨后反而被稀释——不支持渗漏
    for d, na, ca in [(3, 32, 5.5), (20, 32, 5.5), (55, 32, 5.5)]:
        samples.append(_sample(f"A_hi_{d}_dry", 500, 3700, d,
                               "2026-06-01T11:00:00", na, ca, 3.5))
    for d, na, ca in [(3, 18, 3), (20, 18, 3), (55, 18, 3)]:
        samples.append(_sample(f"A_hi_{d}_wet", 500, 3700, d,
                               "2026-06-10T11:00:00", na, ca, 4.2))
    # 污染样本：高带雨后表面掏取的白霜，含盐异常高，现场标注疑似外界污染。
    # 它会虚增高带雨后脉冲比（真实 0.56 → 约 3.1）并伪造表层富集，
    # 使“渗漏”被错误保留、与返潮并列；留理由剔除后才恢复唯一返潮解释。
    samples.append(_sample("A_polluted", 520, 3700, 3, "2026-06-10T12:00:00",
                           300, 100, 8.0,
                           note="高带雨后表面掏取白霜，疑与屋面冲落粉尘/鸟粪混合，污染待核"))
    return samples


def tie_dataset():
    """返潮（低带富集+低带深度梯度）与渗漏（高带雨后脉冲+表层富集）证据并列。"""
    samples = []
    low = [(3, 30, 5), (20, 38, 7), (55, 48, 7)]      # 均值 90
    for d, na, ca in low:
        samples.append(_sample(f"B_low_{d}_dry", 600, 200, d,
                               "2026-06-01T09:00:00", na, ca, 5.0))
        samples.append(_sample(f"B_low_{d}_wet", 600, 200, d,
                               "2026-06-10T10:00:00", na, ca, 9.5))
    hi_dry = [(3, 22, 8), (20, 22, 8), (55, 20, 10)]   # 均值 60
    hi_wet = [(3, 40, 15), (20, 32, 13), (55, 24, 11)]  # 均值 90
    for d, na, ca in hi_dry:
        samples.append(_sample(f"B_hi_{d}_dry", 600, 3700, d,
                               "2026-06-01T11:00:00", na, ca, 3.2))
    for d, na, ca in hi_wet:
        samples.append(_sample(f"B_hi_{d}_wet", 600, 3700, d,
                               "2026-06-10T11:00:00", na, ca, 7.4))
    return samples


def broken_dataset():
    """电荷账不闭合 + 只有干季 + 层位错绑/坐标越界：门控应全部拦截。"""
    bad = _sample("X1", 600, 200, 20, "2026-06-01T09:00:00", 50, 0, 5.0)
    # 人为制造：阴离子只报 SO4 且远低于 Na
    bad["ions"] = {"Na+": {"unit": "mmol/kg", "value": 50, "lod": 0.5},
                   "SO42-": {"unit": "mmol/kg", "value": 6, "lod": 0.5}}
    bad["layer_id"] = "L3_mud"   # 深度 20 却绑粗泥层 -> 层位错绑
    bad["y_mm"] = 99999          # 坐标越界
    bad2 = _sample("X2", 600, 3700, 3, "2026-06-01T11:00:00", 17, 3, 3.0)
    bad2["ions"]["Cl-"].pop("lod")  # 检测限缺失
    return [bad, bad2]


# ---------------------------------------------------------------- 场景 C：敷贴试验

C_LAYERS = [  # 处理区/对照区共用层登记：体积(m³) × 干密度(kg/m³) = 层干质量
    {"layer_id": "L1_pigment", "volume_m3": 0.0005, "dry_density_kg_m3": 1200},
    {"layer_id": "L2_plaster", "volume_m3": 0.0035, "dry_density_kg_m3": 1500},
    {"layer_id": "L3_mud", "volume_m3": 0.004, "dry_density_kg_m3": 1600},
]


def poultice_wall_samples():
    """处理区(500,200) 与对照区(1500,200) 的处理前/后分层样本 + 复核用干湿样本。

    处理区：敷贴后各层净减、深层不升（真脱盐）；对照区：自然漂移 ×1.05。
    """
    out = []
    plan_t = [(3, 80, 20, 24, 6), (20, 60, 15, 30, 8), (55, 40, 10, 36, 9)]
    for d, na0, ca0, na1, ca1 in plan_t:
        out.append(_sample(f"tpre{d}", 500, 200, d, "2026-06-01T09:00:00",
                           na0, ca0, 5.5))
        out.append(_sample(f"tpost{d}", 500, 200, d, "2026-08-20T09:00:00",
                           na1, ca1, 5.0))
    plan_c = [(3, 30, 8, 31.5, 8.4), (20, 25, 6, 26.25, 6.3), (55, 20, 5, 21, 5.25)]
    for d, na0, ca0, na1, ca1 in plan_c:
        out.append(_sample(f"cpre{d}", 1500, 200, d, "2026-06-01T10:00:00",
                           na0, ca0, 5.0))
        out.append(_sample(f"cpost{d}", 1500, 200, d, "2026-08-20T10:00:00",
                           na1, ca1, 4.8))
    # 来源复核闭环所需：低带雨后湿样 + 高带干/湿样
    for d in (3, 20, 55):
        out.append(_sample(f"lw{d}", 500, 200, d, "2026-06-10T10:00:00",
                           40, 8, 10.0))
        out.append(_sample(f"hd{d}", 500, 3700, d, "2026-06-01T11:00:00",
                           15, 3, 3.5))
        out.append(_sample(f"hw{d}", 500, 3700, d, "2026-06-10T11:00:00",
                           15, 3, 4.0))
    return out


def poultice_trial_body(source_version_id, rain_id):
    bindings = []
    for zone, prefix in (("treatment", "t"), ("control", "c")):
        for phase in ("pre", "post"):
            for d in (3, 20, 55):
                bindings.append({"sample_id": f"{prefix}{phase}{d}",
                                 "zone": zone, "phase": phase})
    return {
        "name": "北壁东段纤维素敷贴脱盐试验",
        "source_version_id": source_version_id,
        "treatment_zone": {"x_mm": 500, "y_mm": 200, "radius_mm": 300},
        "control_zone": {"x_mm": 1500, "y_mm": 200, "radius_mm": 300},
        "layers": C_LAYERS, "control_layers": [dict(x) for x in C_LAYERS],
        "poultice": {"material": "纤维素纸浆", "water_content_percent": 45,
                     "contact_area_m2": 0.28},
        "bindings": bindings,
        "env_rain_ids": [rain_id],
    }


def _extract_round(rid, start, end, na=160, ca=40, vol=0.5):
    return {"round_id": rid, "cover_start_ts": start, "cover_end_ts": end,
            "extract": {"volume_l": vol, "ions": {
                "Na+": {"value": na, "unit": "mmol/L", "lod": 0.2},
                "Cl-": {"value": na, "unit": "mmol/L", "lod": 0.2},
                "Ca2+": {"value": ca, "unit": "mmol/L", "lod": 0.2},
                "SO42-": {"value": ca, "unit": "mmol/L", "lod": 0.2}}}}


def poultice_rounds(with_contaminated=True):
    rounds = [
        _extract_round("R1", "2026-07-01T09:00:00", "2026-07-03T09:00:00"),
        _extract_round("R2", "2026-07-08T09:00:00", "2026-07-10T09:00:00"),
        _extract_round("R3", "2026-07-15T09:00:00", "2026-07-17T09:00:00"),
    ]
    if with_contaminated:
        # R4 钠异常：疑敷贴材料本底污染，会把收支账撑破
        rounds.append(_extract_round("R4", "2026-07-22T09:00:00",
                                     "2026-07-24T09:00:00", na=900))
    return rounds


def show_trial(title, result):
    line("=" * 78)
    line(title)
    line("-" * 78)
    for g in result["gates"]:
        mark = "PASS" if g["passed"] else "FAIL"
        line(f"[{mark}] {g['name']}" + (f"  — {g['detail']}" if g["detail"] else ""))
    t = result["balance"]["total"]
    line(f"\n  收支：累计浸出 {t['extract_mmol']} mmol ｜ 漂移校正墙内净减 "
         f"{t['loss_corr_mmol']} mmol ｜ 残差 {t['residual_mmol']} mmol "
         f"（{'闭合' if result['balance']['closed'] else '不闭合'}）")
    inv = result["inventory"]
    line(f"  库存：处理区 {inv['treatment']['pre_total_mmol']} → "
         f"{inv['treatment']['post_total_mmol']} mmol；对照区 "
         f"{inv['control']['pre_total_mmol']} → {inv['control']['post_total_mmol']} mmol")
    line(f"  分类：{result['classification_name']}（{result['classification']}）")
    d = result["decision"]
    line(f"  决定：{d['name']} —— {d['reason']}")
    line("  建议：")
    for a in result["advice"]:
        line(f"    {a['priority']}. [{a['purpose']}] {a['location']}；{a['timing']}")


def line(t=""):
    print(t)


def show(title, result):
    line("=" * 78)
    line(title)
    line("-" * 78)
    for g in result["gates"]:
        mark = "PASS" if g["passed"] else "FAIL"
        line(f"[{mark}] {g['name']}" + (f"  — {g['detail']}" if g["detail"] else ""))
    line("")
    for c, info in result["candidates"].items():
        line(f"  {info['name']}: {info['status']}（分 {info['score']}）")
        for e in info["evidence_support"]:
            line(f"      ＋[{e['weight']}] {e['text']}")
        for e in info["evidence_contradiction"]:
            line(f"      －[{e['weight']}] {e['text']}")
    line("")
    line(f"  裁决：{result['verdict']} —— {result['verdict_reason']}")
    if result["unique_source_name"]:
        line(f"  唯一盐源：{result['unique_source_name']}")
    line("  下一处补样建议：")
    for a in result["next_sampling"]:
        line(f"    {a['priority']}. [{a['purpose']}] {a['location']}；{a['timing']}；{a['action']}")


def main():
    # ---------- 场景 B：直接复核（不经过 HTTP） ----------
    show("场景 B-1：返潮/渗漏并列 → tie 门控拦截，禁止唯一盐源",
         review(WALL, tie_dataset(), RAINS, []))
    show("场景 B-2：层位错绑/坐标越界/检测限/电荷账/时序同时失败",
         review(WALL, broken_dataset(), RAINS, REPAIRS))

    # ---------- 场景 A：完整 API 流程（内存库） ----------
    app = SaltApiApp(Store(":memory:"))
    wall = app.create_wall(WALL)["wall"]
    wid = wall["wall_id"]
    samples = rising_dataset()
    ids = app.add_samples(wid, samples)["sample_ids"]
    app.add_rains(wid, RAINS)
    app.add_repairs(wid, REPAIRS)
    line("\n上传样本 %d 个：%s" % (len(ids), ", ".join(ids[:4]) + " ..."))

    r0 = app.get_analysis(wid)["analysis"]
    show("场景 A-1：含污染样本的首次复核（高带雨后白霜虚增降雨脉冲，禁止下唯一结论）", r0)
    assert r0["verdict"] != "unique_source", "污染样本未剔除时不得输出唯一盐源"

    rev = app.add_revision(wid, {
        "kind": "exclude_sample",
        "reason": "A_polluted 为表面掏取酥碱，与表土/鸟粪混合属外源污染，"
                  "不代表墙内盐剖面；经双人复核剔除，原始数据保留可查。",
        "payload": {"sample_id": "A_polluted"},
        "actor": "张工/李工",
    })
    line(f"\n已生成修订 {rev['revision']['rev_id']}（seq={rev['revision']['seq']}），"
         f"即时裁决：{rev['verdict']}")

    r1 = app.get_analysis(wid)["analysis"]
    show("场景 A-2：剔除污染样本后复核", r1)
    assert r1["verdict"] == "unique_source" and r1["unique_source"] == "rising"

    # 封版
    locked = app.lock(wid, {"actor": "张工"})
    line("\n封版：" + json.dumps(locked, ensure_ascii=False, indent=2))
    try:
        app.add_samples(wid, [_sample("Z1", 1, 1, 3, "2026-06-10T13:00:00", 1, 1, 5)])
        raise SystemExit("封版后竟然还能写入！")
    except PermissionError as e:
        line(f"封版只读校验：409 — {e}")

    # 复算 JSON 与 SVG
    ctype, raw, _ = app.get_version(locked["version_id"], "/recompute.json")
    payload = json.loads(raw)
    ok = verify(payload)
    _, svg, _ = app.get_version(locked["version_id"], "/profile.svg")
    line(f"\n复算 JSON：{len(raw)} 字节，哈希复算一致={ok}，"
         f"input_hash={payload['input_hash']}")
    line(f"剖面 SVG：{len(svg)} 字节，以 <svg> 开头：{svg.lstrip().startswith('<svg')}")
    line(f"修订账可查询：{[r['rev_id'] for r in app.get_revisions(wid)['revisions']]}")

    # ---------- 场景 C：敷贴脱盐试验（接入同一 API，独立试验修订账） ----------
    line("\n" + "=" * 78)
    line("场景 C：敷贴脱盐试验 —— 表面电导降了，盐是真离墙还是退到地杖深处？")
    wall_c = app.create_wall(dict(WALL, name="西配殿北壁（敷贴试验墙）"))["wall"]
    wc = wall_c["wall_id"]
    app.add_samples(wc, poultice_wall_samples())
    # rains 表 rain_id 全局唯一，场景 C 用独立降雨记录
    rain_id = app.add_rains(wc, [dict(RAINS[0], rain_id="rain_c_0609")])["rain_ids"][0]
    verdict_c = app.get_analysis(wc)["analysis"]["verdict"]
    locked_c = app.lock(wc, {"actor": "张工"})
    line(f"来源复核封版：{locked_c['version_id']}（裁决 {verdict_c} → "
         f"{locked_c['unique_source_name']}），作为试验引用来源")

    trial = app.create_trial(wc, poultice_trial_body(locked_c["version_id"],
                                                     rain_id))["trial"]
    tid = trial["trial_id"]
    app.add_rounds(tid, poultice_rounds())
    line(f"试验 {tid}：处理区(500,200)r300 ｜ 对照区(1500,200)r300 ｜ "
         "4 轮浸出液入账（R4 钠异常）")

    a0 = app.get_trial_analysis(tid)["analysis"]
    show_trial("场景 C-1：含污染浸出液的首次核算（收支被撑破，不得结束）", a0)
    assert a0["classification"] == "insufficient_evidence"
    assert not a0["decision"]["can_end"]

    rev = app.add_trial_revision(tid, {
        "kind": "exclude_extract",
        "reason": "R4 浸出液钠浓度异常，空白对照证实为敷贴材料本底污染，"
                  "不代表墙内脱出的盐，经双人复核剔除，原始数据保留。",
        "payload": {"round_id": "R4"},
        "actor": "张工/李工",
    })
    line(f"\n已生成试验修订 {rev['revision']['rev_id']}（独立试验修订账，"
         f"seq={rev['revision']['seq']}），即时分类：{rev['classification']}")

    a1 = app.get_trial_analysis(tid)["analysis"]
    show_trial("场景 C-2：剔除污染液后 —— 收支闭合判净移除，"
               "但残盐仍高于对照区，不得结束", a1)
    assert a1["classification"] == "net_removal"
    assert a1["decision"]["code"] == "continue_treatment"

    conf = app.confirm_trial(tid, {"actor": "张工"})
    line("\n确认版（固定来源、轮次与决定）："
         + json.dumps({k: conf[k] for k in
                       ("version_id", "source_version_id", "confirm_hash",
                        "classification")}, ensure_ascii=False))
    ctype, raw, _ = app.get_trial_version(conf["version_id"], "/rounds.json")
    payload = json.loads(raw)
    from .poultice_recompute import verify as verify_confirm
    ok = verify_confirm(payload)
    _, svg_c, _ = app.get_trial_version(conf["version_id"], "/balance.svg")
    line(f"逐轮 JSON：{len(raw)} 字节，复算一致={ok}，"
         f"confirm_hash={payload['confirm_hash']}")
    line(f"收支 SVG：{len(svg_c)} 字节，以 <svg> 开头："
         f"{svg_c.lstrip().startswith('<svg')}")
    try:
        app.add_rounds(tid, poultice_rounds(with_contaminated=False))
        raise SystemExit("确认版后竟然还能写入！")
    except PermissionError as e:
        line(f"确认版只读校验：409 — {e}")
    line("演示结束。")


if __name__ == "__main__":
    main()

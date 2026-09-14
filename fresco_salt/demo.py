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
    line("演示结束。")


if __name__ == "__main__":
    main()

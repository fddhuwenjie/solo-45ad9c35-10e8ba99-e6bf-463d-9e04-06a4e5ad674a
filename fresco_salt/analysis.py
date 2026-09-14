"""盐害来源复核核心：门控、指标计算、候选迁移路径证据裁决、补样建议。

纯函数模块，不触碰数据库与 HTTP，便于封版复算与单元测试。
候选路径：
  rising   地基返潮（毛细上升）
  leak     屋面/墙体渗漏（雨水自上而下）
  material 水泥修补层材料释放（横向/就地释盐）
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

from .chemistry import (
    ChemistryError, IONS, charge_balance, normalize_ions,
)
from .util import hours_between, parse_dt, r4

DEFAULTS: Dict[str, Any] = {
    # 电荷账
    "cbe_limit_percent": 5.0,            # |CBE| 超过即不闭合
    "censored_max_fraction": 0.5,        # 半检出限代用比例过高 -> 账不可信
    # 时序 / 干湿
    "wet_window_hours": 48.0,            # 雨后 48h 内为湿阶段
    "dry_window_hours": 96.0,            # 雨后 96h 以上为干阶段
    "rain_mm_min": 1.0,                  # 低于该雨量不计为有效降雨
    "min_wet_samples": 1,
    "min_dry_samples": 1,
    "column_xy_bin_mm": 50.0,            # 同一竖孔的 x/y 归并尺度
    "depth_match_mm": 5.0,               # 干湿配对深度容差
    # 含水率
    "moisture_delta_wt": 2.0,            # 湿-干含水率差（重量%）阈值
    # 空间分带
    "rise_band_mm": 500.0,               # 近地基返潮带高度
    "leak_band_mm": 500.0,               # 近屋面渗漏带高度（自墙顶向下）
    "patch_radius_mm": 300.0,            # 水泥修补点影响半径
    # 梯度与富集
    "grad_slope_min": 0.02,              # 最小显著梯度（mmol/kg·mm）
    "rise_ratio": 1.5,                   # 低带/高处总盐比（支持返潮）
    "leak_ratio": 1.5,                   # 高带/低处总盐比（支持渗漏）
    "contra_ratio": 2.0,                 # 反向倍数达到该值才作为矛盾证据排除对侧
    "material_ratio": 1.3,               # 修补旁/远处总盐比
    "surface_depth_mm": 20.0,            # 表层/深层分界
    "rain_pulse_ratio": 1.3,             # 雨后高带盐/干季高带盐
    # 候选裁决
    "retain_min_score": 2.0,
    "tie_eps": 0.5,                      # 候选分差小于该值视为并列
}

CANDIDATES = ("rising", "leak", "material")
CANDIDATE_CN = {
    "rising": "地基返潮（毛细上升）",
    "leak": "屋面/墙体渗漏",
    "material": "水泥修补层材料释放",
}

# 门控定义：任一 blocking 门控失败即禁止唯一盐源结论
GATE_ORDER = [
    ("layer_bind", "层位绑定", True),
    ("coord", "三维坐标越界", True),
    ("lod", "检出限完整性", True),
    ("charge", "电荷当量闭合", True),
    ("basis", "固/液相基准一致", True),
    ("time", "采样时序与干湿覆盖", True),
    ("tie", "候选路径并列", True),
]


# ---------------------------------------------------------------- 基础工具

def _params(wall: Dict[str, Any]) -> Dict[str, Any]:
    p = dict(DEFAULTS)
    p.update(wall.get("params") or {})
    return p


def _mean(xs: List[float]) -> Optional[float]:
    return sum(xs) / len(xs) if xs else None


def _ols(xs: List[float], ys: List[float]) -> Optional[Dict[str, float]]:
    n = len(xs)
    if n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    slope = sxy / sxx
    intercept = my - slope * mx
    if n > 2:
        syy = sum((y - my) ** 2 for y in ys)
        r2 = (sxy * sxy / sxx / syy) if syy else 1.0
    else:
        r2 = 1.0
    return {"slope": slope, "intercept": intercept, "r2": r2, "n": n}


def _column_key(s: Dict[str, Any], binw: float) -> Tuple[int, int]:
    return (round(s["x_mm"] / binw), round(s["y_mm"] / binw))


def infer_stage(ts, rains: List[Dict[str, Any]], p: Dict[str, Any]) -> Tuple[str, Optional[float], Optional[str]]:
    """返回 (stage wet|dry|transition, hours_after_rain, rain_id)。

    雨后 wet_window 内为 wet；雨后 dry_window 之后为 dry；首场有效降雨之前，
    若距降雨达到 dry_window，也视为干季基线（否则时序上永远没有“干”样本）。
    """
    prior = [r for r in rains
             if r.get("rain_mm", 0.0) >= p["rain_mm_min"] and parse_dt(r["ts"]) <= ts]
    if prior:
        r = max(prior, key=lambda x: parse_dt(x["ts"]))
        h = hours_between(ts, parse_dt(r["ts"]))
        if h <= p["wet_window_hours"]:
            return "wet", h, r["rain_id"]
        if h >= p["dry_window_hours"]:
            return "dry", h, r["rain_id"]
        return "transition", h, r["rain_id"]
    future = [r for r in rains
              if r.get("rain_mm", 0.0) >= p["rain_mm_min"] and parse_dt(r["ts"]) > ts]
    if future:
        nxt = min(future, key=lambda x: parse_dt(x["ts"]))
        lead = hours_between(parse_dt(nxt["ts"]), ts)
        if lead >= p["dry_window_hours"]:
            return "dry", -lead, None
        return "transition", -lead, None
    return "transition", None, None


# ---------------------------------------------------------------- 修订应用

def apply_revisions(samples: List[Dict[str, Any]], revisions: List[Dict[str, Any]]
                    ) -> Tuple[Dict[str, bool], Dict[str, str], bool, List[Dict[str, Any]]]:
    excluded: Dict[str, bool] = {}
    rebound: Dict[str, str] = {}
    conservative = False
    for rev in sorted(revisions, key=lambda r: r["seq"]):
        kind = rev["kind"]
        payload = rev.get("payload") or {}
        if kind == "exclude_sample":
            excluded[payload["sample_id"]] = True
        elif kind == "restore_sample":
            excluded.pop(payload["sample_id"], None)
        elif kind == "correct_binding":
            rebound[payload["sample_id"]] = payload["new_layer_id"]
        elif kind == "adopt_conservative":
            conservative = True
    return excluded, rebound, conservative, []


# ---------------------------------------------------------------- 样本评估

def evaluate_sample(s: Dict[str, Any], wall: Dict[str, Any], p: Dict[str, Any],
                    rains: List[Dict[str, Any]], rebound: Dict[str, str]) -> Dict[str, Any]:
    issues: List[Dict[str, Any]] = []
    layers = {l["layer_id"]: l for l in wall["layers"]}

    # 坐标
    coord_ok = True
    for axis, dim in (("x_mm", "width_mm"), ("y_mm", "height_mm"), ("z_mm", "thickness_mm")):
        v = s.get(axis)
        if v is None or v < 0 or v > wall[dim]:
            coord_ok = False
            issues.append({"code": "coord", "msg": f"{s['sample_id']} 坐标 {axis}={v} 越界"
                                    f"（0~{wall[dim]}）"})
    depth = s.get("depth_mm")
    if depth is None:
        depth = s.get("z_mm")

    # 层位绑定
    declared = rebound.get(s["sample_id"], s.get("layer_id"))
    binding_ok = False
    suggested = None
    if declared in layers:
        layer = layers[declared]
        if layer["d_start_mm"] <= depth <= layer["d_end_mm"]:
            binding_ok = True
        else:
            for lid, l in layers.items():
                if l["d_start_mm"] <= depth <= l["d_end_mm"]:
                    suggested = lid
                    break
            issues.append({"code": "layer_bind",
                           "msg": f"{s['sample_id']} 绑定 {declared} 但深度 {depth}mm "
                                  f"落在层位 {suggested or '?'}，属层位错绑"})
    else:
        issues.append({"code": "layer_bind",
                       "msg": f"{s['sample_id']} 绑定的层位 {declared!r} 不在墙体模型中"})

    # 离子归一化与电荷账
    norm: Dict[str, Any] = {}
    basis: Optional[str] = None
    balance: Optional[Dict[str, Any]] = None
    try:
        norm, basis = normalize_ions(s["ions"])
        balance = charge_balance(norm)
        if balance["missing_lod"]:
            issues.append({"code": "lod",
                           "msg": f"{s['sample_id']} 缺检测限：{', '.join(balance['missing_lod'])}"})
        if balance["cbe_percent"] is None:
            issues.append({"code": "charge", "msg": f"{s['sample_id']} 缺少某一极性离子，电荷账无意义"})
        elif abs(balance["cbe_percent"]) > p["cbe_limit_percent"]:
            issues.append({"code": "charge",
                           "msg": f"{s['sample_id']} CBE={balance['cbe_percent']:.1f}% "
                                  f"超过 ±{p['cbe_limit_percent']}%"})
        elif (balance["censored_fraction"] or 0) > p["censored_max_fraction"]:
            issues.append({"code": "charge",
                           "msg": f"{s['sample_id']} 半检出限代用离子占比 "
                                  f"{balance['censored_fraction']:.0%}，电荷账不可信"})
    except ChemistryError as exc:
        msg = str(exc)
        code = "lod" if ("检出限" in msg or "lod" in msg) else (
            "basis" if "基准" in msg else "charge")
        issues.append({"code": code, "msg": f"{s['sample_id']}: {msg}"})

    # 干湿阶段：一律以降雨记录推断为准；申报值只用于对账，不得覆盖推断结果。
    ts = parse_dt(s["ts"])
    inferred_stage, hours_after, rain_id = infer_stage(ts, rains, p)
    declared_stage = s.get("stage")
    stage_conflict = bool(declared_stage) and declared_stage != inferred_stage
    # 推断为 transition：样本落在雨后湿/干窗口之间，或与任何有效降雨都无法对齐，
    # 属于时序断档，不能充当湿阶段或干季基线样本。
    stage_gap = inferred_stage == "transition"
    stage_note = None
    if stage_conflict:
        gap_word = "（且采样落在雨后过渡窗口，构成时序断档）" if stage_gap else ""
        stage_note = (f"申报阶段 {declared_stage} 与降雨记录推断 {inferred_stage} 冲突"
                      f"{gap_word}；不采用申报值，该样本不得放行唯一盐源")
    elif stage_gap:
        if hours_after is None:
            detail = "无有效降雨记录可对齐"
        elif hours_after < 0:
            detail = f"距其后首场有效降雨 {-hours_after:.0f}h，处于雨前过渡窗口"
        else:
            detail = f"雨后 {hours_after:.0f}h，处于湿/干窗口之间"
        stage_note = f"时序断档：{detail}，样本不能计入湿/干阶段"

    total = sum(i.value for i in norm.values())
    return {
        "sample_id": s["sample_id"],
        "x_mm": s.get("x_mm"), "y_mm": s.get("y_mm"), "z_mm": s.get("z_mm"),
        "depth_mm": depth,
        "layer_id": declared if binding_ok else s.get("layer_id"),
        "binding_ok": binding_ok,
        "suggested_layer": suggested,
        "coord_ok": coord_ok,
        "ts": ts.isoformat(),
        "stage": inferred_stage,
        "stage_declared": declared_stage,
        "stage_conflict": stage_conflict,
        "stage_gap": stage_gap,
        "hours_after_rain": r4(hours_after),
        "nearest_rain_id": rain_id,
        "stage_note": stage_note,
        "moisture_wt": s.get("moisture_wt"),
        "temp_c": s.get("temp_c"), "rh_percent": s.get("rh_percent"),
        "basis": basis,
        "ions": {k: {"value": i.value, "detected": i.detected, "lod": i.lod,
                     "censored": i.censored, "unit_basis": ("mmol/kg_dry" if basis == "solid"
                     else "mmol/L")} for k, i in norm.items()},
        "total_conc": total,
        "balance": balance,
        "issues": issues,
        "blocked": bool(issues),
    }


# ---------------------------------------------------------------- 指标计算

def _stage_moisture(rows: List[Dict[str, Any]], stage: str,
                    pred=lambda r: True) -> Optional[float]:
    vals = [r["moisture_wt"] for r in rows
            if r["stage"] == stage and r["moisture_wt"] is not None and pred(r)]
    return _mean(vals)


def compute_metrics(rows: List[Dict[str, Any]], wall: Dict[str, Any],
                    p: Dict[str, Any], repairs: List[Dict[str, Any]]) -> Dict[str, Any]:
    H = wall["height_mm"]
    low = [r for r in rows if r["y_mm"] is not None and r["y_mm"] < p["rise_band_mm"]]
    high = [r for r in rows if r["y_mm"] is not None and r["y_mm"] > H - p["leak_band_mm"]]
    shallow = [r for r in rows if r["depth_mm"] is not None and r["depth_mm"] <= p["surface_depth_mm"]]
    deep = [r for r in rows if r["depth_mm"] is not None and r["depth_mm"] > p["surface_depth_mm"]]

    def conc(rs):
        return [r["total_conc"] for r in rs if r["total_conc"] is not None]

    def _baseline_conc(rs):
        """空间带比较优先用干季基线，避免雨后脉冲混入稀释纵向梯度。"""
        dry = conc([r for r in rs if r["stage"] == "dry"])
        return _mean(dry if dry else conc(rs))

    low_mean, high_mean = _baseline_conc(low), _baseline_conc(high)
    m: Dict[str, Any] = {
        "n_active": len(rows),
        "n_low_band": len(low), "n_high_band": len(high),
        "low_band_total_mean": r4(low_mean),
        "high_band_total_mean": r4(high_mean),
        "low_over_high_ratio": r4(low_mean / high_mean) if low_mean and high_mean else None,
        "high_over_low_ratio": r4(high_mean / low_mean) if low_mean and high_mean else None,
        "depth_regression": None,
        "height_regression": None,
        "depth_regression_low_band": None,
        "surface_enrichment_high": None,
    }

    dreg = _ols([r["depth_mm"] for r in rows if None not in (r["depth_mm"], r["total_conc"])],
                conc(rows))
    hreg = _ols([r["y_mm"] for r in rows if None not in (r["y_mm"], r["total_conc"])],
                conc(rows))
    dreg_low = _ols([r["depth_mm"] for r in low if None not in (r["depth_mm"], r["total_conc"])],
                    conc(low))
    m["depth_regression"] = ({k: r4(v) for k, v in dreg.items()} if dreg else None)
    m["height_regression"] = ({k: r4(v) for k, v in hreg.items()} if hreg else None)
    m["depth_regression_low_band"] = ({k: r4(v) for k, v in dreg_low.items()}
                                      if dreg_low else None)

    # 高带表层富集（雨后湿阶段更能反映外渗水在蒸发面析盐）
    def _shdp_mean(rows_sel, stage_pref):
        chosen = [r for r in rows_sel if r["stage"] == stage_pref] or rows_sel
        sh = _mean(conc([r for r in chosen if r in shallow]))
        dp = _mean(conc([r for r in chosen if r in deep]))
        return sh, dp

    sh_hi, dp_hi = _shdp_mean(high, "wet")
    m["surface_enrichment_high"] = r4(sh_hi - dp_hi) if sh_hi is not None and dp_hi is not None else None

    # 干湿循环响应：同竖孔同深度配对
    columns: Dict[Tuple[int, int], List[Dict[str, Any]]] = {}
    for r in rows:
        columns.setdefault(_column_key(r, p["column_xy_bin_mm"]), []).append(r)
    paired, n_pairs = [], 0
    for key, rs in columns.items():
        for rw in [x for x in rs if x["stage"] == "wet"]:
            cand = [x for x in rs if x["stage"] == "dry"
                    and abs(x["depth_mm"] - rw["depth_mm"]) <= p["depth_match_mm"]]
            if cand:
                rd = cand[0]
                n_pairs += 1
                paired.append({
                    "column": list(key),
                    "depth_mm": rw["depth_mm"],
                    "wet_total": r4(rw["total_conc"]), "dry_total": r4(rd["total_conc"]),
                    "wet_to_dry_ratio": r4(rw["total_conc"] / rd["total_conc"])
                    if rd["total_conc"] else None,
                    "wet_moisture": rw["moisture_wt"], "dry_moisture": rd["moisture_wt"],
                    "wet_id": rw["sample_id"], "dry_id": rd["sample_id"],
                })
    m["paired_cores"] = paired
    m["n_paired_wet_dry"] = n_pairs

    wet_m = _stage_moisture(rows, "wet")
    dry_m = _stage_moisture(rows, "dry")
    m["moisture_wet_mean"] = r4(wet_m)
    m["moisture_dry_mean"] = r4(dry_m)
    m["moisture_wet_dry_delta"] = r4(wet_m - dry_m) if wet_m is not None and dry_m is not None else None
    m["moisture_low_wet"] = r4(_stage_moisture(low, "wet"))
    m["moisture_low_dry"] = r4(_stage_moisture(low, "dry"))
    m["moisture_high_wet"] = r4(_stage_moisture(high, "wet"))
    m["moisture_high_dry"] = r4(_stage_moisture(high, "dry"))

    # 高带雨后脉冲
    hi_wet = _mean(conc([r for r in high if r["stage"] == "wet"]))
    hi_dry = _mean(conc([r for r in high if r["stage"] == "dry"]))
    m["high_band_wet_total"] = r4(hi_wet)
    m["high_band_dry_total"] = r4(hi_dry)
    m["high_band_rain_pulse"] = r4(hi_wet / hi_dry) if hi_wet and hi_dry else None

    # 水泥修补层邻近度
    cement = [rp for rp in repairs if "水泥" in (rp.get("material") or "")
              or rp.get("material_type") == "cement"]
    near_ids, far_rows = set(), []

    def near_patch(r):
        for rp in cement:
            if rp.get("layer_id") and r["layer_id"] == rp["layer_id"]:
                return True
            cx, cy = rp.get("x_mm"), rp.get("y_mm")
            radius = rp.get("radius_mm", p["patch_radius_mm"])
            if cx is not None and cy is not None and r["x_mm"] is not None and r["y_mm"] is not None:
                if ((r["x_mm"] - cx) ** 2 + (r["y_mm"] - cy) ** 2) ** 0.5 <= radius:
                    return True
        return False

    for r in rows:
        if cement and near_patch(r):
            near_ids.add(r["sample_id"])
        else:
            far_rows.append(r)
    near_rows = [r for r in rows if r["sample_id"] in near_ids]
    near_mean, far_mean = _mean(conc(near_rows)), _mean(conc(far_rows))
    m["cement_repairs"] = [rp["repair_id"] for rp in cement]
    m["n_near_patch"] = len(near_rows)
    m["near_patch_total_mean"] = r4(near_mean)
    m["far_patch_total_mean"] = r4(far_mean)
    m["near_over_far_ratio"] = r4(near_mean / far_mean) if near_mean and far_mean else None
    marker = ("Ca2+", "SO42-", "OH-")

    def marker_mean(rs):
        vals = []
        for r in rs:
            vals += [r["ions"][k]["value"] for k in marker if k in r["ions"]]
        return _mean(vals)

    m["near_marker_mean"] = r4(marker_mean(near_rows))
    m["far_marker_mean"] = r4(marker_mean(far_rows))
    m["near_wet_dry_moisture_delta"] = None
    nwm, ndm = _stage_moisture(near_rows, "wet"), _stage_moisture(near_rows, "dry")
    if nwm is not None and ndm is not None:
        m["near_wet_dry_moisture_delta"] = r4(nwm - ndm)

    # 分层、分阶段 CBE 复核
    cbe_layers, cbe_stages = {}, {}
    for r in rows:
        if r["balance"] and r["balance"]["cbe_percent"] is not None:
            cbe_layers.setdefault(r["layer_id"], []).append(r["balance"]["cbe_percent"])
            cbe_stages.setdefault(r["stage"], []).append(r["balance"]["cbe_percent"])
    m["cbe_by_layer"] = {k: r4(_mean(v)) for k, v in sorted(cbe_layers.items())}
    m["cbe_by_stage"] = {k: r4(_mean(v)) for k, v in sorted(cbe_stages.items())}
    m["ion_depth_profiles"] = _ion_depth_profiles(rows)
    return m


def _ion_depth_profiles(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """每种离子：按 深度×阶段 聚合的剖面均值，以及全部原始点。

    多竖孔在同一深度会有多个样本，直接连线会产生竖直假线，故连线用聚合均值。
    """
    prof: Dict[str, Any] = {}
    for ion in sorted(IONS):
        bucket: Dict[Tuple[float, str], List[Dict[str, Any]]] = {}
        for r in rows:
            if ion in r["ions"] and r["depth_mm"] is not None:
                bucket.setdefault((r["depth_mm"], r["stage"]), []).append(r)
        if not bucket:
            continue
        series, raw = [], []
        for (depth, stage), rs in sorted(bucket.items()):
            vals = [r["ions"][ion]["value"] for r in rs]
            series.append({"depth_mm": depth, "stage": stage,
                           "value": r4(_mean(vals)), "n": len(vals)})
            for r in rs:
                raw.append({"depth_mm": depth, "stage": stage,
                            "value": r4(r["ions"][ion]["value"]),
                            "sample_id": r["sample_id"]})
        prof[ion] = {"series": series, "points": raw}
    return prof


# ---------------------------------------------------------------- 候选路径

def _candidate_evidence(rows, m: Dict[str, Any], wall, p, rains, repairs) -> Dict[str, Any]:
    out: Dict[str, Any] = {}

    # ---- 地基返潮 ----
    s_up, c_up = [], []
    if m["low_over_high_ratio"] is not None and m["low_over_high_ratio"] >= p["rise_ratio"]:
        s_up.append((2, f"近地基带总盐为高处的 {m['low_over_high_ratio']} 倍（≥{p['rise_ratio']}），呈下高上低"))
    hreg = m.get("height_regression")
    if hreg and hreg["slope"] <= -p["grad_slope_min"]:
        s_up.append((2, f"总盐随高度线性下降，斜率 {hreg['slope']} mmol/kg·mm，R²={hreg['r2']}"))
    dl = m.get("depth_regression_low_band")
    if dl and dl["slope"] >= p["grad_slope_min"]:
        s_up.append((1, f"低带总盐随深度增加（斜率 {dl['slope']}），符合深部毛细供给"))
    md = m.get("moisture_low_wet"), m.get("moisture_low_dry")
    if md[0] is not None and md[1] is not None and md[0] - md[1] >= p["moisture_delta_wt"]:
        s_up.append((1, f"低带雨后含水率 {md[0]}% 高于干季 {md[1]}%（差 {r4(md[0]-md[1])} 个百分点）"))
    # 反向证据（矛盾倍数阈值高于支持阈值，避免 1.5 倍同时“支持己方、排除对侧”）
    if m["low_over_high_ratio"] is not None and m["low_over_high_ratio"] <= 1.0 / p["contra_ratio"]:
        c_up.append((2, f"低带总盐反远低于高带（比值 {m['low_over_high_ratio']}），与毛细上升相反"))
    if hreg and hreg["slope"] >= p["grad_slope_min"]:
        c_up.append((2, "总盐随高度升高，近地基无富集"))
    out["rising"] = {"supports": s_up, "contradictions": c_up}

    # ---- 渗漏 ----
    s_lk, c_lk = [], []
    if m["high_over_low_ratio"] is not None and m["high_over_low_ratio"] >= p["leak_ratio"]:
        s_lk.append((2, f"近屋面带总盐为低处的 {m['high_over_low_ratio']} 倍（≥{p['leak_ratio']}）"))
    if hreg and hreg["slope"] >= p["grad_slope_min"]:
        s_lk.append((2, f"总盐随高度升高（斜率 {hreg['slope']}，R²={hreg['r2']}），符合自上而下渗水"))
    pulse = m.get("high_band_rain_pulse")
    if pulse is not None and pulse >= p["rain_pulse_ratio"]:
        s_lk.append((2, f"高带雨后盐分为干季的 {pulse} 倍，存在降雨脉冲响应"))
    if m.get("surface_enrichment_high") is not None and m["surface_enrichment_high"] > 0:
        s_lk.append((1, f"高带表层较深层富集 +{m['surface_enrichment_high']}，符合外渗水在蒸发面析盐"))
    mh = m.get("moisture_high_wet"), m.get("moisture_high_dry")
    if mh[0] is not None and mh[1] is not None and mh[0] - mh[1] >= p["moisture_delta_wt"]:
        s_lk.append((1, f"高带雨后含水率 {mh[0]}% 高于干季 {mh[1]}%"))
    if m["high_over_low_ratio"] is not None and m["high_over_low_ratio"] <= 1.0 / p["contra_ratio"]:
        c_lk.append((2, f"高带总盐反远低于低带（比值 {m['high_over_low_ratio']}），不支持渗漏"))
    if pulse is not None and pulse < 1.0:
        c_lk.append((1, f"高带雨后盐分不升反降（{pulse} 倍），无降雨脉冲"))
    out["leak"] = {"supports": s_lk, "contradictions": c_lk}

    # ---- 材料释放 ----
    s_ma, c_ma = [], []
    if not m["cement_repairs"]:
        c_ma.append((2, "墙体模型中不存在水泥修补记录"))
    ratio = m.get("near_over_far_ratio")
    if ratio is not None and ratio >= p["material_ratio"]:
        s_ma.append((2, f"修补层旁总盐为远处的 {ratio} 倍（≥{p['material_ratio']}），横向/就地富集"))
    nm, fm = m.get("near_marker_mean"), m.get("far_marker_mean")
    if nm is not None and fm is not None and fm > 0 and nm / fm >= p["material_ratio"]:
        s_ma.append((2, f"修补层旁 Ca/SO4/OH 标志离子均值为远处的 {r4(nm/fm)} 倍"))
    wd = m.get("near_wet_dry_moisture_delta")
    if wd is not None and abs(wd) < p["moisture_delta_wt"]:
        s_ma.append((1, f"修补层旁干湿含水率差仅 {wd} 个百分点，盐分不随水分脉动，符合固相释放"))
    if ratio is not None and ratio <= 1.0:
        c_ma.append((2, f"修补层旁盐分不高于远处（比值 {ratio}），材料释放不成立"))
    out["material"] = {"supports": s_ma, "contradictions": c_ma}
    return out


def score_candidates(ev: Dict[str, Any], p: Dict[str, Any]) -> Dict[str, Any]:
    res = {}
    for c in CANDIDATES:
        sup, con = ev[c]["supports"], ev[c]["contradictions"]
        score = sum(w for w, _ in sup) - 2 * sum(w for w, _ in con)
        decisive = any(w >= 2 for w, _ in con)
        if decisive or not sup:
            status = "excluded"
        elif score >= p["retain_min_score"]:
            status = "retained"
        else:
            status = "unresolved"
        res[c] = {
            "name": CANDIDATE_CN[c],
            "score": score,
            "status": status,
            "evidence_support": [{"weight": w, "text": t} for w, t in sup],
            "evidence_contradiction": [{"weight": w, "text": t} for w, t in con],
        }
    retained = [c for c in CANDIDATES if res[c]["status"] == "retained"]
    tied_pair = None
    if len(retained) >= 2:
        ss = sorted(((res[c]["score"], c) for c in retained), reverse=True)
        tied_pair = [ss[0][1], ss[1][1]]
        res["_tie_close"] = abs(ss[0][0] - ss[1][0]) < p["tie_eps"]
    else:
        res["_tie_close"] = False
    # 多于两个保留候选时，门控仍针对前两名给建议
    res["_tie"] = tied_pair
    return res


# ---------------------------------------------------------------- 补样建议

def build_advice(gates: Dict[str, bool], scored, m, rows, wall, p) -> List[Dict[str, Any]]:
    advice: List[Dict[str, Any]] = []
    pri = 0

    def add(purpose, target, where, when, action):
        nonlocal pri
        pri += 1
        advice.append({"priority": pri, "purpose": purpose, "target_path": target,
                       "location": where, "timing": when, "action": action})

    if not gates["time"]:
        stages = {r["stage"] for r in rows}
        if "wet" not in stages:
            add("补齐雨后湿阶段样本", "rising/leak",
                "在现有低带与高带竖孔原孔位",
                f"有效降雨（≥{p['rain_mm_min']}mm）后 {p['wet_window_hours']:.0f}h 内",
                "同深度（±%.0fmm）取湿样并记录雨量与雨后小时数" % p["depth_match_mm"])
        if "dry" not in stages:
            add("补齐干季基线样本", "rising/leak", "现有全部竖孔原孔位",
                f"连续无雨 ≥{p['dry_window_hours']:.0f}h 后", "复测全套阴阳离子与含水率")
        if not m.get("n_paired_wet_dry"):
            add("形成干湿配对孔", "rising/leak",
                "选取信息最完整的竖孔（低带与高带各一）",
                "一个完整降雨周期内湿、干各采一次",
                "x/y 归并尺度 %.0fmm，深度误差 ±%.0fmm" % (p["column_xy_bin_mm"],
                                                            p["depth_match_mm"]))
        conflicts = [r for r in rows if r.get("stage_conflict")]
        for r in conflicts:
            add(f"复核申报/推断阶段冲突样本 {r['sample_id']}", "全部候选",
                f"原位（x={r['x_mm']}, y={r['y_mm']}, 深 {r['depth_mm']}mm）",
                f"下次有效降雨后 {p['wet_window_hours']:.0f}h 内与无雨 "
                f"≥{p['dry_window_hours']:.0f}h 后各重采一次",
                f"申报 {r['stage_declared']} 与降雨推断 {r['stage']} 冲突，"
                "以降雨记录为准重采，不得用申报阶段放行")
        gaps = [r for r in rows if r.get("stage_gap") and not r.get("stage_conflict")]
        for r in gaps:
            add(f"补齐时序断档样本 {r['sample_id']}", "全部候选",
                f"原孔位（x={r['x_mm']}, y={r['y_mm']}, 深 {r['depth_mm']}mm）",
                "避开雨后过渡窗口，改在湿窗口与干窗口内采样",
                f"该样本雨后 {r['hours_after_rain']}h 落在 "
                f"{p['wet_window_hours']:.0f}~{p['dry_window_hours']:.0f}h 过渡带，"
                "不能计作湿或干")
    if not gates["charge"]:
        add("闭合电荷账", "全部候选", "对 CBE 超差与代用比例高的样本原位",
            "下次采样同步", "补测缺失极性离子，降低高占比离子的检测限")
    if not gates["tie"]:
        pair = scored.get("_tie")
        loc = {"rising": "中带（距地 %.0f~%.0fmm）加孔" % (p["rise_band_mm"],
                                                        wall["height_mm"] - p["leak_band_mm"]),
               "leak": "近屋面带与疑似裂缝走向",
               "material": "修补层边缘 0/100/300mm 梯度孔"}
        if pair:
            if scored.get("_tie_close"):
                purpose = f"区分并列候选：{CANDIDATE_CN[pair[0]]} vs {CANDIDATE_CN[pair[1]]}"
            else:
                purpose = (f"判别主次并存候选：{CANDIDATE_CN[pair[0]]}（主）vs "
                           f"{CANDIDATE_CN[pair[1]]}（次）")
            add(purpose, f"{pair[0]}/{pair[1]}",
                "；".join(loc[c] for c in pair),
                "雨后与干季各一次", "同孔位干湿配对，加测标志离子与含水率")
        else:
            add("补强未决候选证据", "unresolved", "中带与修补层边缘", "一个降雨周期",
                "按目标路径补梯度孔与干湿配对")
    if gates["time"] and gates["charge"] and gates["tie"]:
        retained = [c for c in CANDIDATES if scored[c]["status"] == "retained"]
        if len(retained) == 1:
            add("验证唯一保留路径的稳健性", retained[0], "现有孔位外扩一条平行断面",
                "下两个降雨周期", "复测梯度与雨后脉冲，确认结论可重复")
    if not advice:
        add("扩大空间覆盖", "全部候选", "墙体中部未采样区域", "下一干湿周期",
            "按三维网格补孔，检查盐峰横向连续性")
    return advice


# ---------------------------------------------------------------- 主入口

def review(wall: Dict[str, Any], samples: List[Dict[str, Any]],
           rains: List[Dict[str, Any]], repairs: List[Dict[str, Any]],
           revisions: Optional[List[Dict[str, Any]]] = None,
           ) -> Dict[str, Any]:
    p = _params(wall)
    revisions = revisions or []
    excluded, rebound, conservative, _ = apply_revisions(samples, revisions)

    evaluated = [evaluate_sample(s, wall, p, rains, rebound) for s in samples]
    excluded_rows = [e for e in evaluated if excluded.get(e["sample_id"])]
    active = [e for e in evaluated if not excluded.get(e["sample_id"])]

    # 门控汇总
    issue_codes = {code for e in active for code, in [(i["code"],) for i in e["issues"]]}
    bases = {e["basis"] for e in active if e["basis"]}
    stages = [e["stage"] for e in active]
    metrics = compute_metrics(active, wall, p, repairs) if active else {}
    evidence = _candidate_evidence(active, metrics, wall, p, rains, repairs) if active else \
        {c: {"supports": [], "contradictions": [(2, "无有效样本")]} for c in CANDIDATES}
    scored = score_candidates(evidence, p) if active else \
        {c: {"name": CANDIDATE_CN[c], "score": -2, "status": "excluded",
             "evidence_support": [], "evidence_contradiction": [
                 {"weight": 2, "text": "无有效样本"}]} for c in CANDIDATES} | {"_tie": None}

    paired = metrics.get("n_paired_wet_dry", 0) if metrics else 0
    conflict_rows = [e for e in active if e.get("stage_conflict")]
    gap_rows = [e for e in active if e.get("stage_gap")]
    # time 门控：湿/干阶段均按降雨推断计数；任何样本存在申报冲突或时序断档都不得放行。
    time_ok = (stages.count("wet") >= p["min_wet_samples"]
               and stages.count("dry") >= p["min_dry_samples"]
               and paired >= 1 and len(rains) >= 1
               and not conflict_rows and not gap_rows)
    tie_ok = scored.get("_tie") is None
    gate_pass = {
        "layer_bind": "layer_bind" not in issue_codes,
        "coord": "coord" not in issue_codes,
        "lod": "lod" not in issue_codes,
        "charge": "charge" not in issue_codes,
        "basis": "basis" not in issue_codes and len(bases) <= 1,
        "time": time_ok,
        "tie": tie_ok,
    }

    gate_details = []
    for code, name, blocking in GATE_ORDER:
        detail = ""
        if not gate_pass[code]:
            if code in ("layer_bind", "coord", "lod", "charge", "basis"):
                detail = "；".join(i["msg"] for e in active for i in e["issues"]
                                   if i["code"] == code)
            elif code == "time":
                lack = []
                if stages.count("wet") < p["min_wet_samples"]:
                    lack.append("缺雨后湿阶段样本（以降雨记录推断为准）")
                if stages.count("dry") < p["min_dry_samples"]:
                    lack.append("缺干季基线样本")
                if paired < 1:
                    lack.append("缺同孔位干湿配对（采样深度/雨后间隔未对齐）")
                if not rains:
                    lack.append("无降雨记录")
                for e in conflict_rows:
                    lack.append(
                        f"{e['sample_id']} 申报 {e['stage_declared']} 与降雨推断 "
                        f"{e['stage']} 冲突，不采用申报值")
                for e in gap_rows:
                    lack.append(f"{e['sample_id']} 时序断档（推断 {e['stage']}，"
                                f"雨后小时数 {e['hours_after_rain']}），不能计入湿/干阶段")
                detail = "；".join(lack)
            elif code == "tie":
                a, b = scored["_tie"]
                if scored.get("_tie_close"):
                    detail = f"{CANDIDATE_CN[a]} 与 {CANDIDATE_CN[b]} 证据势均力敌，无法区分"
                else:
                    detail = (f"{CANDIDATE_CN[a]}（{scored[a]['score']} 分）与 "
                              f"{CANDIDATE_CN[b]}（{scored[b]['score']} 分）同时保留，"
                              "主次并存但不足以排他")
        gate_details.append({"code": code, "name": name, "blocking": blocking,
                             "passed": gate_pass[code], "detail": detail})

    retained = [c for c in CANDIDATES if scored[c]["status"] == "retained"]
    hard_ok = all(gate_pass[c] for c, _, _ in GATE_ORDER)
    if conservative:
        verdict = "indeterminate_conservative"
        unique = None
        reason = "已存在采纳保守解释的修订：即使证据倾向单一来源，也不输出唯一盐源"
    elif hard_ok and len(retained) == 1:
        verdict = "unique_source"
        unique = retained[0]
        reason = "全部门控通过，仅一条候选路径保留"
    elif retained:
        verdict = "indeterminate_multiple"
        unique = None
        reason = "门控未全部通过或候选并列，保留多种解释"
    else:
        verdict = "indeterminate_none"
        unique = None
        reason = "无候选路径达到保留标准，现有证据不足"

    advice = build_advice(gate_pass, scored, metrics, active, wall, p) if active else []

    return {
        "params_effective": p,
        "samples": {
            "active": [_public_sample(e) for e in active],
            "excluded": [{"sample_id": e["sample_id"],
                          "reason": next((r["reason"] for r in revisions
                                          if r["kind"] == "exclude_sample"
                                          and (r.get("payload") or {}).get("sample_id")
                                          == e["sample_id"]), "按修订剔除")}
                         for e in excluded_rows],
            "rebound": [{"sample_id": sid, "new_layer_id": lid,
                         "reason": next((r["reason"] for r in revisions
                                         if r["kind"] == "correct_binding"
                                         and (r.get("payload") or {}).get("sample_id") == sid),
                                        "按修订更正")}
                        for sid, lid in sorted(rebound.items())],
        },
        "gates": gate_details,
        "all_gates_passed": all(g["passed"] for g in gate_details),
        "metrics": metrics,
        "candidates": {c: scored[c] for c in CANDIDATES},
        "verdict": verdict,
        "verdict_reason": reason,
        "unique_source": unique,
        "unique_source_name": CANDIDATE_CN[unique] if unique else None,
        "conservative_mode": conservative,
        "next_sampling": advice,
        "rainfall": [{"rain_id": r["rain_id"], "ts": r["ts"], "rain_mm": r.get("rain_mm")}
                     for r in sorted(rains, key=lambda r: r["ts"])],
        "repairs": [{"repair_id": r["repair_id"], "date": r.get("date"),
                     "material": r.get("material"), "layer_id": r.get("layer_id")}
                    for r in repairs],
    }


def _public_sample(e: Dict[str, Any]) -> Dict[str, Any]:
    return {k: e[k] for k in (
        "sample_id", "layer_id", "x_mm", "y_mm", "z_mm", "depth_mm", "ts", "stage",
        "stage_declared", "stage_conflict", "stage_gap", "hours_after_rain",
        "nearest_rain_id", "stage_note", "moisture_wt", "temp_c", "rh_percent",
        "basis", "total_conc", "balance", "ions", "binding_ok", "coord_ok",
        "suggested_layer", "issues")}

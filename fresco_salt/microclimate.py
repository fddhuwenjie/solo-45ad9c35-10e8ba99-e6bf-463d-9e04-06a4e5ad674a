"""微气候盐结晶循环分析：传感器分区映射、校准/时序/单位核查、潮解—析晶
滞回状态机、逐区循环统计与待判缺口。

纯函数模块，不触碰数据库与 HTTP，便于确认版复算与单元测试。

背景：壁画脱盐之后，库房平均湿度看似平稳，墙角传感器却可能在昼夜波动中
反复越过某类盐的潮解临界（DRH）与析晶临界（CRH）。只看一次取样无法判断
哪处正在经历“潮解（湿润）→ 停留 → 析晶（回返）”的破坏性循环。本模块把
逐时温湿度序列按盐类规则（含温度适用范围、最短持续时间与滞回带）扫描：

  * RH 向上越过 DRH 进入湿润，只有向下越过 CRH（更低）才回到干燥——
    落在 [CRH, DRH] 滞回带内保持原态，避免带内抖动伪造循环；
  * 完整循环 = 越阈湿润且停留 ≥ 最短湿润时长，再越 CRH 回返且
    干燥停留 ≥ 最短干燥时长；短暂跨阈即回（短湿润）只登记为“短时回返”，
    不计完整循环；
  * 时序断档两侧的湿润段不拼接；断档时仍未闭合的湿润段记为开口湿润。

红线（任一不满足，对应区段保持“待判”并列出缺口，绝不输出循环结论）：
  source     引用的来源复核/敷贴试验封版仍无结论
  mapping    传感器映射到样本区多解（重叠/多序列）或无传感器
  calibration 校准失效（读数落在校准有效期之外）
  time       时序断档（相邻读数间隔超阈、缺测、时间戳重复冲突）
  units      单位冲突（同一序列摄氏/华氏、%/小数混用）
  rule_range 规则温度适用范围不足（序列温度超出规则温区）
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from .util import hours_between, parse_dt, r4

MICROCLIMATE_DEFAULTS: Dict[str, Any] = {
    "gap_max_hours": 6.0,            # 相邻读数间隔超过该值即判时序断档
    "drh_conservative_shift_pct": 5.0,   # 保守规则 DRH 自动下调（百分点）
    "crh_conservative_shift_pct": 5.0,   # 保守规则 CRH 自动下调（百分点）
    "temp_conservative_widen_c": 2.0,    # 保守规则温区向两侧放宽（℃）
    "risk_cycles_high": 3,           # 完整循环 ≥ 该值判高风险
    "risk_cycles_moderate": 1,       # ≥ 该值判中风险
}

GATE_ORDER = [
    ("source", "来源版结论", True),
    ("mapping", "传感器—样本区映射", True),
    ("calibration", "校准有效性", True),
    ("time", "采样时序连续", True),
    ("units", "单位一致", True),
    ("rule_range", "规则温度适用范围", True),
]
GATE_CN = {code: name for code, name, _ in GATE_ORDER}

GAP_CN = {
    "source_inconclusive": "来源版无结论",
    "no_sensor": "分区无传感器",
    "multiple_series": "一区多序列",
    "mapping_ambiguous": "映射多解（传感器跨区）",
    "calibration_expired": "校准失效",
    "calibration_missing": "缺校准记录",
    "time_gap": "时序断档",
    "missing_value": "缺测",
    "duplicate_ts": "时间戳重复",
    "unit_conflict": "单位冲突",
    "temp_range_uncovered": "规则温区覆盖不足",
}

TEMP_UNITS = ("C", "F", "K")
RH_UNITS = ("%", "fraction")


class RuleError(ValueError):
    """盐类规则/保守派生错误。"""


# ---------------------------------------------------------------- 参数与几何

def _params(monitor: Dict[str, Any]) -> Dict[str, Any]:
    p = dict(MICROCLIMATE_DEFAULTS)
    p.update(monitor.get("params") or {})
    return p


def point_in_zone(z: Dict[str, Any], x: float, y: float) -> bool:
    """矩形分区（默认）或圆形分区（radius_mm）。"""
    if "radius_mm" in z:
        return ((x - z["x_mm"]) ** 2 + (y - z["y_mm"]) ** 2) ** 0.5 <= z["radius_mm"]
    return (z["x_mm"] <= x <= z["x_mm"] + z["width_mm"]
            and z["y_mm"] <= y <= z["y_mm"] + z["height_mm"])


def validate_rule(rule: Dict[str, Any], where: str = "盐类规则") -> None:
    for k in ("rule_id", "salt", "drh_percent", "crh_percent",
              "temp_min_c", "temp_max_c", "min_wet_hours", "min_dry_hours"):
        if k not in rule:
            raise RuleError(f"{where} 缺字段 {k}")
    if not (0 <= rule["drh_percent"] <= 100 and 0 <= rule["crh_percent"] <= 100):
        raise RuleError(f"{where} {rule['rule_id']} 临界 RH 必须在 0~100（%）")
    if rule["crh_percent"] >= rule["drh_percent"]:
        raise RuleError(f"{where} {rule['rule_id']} 析晶 RH 必须低于潮解 RH"
                        "（滞回带 CRH < DRH），否则状态机无法区分湿润与析晶")
    if rule["temp_min_c"] >= rule["temp_max_c"]:
        raise RuleError(f"{where} {rule['rule_id']} 温度适用范围倒置")
    if rule["min_wet_hours"] < 0 or rule["min_dry_hours"] < 0:
        raise RuleError(f"{where} {rule['rule_id']} 最短持续时间不可为负")
    hyst = rule.get("hysteresis_pct", rule["drh_percent"] - rule["crh_percent"])
    if hyst < 0:
        raise RuleError(f"{where} {rule['rule_id']} 滞回带宽度不可为负")


def derive_conservative_rule(base: Dict[str, Any], override: Dict[str, Any],
                             p: Dict[str, Any]) -> Dict[str, Any]:
    """从已封版规则派生保守规则。

    保守 = 更易报湿润、更难报析晶：DRH 只许下调、CRH 只许下调（滞回带只许
    加宽）、温区只许放宽、最短湿润时长只许缩短。任何反向修改都被拒绝——
    “保守”不能成为收紧阈值的借口。
    """
    derived = dict(base)
    auto_drh = round(max(base["crh_percent"] + 0.1,
                         base["drh_percent"] - p["drh_conservative_shift_pct"]), 4)
    auto_crh = round(max(0.1,
                         base["crh_percent"] - p["crh_conservative_shift_pct"]), 4)
    if auto_crh >= auto_drh:
        auto_crh = round(auto_drh - 0.1, 4)
    derived.update({
        "rule_id": base["rule_id"] + "_conservative",
        "cn": (base.get("cn") or base["salt"]) + "（保守派生）",
        "drh_percent": override.get("drh_percent", auto_drh),
        "crh_percent": override.get("crh_percent", auto_crh),
        "temp_min_c": override.get("temp_min_c",
                                   base["temp_min_c"] - p["temp_conservative_widen_c"]),
        "temp_max_c": override.get("temp_max_c",
                                   base["temp_max_c"] + p["temp_conservative_widen_c"]),
        "min_wet_hours": override.get("min_wet_hours", base["min_wet_hours"]),
        "min_dry_hours": override.get("min_dry_hours", base["min_dry_hours"]),
        "source": "derived_conservative",
        "derived_from_rule_id": base["rule_id"],
    })
    derived["hysteresis_pct"] = round(derived["drh_percent"] - derived["crh_percent"], 4)
    validate_rule(derived, "保守派生规则")
    # 安全方向检查
    if derived["drh_percent"] > base["drh_percent"] + 1e-9:
        raise RuleError("保守规则的潮解 RH 只许下调（更易识别湿润），不得上调")
    if derived["crh_percent"] > base["crh_percent"] + 1e-9:
        raise RuleError("保守规则的析晶 RH 只许下调（须更干燥才算析晶），不得上调")
    if derived["crh_percent"] >= derived["drh_percent"]:
        raise RuleError("保守派生后 CRH 仍须低于 DRH")
    if derived["temp_min_c"] > base["temp_min_c"] + 1e-9:
        raise RuleError("保守规则温区下界只许放宽（更低），不得收紧")
    if derived["temp_max_c"] < base["temp_max_c"] - 1e-9:
        raise RuleError("保守规则温区上界只许放宽（更高），不得收紧")
    if derived["min_wet_hours"] > base["min_wet_hours"] + 1e-9:
        raise RuleError("保守规则最短湿润时长只许缩短（更易计循环），不得延长")
    return derived


# ---------------------------------------------------------------- 修订应用

def apply_monitor_revisions(
    revisions: List[Dict[str, Any]],
    catalog: Dict[str, Dict[str, Any]],
    p: Dict[str, Any],
) -> Tuple[Dict[str, Optional[str]], Dict[str, str], Dict[Tuple[str, str], Dict[str, Any]]]:
    """按 seq 应用微气候修订。

    返回：
      rebind   {sensor_id: zone_id 或 None(解绑)}  改绑/解绑
      adopted  {zone_id: sensor_id}               指定采用序列（解决多解）
      conservative {(zone_id, rule_id): {rule, rev_id, reason}}  保守规则
    """
    rebind: Dict[str, Optional[str]] = {}
    adopted: Dict[str, str] = {}
    conservative: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for rev in sorted(revisions, key=lambda r: r["seq"]):
        kind, payload = rev["kind"], rev.get("payload") or {}
        if kind == "rebind_sensor":
            rebind[payload["sensor_id"]] = payload.get("zone_id")
        elif kind == "adopt_sensor":
            adopted[payload["zone_id"]] = payload["sensor_id"]
        elif kind == "adopt_conservative_rule":
            zid, rid = payload["zone_id"], payload["rule_id"]
            base = catalog.get(rid)
            if base is None:
                continue  # 录入阶段已校验；容错跳过
            override = {k: v for k, v in payload.items()
                        if k in ("drh_percent", "crh_percent", "temp_min_c",
                                 "temp_max_c", "min_wet_hours", "min_dry_hours")}
            try:
                derived = derive_conservative_rule(base, override, p)
            except RuleError:
                continue
            derived["derived_by_rev_id"] = rev["rev_id"]
            conservative[(zid, rid)] = {"rule": derived, "rev_id": rev["rev_id"],
                                        "reason": rev["reason"]}
    return rebind, adopted, conservative


def resolve_mapping(monitor: Dict[str, Any],
                    rebind: Dict[str, Optional[str]],
                    adopted: Dict[str, str]
                    ) -> Tuple[Dict[str, List[str]], Dict[str, List[Dict[str, Any]]]]:
    """计算每个分区生效的传感器序列与映射说明。

    显式改绑（建账时 zone_id 申报、rebind_sensor、adopt_sensor）优先于几何；
    无显式归属的传感器按几何落区：落唯一分区自动映射，跨多分区构成多解——
    多解传感器挂在每个候选分区上标记 mapping_ambiguous（有传感器但映射多解），
    须改绑或 adopt_sensor 指定采用序列后才从其他分区移除。
    """
    zones = monitor["zones"]
    sensors = monitor["sensors"]
    zone_by_id = {z["zone_id"]: z for z in zones}
    notes: Dict[str, List[Dict[str, Any]]] = {z["zone_id"]: [] for z in zones}
    mapping: Dict[str, List[str]] = {z["zone_id"]: [] for z in zones}

    explicit: Dict[str, Optional[str]] = {}
    for s in sensors:
        if s.get("zone_id"):
            explicit[s["sensor_id"]] = s["zone_id"]
    explicit.update(rebind)
    # adopt_sensor 等价于把该传感器显式指派给该分区（解决多解）
    adopted_sensors = set()
    for zid, sid in adopted.items():
        explicit[sid] = zid
        adopted_sensors.add(sid)

    # 几何落区（仅无显式归属的传感器）
    geom: Dict[str, List[str]] = {}
    for s in sensors:
        sid = s["sensor_id"]
        if sid in explicit:
            continue
        geom[sid] = [z["zone_id"] for z in zones
                     if point_in_zone(z, s["x_mm"], s["y_mm"])]

    # 显式绑定（几何不符只记录说明，理由在改绑修订中）
    for sid, zid in explicit.items():
        if zid is None:
            continue
        s = next((x for x in sensors if x["sensor_id"] == sid), None)
        if s is None or zid not in zone_by_id:
            continue
        mapping[zid].append(sid)
        if not point_in_zone(zone_by_id[zid], s["x_mm"], s["y_mm"]):
            notes[zid].append({"code": "rebind_off_geometry", "sensor_id": sid,
                               "msg": f"传感器 {sid} 坐标不在分区 {zid} 几何范围内，"
                                      "按改绑修订映射（理由见修订账）"})

    # 几何落区：唯一落区自动映射；跨区多解挂到每个候选分区
    for sid, hits in geom.items():
        if len(hits) == 1:
            mapping[hits[0]].append(sid)
        elif len(hits) >= 2:
            for zid in hits:
                notes[zid].append({"code": "mapping_ambiguous", "sensor_id": sid,
                                   "msg": f"传感器 {sid} 几何上同时落在多个分区，"
                                          "映射多解；须改绑或指定采用序列"})

    for zid, sid in adopted.items():
        if zid in zone_by_id and sid in mapping[zid]:
            notes[zid].append({"code": "adopted_series", "sensor_id": sid,
                               "msg": f"按修订指定采用传感器 {sid} 的序列代表分区 {zid}"})

    # 多序列裁剪：被 adopt 的传感器固定归属该分区；adopt 后该区仅保留被采用者。
    # 未解决多解时，候选序列与多解标记都保留，由分析层判待判。
    final: Dict[str, List[str]] = {}
    for z in zones:
        zid = z["zone_id"]
        sids = mapping.get(zid, [])
        seen, uniq = set(), []
        for sid in sids:
            if sid not in seen:
                seen.add(sid)
                uniq.append(sid)
        choice = adopted.get(zid)
        final[zid] = [choice] if choice and choice in uniq else uniq
    return final, notes


# ---------------------------------------------------------------- 单位与校准

def convert_temp(v: float, unit: str) -> float:
    if unit == "C":
        return v
    if unit == "F":
        return (v - 32.0) * 5.0 / 9.0
    if unit == "K":
        return v - 273.15
    raise ValueError(f"温度单位 {unit!r} 不受支持（C/F/K）")


def convert_rh(v: float, unit: str) -> float:
    if unit == "%":
        return v
    if unit == "fraction":
        return v * 100.0
    raise ValueError(f"湿度单位 {unit!r} 不受支持（%/fraction）")


def active_calibration(sensor: Dict[str, Any], ts: datetime
                       ) -> Optional[Dict[str, Any]]:
    """取读数时刻生效的校准记录（calibrated_ts ≤ ts 中最新的一条）。"""
    cals = sorted(sensor.get("calibrations") or sensor.get("calibration_list") or [],
                  key=lambda c: parse_dt(c["calibrated_ts"]))
    chosen = None
    for c in cals:
        if parse_dt(c["calibrated_ts"]) <= ts:
            chosen = c
        else:
            break
    return chosen


def normalize_series(sensor: Dict[str, Any], readings: List[Dict[str, Any]],
                     p: Dict[str, Any]) -> Dict[str, Any]:
    """一条传感器序列归一化：单位统一、校准覆盖与偏移、缺测/断档检查。

    输出 points（valid=True 的点才可进入状态机）与 sensor 级缺口说明。
    """
    sid = sensor["sensor_id"]
    def_tu, def_rhu = sensor.get("temp_unit", "C"), sensor.get("rh_unit", "%")
    gaps: List[Dict[str, Any]] = []
    units_seen_t, units_seen_rh = set(), set()

    rows = []
    for r in readings:
        tu = r.get("temp_unit", def_tu)
        rhu = r.get("rh_unit", def_rhu)
        units_seen_t.add(tu)
        units_seen_rh.add(rhu)
        rows.append((r, tu, rhu))
    rows.sort(key=lambda x: parse_dt(x[0]["ts"]))

    conflict = (not units_seen_t.issubset({def_tu}) or len(units_seen_t) > 1
                or not units_seen_rh.issubset({def_rhu}) or len(units_seen_rh) > 1)
    if conflict:
        gaps.append({"code": "unit_conflict", "sensor_id": sid,
                     "msg": f"传感器 {sid} 序列单位冲突：温度 {sorted(map(str, units_seen_t))}、"
                            f"湿度 {sorted(map(str, units_seen_rh))}，"
                            f"登记单位为 {def_tu}/{def_rhu}"})

    points: List[Dict[str, Any]] = []
    dup_seen: Dict[datetime, int] = {}
    uncovered: List[datetime] = []
    missing: List[datetime] = []
    for r, tu, rhu in rows:
        ts = parse_dt(r["ts"])
        dup_seen[ts] = dup_seen.get(ts, 0) + 1
        temp_raw, rh_raw = r.get("temp"), r.get("rh")
        reasons = []
        try:
            temp = convert_temp(float(temp_raw), tu) if temp_raw is not None else None
            rh = convert_rh(float(rh_raw), rhu) if rh_raw is not None else None
        except (TypeError, ValueError):
            temp, rh = None, None
            reasons.append("bad_unit")
        if temp is None or rh is None:
            missing.append(ts)
            reasons.append("missing")
        cal = active_calibration(sensor, ts)
        if cal is None or ts > parse_dt(cal["valid_until_ts"]):
            uncovered.append(ts)
            reasons.append("calibration")
        else:
            if temp is not None:
                temp += cal.get("temp_offset_c", 0.0) or 0.0
            if rh is not None:
                rh += cal.get("rh_offset_pct", 0.0) or 0.0
        points.append({"ts": ts, "temp_c": temp, "rh_percent": rh,
                       "valid": not reasons, "invalid_reasons": reasons,
                       "reading_id": r.get("reading_id")})

    dup_ts = sorted(t for t, n in dup_seen.items() if n > 1)
    if dup_ts:
        gaps.append({"code": "duplicate_ts", "sensor_id": sid,
                     "msg": f"传感器 {sid} 存在 {len(dup_ts)} 个重复时间戳",
                     "ts": [t.isoformat() for t in dup_ts]})
    if missing:
        gaps.append({"code": "missing_value", "sensor_id": sid,
                     "msg": f"传感器 {sid} 有 {len(missing)} 个缺测/不可解析读数",
                     "ts": [t.isoformat() for t in missing[:20]]})
    if uncovered:
        gaps.append({"code": "calibration_expired", "sensor_id": sid,
                     "msg": f"传感器 {sid} 有 {len(uncovered)} 个读数落在校准有效期之外"
                            "（含校准缺失/已过期）",
                     "ts": [t.isoformat() for t in uncovered[:20]]})

    # 时序断档：相邻读数（不论是否有效）间隔超阈
    gap_iv: List[Dict[str, str]] = []
    all_ts = sorted(dup_seen)
    for a, b in zip(all_ts, all_ts[1:]):
        h = hours_between(b, a)
        if h > p["gap_max_hours"]:
            gap_iv.append({"from_ts": a.isoformat(), "to_ts": b.isoformat(),
                           "gap_hours": r4(h)})
    if gap_iv:
        gaps.append({"code": "time_gap", "sensor_id": sid,
                     "msg": f"传感器 {sid} 有 {len(gap_iv)} 处时序断档"
                            f"（> {p['gap_max_hours']:g}h）",
                     "intervals": gap_iv})
    return {"sensor_id": sid, "points": points, "gaps": gaps,
            "temp_unit": def_tu, "rh_unit": def_rhu,
            "n_points": len(points)}


# ---------------------------------------------------------------- 滞回状态机

def _segments(points: List[Dict[str, Any]], rule: Dict[str, Any],
              gap_max_hours: float) -> Tuple[List[List[Dict[str, Any]]],
                                             List[Dict[str, Any]]]:
    """把有效点切成连续段：缺测/校准失效/温区外点与超时间隔都断开。

    温区外点另记入 out_of_range（规则范围不足缺口）。
    """
    out_range: List[Dict[str, Any]] = []
    segs: List[List[Dict[str, Any]]] = []
    cur: List[Dict[str, Any]] = []
    prev_ts: Optional[datetime] = None
    for pt in points:
        ts = pt["ts"]
        split = not pt["valid"]
        if pt["valid"] and not (rule["temp_min_c"] <= pt["temp_c"] <= rule["temp_max_c"]):
            out_range.append({"ts": ts.isoformat(), "temp_c": r4(pt["temp_c"]),
                              "range": [rule["temp_min_c"], rule["temp_max_c"]]})
            split = True
        if split:
            if cur:
                segs.append(cur)
                cur = []
            prev_ts = ts if pt["valid"] else None
            continue
        if prev_ts is not None and hours_between(ts, prev_ts) > gap_max_hours:
            segs.append(cur)
            cur = []
        cur.append(pt)
        prev_ts = ts
    if cur:
        segs.append(cur)
    return segs, out_range


def _cross_time(a: Dict[str, Any], b: Dict[str, Any], thr: float) -> datetime:
    """a→b 跨越阈值 thr 的线性插值时刻。"""
    frac = (thr - a["rh_percent"]) / (b["rh_percent"] - a["rh_percent"])
    return a["ts"] + (b["ts"] - a["ts"]) * frac


def scan_rule(points: List[Dict[str, Any]], rule: Dict[str, Any],
              gap_max_hours: float) -> Dict[str, Any]:
    """在一条序列上扫描某盐类规则的潮解—析晶循环（含滞回带）。"""
    drh, crh = rule["drh_percent"], rule["crh_percent"]
    min_wet, min_dry = rule["min_wet_hours"], rule["min_dry_hours"]
    segs, out_range = _segments(points, rule, gap_max_hours)

    cycles: List[Dict[str, Any]] = []
    short_returns: List[Dict[str, Any]] = []
    crossings: List[Dict[str, Any]] = []
    open_wet: Optional[Dict[str, Any]] = None
    idx = 0
    wet_qualified_durations: List[float] = []
    first_risk_ts: Optional[datetime] = None

    for seg in segs:
        if len(seg) < 2:
            continue
        wet = seg[0]["rh_percent"] >= drh
        wet_start = seg[0]["ts"] if wet else None
        last_wet_end: Optional[datetime] = None
        wet_spells: List[Tuple[datetime, Optional[datetime]]] = []
        dry_spells: List[Tuple[datetime, datetime]] = []
        if wet:
            crossings.append({"kind": "up_drh", "ts": wet_start,
                              "at_segment_start": True})
        for a, b in zip(seg, seg[1:]):
            va, vb = a["rh_percent"], b["rh_percent"]
            if not wet and va <= drh < vb:
                t = _cross_time(a, b, drh)
                wet, wet_start = True, t
                crossings.append({"kind": "up_drh", "ts": t})
                if last_wet_end is not None:
                    dry_spells.append((last_wet_end, t))
            elif wet and va >= crh > vb:
                t = _cross_time(a, b, crh)
                wet = False
                wet_spells.append((wet_start, t))
                last_wet_end = t
                crossings.append({"kind": "down_crh", "ts": t})
        # 末段若停留在干燥态，最后一次析晶到序列末端也是一段干燥停留
        if not wet and last_wet_end is not None:
            tail = seg[-1]["ts"]
            if tail > last_wet_end:
                dry_spells.append((last_wet_end, tail))
        if wet:
            wet_spells.append((wet_start, None))

        # 干燥段索引：第 k 个闭合湿润段之后的干燥段
        dry_after: List[Optional[Tuple[datetime, datetime]]] = []
        for i in range(len([w for w in wet_spells if w[1] is not None])):
            dry_after.append(dry_spells[i] if i < len(dry_spells) else None)

        closed_n = 0
        for ws, we in wet_spells:
            if we is None:
                hours = hours_between(seg[-1]["ts"], ws)
                if hours >= min_wet:
                    # 达标的开口湿润段同样参与最长湿润段比较：
                    # 期末/断档未析晶不代表该段不破坏，漏计会低估风险
                    wet_qualified_durations.append(hours)
                    open_wet = {"start_ts": ws.isoformat(), "hours": r4(hours),
                                "segment_end_ts": seg[-1]["ts"].isoformat()}
                    if first_risk_ts is None or ws < first_risk_ts:
                        first_risk_ts = ws
                continue
            wet_hours = hours_between(we, ws)
            qualified_wet = wet_hours >= min_wet
            if qualified_wet:
                wet_qualified_durations.append(wet_hours)
                if first_risk_ts is None or ws < first_risk_ts:
                    first_risk_ts = ws
            dry = dry_after[closed_n]
            closed_n += 1
            if not qualified_wet:
                short_returns.append({"wet_start_ts": ws.isoformat(),
                                      "wet_end_ts": we.isoformat(),
                                      "wet_hours": r4(wet_hours),
                                      "min_wet_hours": min_wet})
                continue
            complete, reason = False, None
            if dry is None:
                reason = "series_ended_before_dry_dwell"
                dry_hours = None
            else:
                dry_hours = hours_between(dry[1], dry[0])
                if dry_hours < min_dry:
                    reason = "dry_dwell_too_short"
                else:
                    complete = True
            idx += 1
            cycles.append({"index": idx, "complete": complete,
                           "incomplete_reason": reason,
                           "wet_start_ts": ws.isoformat(),
                           "wet_end_ts": we.isoformat(),
                           "wet_hours": r4(wet_hours),
                           "dry_end_ts": dry[1].isoformat() if dry else None,
                           "dry_hours": r4(dry_hours) if dry_hours is not None else None,
                           "min_wet_hours": min_wet, "min_dry_hours": min_dry})

    complete = [c for c in cycles if c["complete"]]
    return {
        "n_segments": len(segs),
        "crossings": [{"kind": c["kind"], "ts": c["ts"].isoformat(),
                       "at_segment_start": c.get("at_segment_start", False)}
                      for c in crossings],
        "cycles": cycles,
        "n_complete_cycles": len(complete),
        "short_returns": short_returns,
        "n_short_returns": len(short_returns),
        "longest_wet_hours": r4(max(wet_qualified_durations, default=None)),
        "open_wet": open_wet,
        "first_risk_ts": first_risk_ts.isoformat() if first_risk_ts else None,
        "out_of_range": out_range,
        "thresholds": {"drh_percent": drh, "crh_percent": crh,
                       "hysteresis_pct": rule.get("hysteresis_pct", drh - crh)},
    }


# ---------------------------------------------------------------- 逐区装配

def _zone_rules(monitor: Dict[str, Any], zid: str,
                catalog: Dict[str, Dict[str, Any]],
                conservative: Dict[Tuple[str, str], Dict[str, Any]]
                ) -> List[Tuple[Dict[str, Any], Optional[Dict[str, Any]]]]:
    """返回分区适用的 (生效规则, 保守派生信息)；zone_salts 指定否则全部规则。"""
    zs = monitor.get("zone_salts")
    if zs:
        rids = [q["rule_id"] for q in zs if q["zone_id"] == zid]
    else:
        rids = sorted(catalog)
    out = []
    for rid in rids:
        base = catalog.get(rid)
        if base is None:
            continue
        cons = conservative.get((zid, rid))
        out.append((cons["rule"] if cons else base, cons))
    return out


def analyze_zone(zone: Dict[str, Any], monitor: Dict[str, Any],
                 readings_by_sensor: Dict[str, List[Dict[str, Any]]],
                 mapped: List[str], map_notes: List[Dict[str, Any]],
                 source: Dict[str, Any],
                 catalog: Dict[str, Dict[str, Any]],
                 conservative: Dict[Tuple[str, str], Dict[str, Any]],
                 p: Dict[str, Any]) -> Dict[str, Any]:
    zid = zone["zone_id"]
    gaps: List[Dict[str, Any]] = []
    gates: Dict[str, bool] = {code: True for code, _, _ in GATE_ORDER}
    detail: Dict[str, List[str]] = {code: [] for code, _, _ in GATE_ORDER}

    # ---- 来源版结论 ----
    if not source.get("decisive"):
        gates["source"] = False
        detail["source"].append(
            f"引用的{source.get('kind_name', '来源封版')} {source.get('version_id')} "
            f"仍无结论（{source.get('summary', '状态未知')}），"
            "盐类行为未经封版确认，循环统计不得作数")
        gaps.append({"code": "source_inconclusive",
                     "msg": detail["source"][0],
                     "source_version_id": source.get("version_id"),
                     "source_kind": source.get("kind")})

    # ---- 映射 ----
    amb_sensors = sorted({n["sensor_id"] for n in map_notes
                          if n["code"] == "mapping_ambiguous"})
    if not mapped and not amb_sensors:
        gates["mapping"] = False
        detail["mapping"].append(f"分区 {zid} 没有任何传感器，无法评估")
        gaps.append({"code": "no_sensor", "msg": detail["mapping"][0]})
    elif not mapped and amb_sensors:
        gates["mapping"] = False
        msg = (f"传感器 {amb_sensors} 几何上同时落在多个分区，{zid} 无唯一映射序列，"
               "须改绑传感器或指定采用序列并说明理由")
        detail["mapping"].append(msg)
        gaps.append({"code": "mapping_ambiguous", "msg": msg,
                     "sensor_ids": amb_sensors})
    elif len(mapped) > 1:
        gates["mapping"] = False
        msg = (f"分区 {zid} 同时存在多条传感器序列 {mapped}，无法确定代表序列，"
               "须改绑或指定采用序列并说明理由")
        detail["mapping"].append(msg)
        gaps.append({"code": "multiple_series", "msg": msg, "sensor_ids": mapped})

    # ---- 序列级核查（校准/时序/单位） ----
    series = None
    if len(mapped) == 1:
        sid = mapped[0]
        sensor = next(s for s in monitor["sensors"] if s["sensor_id"] == sid)
        series = normalize_series(sensor, readings_by_sensor.get(sid, []), p)
        if len(series["points"]) < 2:
            gates["time"] = False
            detail["time"].append(f"传感器 {sid} 有效读数不足 2 个，无法识别跨阈循环")
            gaps.append({"code": "time_gap", "sensor_id": sid,
                         "msg": detail["time"][-1]})
        for g in series["gaps"]:
            code = g["code"]
            if code in ("calibration_expired",):
                gates["calibration"] = False
            elif code in ("time_gap", "missing_value", "duplicate_ts"):
                gates["time"] = False
            elif code == "unit_conflict":
                gates["units"] = False
            gaps.append(g)
            detail[code if code in detail else "time"].append(g["msg"])

    # ---- 逐盐类规则扫描 ----
    rule_rows: List[Dict[str, Any]] = []
    for rule, cons in _zone_rules(monitor, zid, catalog, conservative):
        row: Dict[str, Any] = {"rule_id": rule["rule_id"], "salt": rule["salt"],
                               "cn": rule.get("cn", rule["salt"]),
                               "drh_percent": rule["drh_percent"],
                               "crh_percent": rule["crh_percent"],
                               "temp_min_c": rule["temp_min_c"],
                               "temp_max_c": rule["temp_max_c"],
                               "min_wet_hours": rule["min_wet_hours"],
                               "min_dry_hours": rule["min_dry_hours"],
                               "conservative": bool(cons)}
        if cons:
            row["derived"] = {"rev_id": cons["rev_id"], "reason": cons["reason"],
                              "derived_from_rule_id": rule.get("derived_from_rule_id")}
        if series is None or len(series["points"]) < 2:
            row.update({"assessable": False,
                        "reason": "no_effective_series" if not mapped
                        else "series_invalid"})
            rule_rows.append(row)
            continue
        scan = scan_rule(series["points"], rule, p["gap_max_hours"])
        if scan["out_of_range"]:
            gates["rule_range"] = False
            ts = [x["ts"] for x in scan["out_of_range"]]
            msg = (f"规则 {rule['rule_id']}（{rule.get('cn', rule['salt'])}）温度适用范围 "
                   f"[{rule['temp_min_c']}, {rule['temp_max_c']}]℃ 覆盖不足："
                   f"{len(ts)} 个读数超出（如 {ts[0]}）；可在留理由后采用放宽温区的"
                   "保守派生规则")
            detail["rule_range"].append(msg)
            gaps.append({"code": "temp_range_uncovered", "rule_id": rule["rule_id"],
                         "msg": msg, "points": scan["out_of_range"][:20]})
        row.update({"assessable": True, **scan})
        rule_rows.append(row)

    if not any(r.get("assessable") for r in rule_rows) and mapped:
        # 没有任何可评估规则（温区外导致全部不可算时 scan 仍给出，故仅在缺规则时）
        pass

    # ---- 门控明细 ----
    gate_details = []
    for code, name, blocking in GATE_ORDER:
        gate_details.append({"code": code, "name": name, "blocking": blocking,
                             "passed": gates[code],
                             "detail": "；".join(detail[code])})

    pending = not all(gates.values())
    # 仅汇总无阻塞规则的统计
    assessable = [r for r in rule_rows if r.get("assessable")
                  and not r.get("out_of_range")]
    n_cycles = sum(r["n_complete_cycles"] for r in assessable)
    longs = [r["longest_wet_hours"] for r in assessable
             if r["longest_wet_hours"] is not None]
    risks = [(r["first_risk_ts"], r["rule_id"]) for r in assessable
             if r["first_risk_ts"]]
    risks.sort()
    opens = [r for r in assessable if r.get("open_wet")]

    if pending:
        status, risk_level = "pending", "unknown"
    else:
        status = "assessed"
        if opens:
            risk_level = "current_wet"
        elif n_cycles >= p["risk_cycles_high"]:
            risk_level = "high"
        elif n_cycles >= p["risk_cycles_moderate"]:
            risk_level = "moderate"
        else:
            risk_level = "low"

    return {
        "zone_id": zid, "name": zone.get("name", zid), "geometry": zone,
        "status": status, "risk_level": risk_level,
        "mapped_sensor_ids": mapped,
        "mapping_notes": map_notes,
        "gates": gate_details,
        "all_gates_passed": not pending,
        "gaps": gaps,
        "rules": rule_rows,
        "n_complete_cycles": n_cycles,
        "longest_wet_hours": r4(max(longs, default=None)),
        "first_risk_ts": risks[0][0] if risks else None,
        "first_risk_rule_id": risks[0][1] if risks else None,
        "open_wet": ({"rule_id": opens[0]["rule_id"], **opens[0]["open_wet"]}
                     if opens else None),
        "series_points": ([{"ts": pt["ts"].isoformat(),
                            "temp_c": r4(pt["temp_c"]),
                            "rh_percent": r4(pt["rh_percent"]),
                            "valid": pt["valid"],
                            "invalid_reasons": pt["invalid_reasons"]}
                           for pt in series["points"]] if series else []),
    }


# ---------------------------------------------------------------- 建议

def build_microclimate_advice(zones: List[Dict[str, Any]],
                              p: Dict[str, Any]) -> List[Dict[str, Any]]:
    advice: List[Dict[str, Any]] = []
    pri = 0

    def add(purpose, where, when, action):
        nonlocal pri
        pri += 1
        advice.append({"priority": pri, "purpose": purpose, "location": where,
                       "timing": when, "action": action})

    for z in zones:
        zid, name = z["zone_id"], z["name"]
        codes = {g["code"] for g in z["gaps"]}
        if "source_inconclusive" in codes:
            add("先闭合来源结论", f"{name}（{zid}）", "微气候判定前",
                "来源复核/敷贴试验封版仍无结论，先按补样建议完成来源复核并重新封版，"
                "再解释微气候循环")
        if "no_sensor" in codes:
            add("补装温湿度传感器", f"{name}（{zid}）", "下一入库周期前",
                "该分区无传感器，无法识别昼夜跨阈；在分区几何范围内加装并登记校准记录")
        if "mapping_ambiguous" in codes or "multiple_series" in codes:
            add("消除传感器映射多解", f"{name}（{zid}）", "下一次分析前",
                "提交 rebind_sensor 改绑（须说明现场布设理由）或 adopt_sensor "
                "指定代表序列并派生修订；多解解除前分区保持待判")
        if "calibration_expired" in codes:
            add("重新校准传感器", f"{name}（{zid}）", "立即",
                "部分读数超出校准有效期；送检/现场标定后登记新校准记录"
                "（calibrated_ts/valid_until_ts），失效时段保持待判")
        if "unit_conflict" in codes:
            add("统一温湿度单位", f"{name}（{zid}）", "下一次数据入库前",
                "同一序列出现 ℃/℉/K 或 %/小数混用；核对导出配置并统一到 ℃ 与 RH%，"
                "冲突解除前不得计循环")
        if "time_gap" in codes or "missing_value" in codes:
            add("补齐时序断档", f"{name}（{zid}）", "下一监测周期",
                f"加密采集使相邻读数间隔 ≤ {p['gap_max_hours']:g}h，排查断电/丢包；"
                "断档两侧湿润段不拼接")
        if "temp_range_uncovered" in codes:
            add("处理规则温区不足", f"{name}（{zid}）", "下一次分析前",
                "现场温度超出盐类规则适用温区：可补做该温区的潮解/析晶实验更新规则，"
                "或提交 adopt_conservative_rule 留理由派生放宽温区的保守规则")
        if z["status"] == "assessed" and z["risk_level"] in ("high", "current_wet"):
            add("优先巡查并稳定微气候", f"{name}（{zid}）", "即刻",
                f"该区已记录 {z['n_complete_cycles']} 个完整潮解—析晶循环"
                + ("且当前处于开口湿润段" if z["risk_level"] == "current_wet" else "")
                + "；优先把 RH 持续压在 CRH 以下并减小昼夜波动，复查表面粉化/白霜")
    if not advice:
        add("维持连续监测", "全部分区", "下一监测周期",
            "保持采集间隔与校准周期连续，关注季节性 RH 抬升")
    return advice


# ---------------------------------------------------------------- 主入口

def analyze_monitor(wall: Dict[str, Any], monitor: Dict[str, Any],
                    readings: List[Dict[str, Any]],
                    revisions: Optional[List[Dict[str, Any]]],
                    source_info: Dict[str, Any]) -> Dict[str, Any]:
    p = _params(monitor)
    revisions = revisions or []
    catalog = {r["rule_id"]: r for r in monitor.get("rules") or []}
    rebind, adopted, conservative = apply_monitor_revisions(revisions, catalog, p)
    mapping, notes = resolve_mapping(monitor, rebind, adopted)

    readings_by_sensor: Dict[str, List[Dict[str, Any]]] = {}
    for r in readings:
        readings_by_sensor.setdefault(r["sensor_id"], []).append(r)

    sensor_by_id = {s["sensor_id"]: s for s in monitor["sensors"]}
    zones_out = []
    for zone in monitor["zones"]:
        z = analyze_zone(zone, monitor, readings_by_sensor,
                         mapping.get(zone["zone_id"], []),
                         notes.get(zone["zone_id"], []),
                         source_info, catalog, conservative, p)
        zones_out.append(z)

    revisions_applied = [{"seq": r["seq"], "rev_id": r["rev_id"], "kind": r["kind"],
                          "reason": r["reason"], "payload": r.get("payload")}
                         for r in sorted(revisions, key=lambda x: x["seq"])]
    return {
        "monitor_id": monitor["monitor_id"],
        "wall_id": monitor["wall_id"],
        "params_effective": p,
        "source": source_info,
        "sensor_registry": [
            {"sensor_id": s["sensor_id"], "x_mm": s["x_mm"], "y_mm": s["y_mm"],
             "declared_zone_id": s.get("zone_id"),
             "temp_unit": s.get("temp_unit", "C"), "rh_unit": s.get("rh_unit", "%"),
             "n_calibrations": len(s.get("calibrations") or []),
             "n_readings": len(readings_by_sensor.get(s["sensor_id"], []))}
            for s in monitor["sensors"]],
        "rule_catalog": [{"rule_id": r["rule_id"], "salt": r["salt"],
                          "cn": r.get("cn", r["salt"]),
                          "drh_percent": r["drh_percent"],
                          "crh_percent": r["crh_percent"],
                          "hysteresis_pct": r.get("hysteresis_pct",
                                                  r["drh_percent"] - r["crh_percent"]),
                          "temp_min_c": r["temp_min_c"], "temp_max_c": r["temp_max_c"],
                          "min_wet_hours": r["min_wet_hours"],
                          "min_dry_hours": r["min_dry_hours"]}
                         for r in sorted(catalog.values(), key=lambda r: r["rule_id"])],
        "zones": zones_out,
        "n_zones": len(zones_out),
        "n_pending": sum(1 for z in zones_out if z["status"] == "pending"),
        "n_assessed": sum(1 for z in zones_out if z["status"] == "assessed"),
        "revisions_applied": revisions_applied,
        "advice": build_microclimate_advice(zones_out, p),
    }

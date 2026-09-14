"""敷贴脱盐试验核算：逐轮浸出、墙内库存、对照漂移、收支闭合与结束判定。

纯函数模块，不触碰数据库与 HTTP，便于确认版复算与单元测试。

背景：敷贴揭下后表面电导下降，不等于盐已离墙——盐可能只从颜料层退到
地仗深处，下一次受潮又返回表面。因此本模块把每轮浸出液带走的离子量、
处理区分层库存变化与邻近对照区漂移放在同一本账上核算，区分：

  net_removal           净移除：收支闭合，盐确实被浸出液带离墙体
  inward_migration      向内迁移：深层浓度升高，盐只是退到地杖深处
  insufficient_evidence 证据不足：门控未过或收支不闭合

六道门控任一失败即不得建议结束处理：
  pairing  前后样本无法配对 / rounds 轮次覆盖时段重叠 / basis 固液基准混用 /
  lod      浸出液缺检测限   / balance 离子收支不闭合   / deep_rise 深层浓度升高
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .chemistry import ChemistryError, charge_balance, normalize_ions
from .util import parse_dt, r4

TRIAL_DEFAULTS: Dict[str, Any] = {
    "column_xy_bin_mm": 50.0,        # 同位置配对的 x/y 归并尺度
    "depth_match_mm": 5.0,           # 前后配对深度容差
    "censored_max_fraction": 0.5,    # 浸出液半检出限代用比例上限
    "balance_tolerance": 0.25,       # 收支闭合容差（相对收支较大侧）
    "deep_layer_min_depth_mm": 20.0,  # 深层分界（自表层起算的深度）
    "deep_rise_ratio": 1.10,         # 深层浓度升高判定比（且须超过对照漂移）
    "end_excess_ratio": 1.10,        # 处理区残盐/对照区 ≤ 该值才建议结束
}

ZONES = ("treatment", "control")
PHASES = ("pre", "post")
ZONE_CN = {"treatment": "处理区", "control": "对照区"}
PHASE_CN = {"pre": "处理前", "post": "处理后"}

CLASSIFICATION_CN = {
    "net_removal": "净移除",
    "inward_migration": "向内迁移",
    "insufficient_evidence": "证据不足",
}

TRIAL_GATE_ORDER = [
    ("pairing", "前后样本配对", True),
    ("rounds", "轮次覆盖时段", True),
    ("basis", "固/液相基准", True),
    ("lod", "浸出液检测限", True),
    ("balance", "离子收支闭合", True),
    ("deep_rise", "深层浓度变化", True),
]

DECISION_CN = {
    "end_treatment": "建议结束处理",
    "continue_treatment": "继续处理",
    "hold_inward_migration": "不得结束（盐向内迁移）",
    "hold_insufficient_evidence": "不得结束（证据不足）",
}


# ---------------------------------------------------------------- 基础工具

def _params(trial: Dict[str, Any]) -> Dict[str, Any]:
    p = dict(TRIAL_DEFAULTS)
    p.update(trial.get("params") or {})
    return p


def _depth(s: Dict[str, Any]) -> Optional[float]:
    d = s.get("depth_mm")
    return d if d is not None else s.get("z_mm")


# ---------------------------------------------------------------- 试验修订

def apply_trial_revisions(bindings: List[Dict[str, Any]],
                          revisions: List[Dict[str, Any]]
                          ) -> Tuple[Dict[str, str], List[Dict[str, Any]]]:
    """按 seq 应用试验修订：排除/恢复污染浸出液、改绑/解绑配对样本。

    返回 (被剔除轮次 {round_id: reason}, 生效绑定列表)。
    """
    excluded: Dict[str, str] = {}
    eff: Dict[str, Dict[str, Any]] = {b["sample_id"]: dict(b) for b in bindings}
    for rev in sorted(revisions, key=lambda r: r["seq"]):
        kind = rev["kind"]
        payload = rev.get("payload") or {}
        if kind == "exclude_extract":
            excluded[payload["round_id"]] = rev["reason"]
        elif kind == "restore_extract":
            excluded.pop(payload["round_id"], None)
        elif kind == "unbind_sample":
            eff.pop(payload["sample_id"], None)
        elif kind == "rebind_sample":
            b = eff.get(payload["sample_id"])
            if b is not None:
                if payload.get("zone"):
                    b["zone"] = payload["zone"]
                if payload.get("phase"):
                    b["phase"] = payload["phase"]
    return excluded, list(eff.values())


# ---------------------------------------------------------------- 轮次核算

def evaluate_round(rnd: Dict[str, Any], p: Dict[str, Any]) -> Dict[str, Any]:
    """归一化一轮浸出液并核算本轮各离子移出量（mmol = mmol/L × L）。"""
    rid = rnd["round_id"]
    ext = rnd.get("extract") or {}
    volume = ext.get("volume_l")
    issues: List[Dict[str, Any]] = []
    norm: Dict[str, Any] = {}
    basis: Optional[str] = None
    balance: Optional[Dict[str, Any]] = None
    try:
        norm, basis = normalize_ions(ext.get("ions") or {})
        balance = charge_balance(norm)
        if basis != "liquid":
            issues.append({"code": "basis",
                           "msg": f"轮次 {rid} 浸出液为固相基准，浸出液必须用液相单位"
                                  "（mmol/L 等），固液基准不可混账"})
        if balance["missing_lod"]:
            issues.append({"code": "lod",
                           "msg": f"轮次 {rid} 浸出液缺检测限："
                                  f"{', '.join(balance['missing_lod'])}"})
        if (balance["censored_fraction"] or 0) > p["censored_max_fraction"]:
            issues.append({"code": "lod",
                           "msg": f"轮次 {rid} 半检出限代用离子占比 "
                                  f"{balance['censored_fraction']:.0%}，浸出量不可信"})
    except ChemistryError as exc:
        msg = str(exc)
        code = "lod" if ("检出限" in msg or "lod" in msg) else "basis"
        issues.append({"code": code, "msg": f"轮次 {rid}: {msg}"})
    removed = {ion: norm[ion].value * volume for ion in norm} \
        if isinstance(volume, (int, float)) else {}
    return {
        "round_id": rid,
        "cover_start_ts": rnd.get("cover_start_ts"),
        "cover_end_ts": rnd.get("cover_end_ts"),
        "volume_l": volume,
        "basis": basis,
        "ions": {k: {"value": i.value, "detected": i.detected, "lod": i.lod,
                     "censored": i.censored} for k, i in norm.items()},
        "removed": removed,
        "removed_total": sum(removed.values()),
        "missing_lod": balance["missing_lod"] if balance else [],
        "censored_ions": balance["lod_censored_ions"] if balance else [],
        "issues": issues,
    }


def _find_overlaps(rounds: List[Dict[str, Any]]) -> List[Tuple[str, str]]:
    """覆盖时段两两重叠检查（含被剔除轮次：敷贴物理上仍覆盖过墙面）。"""
    iv = []
    for r in rounds:
        try:
            iv.append((parse_dt(r["cover_start_ts"]), parse_dt(r["cover_end_ts"]),
                       r["round_id"]))
        except (ValueError, TypeError, KeyError):
            continue
    iv.sort()
    out = []
    for i in range(len(iv)):
        for j in range(i + 1, len(iv)):
            if iv[j][0] < iv[i][1]:
                out.append((iv[i][2], iv[j][2]))
    return out


# ---------------------------------------------------------------- 配对与库存

def _build_norm_map(eff_bindings: List[Dict[str, Any]],
                    samples_by_id: Dict[str, Any]
                    ) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """归一化生效绑定的墙样；墙内库存必须固相基准，液相样本混入即记基准问题。"""
    norm_map: Dict[str, Any] = {}
    issues: List[Dict[str, Any]] = []
    for b in eff_bindings:
        sid = b["sample_id"]
        if sid in norm_map or sid not in samples_by_id:
            continue
        s = samples_by_id[sid]
        try:
            ions, basis = normalize_ions(s["ions"])
            norm_map[sid] = {"ions": ions, "basis": basis, "error": None}
            if basis != "solid":
                issues.append({"code": "basis",
                               "msg": f"绑定样本 {sid} 为液相基准，墙内库存须用固相"
                                      "（mmol/kg_dry），固液基准不可混账"})
        except ChemistryError as exc:
            norm_map[sid] = {"ions": {}, "basis": None, "error": str(exc)}
            issues.append({"code": "basis", "msg": f"绑定样本 {sid}: {exc}"})
    return norm_map, issues


def _in_zone(zone: Dict[str, Any], x: float, y: float) -> bool:
    return ((x - zone["x_mm"]) ** 2 + (y - zone["y_mm"]) ** 2) ** 0.5 \
        <= zone["radius_mm"]


def build_pairs(eff_bindings: List[Dict[str, Any]],
                samples_by_id: Dict[str, Any],
                trial: Dict[str, Any], p: Dict[str, Any]
                ) -> Tuple[List[Tuple[str, Any, Any]], List[Dict[str, Any]],
                           List[Dict[str, Any]]]:
    """同位置分层配对：同区、同层位、x/y 归并、深度容差内的处理前↔处理后样本。"""
    issues: List[Dict[str, Any]] = []
    pairs: List[Tuple[str, Any, Any]] = []
    unpaired: List[Dict[str, Any]] = []
    zones = {"treatment": trial["treatment_zone"], "control": trial["control_zone"]}
    layer_reg = {"treatment": {l["layer_id"] for l in trial["layers"]},
                 "control": {l["layer_id"] for l in trial["control_layers"]}}
    by: Dict[Tuple[str, str], List[Dict[str, Any]]] = {
        (z, ph): [] for z in ZONES for ph in PHASES}
    for b in eff_bindings:
        sid = b["sample_id"]
        s = samples_by_id.get(sid)
        if s is None:
            issues.append({"code": "pairing",
                           "msg": f"绑定样本 {sid} 不在墙体样本中"})
            continue
        z = b["zone"]
        if not _in_zone(zones[z], s["x_mm"], s["y_mm"]):
            issues.append({"code": "pairing",
                           "msg": f"{sid} 绑定到{ZONE_CN[z]}，但坐标 "
                                  f"({s['x_mm']},{s['y_mm']}) 落在该区之外"})
        if s.get("layer_id") not in layer_reg[z]:
            issues.append({"code": "pairing",
                           "msg": f"{sid} 层位 {s.get('layer_id')} 未在{ZONE_CN[z]}"
                                  "登记层体积/干密度，无法核算该层库存"})
        by[(z, b["phase"])].append(s)
    for z in ZONES:
        used = set()
        for pre in by[(z, "pre")]:
            best = None
            for post in by[(z, "post")]:
                if post["sample_id"] in used:
                    continue
                if post.get("layer_id") != pre.get("layer_id"):
                    continue
                if abs(post["x_mm"] - pre["x_mm"]) > p["column_xy_bin_mm"]:
                    continue
                if abs(post["y_mm"] - pre["y_mm"]) > p["column_xy_bin_mm"]:
                    continue
                if abs((_depth(post) or 0) - (_depth(pre) or 0)) > p["depth_match_mm"]:
                    continue
                best = post
                break
            if best is None:
                unpaired.append({"sample_id": pre["sample_id"], "zone": z,
                                 "phase": "pre"})
                issues.append({"code": "pairing",
                               "msg": f"{ZONE_CN[z]}{PHASE_CN['pre']}样本 "
                                      f"{pre['sample_id']} 无同位置同层"
                                      f"{PHASE_CN['post']}样本可配对"})
            else:
                used.add(best["sample_id"])
                pairs.append((z, pre, best))
        for post in by[(z, "post")]:
            if post["sample_id"] not in used:
                unpaired.append({"sample_id": post["sample_id"], "zone": z,
                                 "phase": "post"})
                issues.append({"code": "pairing",
                               "msg": f"{ZONE_CN[z]}{PHASE_CN['post']}样本 "
                                      f"{post['sample_id']} 无同位置同层"
                                      f"{PHASE_CN['pre']}样本可配对"})
    return pairs, unpaired, issues


def compute_inventory(pairs: List[Tuple[str, Any, Any]],
                      trial: Dict[str, Any],
                      norm_map: Dict[str, Any]
                      ) -> Tuple[Dict[str, Any], List[Dict[str, Any]], Dict[str, Any]]:
    """分层库存：层平均浓度(mmol/kg) × 层干质量(kg=体积×干密度) → mmol。"""
    layer_mass = {
        z: {l["layer_id"]: l["volume_m3"] * l["dry_density_kg_m3"]
            for l in trial[key]}
        for z, key in (("treatment", "layers"), ("control", "control_layers"))}
    inv = {z: {"pre": {}, "post": {}} for z in ZONES}
    groups: Dict[Tuple[str, str], List[Tuple[Any, Any]]] = {}
    for z, pre, post in pairs:
        groups.setdefault((z, pre["layer_id"]), []).append((pre, post))
    layer_rows: List[Dict[str, Any]] = []
    for (z, lid), lp in sorted(groups.items()):
        mass = layer_mass.get(z, {}).get(lid)
        if mass is None:
            continue  # 未登记层：pairing 门控已拦截
        ions = sorted({ion for pre, post in lp
                       for ion in set(norm_map[pre["sample_id"]]["ions"])
                       & set(norm_map[post["sample_id"]]["ions"])})
        row: Dict[str, Any] = {"zone": z, "layer_id": lid, "mass_kg": mass,
                               "n_pairs": len(lp), "ions": {}}
        pre_tot = post_tot = 0.0
        for ion in ions:
            vs = [(norm_map[pre["sample_id"]]["ions"][ion].value,
                   norm_map[post["sample_id"]]["ions"][ion].value)
                  for pre, post in lp
                  if ion in norm_map[pre["sample_id"]]["ions"]
                  and ion in norm_map[post["sample_id"]]["ions"]]
            mp = sum(a for a, _ in vs) / len(vs)
            mq = sum(b for _, b in vs) / len(vs)
            inv[z]["pre"][ion] = inv[z]["pre"].get(ion, 0.0) + mp * mass
            inv[z]["post"][ion] = inv[z]["post"].get(ion, 0.0) + mq * mass
            row["ions"][ion] = {"pre_conc": mp, "post_conc": mq}
            pre_tot += mp
            post_tot += mq
        row["pre_total_conc"] = pre_tot
        row["post_total_conc"] = post_tot
        layer_rows.append(row)
    return inv, layer_rows, layer_mass


def _drift_ratios(inv: Dict[str, Any]) -> Dict[str, float]:
    """对照区漂移比（后/前库存）；对照前库存为 0 的离子无法给比。"""
    out = {}
    for ion, pre_v in inv["control"]["pre"].items():
        if pre_v > 0:
            out[ion] = inv["control"]["post"].get(ion, 0.0) / pre_v
    return out


def compute_balance(inv: Dict[str, Any], drift: Dict[str, float],
                    cumulative: Dict[str, float], tol: float) -> Dict[str, Any]:
    """离子收支：累计浸出移出量 vs 漂移校正后的墙内净减（正=离墙）。

    loss_corr = 处理前库存 × 对照漂移比 − 处理后库存；residual = 浸出 − 净减。
    闭合判定：总量闭合只是必要条件——**任一离子的分项残差超容差即不闭合**，
    防止正/负分项残差相互抵消（如 Na+ 多浸出、Cl− 少浸出，加总后假装闭合）。
    """
    ions = sorted(set(cumulative) | set(inv["treatment"]["pre"])
                  | set(inv["treatment"]["post"]))
    per_ion: Dict[str, Any] = {}
    ext_t = loss_t = res_t = 0.0
    assumed = False
    for ion in ions:
        ext = cumulative.get(ion, 0.0)
        pre_t = inv["treatment"]["pre"].get(ion, 0.0)
        post_t = inv["treatment"]["post"].get(ion, 0.0)
        d = drift.get(ion)
        if d is None:
            d, assumed = 1.0, True
        loss = pre_t * d - post_t
        res = ext - loss
        scale = max(abs(ext), abs(loss), 1e-9)
        per_ion[ion] = {"extract_mmol": ext, "wall_pre_mmol": pre_t,
                        "wall_post_mmol": post_t, "control_drift": drift.get(ion),
                        "loss_corr_mmol": loss, "residual_mmol": res,
                        "closed": abs(res) <= tol * scale}
        ext_t += ext
        loss_t += loss
        res_t += res
    scale_t = max(abs(ext_t), abs(loss_t), 1e-9)
    total_closed = abs(res_t) <= tol * scale_t
    open_ions = [ion for ion in ions if not per_ion[ion]["closed"]]
    return {"per_ion": per_ion,
            "total": {"extract_mmol": ext_t, "loss_corr_mmol": loss_t,
                      "residual_mmol": res_t, "closed": total_closed},
            "total_closed": total_closed,
            "open_ions": open_ions,
            "closed": total_closed and not open_ions,
            "tolerance": tol, "drift_assumed": assumed}


def compute_deep(wall: Dict[str, Any], trial: Dict[str, Any],
                 layer_rows: List[Dict[str, Any]], p: Dict[str, Any]
                 ) -> List[Dict[str, Any]]:
    """深层浓度核查：处理区深层后/前浓度比超过阈值且超过对照漂移 → 向内迁移。"""
    depth_of = {l["layer_id"]: l for l in wall["layers"]}
    rows: List[Dict[str, Any]] = []
    for reg in trial["layers"]:
        lid = reg["layer_id"]
        wl = depth_of.get(lid)
        if wl is None or wl["d_start_mm"] < p["deep_layer_min_depth_mm"]:
            continue
        t = next((r for r in layer_rows
                  if r["zone"] == "treatment" and r["layer_id"] == lid), None)
        c = next((r for r in layer_rows
                  if r["zone"] == "control" and r["layer_id"] == lid), None)
        if t is None:
            rows.append({"layer_id": lid, "computable": False, "rise": False})
            continue
        pre_t, post_t = t["pre_total_conc"], t["post_total_conc"]
        rt: Optional[float] = (post_t / pre_t) if pre_t else None
        rc = (c["post_total_conc"] / c["pre_total_conc"]) \
            if c and c["pre_total_conc"] else None
        if pre_t == 0 and post_t > 0:
            rise = True  # 处理前未检出、处理后出现，视为升高
        else:
            rise = bool(rt is not None and rt >= p["deep_rise_ratio"]
                        and (rc is None or rt > rc))
        rows.append({"layer_id": lid, "computable": True, "rise": rise,
                     "pre_conc": pre_t, "post_conc": post_t,
                     "ratio_treatment": rt, "ratio_control": rc})
    return rows


# ---------------------------------------------------------------- 建议

def build_trial_advice(gates: Dict[str, bool], classification: str,
                       decision_code: str, p: Dict[str, Any]) -> List[Dict[str, Any]]:
    advice: List[Dict[str, Any]] = []
    pri = 0

    def add(purpose: str, where: str, when: str, action: str) -> None:
        nonlocal pri
        pri += 1
        advice.append({"priority": pri, "purpose": purpose,
                       "location": where, "timing": when, "action": action})

    if not gates["pairing"]:
        add("补齐同位置分层配对样本",
            f"处理区与对照区原孔位（x/y 归并 {p['column_xy_bin_mm']:.0f}mm、"
            f"深度 ±{p['depth_match_mm']:.0f}mm）",
            "下一轮敷贴前",
            "按处理前相同的层位与孔位补采处理后样本，或补登处理前基线")
    if not gates["rounds"]:
        add("厘清轮次覆盖时段", "全部敷贴轮次", "确认版之前",
            "修正起止时间使轮次互不重叠，并保证至少一轮有效浸出液入账")
    if not gates["basis"]:
        add("统一固/液相基准", "浸出液与墙样登记", "下一轮检测",
            "浸出液用液相单位（mmol/L 等），墙样用固相（mmol/kg_dry 等），不得混账")
    if not gates["lod"]:
        add("补测浸出液检测限", "各轮浸出液", "下一轮浸出液检测",
            "全部离子给出检出限；降低高占比离子的检测限以压下半检出限代用比例")
    if not gates["balance"]:
        add("核查离子收支", "浸出液计量与墙内库存", "下一轮敷贴前后",
            "复核浸出液体积与浓度、层体积/干密度登记，排查未收集的渗出液与"
            "敷贴材料本底；收支不闭合不得结束处理")
    if not gates["deep_rise"]:
        add("查明深层盐分去向", f"深层（≥{p['deep_layer_min_depth_mm']:.0f}mm）取芯",
            "下次受潮（降雨/高湿）后复测表层",
            "深层浓度升高提示盐退到地杖深处，受潮后可能返回表面；"
            "不得结束处理，应加密深层监测")
    if classification == "net_removal" and decision_code == "continue_treatment":
        add("继续下一轮敷贴", "处理区原位", "下一轮覆盖时段",
            f"净移除已确认，但残盐仍高于对照区（>{p['end_excess_ratio']} 倍），"
            "继续敷贴至处理区残盐接近对照区水平")
    if classification == "net_removal" and decision_code == "end_treatment":
        add("结束后跟踪一个干湿周期", "处理区表层与深层", "结束后至少一个降雨周期",
            "确认表层电导不反弹、深层不再升高，防止盐从深部返回表面")
    if not advice:
        advice.append({"priority": 1, "purpose": "维持监测",
                       "location": "处理区与对照区", "timing": "下一干湿周期",
                       "action": "复测分层样本与浸出液，保持收支账连续"})
    return advice


# ---------------------------------------------------------------- 主入口

def analyze_trial(wall: Dict[str, Any], trial: Dict[str, Any],
                  rounds: List[Dict[str, Any]],
                  revisions: Optional[List[Dict[str, Any]]],
                  samples: List[Dict[str, Any]],
                  rains: List[Dict[str, Any]]) -> Dict[str, Any]:
    p = _params(trial)
    revisions = revisions or []
    samples_by_id = {s.get("sample_id"): s for s in samples}
    env_ids = set(trial.get("env_rain_ids") or [])
    env_rains = [r for r in rains if r.get("rain_id") in env_ids]

    excluded_rounds, eff_bindings = apply_trial_revisions(
        trial.get("bindings") or [], revisions)

    # ---- 轮次与逐轮浸出 ----
    evaluated = [evaluate_round(r, p) for r in rounds]
    overlaps = _find_overlaps(rounds)
    active_rounds = [e for e in evaluated if e["round_id"] not in excluded_rounds]
    running: Dict[str, float] = {}
    for e in sorted(active_rounds, key=lambda e: parse_dt(e["cover_start_ts"])):
        for ion, v in e["removed"].items():
            running[ion] = running.get(ion, 0.0) + v
        e["cumulative"] = dict(running)
    cumulative = dict(running)

    # ---- 绑定样本：归一化、配对、库存 ----
    norm_map, sample_basis_issues = _build_norm_map(eff_bindings, samples_by_id)
    pairs, unpaired, pairing_issues = build_pairs(eff_bindings, samples_by_id,
                                                  trial, p)
    inv, layer_rows, layer_mass = compute_inventory(pairs, trial, norm_map)
    drift = _drift_ratios(inv)
    balance = compute_balance(inv, drift, cumulative, p["balance_tolerance"])
    deep_rows = compute_deep(wall, trial, layer_rows, p)

    # ---- 门控 ----
    round_issues = [i for e in active_rounds for i in e["issues"]]
    basis_issues = sample_basis_issues + [i for i in round_issues
                                          if i["code"] == "basis"]
    lod_issues = [i for i in round_issues if i["code"] == "lod"]
    n_pairs = {z: sum(1 for zz, _, _ in pairs if zz == z) for z in ZONES}
    deep_computable = any(r["computable"] for r in deep_rows)
    any_rise = any(r.get("rise") for r in deep_rows)
    balance_computable = n_pairs["treatment"] >= 1 and len(active_rounds) >= 1

    gate_pass = {
        "pairing": not pairing_issues and n_pairs["treatment"] >= 1
        and n_pairs["control"] >= 1,
        "rounds": not overlaps and len(active_rounds) >= 1,
        "basis": not basis_issues,
        "lod": not lod_issues,
        "balance": balance_computable and balance["closed"]
        and not basis_issues and not lod_issues,
        "deep_rise": deep_computable and not any_rise,
    }
    gate_details = []
    for code, name, blocking in TRIAL_GATE_ORDER:
        detail = ""
        if not gate_pass[code]:
            if code == "pairing":
                detail = "；".join(i["msg"] for i in pairing_issues) or \
                    "处理区/对照区均须至少一对前后配对样本"
            elif code == "rounds":
                lack = [f"轮次 {a} 与 {b} 覆盖时段重叠" for a, b in overlaps]
                if not active_rounds:
                    lack.append("无有效浸出轮次（全部缺失或被剔除）")
                detail = "；".join(lack)
            elif code == "basis":
                detail = "；".join(i["msg"] for i in basis_issues)
            elif code == "lod":
                detail = "；".join(i["msg"] for i in lod_issues)
            elif code == "balance":
                if not balance_computable:
                    detail = "缺处理区配对样本或有效轮次，收支无法核算"
                elif not balance["closed"]:
                    lack = []
                    if balance["open_ions"]:
                        lack.append("分项收支不闭合：" + "、".join(
                            f"{ion}（残差 "
                            f"{r4(balance['per_ion'][ion]['residual_mmol'])} mmol）"
                            for ion in balance["open_ions"]))
                    if not balance["total_closed"]:
                        t = balance["total"]
                        lack.append(f"总量不闭合：累计浸出 {r4(t['extract_mmol'])} "
                                    f"mmol 与漂移校正后墙内净减 "
                                    f"{r4(t['loss_corr_mmol'])} mmol 残差 "
                                    f"{r4(t['residual_mmol'])} mmol"
                                    f"（容差 ±{p['balance_tolerance']:.0%}）")
                    detail = "；".join(lack)
                else:
                    detail = "浸出液基准或检测限问题未决，收支账不可信"
            elif code == "deep_rise":
                if not deep_computable:
                    detail = (f"深层（≥{p['deep_layer_min_depth_mm']:.0f}mm）无配对"
                              "样本，无法排除盐向地杖深处迁移")
                else:
                    detail = "；".join(
                        f"深层 {r['layer_id']} 浓度升至处理前的 "
                        f"{r4(r['ratio_treatment'])} 倍"
                        + (f"（对照 {r4(r['ratio_control'])} 倍）"
                           if r.get("ratio_control") else "")
                        for r in deep_rows if r.get("rise"))
        gate_details.append({"code": code, "name": name, "blocking": blocking,
                             "passed": gate_pass[code], "detail": detail})

    # ---- 分类与决定 ----
    data_ok = all(gate_pass[c] for c in ("pairing", "rounds", "basis", "lod"))
    ext_total = balance["total"]["extract_mmol"]
    loss_total = balance["total"]["loss_corr_mmol"]
    if not data_ok:
        classification = "insufficient_evidence"
    elif any_rise:
        classification = "inward_migration"
    elif not deep_computable:
        classification = "insufficient_evidence"
    elif gate_pass["balance"] and ext_total > 0 and loss_total > 0:
        classification = "net_removal"
    else:
        classification = "insufficient_evidence"

    all_pass = all(g["passed"] for g in gate_details)
    mass_t = sum(layer_mass["treatment"].values())
    mass_c = sum(layer_mass["control"].values())
    post_t = sum(inv["treatment"]["post"].values())
    post_c = sum(inv["control"]["post"].values())
    per_kg_t = post_t / mass_t if mass_t else None
    per_kg_c = post_c / mass_c if mass_c else None
    excess_ratio = (per_kg_t / per_kg_c) if per_kg_t is not None and per_kg_c else None

    if classification == "net_removal" and all_pass:
        if excess_ratio is not None and excess_ratio <= p["end_excess_ratio"]:
            d_code, can_end = "end_treatment", True
            d_reason = (f"收支闭合（浸出 {r4(ext_total)} mmol ≈ 漂移校正后墙内净减 "
                        f"{r4(loss_total)} mmol），且处理区残盐 {r4(per_kg_t)} "
                        f"mmol/kg 已降至对照区 {r4(per_kg_c)} mmol/kg 的 "
                        f"{p['end_excess_ratio']} 倍以内，可建议结束处理")
        else:
            d_code, can_end = "continue_treatment", False
            d_reason = (f"净移除确认（浸出 {r4(ext_total)} mmol ≈ 漂移校正后墙内净减 "
                        f"{r4(loss_total)} mmol），但处理区残盐仍为对照区的 "
                        f"{r4(excess_ratio)} 倍（>{p['end_excess_ratio']}），"
                        "建议继续敷贴，不得结束处理")
    elif classification == "inward_migration":
        d_code, can_end = "hold_inward_migration", False
        risen = "、".join(r["layer_id"] for r in deep_rows if r.get("rise"))
        d_reason = (f"深层 {risen} 浓度升高超出对照漂移，盐疑从颜料层退到地杖深处"
                    "而非离墙，受潮后可能返回表面，不得建议结束处理")
    else:
        d_code, can_end = "hold_insufficient_evidence", False
        failed = "、".join(g["name"] for g in gate_details if not g["passed"])
        d_reason = f"门控未全部通过（{failed}），证据不足，不得建议结束处理"

    advice = build_trial_advice(gate_pass, classification, d_code, p)

    # ---- 输出装配 ----
    rounds_public = []
    for e in evaluated:
        excluded = e["round_id"] in excluded_rounds
        rounds_public.append({
            "round_id": e["round_id"],
            "cover_start_ts": e["cover_start_ts"],
            "cover_end_ts": e["cover_end_ts"],
            "excluded": excluded,
            "exclude_reason": excluded_rounds.get(e["round_id"]),
            "volume_l": e["volume_l"],
            "basis": e["basis"],
            "ions": {k: {"value": r4(v["value"]), "detected": v["detected"],
                         "lod": r4(v["lod"]), "censored": v["censored"]}
                     for k, v in e["ions"].items()},
            "removed_mmol": {k: r4(v) for k, v in sorted(e["removed"].items())},
            "removed_total_mmol": r4(e["removed_total"]),
            "cumulative_mmol": (None if excluded
                                else {k: r4(v) for k, v in
                                      sorted(e.get("cumulative", {}).items())}),
            "cumulative_total_mmol": (None if excluded
                                      else r4(sum(e.get("cumulative", {}).values()))),
            "missing_lod": e["missing_lod"],
            "censored_ions": e["censored_ions"],
            "issues": e["issues"],
        })

    def _zone_public(z: str) -> Dict[str, Any]:
        pre, post = inv[z]["pre"], inv[z]["post"]
        ions = sorted(set(pre) | set(post))
        return {
            "mass_kg": r4(sum(layer_mass[z].values())),
            "per_ion": {ion: {"pre_mmol": r4(pre.get(ion, 0.0)),
                              "post_mmol": r4(post.get(ion, 0.0)),
                              "delta_mmol": r4(post.get(ion, 0.0)
                                               - pre.get(ion, 0.0))}
                        for ion in ions},
            "pre_total_mmol": r4(sum(pre.values())),
            "post_total_mmol": r4(sum(post.values())),
            "delta_total_mmol": r4(sum(post.values()) - sum(pre.values())),
        }

    moisture = {}
    for z in ZONES:
        for ph, idx in (("pre", 1), ("post", 2)):
            vals = [row[idx].get("moisture_wt") for row in pairs if row[0] == z]
            vals = [v for v in vals if isinstance(v, (int, float))]
            moisture.setdefault(z, {})[ph] = r4(sum(vals) / len(vals)) if vals else None

    return {
        "trial_id": trial["trial_id"],
        "wall_id": trial["wall_id"],
        "source_version_id": trial.get("source_version_id"),
        "params_effective": p,
        "poultice": trial.get("poultice"),
        "zones": {"treatment": trial["treatment_zone"],
                  "control": trial["control_zone"]},
        "bindings": {"effective": sorted(eff_bindings,
                                         key=lambda b: (b["zone"], b["phase"],
                                                        b["sample_id"])),
                     "n_original": len(trial.get("bindings") or []),
                     "n_effective": len(eff_bindings)},
        "pairs": {
            "treatment": n_pairs["treatment"], "control": n_pairs["control"],
            "unpaired": unpaired,
            "rows": [{"zone": z, "layer_id": pre.get("layer_id"),
                      "pre_sample_id": pre["sample_id"],
                      "post_sample_id": post["sample_id"],
                      "depth_mm": r4(_depth(pre))}
                     for z, pre, post in pairs],
        },
        "rounds": rounds_public,
        "cumulative_removed_mmol": {k: r4(v) for k, v in sorted(cumulative.items())},
        "cumulative_removed_total_mmol": r4(sum(cumulative.values())),
        "inventory": {
            "treatment": _zone_public("treatment"),
            "control": _zone_public("control"),
            "control_drift_ratio": {k: r4(v) for k, v in sorted(drift.items())},
            "layers": [{"zone": r["zone"], "zone_name": ZONE_CN[r["zone"]],
                        "layer_id": r["layer_id"], "mass_kg": r4(r["mass_kg"]),
                        "n_pairs": r["n_pairs"],
                        "ions": {ion: {"pre_conc": r4(v["pre_conc"]),
                                       "post_conc": r4(v["post_conc"])}
                                 for ion, v in sorted(r["ions"].items())},
                        "pre_total_conc": r4(r["pre_total_conc"]),
                        "post_total_conc": r4(r["post_total_conc"])}
                       for r in layer_rows],
        },
        "balance": {
            "tolerance": balance["tolerance"],
            "closed": balance["closed"],
            "total_closed": balance["total_closed"],
            "open_ions": balance["open_ions"],
            "drift_assumed": balance["drift_assumed"],
            "per_ion": {ion: {k: (r4(v) if isinstance(v, (int, float)) else v)
                              for k, v in row.items()}
                        for ion, row in balance["per_ion"].items()},
            "total": {k: (r4(v) if isinstance(v, (int, float)) else v)
                      for k, v in balance["total"].items()},
        },
        "deep_layers": [{**r,
                         "pre_conc": r4(r.get("pre_conc")),
                         "post_conc": r4(r.get("post_conc")),
                         "ratio_treatment": r4(r.get("ratio_treatment")),
                         "ratio_control": r4(r.get("ratio_control"))}
                        for r in deep_rows],
        "gates": gate_details,
        "all_gates_passed": all_pass,
        "classification": classification,
        "classification_name": CLASSIFICATION_CN[classification],
        "decision": {"can_end": can_end, "code": d_code,
                     "name": DECISION_CN[d_code], "reason": d_reason,
                     "residual_excess_ratio": r4(excess_ratio)},
        "environment": {
            "rains": [{"rain_id": r.get("rain_id"), "ts": r.get("ts"),
                       "rain_mm": r.get("rain_mm")} for r in env_rains],
            "moisture_wt_mean": moisture,
        },
        "advice": advice,
    }

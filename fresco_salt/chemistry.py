"""离子登记、单位统一与电荷当量闭合（equivalent charge balance）。

单位约定（接口统一后）：
  * 固相干土/壁画地杖浓度  -> mmol/kg_dry
  * 孔隙水 / 浸出液浓度     -> mmol/L
所有换算只改变浓度数值，不改变离子电荷。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

# 离子登记：化合价（符号带方向）、摩尔质量 g/mol、中文名
IONS: Dict[str, Dict[str, Any]] = {
    "Na+":  {"charge": +1, "mw": 22.990, "cn": "钠离子"},
    "K+":   {"charge": +1, "mw": 39.098, "cn": "钾离子"},
    "Ca2+": {"charge": +2, "mw": 40.078, "cn": "钙离子"},
    "Mg2+": {"charge": +2, "mw": 24.305, "cn": "镁离子"},
    "NH4+": {"charge": +1, "mw": 18.038, "cn": "铵根"},
    "Cl-":  {"charge": -1, "mw": 35.453, "cn": "氯离子"},
    "NO3-": {"charge": -1, "mw": 62.005, "cn": "硝酸根"},
    "SO42-": {"charge": -2, "mw": 96.06, "cn": "硫酸根"},
    "HCO3-": {"charge": -1, "mw": 61.017, "cn": "碳酸氢根"},
    "CO32-": {"charge": -2, "mw": 60.009, "cn": "碳酸根"},
    "NO2-":  {"charge": -1, "mw": 46.006, "cn": "亚硝酸根"},
    "PO43-": {"charge": -3, "mw": 94.971, "cn": "磷酸根"},
    "Br-":   {"charge": -1, "mw": 79.904, "cn": "溴离子"},
    "F-":    {"charge": -1, "mw": 18.998, "cn": "氟离子"},
    "OH-":   {"charge": -1, "mw": 17.007, "cn": "氢氧根"},
}

CATIONS = {k for k, v in IONS.items() if v["charge"] > 0}
ANIONS = {k for k, v in IONS.items() if v["charge"] < 0}

# 单位换算到统一基准（factor: 数值乘以该系数）
# mmol/kg_dry 为固相基准；mmol/L 为液相基准，两者之间不跨相换算。
UNITS_TO_SOLID = {
    "mmol/kg": 1.0, "mmol/kg_dry": 1.0,
    "meq/kg": None,        # 需按 |z| 换算
    "mg/kg": None,         # 需按摩尔质量换算
    "ug/kg": None,         # mg/kg 的 1/1000
}
UNITS_TO_LIQUID = {
    "mmol/L": 1.0, "mM": 1.0,
    "meq/L": None,
    "mg/L": None,
    "ppm": None,          # 稀溶液近似 mg/L
    "ug/L": None,
}
CEMENT_MARKERS = {"Ca2+", "SO42-", "OH-", "K+", "Na+"}  # OH- 作为备注离子
SUPPORTED_UNITS = set(UNITS_TO_SOLID) | set(UNITS_TO_LIQUID)


class ChemistryError(ValueError):
    """接口录入阶段的化学数据错误（HTTP 400）。"""


@dataclass
class NormalizedIon:
    ion: str
    value: float                 # 统一单位后的浓度（检出时为实测值，未检出为半检出限）
    detected: bool
    lod: Optional[float]         # 统一单位后的检出限
    censored: bool               # 原值低于检出限，按 LOD/2 代用
    charge: int
    eq_pos: float                # 阳离子当量（其余为 0）
    eq_neg: float                # 阴离子当量绝对值（其余为 0）


def _convert_one(ion: str, value: float, unit: str) -> Tuple[float, str]:
    """把单个浓度换算到统一基准，返回 (换算值, basis solid|liquid)。"""
    if ion not in IONS:
        raise ChemistryError(f"未知离子 {ion!r}，登记离子：{sorted(IONS)}")
    z = abs(IONS[ion]["charge"])
    mw = IONS[ion]["mw"]
    if unit in UNITS_TO_SOLID:
        basis = "solid"
        if unit in ("mmol/kg", "mmol/kg_dry"):
            return value, basis
        if unit == "meq/kg":
            return value / z, basis
        if unit == "mg/kg":
            return value / mw, basis
        if unit == "ug/kg":
            return value / mw / 1000.0, basis
    if unit in UNITS_TO_LIQUID:
        basis = "liquid"
        if unit in ("mmol/L", "mM"):
            return value, basis
        if unit == "meq/L":
            return value / z, basis
        if unit in ("mg/L", "ppm"):
            return value / mw, basis
        if unit == "ug/L":
            return value / mw / 1000.0, basis
    raise ChemistryError(f"离子 {ion} 使用了不支持的单位 {unit!r}")


def normalize_ions(raw_ions: Dict[str, Any]) -> Tuple[Dict[str, NormalizedIon], str]:
    """统一一批离子的单位并核对基准一致；返回 (归一化字典, basis)。

    每个离子条目：{"value": float, "unit": str, "lod": float 可选}
    value 为 null 或小于等于 0 时视为未检出，必须给 lod，按 LOD/2 代用。
    """
    if not raw_ions:
        raise ChemistryError("ions 为空，无法计算电荷账")
    out: Dict[str, NormalizedIon] = {}
    basis: Optional[str] = None
    for ion in sorted(raw_ions):
        entry = raw_ions[ion]
        if not isinstance(entry, dict):
            raise ChemistryError(f"离子 {ion} 的条目必须是对象")
        unit = entry.get("unit")
        if unit not in SUPPORTED_UNITS:
            raise ChemistryError(f"离子 {ion} 单位缺失或不支持：{unit!r}")
        raw_val = entry.get("value")
        raw_lod = entry.get("lod")
        val, b = _convert_one(ion, float(raw_val), unit) if raw_val is not None else (0.0, None)
        b = b or ("solid" if unit in UNITS_TO_SOLID else "liquid")
        if basis is None:
            basis = b
        elif basis != b:
            raise ChemistryError(
                f"离子 {ion} 基准为 {b}，与本批 {basis} 基准混用（固相与液相浓度不可直接共账）"
            )
        lod: Optional[float] = None
        censored = False
        if raw_lod is not None:
            lod, _ = _convert_one(ion, float(raw_lod), unit)
        if raw_val is None or (raw_val is not None and val <= 0.0):
            if lod is None:
                raise ChemistryError(f"离子 {ion} 未检出（value 为空/<=0）却缺少检出限 lod")
            val = lod / 2.0
            detected = False
            censored = True
        elif lod is not None and val < lod:
            val = lod / 2.0
            detected = False
            censored = True
        else:
            detected = True
        z_signed = IONS[ion]["charge"]
        eq = abs(z_signed) * val
        out[ion] = NormalizedIon(
            ion=ion, value=val, detected=detected, lod=lod, censored=censored,
            charge=z_signed, eq_pos=eq if z_signed > 0 else 0.0,
            eq_neg=eq if z_signed < 0 else 0.0,
        )
    return out, basis or "solid"


def charge_balance(ions: Dict[str, NormalizedIon]) -> Dict[str, Any]:
    """电荷当量闭合：CBE = (Σ+ − Σ−) / ((Σ+ + Σ−)/2) × 100%。

    同时检查检测限完整性与代用比例；返回闭合账，不做放行/拦截判定。
    """
    pos = sum(i.eq_pos for i in ions.values())
    neg = sum(i.eq_neg for i in ions.values())
    denom = (pos + neg) / 2.0
    cbe = None if denom == 0 else (pos - neg) / denom * 100.0
    missing_lod = []
    censored = []
    for i in ions.values():
        if i.lod is None:
            missing_lod.append(i.ion)
        if i.censored:
            censored.append(i.ion)
    # 任一极性完全缺测 -> 账本身无意义
    sides = {
        "cations_present": [k for k in ions if k in CATIONS],
        "anions_present": [k for k in ions if k in ANIONS],
    }
    return {
        "eq_pos_total": pos,
        "eq_neg_total": neg,
        "cbe_percent": cbe,
        "imbalance_abs": abs(cbe) if cbe is not None else None,
        "missing_lod": sorted(missing_lod),
        "lod_censored_ions": sorted(censored),
        "censored_fraction": (len(censored) / len(ions)) if ions else None,
        **sides,
    }


def ion_vectors(raw_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """批量归一化（逐样本独立基准，跨样本混合基准在复核层拦截）。"""
    out = []
    for s in raw_list:
        ions, basis = normalize_ions(s["ions"])
        out.append({"sample_id": s["sample_id"], "ions": ions, "basis": basis,
                    "balance": charge_balance(ions)})
    return out

"""HTTP 服务：http.server 实现的壁画盐害来源复核 API。

启动：python -m fresco_salt.app --db fresco.db --port 8000
所有请求/响应均为 UTF-8 JSON。封版后写操作返回 409。
"""
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from . import __version__
from .analysis import review
from .chemistry import SUPPORTED_UNITS, IONS
from .db import Store
from .microclimate import (
    MICROCLIMATE_DEFAULTS, TEMP_UNITS, RH_UNITS, analyze_monitor,
    derive_conservative_rule, point_in_zone, validate_rule,
)
from .microclimate_recompute import build_confirm as build_micro_confirm
from .microclimate_recompute import serialize as serialize_micro_confirm
from .microclimate_svg import render_risk_svg
from .poultice import analyze_trial
from .poultice_recompute import build_confirm
from .poultice_recompute import serialize as serialize_confirm
from .poultice_svg import render_balance_svg
from .recompute import build_recompute, serialize
from .svg import render_profile_svg
from .util import parse_dt

REVISION_KINDS = {"exclude_sample", "restore_sample", "correct_binding",
                  "adopt_conservative"}
TRIAL_REVISION_KINDS = {"exclude_extract", "restore_extract",
                        "rebind_sample", "unbind_sample"}
MONITOR_REVISION_KINDS = {"rebind_sensor", "unbind_sensor", "adopt_sensor",
                          "adopt_conservative_rule"}


class ApiError(Exception):
    def __init__(self, status: int, message: str, detail: Any = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.detail = detail


# ---------------------------------------------------------------- 录入校验

def _require(d: Dict[str, Any], keys: List[str], where: str) -> None:
    miss = [k for k in keys if k not in d]
    if miss:
        raise ApiError(400, f"{where} 缺少必填字段：{', '.join(miss)}")


def validate_wall(body: Dict[str, Any]) -> Dict[str, Any]:
    _require(body, ["name", "width_mm", "height_mm", "thickness_mm", "layers"], "墙体模型")
    for k in ("width_mm", "height_mm", "thickness_mm"):
        if not isinstance(body[k], (int, float)) or body[k] <= 0:
            raise ApiError(400, f"{k} 必须为正数（毫米）")
    layers = body["layers"]
    if not isinstance(layers, list) or not layers:
        raise ApiError(400, "layers 必须为非空数组")
    ids = set()
    prev_end = 0.0
    for i, l in enumerate(layers):
        _require(l, ["layer_id", "d_start_mm", "d_end_mm"], f"第 {i+1} 层")
        if l["layer_id"] in ids:
            raise ApiError(400, f"层位 id 重复：{l['layer_id']}")
        ids.add(l["layer_id"])
        if not (0 <= l["d_start_mm"] < l["d_end_mm"] <= body["thickness_mm"]):
            raise ApiError(400, f"层位 {l['layer_id']} 深度区间非法或越出墙厚")
        if l["d_start_mm"] != prev_end:
            raise ApiError(400, f"层位 {l['layer_id']} 与上层不连续（应从 {prev_end} 开始）")
        prev_end = l["d_end_mm"]
    if "params" in body and not isinstance(body["params"], dict):
        raise ApiError(400, "params 必须为对象")
    return body


def validate_sample(body: Dict[str, Any], wall: Dict[str, Any]) -> Dict[str, Any]:
    _require(body, ["layer_id", "x_mm", "y_mm", "z_mm", "ts", "ions", "moisture_wt"], "样本")
    if body["layer_id"] not in {l["layer_id"] for l in wall["layers"]}:
        raise ApiError(400, f"layer_id {body['layer_id']!r} 不在墙体模型中（层位错绑）")
    for axis, dim in (("x_mm", "width_mm"), ("y_mm", "height_mm"), ("z_mm", "thickness_mm")):
        v = body[axis]
        if not isinstance(v, (int, float)) or not 0 <= v <= wall[dim]:
            raise ApiError(400, f"{axis}={v} 越界（0~{wall[dim]}mm）")
    depth = body.get("depth_mm", body["z_mm"])
    layer = next(l for l in wall["layers"] if l["layer_id"] == body["layer_id"])
    if not layer["d_start_mm"] <= depth <= layer["d_end_mm"]:
        raise ApiError(400, f"depth_mm={depth} 不在层位 {body['layer_id']} 区间 "
                            f"[{layer['d_start_mm']},{layer['d_end_mm']}]（层位错绑）；"
                            "请更正绑定或深度后重新提交，必要时走 /revisions 更正")
    try:
        parse_dt(body["ts"])
    except (ValueError, TypeError):
        raise ApiError(400, "ts 必须为 ISO8601 时间")
    ions = body["ions"]
    if not isinstance(ions, dict) or not ions:
        raise ApiError(400, "ions 必须为非空对象")
    for ion, e in ions.items():
        if ion not in IONS:
            raise ApiError(400, f"未知离子 {ion!r}")
        if not isinstance(e, dict):
            raise ApiError(400, f"ions.{ion} 必须为对象")
        unit = e.get("unit")
        if unit not in SUPPORTED_UNITS:
            raise ApiError(400, f"ions.{ion}.unit={unit!r} 不受支持，可选：{sorted(SUPPORTED_UNITS)}")
        if "value" not in e:
            raise ApiError(400, f"ions.{ion} 缺 value（未检出请传 null 并必须给 lod）")
        if e["value"] is None and e.get("lod") is None:
            raise ApiError(400, f"ions.{ion} 未检出却缺检测限 lod（禁止无检出限数据入账）")
    if not isinstance(body.get("moisture_wt"), (int, float)):
        raise ApiError(400, "moisture_wt 必须为重量含水率百分数数值")
    if body.get("stage") and body["stage"] not in ("wet", "dry", "transition"):
        raise ApiError(400, "stage 仅允许 wet/dry/transition")
    return body


def validate_rain(body: Dict[str, Any]) -> Dict[str, Any]:
    _require(body, ["ts", "rain_mm"], "降雨记录")
    parse_dt(body["ts"])
    if not isinstance(body["rain_mm"], (int, float)) or body["rain_mm"] < 0:
        raise ApiError(400, "rain_mm 必须为非负数值（毫米）")
    return body


def validate_repair(body: Dict[str, Any], wall: Dict[str, Any]) -> Dict[str, Any]:
    _require(body, ["date", "material"], "修缮记录")
    parse_dt(body["date"])
    if body.get("layer_id") and body["layer_id"] not in {l["layer_id"] for l in wall["layers"]}:
        raise ApiError(400, f"修缮 layer_id {body['layer_id']!r} 不在墙体模型中")
    return body


def validate_revision(body: Dict[str, Any], wall: Dict[str, Any], store: Store) -> Dict[str, Any]:
    _require(body, ["kind", "reason", "payload"], "修订")
    if body["kind"] not in REVISION_KINDS:
        raise ApiError(400, f"kind 必须为 {sorted(REVISION_KINDS)}")
    if not str(body["reason"]).strip():
        raise ApiError(400, "每次修订必须留理由 reason")
    p = body["payload"]
    if not isinstance(p, dict):
        raise ApiError(400, "payload 必须为对象")
    sample_ids = {s["sample_id"] for s in store.get_samples(wall["wall_id"])}
    if body["kind"] in ("exclude_sample", "restore_sample"):
        _require(p, ["sample_id"], "payload")
        if p["sample_id"] not in sample_ids:
            raise ApiError(400, f"样本 {p['sample_id']} 不存在")
    if body["kind"] == "correct_binding":
        _require(p, ["sample_id", "new_layer_id"], "payload")
        if p["sample_id"] not in sample_ids:
            raise ApiError(400, f"样本 {p['sample_id']} 不存在")
        if p["new_layer_id"] not in {l["layer_id"] for l in wall["layers"]}:
            raise ApiError(400, f"新层位 {p['new_layer_id']!r} 不在墙体模型中")
    return body


# ---------------------------------------------------------------- 敷贴试验校验

def _validate_zone(z: Dict[str, Any], wall: Dict[str, Any], name: str) -> None:
    _require(z, ["x_mm", "y_mm", "radius_mm"], name)
    for k in ("x_mm", "y_mm", "radius_mm"):
        if not isinstance(z[k], (int, float)):
            raise ApiError(400, f"{name}.{k} 必须为数值（毫米）")
    if z["radius_mm"] <= 0:
        raise ApiError(400, f"{name}.radius_mm 必须为正数")
    if not 0 <= z["x_mm"] <= wall["width_mm"] or not 0 <= z["y_mm"] <= wall["height_mm"]:
        raise ApiError(400, f"{name} 圆心越出墙面")


def validate_trial(body: Dict[str, Any], wall: Dict[str, Any], store: Store
                   ) -> Dict[str, Any]:
    _require(body, ["name", "source_version_id", "treatment_zone", "control_zone",
                    "layers", "control_layers", "poultice", "bindings"], "敷贴试验")
    src = store.get_version(body["source_version_id"])
    if not src or src["wall_id"] != wall["wall_id"]:
        raise ApiError(400, f"引用的来源复核封版 {body['source_version_id']!r} 不存在"
                            "或不属于该墙体，请先在来源墙体 /lock 封版")
    _validate_zone(body["treatment_zone"], wall, "处理区")
    _validate_zone(body["control_zone"], wall, "对照区")
    t, c = body["treatment_zone"], body["control_zone"]
    dist = ((t["x_mm"] - c["x_mm"]) ** 2 + (t["y_mm"] - c["y_mm"]) ** 2) ** 0.5
    if dist < t["radius_mm"] + c["radius_mm"]:
        raise ApiError(400, "处理区与对照区重叠：对照区须邻近但独立，"
                            "否则漂移校正失去意义")
    layer_ids = {l["layer_id"] for l in wall["layers"]}
    for key, label in (("layers", "处理区层登记"), ("control_layers", "对照区层登记")):
        regs = body[key]
        if not isinstance(regs, list) or not regs:
            raise ApiError(400, f"{label}（{key}）必须为非空数组")
        seen = set()
        for reg in regs:
            _require(reg, ["layer_id", "volume_m3", "dry_density_kg_m3"], label)
            if reg["layer_id"] not in layer_ids:
                raise ApiError(400, f"{label}层位 {reg['layer_id']!r} 不在墙体模型中")
            if reg["layer_id"] in seen:
                raise ApiError(400, f"{label}层位重复：{reg['layer_id']}")
            seen.add(reg["layer_id"])
            for k in ("volume_m3", "dry_density_kg_m3"):
                if not isinstance(reg[k], (int, float)) or reg[k] <= 0:
                    raise ApiError(400, f"{label}.{k} 必须为正数")
    pq = body["poultice"]
    if not isinstance(pq, dict):
        raise ApiError(400, "poultice 必须为对象")
    _require(pq, ["material", "water_content_percent", "contact_area_m2"], "敷贴")
    if not str(pq["material"]).strip():
        raise ApiError(400, "敷贴材料 material 不能为空")
    if not isinstance(pq["water_content_percent"], (int, float)) \
            or not 0 <= pq["water_content_percent"] <= 100:
        raise ApiError(400, "敷贴含水量 water_content_percent 须为 0~100 的数值")
    if not isinstance(pq["contact_area_m2"], (int, float)) or pq["contact_area_m2"] <= 0:
        raise ApiError(400, "接触面积 contact_area_m2 必须为正数（平方米）")
    bindings = body["bindings"]
    if not isinstance(bindings, list) or not bindings:
        raise ApiError(400, "bindings 必须为非空数组（绑定处理前后同位置分层样本）")
    sample_ids = {s["sample_id"] for s in store.get_samples(wall["wall_id"])}
    seen, covered = set(), set()
    for b in bindings:
        _require(b, ["sample_id", "zone", "phase"], "绑定")
        if b["sample_id"] in seen:
            raise ApiError(400, f"样本 {b['sample_id']} 被重复绑定")
        seen.add(b["sample_id"])
        if b["sample_id"] not in sample_ids:
            raise ApiError(400, f"绑定样本 {b['sample_id']} 不在墙体样本中")
        if b["zone"] not in ("treatment", "control"):
            raise ApiError(400, "绑定 zone 仅允许 treatment/control")
        if b["phase"] not in ("pre", "post"):
            raise ApiError(400, "绑定 phase 仅允许 pre/post")
        covered.add((b["zone"], b["phase"]))
    if len(covered) < 4:
        raise ApiError(400, "绑定须覆盖 处理区/对照区 × 处理前/处理后 四类样本")
    rain_ids = {r["rain_id"] for r in store.get_rains(wall["wall_id"])}
    for rid in body.get("env_rain_ids") or []:
        if rid not in rain_ids:
            raise ApiError(400, f"环境记录 {rid!r} 不在墙体降雨记录中")
    if "params" in body and not isinstance(body["params"], dict):
        raise ApiError(400, "params 必须为对象")
    return body


def validate_round(body: Dict[str, Any]) -> Dict[str, Any]:
    _require(body, ["cover_start_ts", "cover_end_ts", "extract"], "敷贴轮次")
    try:
        start, end = parse_dt(body["cover_start_ts"]), parse_dt(body["cover_end_ts"])
    except (ValueError, TypeError):
        raise ApiError(400, "覆盖时段起止必须为 ISO8601 时间")
    if end <= start:
        raise ApiError(400, "覆盖时段起止颠倒：cover_end_ts 必须晚于 cover_start_ts")
    ext = body["extract"]
    if not isinstance(ext, dict):
        raise ApiError(400, "extract 必须为对象")
    _require(ext, ["volume_l", "ions"], "浸出液")
    if not isinstance(ext["volume_l"], (int, float)) or ext["volume_l"] <= 0:
        raise ApiError(400, "浸出液体积 volume_l 必须为正数（升）")
    ions = ext["ions"]
    if not isinstance(ions, dict) or not ions:
        raise ApiError(400, "浸出液 ions 必须为非空对象")
    for ion, e in ions.items():
        if ion not in IONS:
            raise ApiError(400, f"未知离子 {ion!r}")
        if not isinstance(e, dict):
            raise ApiError(400, f"ions.{ion} 必须为对象")
        if e.get("unit") not in SUPPORTED_UNITS:
            raise ApiError(400, f"ions.{ion}.unit={e.get('unit')!r} 不受支持，"
                                f"可选：{sorted(SUPPORTED_UNITS)}")
        if "value" not in e:
            raise ApiError(400, f"ions.{ion} 缺 value（未检出请传 null 并必须给 lod）")
        if e["value"] is None and e.get("lod") is None:
            raise ApiError(400, f"ions.{ion} 未检出却缺检测限 lod（禁止无检出限数据入账）")
    return body


def validate_trial_revision(body: Dict[str, Any], trial: Dict[str, Any],
                            store: Store) -> Dict[str, Any]:
    _require(body, ["kind", "reason", "payload"], "试验修订")
    if body["kind"] not in TRIAL_REVISION_KINDS:
        raise ApiError(400, f"kind 必须为 {sorted(TRIAL_REVISION_KINDS)}")
    if not str(body["reason"]).strip():
        raise ApiError(400, "每次试验修订必须留理由 reason")
    p = body["payload"]
    if not isinstance(p, dict):
        raise ApiError(400, "payload 必须为对象")
    if body["kind"] in ("exclude_extract", "restore_extract"):
        _require(p, ["round_id"], "payload")
        round_ids = {r["round_id"] for r in store.get_rounds(trial["trial_id"])}
        if p["round_id"] not in round_ids:
            raise ApiError(400, f"轮次 {p['round_id']} 不存在")
    if body["kind"] in ("rebind_sample", "unbind_sample"):
        _require(p, ["sample_id"], "payload")
        bound = {b["sample_id"] for b in trial.get("bindings") or []}
        if p["sample_id"] not in bound:
            raise ApiError(400, f"样本 {p['sample_id']} 未在本试验绑定中")
    if body["kind"] == "rebind_sample":
        if not p.get("zone") and not p.get("phase"):
            raise ApiError(400, "rebind_sample 至少给出 zone 或 phase 之一")
        if p.get("zone") and p["zone"] not in ("treatment", "control"):
            raise ApiError(400, "zone 仅允许 treatment/control")
        if p.get("phase") and p["phase"] not in ("pre", "post"):
            raise ApiError(400, "phase 仅允许 pre/post")
    return body


# ---------------------------------------------------------------- 微气候监测校验

def _validate_micro_zone(z: Dict[str, Any], wall: Dict[str, Any],
                         seen: set, i: int) -> None:
    _require(z, ["zone_id"], f"第 {i+1} 分区")
    if z["zone_id"] in seen:
        raise ApiError(400, f"分区 id 重复：{z['zone_id']}")
    seen.add(z["zone_id"])
    has_rect = all(k in z for k in ("x_mm", "y_mm", "width_mm", "height_mm"))
    has_circle = all(k in z for k in ("x_mm", "y_mm", "radius_mm"))
    if not has_rect and not has_circle:
        raise ApiError(400, f"分区 {z['zone_id']} 须为矩形"
                            "（x_mm/y_mm/width_mm/height_mm）或圆形（x_mm/y_mm/radius_mm）")
    for k in ("x_mm", "y_mm"):
        if not isinstance(z.get(k), (int, float)):
            raise ApiError(400, f"分区 {z['zone_id']}.{k} 必须为数值（毫米）")
    if has_rect:
        for k in ("width_mm", "height_mm"):
            if not isinstance(z.get(k), (int, float)) or z[k] <= 0:
                raise ApiError(400, f"分区 {z['zone_id']}.{k} 必须为正数")
        if not (0 <= z["x_mm"] <= wall["width_mm"]
                and 0 <= z["y_mm"] <= wall["height_mm"]
                and z["x_mm"] + z["width_mm"] <= wall["width_mm"]
                and z["y_mm"] + z["height_mm"] <= wall["height_mm"]):
            raise ApiError(400, f"分区 {z['zone_id']} 越出墙面范围")
    else:
        if not isinstance(z.get("radius_mm"), (int, float)) or z["radius_mm"] <= 0:
            raise ApiError(400, f"分区 {z['zone_id']}.radius_mm 必须为正数")
        if not (0 <= z["x_mm"] <= wall["width_mm"]
                and 0 <= z["y_mm"] <= wall["height_mm"]):
            raise ApiError(400, f"分区 {z['zone_id']} 圆心越出墙面")


def _validate_calibration(c: Dict[str, Any]) -> None:
    for k in ("calibrated_ts", "valid_until_ts"):
        try:
            parse_dt(c[k])
        except (KeyError, ValueError, TypeError):
            raise ApiError(400, f"校准记录缺合法 {k}")
    if parse_dt(c["valid_until_ts"]) <= parse_dt(c["calibrated_ts"]):
        raise ApiError(400, "校准 valid_until_ts 必须晚于 calibrated_ts")
    for k in ("temp_offset_c", "rh_offset_pct"):
        if k in c and not isinstance(c[k], (int, float)):
            raise ApiError(400, f"校准 {k} 必须为数值")


def validate_monitor(body: Dict[str, Any], wall: Dict[str, Any],
                     store: Store) -> Dict[str, Any]:
    _require(body, ["name", "source_version_id", "zones", "sensors", "rules"],
             "微气候监测计划")
    # 来源必须是已封版：v_*=来源复核封版，tv_*=敷贴试验确认版
    src_kind = body.get("source_kind", "review")
    if src_kind == "review":
        src = store.get_version(body["source_version_id"])
        if not src or src["wall_id"] != wall["wall_id"]:
            raise ApiError(400, f"引用的来源复核封版 {body['source_version_id']!r} "
                                "不存在或不属于该墙体，请先 /lock 封版")
    elif src_kind == "trial":
        src = store.get_trial_version(body["source_version_id"])
        if not src:
            raise ApiError(400, f"引用的敷贴试验确认版 {body['source_version_id']!r} 不存在")
        t = store.get_trial(src["trial_id"])
        if not t or t["wall_id"] != wall["wall_id"]:
            raise ApiError(400, "引用的敷贴试验确认版不属于该墙体")
    else:
        raise ApiError(400, "source_kind 仅允许 review（来源复核封版）/ trial（敷贴确认版）")

    zones, sensors, rules = body["zones"], body["sensors"], body["rules"]
    if not isinstance(zones, list) or not zones:
        raise ApiError(400, "zones 必须为非空数组（按墙面分区）")
    seen_zones: set = set()
    for i, z in enumerate(zones):
        _validate_micro_zone(z, wall, seen_zones, i)
    if not isinstance(sensors, list) or not sensors:
        raise ApiError(400, "sensors 必须为非空数组（温湿度传感器）")
    seen_sensors: set = set()
    for i, s in enumerate(sensors):
        _require(s, ["sensor_id", "x_mm", "y_mm"], f"第 {i+1} 传感器")
        if s["sensor_id"] in seen_sensors:
            raise ApiError(400, f"sensor_id 重复：{s['sensor_id']}")
        seen_sensors.add(s["sensor_id"])
        if not isinstance(s["x_mm"], (int, float)) or not isinstance(s["y_mm"], (int, float)):
            raise ApiError(400, f"传感器 {s['sensor_id']} 坐标必须为数值")
        if not (0 <= s["x_mm"] <= wall["width_mm"]
                and 0 <= s["y_mm"] <= wall["height_mm"]):
            raise ApiError(400, f"传感器 {s['sensor_id']} 坐标越出墙面")
        if s.get("zone_id") and s["zone_id"] not in seen_zones:
            raise ApiError(400, f"传感器 {s['sensor_id']} 声明分区 "
                                f"{s['zone_id']!r} 不在分区表中")
        if s.get("temp_unit", "C") not in TEMP_UNITS:
            raise ApiError(400, f"传感器 {s['sensor_id']} 温度单位仅允许 {TEMP_UNITS}")
        if s.get("rh_unit", "%") not in RH_UNITS:
            raise ApiError(400, f"传感器 {s['sensor_id']} 湿度单位仅允许 {RH_UNITS}")
        cals = s.get("calibrations") or s.get("calibration_list")
        if not cals:
            raise ApiError(400, f"传感器 {s['sensor_id']} 必须至少登记一条校准记录 "
                                "(calibrated_ts/valid_until_ts)")
        if not isinstance(cals, list):
            raise ApiError(400, "calibrations 必须为数组")
        for c in cals:
            _require(c, ["calibrated_ts", "valid_until_ts"], f"传感器 {s['sensor_id']} 校准")
            _validate_calibration(c)
    if not isinstance(rules, list) or not rules:
        raise ApiError(400, "rules 必须为非空数组（盐类潮解/析晶规则）")
    rule_ids: set = set()
    for r in rules:
        _require(r, ["rule_id", "salt", "drh_percent", "crh_percent", "temp_min_c",
                     "temp_max_c", "min_wet_hours", "min_dry_hours"], "盐类规则")
        if r["rule_id"] in rule_ids:
            raise ApiError(400, f"rule_id 重复：{r['rule_id']}")
        rule_ids.add(r["rule_id"])
        try:
            validate_rule(r)
        except ValueError as exc:
            raise ApiError(400, str(exc))
        for k in ("drh_percent", "crh_percent", "temp_min_c", "temp_max_c",
                  "min_wet_hours", "min_dry_hours"):
            if not isinstance(r[k], (int, float)):
                raise ApiError(400, f"规则 {r['rule_id']}.{k} 必须为数值")
    zs = body.get("zone_salts")
    if zs:
        if not isinstance(zs, list):
            raise ApiError(400, "zone_salts 必须为数组（{zone_id, rule_id}）")
        for q in zs:
            _require(q, ["zone_id", "rule_id"], "zone_salts")
            if q["zone_id"] not in seen_zones:
                raise ApiError(400, f"zone_salts 引用未知分区 {q['zone_id']!r}")
            if q["rule_id"] not in rule_ids:
                raise ApiError(400, f"zone_salts 引用未知规则 {q['rule_id']!r}")
    if "params" in body and not isinstance(body["params"], dict):
        raise ApiError(400, "params 必须为对象")
    return body


def validate_reading(body: Dict[str, Any], sensor_ids: set) -> Dict[str, Any]:
    _require(body, ["sensor_id", "ts"], "传感器读数")
    if body["sensor_id"] not in sensor_ids:
        raise ApiError(400, f"传感器 {body['sensor_id']} 不在监测计划中")
    try:
        parse_dt(body["ts"])
    except (ValueError, TypeError):
        raise ApiError(400, "ts 必须为 ISO8601 时间")
    if "temp" in body and body["temp"] is not None and not isinstance(body["temp"], (int, float)):
        raise ApiError(400, "temp 必须为数值或 null（缺测）")
    if "rh" in body and body["rh"] is not None and not isinstance(body["rh"], (int, float)):
        raise ApiError(400, "rh 必须为数值或 null（缺测）")
    if body.get("temp_unit") and body["temp_unit"] not in TEMP_UNITS:
        raise ApiError(400, f"temp_unit 仅允许 {TEMP_UNITS}，混录将判单位冲突待判")
    if body.get("rh_unit") and body["rh_unit"] not in RH_UNITS:
        raise ApiError(400, f"rh_unit 仅允许 {RH_UNITS}，混录将判单位冲突待判")
    if "rh" in body and body["rh"] is not None:
        unit = body.get("rh_unit", "%")
        if unit == "%" and not 0 <= body["rh"] <= 100:
            raise ApiError(400, "rh 以 % 记录时必须在 0~100")
        if unit == "fraction" and not 0 <= body["rh"] <= 1:
            raise ApiError(400, "rh 以 fraction 记录时必须在 0~1")
    return body


def validate_monitor_revision(body: Dict[str, Any], monitor: Dict[str, Any]
                              ) -> Dict[str, Any]:
    _require(body, ["kind", "reason", "payload"], "微气候修订")
    if body["kind"] not in MONITOR_REVISION_KINDS:
        raise ApiError(400, f"kind 必须为 {sorted(MONITOR_REVISION_KINDS)}")
    if not str(body["reason"]).strip():
        raise ApiError(400, "每次改绑/采用保守规则必须留理由 reason")
    p = body["payload"]
    if not isinstance(p, dict):
        raise ApiError(400, "payload 必须为对象")
    zone_ids = {z["zone_id"] for z in monitor["zones"]}
    sensor_ids = {s["sensor_id"] for s in monitor["sensors"]}
    rules = {r["rule_id"]: r for r in monitor["rules"]}
    if body["kind"] in ("rebind_sensor", "unbind_sensor"):
        _require(p, ["sensor_id"], "payload")
        if p["sensor_id"] not in sensor_ids:
            raise ApiError(400, f"传感器 {p['sensor_id']} 不在监测计划中")
    if body["kind"] == "rebind_sensor":
        _require(p, ["zone_id"], "payload")
        if p["zone_id"] not in zone_ids:
            raise ApiError(400, f"目标分区 {p['zone_id']} 不在监测计划中")
        sensor = next(s for s in monitor["sensors"]
                      if s["sensor_id"] == p["sensor_id"])
        target = next(z for z in monitor["zones"] if z["zone_id"] == p["zone_id"])
        if not point_in_zone(target, sensor["x_mm"], sensor["y_mm"]):
            if not str(body["reason"]).strip() or len(str(body["reason"]).strip()) < 6:
                raise ApiError(400, "改绑到几何范围之外的分区时，理由必须说明现场布设/"
                                    "迁移依据（不少于 6 字）")
    if body["kind"] == "adopt_sensor":
        _require(p, ["zone_id", "sensor_id"], "payload")
        if p["zone_id"] not in zone_ids:
            raise ApiError(400, f"分区 {p['zone_id']} 不存在")
        if p["sensor_id"] not in sensor_ids:
            raise ApiError(400, f"传感器 {p['sensor_id']} 不存在")
    if body["kind"] == "adopt_conservative_rule":
        _require(p, ["zone_id", "rule_id"], "payload")
        if p["zone_id"] not in zone_ids:
            raise ApiError(400, f"分区 {p['zone_id']} 不存在")
        if p["rule_id"] not in rules:
            raise ApiError(400, f"基础规则 {p['rule_id']} 不在监测计划规则目录中；"
                                "保守规则只能从已封版登记的规则派生")
        params = dict(MICROCLIMATE_DEFAULTS)
        params.update(monitor.get("params") or {})
        override = {k: p[k] for k in ("drh_percent", "crh_percent", "temp_min_c",
                                      "temp_max_c", "min_wet_hours", "min_dry_hours")
                    if k in p}
        try:
            derive_conservative_rule(rules[p["rule_id"]], override, params)
        except ValueError as exc:
            raise ApiError(400, f"保守派生规则不合法：{exc}")
    return body


# ---------------------------------------------------------------- 服务

class SaltApiApp:
    def __init__(self, store: Store):
        self.store = store

    def _wall_or_404(self, wall_id: str) -> Dict[str, Any]:
        wall = self.store.get_wall(wall_id)
        if not wall:
            raise ApiError(404, f"墙体 {wall_id} 不存在")
        return wall

    def _run_review(self, wall_id: str) -> Dict[str, Any]:
        wall = self._wall_or_404(wall_id)
        b = self.store.bundle(wall_id)
        return review(wall, b["samples"], b["rains"], b["repairs"], b["revisions"])

    # GET ------------------------------------------------------------------
    def health(self) -> Dict[str, Any]:
        return {"status": "ok", "service": "fresco-salt-review", "subservice": "microclimate-cycles", "version": __version__}

    def list_walls(self) -> Dict[str, Any]:
        return {"walls": self.store.list_walls()}

    def get_wall(self, wall_id: str) -> Dict[str, Any]:
        return {"wall": self._wall_or_404(wall_id),
                "counts": {k: len(v) for k, v in self.store.bundle(wall_id).items()}}

    def get_analysis(self, wall_id: str) -> Dict[str, Any]:
        wall = self._wall_or_404(wall_id)
        result = self._run_review(wall_id)
        return {"wall_id": wall_id, "locked": wall.get("_locked", False), "analysis": result}

    def get_revisions(self, wall_id: str) -> Dict[str, Any]:
        self._wall_or_404(wall_id)
        return {"wall_id": wall_id, "revisions": self.store.get_revisions(wall_id)}

    def list_versions(self, wall_id: str) -> Dict[str, Any]:
        self._wall_or_404(wall_id)
        return {"wall_id": wall_id, "versions": self.store.list_versions(wall_id)}

    def get_version(self, version_id: str, suffix: str) -> Tuple[str, str, Any]:
        v = self.store.get_version(version_id)
        if not v:
            raise ApiError(404, f"版本 {version_id} 不存在")
        if suffix == "/recompute.json":
            return "application/json", serialize(v["recompute"]), None
        if suffix == "/profile.svg":
            return "image/svg+xml", v["svg"], None
        return ("application/json",
                json.dumps({"version_id": version_id, "wall_id": v["wall_id"],
                            "created_ts": v["created_ts"],
                            "recompute_hash": v["recompute"]["recompute_hash"],
                            "verdict": v["recompute"]["result"]["verdict"],
                            "unique_source_name": v["recompute"]["result"]["unique_source_name"]},
                           ensure_ascii=False), None)

    # POST -----------------------------------------------------------------
    def create_wall(self, body: Dict[str, Any]) -> Dict[str, Any]:
        model = validate_wall(body)
        return {"wall": self.store.create_wall(model)}

    def add_samples(self, wall_id: str, body: Any) -> Dict[str, Any]:
        wall = self._wall_or_404(wall_id)
        self.store.assert_unlocked(wall_id)
        items = body if isinstance(body, list) else body.get("samples")
        if not isinstance(items, list) or not items:
            raise ApiError(400, "请提交 samples 数组")
        cleaned = [validate_sample(dict(it), wall) for it in items]
        ids = self.store.add_samples(wall_id, cleaned)
        return {"added": len(ids), "sample_ids": ids}

    def add_rains(self, wall_id: str, body: Any) -> Dict[str, Any]:
        self._wall_or_404(wall_id)
        self.store.assert_unlocked(wall_id)
        items = body if isinstance(body, list) else body.get("rains")
        if not isinstance(items, list) or not items:
            raise ApiError(400, "请提交 rains 数组")
        cleaned = [validate_rain(dict(it)) for it in items]
        return {"rain_ids": self.store.add_rains(wall_id, cleaned)}

    def add_repairs(self, wall_id: str, body: Any) -> Dict[str, Any]:
        wall = self._wall_or_404(wall_id)
        self.store.assert_unlocked(wall_id)
        items = body if isinstance(body, list) else body.get("repairs")
        if not isinstance(items, list) or not items:
            raise ApiError(400, "请提交 repairs 数组")
        cleaned = [validate_repair(dict(it), wall) for it in items]
        return {"repair_ids": self.store.add_repairs(wall_id, cleaned)}

    def analyze(self, wall_id: str, body: Any) -> Dict[str, Any]:
        self._wall_or_404(wall_id)
        return {"wall_id": wall_id, "analysis": self._run_review(wall_id)}

    def add_revision(self, wall_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        wall = self._wall_or_404(wall_id)
        self.store.assert_unlocked(wall_id)
        body = validate_revision(body, wall, self.store)
        rev = self.store.add_revision(wall_id, body["kind"], body["reason"],
                                      body["payload"], body.get("actor"))
        re_result = self._run_review(wall_id)
        return {"revision": rev,
                "note": "已生成新修订；以下为应用修订后的即时复核（未封版）",
                "verdict": re_result["verdict"],
                "unique_source_name": re_result["unique_source_name"],
                "analysis": re_result}

    def lock(self, wall_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        wall = self._wall_or_404(wall_id)
        self.store.assert_unlocked(wall_id)
        if not isinstance(body, dict):
            body = {}
        actor = body.get("actor")
        b = self.store.bundle(wall_id)
        recompute = build_recompute(wall, b["samples"], b["rains"], b["repairs"],
                                    b["revisions"])
        version_id = recompute["version_id"]
        recompute["locked_by"] = actor
        svg = render_profile_svg(wall, recompute["result"])
        self.store.save_version(version_id, wall_id, recompute, svg)
        return {"version_id": version_id, "wall_id": wall_id, "locked": True,
                "input_hash": recompute["input_hash"],
                "params_hash": recompute["params_hash"],
                "recompute_hash": recompute["recompute_hash"],
                "verdict": recompute["result"]["verdict"],
                "unique_source_name": recompute["result"]["unique_source_name"],
                "profile_svg": f"/versions/{version_id}/profile.svg",
                "recompute_json": f"/versions/{version_id}/recompute.json"}

    # 敷贴试验 -------------------------------------------------------------
    def _trial_or_404(self, trial_id: str) -> Dict[str, Any]:
        trial = self.store.get_trial(trial_id)
        if not trial:
            raise ApiError(404, f"敷贴试验 {trial_id} 不存在")
        return trial

    def _run_trial_analysis(self, trial_id: str) -> Dict[str, Any]:
        trial = self._trial_or_404(trial_id)
        wall = self.store.get_wall(trial["wall_id"])
        b = self.store.trial_bundle(trial_id)
        return analyze_trial(wall, trial, b["rounds"], b["revisions"],
                             self.store.get_samples(trial["wall_id"]),
                             self.store.get_rains(trial["wall_id"]))

    def create_trial(self, wall_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        wall = self._wall_or_404(wall_id)
        model = validate_trial(body, wall, self.store)
        return {"trial": self.store.create_trial(wall_id, model)}

    def list_trials(self, wall_id: str) -> Dict[str, Any]:
        self._wall_or_404(wall_id)
        return {"wall_id": wall_id, "trials": self.store.list_trials(wall_id)}

    def get_trial(self, trial_id: str) -> Dict[str, Any]:
        trial = self._trial_or_404(trial_id)
        b = self.store.trial_bundle(trial_id)
        return {"trial": trial,
                "counts": {"rounds": len(b["rounds"]),
                           "revisions": len(b["revisions"])},
                "rounds": b["rounds"]}

    def add_rounds(self, trial_id: str, body: Any) -> Dict[str, Any]:
        self._trial_or_404(trial_id)
        self.store.assert_trial_unlocked(trial_id)
        items = body if isinstance(body, list) else body.get("rounds")
        if not isinstance(items, list) or not items:
            raise ApiError(400, "请提交 rounds 数组")
        cleaned = [validate_round(dict(it)) for it in items]
        return {"round_ids": self.store.add_rounds(trial_id, cleaned)}

    def get_trial_analysis(self, trial_id: str) -> Dict[str, Any]:
        trial = self._trial_or_404(trial_id)
        return {"trial_id": trial_id, "locked": trial.get("_locked", False),
                "analysis": self._run_trial_analysis(trial_id)}

    def get_trial_revisions(self, trial_id: str) -> Dict[str, Any]:
        self._trial_or_404(trial_id)
        return {"trial_id": trial_id,
                "revisions": self.store.get_trial_revisions(trial_id)}

    def add_trial_revision(self, trial_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        trial = self._trial_or_404(trial_id)
        self.store.assert_trial_unlocked(trial_id)
        body = validate_trial_revision(body, trial, self.store)
        rev = self.store.add_trial_revision(trial_id, body["kind"], body["reason"],
                                            body["payload"], body.get("actor"))
        re_result = self._run_trial_analysis(trial_id)
        return {"revision": rev,
                "note": "已生成试验修订（独立试验修订账）；以下为应用修订后的即时核算",
                "classification": re_result["classification"],
                "decision": re_result["decision"],
                "analysis": re_result}

    def confirm_trial(self, trial_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        trial = self._trial_or_404(trial_id)
        self.store.assert_trial_unlocked(trial_id)
        if not isinstance(body, dict):
            body = {}
        wall = self.store.get_wall(trial["wall_id"])
        src = self.store.get_version(trial["source_version_id"])
        if not src:
            raise ApiError(400, "引用的来源复核封版已不存在，无法确认")
        b = self.store.trial_bundle(trial_id)
        confirm = build_confirm(wall, trial, b["rounds"], b["revisions"],
                                self.store.get_samples(trial["wall_id"]),
                                self.store.get_rains(trial["wall_id"]), src)
        version_id = confirm["version_id"]
        confirm["confirmed_by"] = body.get("actor")
        svg = render_balance_svg(trial, confirm["result"])
        self.store.save_trial_version(version_id, trial_id, confirm, svg)
        result = confirm["result"]
        return {"version_id": version_id, "trial_id": trial_id, "locked": True,
                "source_version_id": trial["source_version_id"],
                "input_hash": confirm["input_hash"],
                "params_hash": confirm["params_hash"],
                "confirm_hash": confirm["confirm_hash"],
                "classification": result["classification"],
                "classification_name": result["classification_name"],
                "decision": result["decision"],
                "rounds_json": f"/trial-versions/{version_id}/rounds.json",
                "balance_svg": f"/trial-versions/{version_id}/balance.svg"}

    def list_trial_versions(self, trial_id: str) -> Dict[str, Any]:
        self._trial_or_404(trial_id)
        return {"trial_id": trial_id,
                "versions": self.store.list_trial_versions(trial_id)}

    def get_trial_version(self, version_id: str, suffix: str) -> Tuple[str, str, Any]:
        v = self.store.get_trial_version(version_id)
        if not v:
            raise ApiError(404, f"试验确认版 {version_id} 不存在")
        if suffix == "/rounds.json":
            return "application/json", serialize_confirm(v["confirm"]), None
        if suffix == "/balance.svg":
            return "image/svg+xml", v["svg"], None
        result = v["confirm"]["result"]
        return ("application/json",
                json.dumps({"version_id": version_id, "trial_id": v["trial_id"],
                            "created_ts": v["created_ts"],
                            "confirm_hash": v["confirm"]["confirm_hash"],
                            "source_version_id": v["confirm"]["source"]["version_id"],
                            "classification": result["classification"],
                            "decision": result["decision"]["code"],
                            "can_end": result["decision"]["can_end"]},
                           ensure_ascii=False), None)

    # 微气候监测 -------------------------------------------------------------
    def _monitor_or_404(self, monitor_id: str) -> Dict[str, Any]:
        m = self.store.get_monitor(monitor_id)
        if not m:
            raise ApiError(404, f"微气候监测计划 {monitor_id} 不存在")
        return m

    def _source_info(self, monitor: Dict[str, Any]) -> Dict[str, Any]:
        """解析引用的已封版来源：v_*=来源复核，tv_*=敷贴确认版。

        只有唯一盐源（unique_source）或净移除（net_removal）才算“有结论”，
        否则来源版仍无结论，分区保持待判。
        """
        svid = monitor["source_version_id"]
        kind = monitor.get("source_kind", "review")
        if kind == "trial":
            v = self.store.get_trial_version(svid)
            if not v:
                return {"kind": "trial", "kind_name": "敷贴试验确认版",
                        "version_id": svid, "hash": None, "summary": "来源缺失",
                        "decisive": False}
            result = v["confirm"]["result"]
            cls = result["classification"]
            return {"kind": "trial", "kind_name": "敷贴试验确认版",
                    "version_id": svid, "hash": v["confirm"]["confirm_hash"],
                    "summary": f"{result['classification_name']}｜"
                               f"{result['decision']['name']}",
                    "classification": cls,
                    "decision": result["decision"]["code"],
                    "decisive": cls == "net_removal"}
        v = self.store.get_version(svid)
        if not v:
            return {"kind": "review", "kind_name": "来源复核封版",
                    "version_id": svid, "hash": None, "summary": "来源缺失",
                    "decisive": False}
        result = v["recompute"]["result"]
        return {"kind": "review", "kind_name": "来源复核封版",
                "version_id": svid, "hash": v["recompute"]["recompute_hash"],
                "summary": result.get("verdict_reason", result["verdict"]),
                "verdict": result["verdict"],
                "unique_source": result.get("unique_source"),
                "unique_source_name": result.get("unique_source_name"),
                "decisive": result["verdict"] == "unique_source"}

    def _run_monitor_analysis(self, monitor_id: str) -> Dict[str, Any]:
        monitor = self._monitor_or_404(monitor_id)
        wall = self.store.get_wall(monitor["wall_id"])
        b = self.store.monitor_bundle(monitor_id)
        source = self._source_info(monitor)
        return analyze_monitor(wall, monitor, b["readings"], b["revisions"], source)

    def create_monitor(self, wall_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        wall = self._wall_or_404(wall_id)
        model = validate_monitor(body, wall, self.store)
        return {"monitor": self.store.create_monitor(wall_id, model)}

    def list_monitors(self, wall_id: str) -> Dict[str, Any]:
        self._wall_or_404(wall_id)
        return {"wall_id": wall_id, "monitors": self.store.list_monitors(wall_id)}

    def get_monitor(self, monitor_id: str) -> Dict[str, Any]:
        monitor = self._monitor_or_404(monitor_id)
        b = self.store.monitor_bundle(monitor_id)
        return {"monitor": monitor,
                "counts": {"readings": len(b["readings"]),
                           "revisions": len(b["revisions"])}}

    def add_readings(self, monitor_id: str, body: Any) -> Dict[str, Any]:
        monitor = self._monitor_or_404(monitor_id)
        self.store.assert_monitor_unlocked(monitor_id)
        items = body if isinstance(body, list) else body.get("readings")
        if not isinstance(items, list) or not items:
            raise ApiError(400, "请提交 readings 数组")
        sensor_ids = {s["sensor_id"] for s in monitor["sensors"]}
        cleaned = [validate_reading(dict(it), sensor_ids) for it in items]
        return {"reading_ids": self.store.add_readings(monitor_id, cleaned)}

    def get_monitor_analysis(self, monitor_id: str) -> Dict[str, Any]:
        monitor = self._monitor_or_404(monitor_id)
        return {"monitor_id": monitor_id, "locked": monitor.get("_locked", False),
                "analysis": self._run_monitor_analysis(monitor_id)}

    def get_monitor_revisions(self, monitor_id: str) -> Dict[str, Any]:
        self._monitor_or_404(monitor_id)
        return {"monitor_id": monitor_id,
                "revisions": self.store.get_monitor_revisions(monitor_id)}

    def add_monitor_revision(self, monitor_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        monitor = self._monitor_or_404(monitor_id)
        self.store.assert_monitor_unlocked(monitor_id)
        body = validate_monitor_revision(body, monitor)
        rev = self.store.add_monitor_revision(monitor_id, body["kind"], body["reason"],
                                              body["payload"], body.get("actor"))
        result = self._run_monitor_analysis(monitor_id)
        return {"revision": rev,
                "note": "已生成微气候修订；以下为应用修订后的即时循环分析（未确认封版）",
                "n_pending": result["n_pending"],
                "analysis": result}

    def confirm_monitor(self, monitor_id: str, body: Any) -> Dict[str, Any]:
        monitor = self._monitor_or_404(monitor_id)
        self.store.assert_monitor_unlocked(monitor_id)
        if not isinstance(body, dict):
            body = {}
        wall = self.store.get_wall(monitor["wall_id"])
        b = self.store.monitor_bundle(monitor_id)
        source = self._source_info(monitor)
        confirm = build_micro_confirm(wall, monitor, b["readings"],
                                      b["revisions"], source)
        version_id = confirm["version_id"]
        confirm["confirmed_by"] = body.get("actor")
        svg = render_risk_svg(monitor, confirm["result"])
        self.store.save_monitor_version(version_id, monitor_id, confirm, svg)
        result = confirm["result"]
        assessed = [z for z in result["zones"] if z["status"] == "assessed"]
        return {"version_id": version_id, "monitor_id": monitor_id, "locked": True,
                "source_version_id": monitor["source_version_id"],
                "source_kind": monitor.get("source_kind", "review"),
                "input_hash": confirm["input_hash"],
                "params_hash": confirm["params_hash"],
                "confirm_hash": confirm["confirm_hash"],
                "n_pending": result["n_pending"],
                "n_assessed": result["n_assessed"],
                "assessed_summary": [{"zone_id": z["zone_id"],
                                      "risk_level": z["risk_level"],
                                      "n_complete_cycles": z["n_complete_cycles"]}
                                     for z in assessed],
                "zones_json": f"/microclimate-versions/{version_id}/zones.json",
                "risk_svg": f"/microclimate-versions/{version_id}/risk.svg"}

    def list_monitor_versions(self, monitor_id: str) -> Dict[str, Any]:
        self._monitor_or_404(monitor_id)
        return {"monitor_id": monitor_id,
                "versions": self.store.list_monitor_versions(monitor_id)}

    def get_monitor_version(self, version_id: str, suffix: str
                            ) -> Tuple[str, str, Any]:
        v = self.store.get_monitor_version(version_id)
        if not v:
            raise ApiError(404, f"微气候确认版 {version_id} 不存在")
        if suffix == "/zones.json":
            return "application/json", serialize_micro_confirm(v["confirm"]), None
        if suffix == "/risk.svg":
            return "image/svg+xml", v["svg"], None
        result = v["confirm"]["result"]
        return ("application/json",
                json.dumps({"version_id": version_id,
                            "monitor_id": v["monitor_id"],
                            "created_ts": v["created_ts"],
                            "confirm_hash": v["confirm"]["confirm_hash"],
                            "source_version_id": v["confirm"]["source"]["version_id"],
                            "n_pending": result["n_pending"],
                            "n_assessed": result["n_assessed"]},
                           ensure_ascii=False), None)


# 路由表：(method, pattern_kind) -> handler 名
class Handler(BaseHTTPRequestHandler):
    server_version = f"FrescoSalt/{__version__}"

    def log_message(self, fmt, *args):  # 静默常规访问日志
        return

    @property
    def app(self) -> SaltApiApp:
        return self.server.app  # type: ignore[attr-defined]

    def _send_json(self, status: int, payload: Any):
        raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_raw(self, status: int, ctype: str, raw: str):
        data = raw.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ApiError(400, f"请求体不是合法 JSON：{exc}")

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method: str):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        body: Any = {}
        try:
            if method == "POST":
                body = self._read_body()
            ctype, raw, payload = self._route(method, path, body)
            if raw is not None:
                self._send_raw(200, ctype, raw)
            else:
                self._send_json(200, payload)
        except ApiError as e:
            self._send_json(e.status, {"error": e.message, "status": e.status,
                                       "detail": e.detail})
        except PermissionError as e:
            self._send_json(409, {"error": str(e), "status": 409})
        except LookupError as e:
            self._send_json(404, {"error": str(e), "status": 404})
        except Exception as e:  # 兜底，避免连接挂死
            self._send_json(500, {"error": f"内部错误：{e}", "status": 500})

    def _route(self, method: str, path: str, body: Any):
        app = self.app
        # 固定路径
        static_get = {"/": ("health",), "/health": ("health",),
                      "/walls": ("list_walls",)}
        static_post = {"/walls": ("create_wall",)}
        if method == "GET" and path in static_get:
            fn = getattr(app, static_get[path][0])
            return "application/json", None, fn()
        if method == "POST" and path in static_post:
            return "application/json", None, app.create_wall(body)

        parts = [p for p in path.split("/") if p]
        # /walls/{id}...
        if len(parts) >= 2 and parts[0] == "walls":
            wid = parts[1]
            sub = "/" + "/".join(parts[2:])
            if method == "GET" and sub == "":
                return "application/json", None, app.get_wall(wid)
            if method == "POST" and sub == "/samples":
                return "application/json", None, app.add_samples(wid, body)
            if method == "POST" and sub == "/rainfall":
                return "application/json", None, app.add_rains(wid, body)
            if method == "POST" and sub == "/repairs":
                return "application/json", None, app.add_repairs(wid, body)
            if method == "POST" and sub == "/analysis":
                return "application/json", None, app.analyze(wid, body)
            if method == "GET" and sub == "/analysis":
                return "application/json", None, app.get_analysis(wid)
            if method == "GET" and sub == "/revisions":
                return "application/json", None, app.get_revisions(wid)
            if method == "POST" and sub == "/revisions":
                return "application/json", None, app.add_revision(wid, body)
            if method == "POST" and sub == "/lock":
                return "application/json", None, app.lock(wid, body)
            if method == "GET" and sub == "/versions":
                return "application/json", None, app.list_versions(wid)
            if method == "POST" and sub == "/trials":
                return "application/json", None, app.create_trial(wid, body)
            if method == "GET" and sub == "/trials":
                return "application/json", None, app.list_trials(wid)
            if method == "POST" and sub == "/monitors":
                return "application/json", None, app.create_monitor(wid, body)
            if method == "GET" and sub == "/monitors":
                return "application/json", None, app.list_monitors(wid)
        # /trials/{id}...
        if len(parts) >= 2 and parts[0] == "trials":
            tid = parts[1]
            sub = "/" + "/".join(parts[2:])
            if method == "GET" and sub == "":
                return "application/json", None, app.get_trial(tid)
            if method == "POST" and sub == "/rounds":
                return "application/json", None, app.add_rounds(tid, body)
            if method == "GET" and sub == "/analysis":
                return "application/json", None, app.get_trial_analysis(tid)
            if method == "POST" and sub == "/analysis":
                return "application/json", None, app.get_trial_analysis(tid)
            if method == "GET" and sub == "/revisions":
                return "application/json", None, app.get_trial_revisions(tid)
            if method == "POST" and sub == "/revisions":
                return "application/json", None, app.add_trial_revision(tid, body)
            if method == "POST" and sub == "/confirm":
                return "application/json", None, app.confirm_trial(tid, body)
            if method == "GET" and sub == "/versions":
                return "application/json", None, app.list_trial_versions(tid)
        # /monitors/{id}...
        if len(parts) >= 2 and parts[0] == "monitors":
            mid = parts[1]
            sub = "/" + "/".join(parts[2:])
            if method == "GET" and sub == "":
                return "application/json", None, app.get_monitor(mid)
            if method == "POST" and sub == "/readings":
                return "application/json", None, app.add_readings(mid, body)
            if method == "GET" and sub == "/analysis":
                return "application/json", None, app.get_monitor_analysis(mid)
            if method == "POST" and sub == "/analysis":
                return "application/json", None, app.get_monitor_analysis(mid)
            if method == "GET" and sub == "/revisions":
                return "application/json", None, app.get_monitor_revisions(mid)
            if method == "POST" and sub == "/revisions":
                return "application/json", None, app.add_monitor_revision(mid, body)
            if method == "POST" and sub == "/confirm":
                return "application/json", None, app.confirm_monitor(mid, body)
            if method == "GET" and sub == "/versions":
                return "application/json", None, app.list_monitor_versions(mid)
        # /microclimate-versions/{id}[/zones.json|/risk.svg]
        if len(parts) >= 2 and parts[0] == "microclimate-versions" and method == "GET":
            vid = parts[1]
            suffix = "/" + "/".join(parts[2:]) if len(parts) > 2 else ""
            return app.get_monitor_version(vid, suffix)
        # /trial-versions/{id}[/rounds.json|/balance.svg]
        if len(parts) >= 2 and parts[0] == "trial-versions" and method == "GET":
            vid = parts[1]
            suffix = "/" + "/".join(parts[2:]) if len(parts) > 2 else ""
            ctype, raw, payload = app.get_trial_version(vid, suffix)
            return ctype, raw, payload
        # /versions/{id}[/recompute.json|/profile.svg]
        if len(parts) >= 2 and parts[0] == "versions" and method == "GET":
            vid = parts[1]
            suffix = "/" + "/".join(parts[2:]) if len(parts) > 2 else ""
            ctype, raw, payload = app.get_version(vid, suffix)
            return ctype, raw, payload
        raise ApiError(404, f"无此路由：{method} {path}")


def make_server(host: str, port: int, db_path: str) -> ThreadingHTTPServer:
    store = Store(db_path)
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.app = SaltApiApp(store)  # type: ignore[attr-defined]
    return httpd


def main(argv: Optional[List[str]] = None) -> None:
    ap = argparse.ArgumentParser(description="壁画盐害来源复核 API")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--db", default="fresco_salt.db")
    args = ap.parse_args(argv)
    httpd = make_server(args.host, args.port, args.db)
    print(f"壁画盐害来源复核 API 监听 http://{args.host}:{args.port} （库 {args.db}）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.app.store.close()
        httpd.server_close()


if __name__ == "__main__":
    main()

"""微气候确认版产物：冻结来源、规则与采用序列，生成确定性 JSON。

确认版冻结：监测分区模型（分区/传感器/校准/盐类规则）、全部采样时序读数、
微气候修订账（改绑/采用序列/保守规则），以及引用的已封版来源（来源复核封版
v_* 或敷贴试验确认版 tv_*：版本号 + 内容哈希 + 结论），并冻结逐区循环统计
结果；可用 verify() 离线重算比对哈希。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .microclimate import analyze_monitor
from .util import content_hash


def build_confirm(wall: Dict[str, Any], monitor: Dict[str, Any],
                  readings: List[Dict[str, Any]],
                  revisions: List[Dict[str, Any]],
                  source_info: Dict[str, Any],
                  version_id: Optional[str] = None) -> Dict[str, Any]:
    result = analyze_monitor(wall, monitor, readings, revisions, source_info)
    frozen = {
        "wall": {k: v for k, v in wall.items() if not k.startswith("_")},
        "monitor": monitor,
        "readings": readings,
        "revisions": revisions,
        "source": source_info,
    }
    input_hash = content_hash(frozen)
    params_hash = content_hash({"params": result["params_effective"]})
    # 版本号只依赖冻结输入与参数，避免与 confirm_hash 形成循环依赖
    if version_id is None:
        version_id = "mc_" + content_hash(
            {"input_hash": input_hash, "params_hash": params_hash})[:8]
    payload = {
        "schema": "fresco-salt-microclimate-confirm/1",
        "version_id": version_id,
        "monitor_id": monitor["monitor_id"],
        "wall_id": monitor["wall_id"],
        "source": source_info,
        "frozen_input": frozen,
        "params_effective": result["params_effective"],
        "input_hash": input_hash,
        "params_hash": params_hash,
        "result": result,
    }
    payload["confirm_hash"] = content_hash(
        {k: v for k, v in payload.items() if k != "confirm_hash"})
    return payload


def serialize(payload: Dict[str, Any]) -> str:
    from .util import canonical_json
    return canonical_json(payload)


def verify(payload: Dict[str, Any]) -> bool:
    """复算校验：用冻结输入重跑微气候循环分析并比对哈希。"""
    f = payload["frozen_input"]
    rerun = build_confirm(f["wall"], f["monitor"], f["readings"],
                          f["revisions"], f["source"], payload.get("version_id"))
    return (rerun["input_hash"] == payload["input_hash"]
            and rerun["params_hash"] == payload["params_hash"]
            and rerun["confirm_hash"] == payload["confirm_hash"])

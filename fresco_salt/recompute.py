"""封版复算产物：冻结输入、参数、离子登记与复核结果，生成确定性 JSON。"""
from __future__ import annotations

from typing import Any, Dict

from .analysis import review
from .chemistry import IONS, SUPPORTED_UNITS
from .util import canonical_json, content_hash


def build_recompute(wall: Dict[str, Any], samples, rains, repairs, revisions,
                    version_id: str | None = None) -> Dict[str, Any]:
    result = review(wall, samples, rains, repairs, revisions)
    frozen = {
        "wall": wall,
        "samples": samples,
        "rainfall": rains,
        "repairs": repairs,
        "revisions": revisions,
    }
    input_hash = content_hash(frozen)
    params_hash = content_hash({"params": result["params_effective"]})
    # 版本号只依赖冻结输入与参数，避免与 recompute_hash 形成循环依赖
    if version_id is None:
        version_id = "v_" + content_hash(
            {"input_hash": input_hash, "params_hash": params_hash})[:8]
    payload = {
        "schema": "fresco-salt-recompute/1",
        "version_id": version_id,
        "wall_id": wall["wall_id"],
        "frozen_input": frozen,
        "params_effective": result["params_effective"],
        "ion_registry": {k: {"charge": v["charge"], "mw": v["mw"], "cn": v["cn"]}
                         for k, v in sorted(IONS.items())},
        "accepted_units": sorted(SUPPORTED_UNITS),
        "input_hash": input_hash,
        "params_hash": params_hash,
        "result": result,
    }
    payload["recompute_hash"] = content_hash(
        {k: v for k, v in payload.items() if k != "recompute_hash"})
    return payload


def serialize(payload: Dict[str, Any]) -> str:
    return canonical_json(payload)


def verify(payload: Dict[str, Any]) -> bool:
    """复算校验：用冻结输入重跑 review 并比对哈希（用于版本可信检查）。"""
    f = payload["frozen_input"]
    rerun = build_recompute(f["wall"], f["samples"], f["rainfall"], f["repairs"],
                            f["revisions"], payload.get("version_id"))
    return (rerun["input_hash"] == payload["input_hash"]
            and rerun["params_hash"] == payload["params_hash"]
            and rerun["recompute_hash"] == payload["recompute_hash"])

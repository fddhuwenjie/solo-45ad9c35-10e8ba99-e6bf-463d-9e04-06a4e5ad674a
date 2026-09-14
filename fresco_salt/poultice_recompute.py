"""敷贴试验确认版产物：固定来源复核封版、轮次与决定，生成确定性 JSON。

确认版冻结：试验定义与生效参数、轮次与浸出液、试验修订账、绑定样本与
环境记录快照、引用的来源复核封版（version_id + recompute_hash），以及
逐轮核算、离子收支与结束判定结果；可用 verify() 离线复算比对哈希。
"""
from __future__ import annotations

from typing import Any, Dict

from .chemistry import IONS, SUPPORTED_UNITS
from .poultice import analyze_trial
from .util import canonical_json, content_hash


def build_confirm(wall: Dict[str, Any], trial: Dict[str, Any],
                  rounds, revisions, samples, rains,
                  source_version: Dict[str, Any],
                  version_id: str | None = None) -> Dict[str, Any]:
    result = analyze_trial(wall, trial, rounds, revisions, samples, rains)
    bound_ids = {b["sample_id"] for b in trial.get("bindings") or []}
    env_ids = set(trial.get("env_rain_ids") or [])
    frozen = {
        "wall": {k: v for k, v in wall.items() if not k.startswith("_")},
        "trial": trial,
        "rounds": rounds,
        "trial_revisions": revisions,
        "bound_samples": [s for s in samples if s.get("sample_id") in bound_ids],
        "env_rains": [r for r in rains if r.get("rain_id") in env_ids],
        "source_version": {
            "version_id": source_version["version_id"],
            "recompute_hash": source_version["recompute"]["recompute_hash"],
        },
    }
    input_hash = content_hash(frozen)
    params_hash = content_hash({"params": result["params_effective"]})
    # 版本号只依赖冻结输入与参数，避免与 confirm_hash 形成循环依赖
    if version_id is None:
        version_id = "tv_" + content_hash(
            {"input_hash": input_hash, "params_hash": params_hash})[:8]
    src_result = source_version["recompute"]["result"]
    payload = {
        "schema": "fresco-salt-poultice-confirm/1",
        "version_id": version_id,
        "trial_id": trial["trial_id"],
        "wall_id": trial["wall_id"],
        "source": {**frozen["source_version"],
                   "verdict": src_result["verdict"],
                   "unique_source": src_result["unique_source"],
                   "unique_source_name": src_result["unique_source_name"]},
        "frozen_input": frozen,
        "params_effective": result["params_effective"],
        "ion_registry": {k: {"charge": v["charge"], "mw": v["mw"], "cn": v["cn"]}
                         for k, v in sorted(IONS.items())},
        "accepted_units": sorted(SUPPORTED_UNITS),
        "input_hash": input_hash,
        "params_hash": params_hash,
        "result": result,
    }
    payload["confirm_hash"] = content_hash(
        {k: v for k, v in payload.items() if k != "confirm_hash"})
    return payload


def serialize(payload: Dict[str, Any]) -> str:
    return canonical_json(payload)


def verify(payload: Dict[str, Any]) -> bool:
    """复算校验：用冻结输入重跑试验核算并比对哈希（用于确认版可信检查）。"""
    f = payload["frozen_input"]
    src = payload["source"]
    source_version = {
        "version_id": src["version_id"],
        "recompute": {"recompute_hash": src["recompute_hash"],
                      "result": {"verdict": src["verdict"],
                                 "unique_source": src["unique_source"],
                                 "unique_source_name": src["unique_source_name"]}},
    }
    rerun = build_confirm(f["wall"], f["trial"], f["rounds"], f["trial_revisions"],
                          f["bound_samples"], f["env_rains"], source_version,
                          payload.get("version_id"))
    return (rerun["input_hash"] == payload["input_hash"]
            and rerun["params_hash"] == payload["params_hash"]
            and rerun["confirm_hash"] == payload["confirm_hash"])

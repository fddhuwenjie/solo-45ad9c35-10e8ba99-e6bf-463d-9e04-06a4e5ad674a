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
from .recompute import build_recompute, serialize
from .svg import render_profile_svg
from .util import parse_dt

REVISION_KINDS = {"exclude_sample", "restore_sample", "correct_binding",
                  "adopt_conservative"}


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
        return {"status": "ok", "service": "fresco-salt-review", "version": __version__}

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

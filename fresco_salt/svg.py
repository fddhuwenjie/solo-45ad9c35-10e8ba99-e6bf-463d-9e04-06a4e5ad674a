"""盐分剖面 SVG 生成：左—深度剖面，中—高度带响应，右—候选证据裁决。

深度轴按壁画习惯向下为正（表层在上、地杖/砌体深处在下）。
"""
from __future__ import annotations

from typing import Any, Dict, List
from xml.sax.saxutils import escape

from .analysis import CANDIDATE_CN, CANDIDATES
from .chemistry import IONS
from .util import r4

W, H = 1080, 640
MARGIN = {"l": 70, "r": 30, "t": 60, "b": 50}
ION_COLORS = ["#c0392b", "#2980b9", "#27ae60", "#8e44ad",
              "#d35400", "#16a085", "#7f8c8d"]
CAND_COLORS = {"rising": "#2980b9", "leak": "#c0392b", "material": "#8e44ad"}


def _esc(s: Any) -> str:
    return escape(str(s))


def _nice_max(v: float) -> float:
    if v <= 0:
        return 1.0
    import math
    mag = 10 ** math.floor(math.log10(v))
    for f in (1, 2, 2.5, 5, 10):
        if v <= f * mag:
            return f * mag
    return 10 * mag


def render_profile_svg(wall: Dict[str, Any], result: Dict[str, Any]) -> str:
    m = result.get("metrics") or {}
    prof = m.get("ion_depth_profiles", {})
    # 选取有数据的前 4 种离子（按原始点峰值排序）
    peak = {ion: max(p["value"] for p in data["points"]) for ion, data in prof.items()}
    ions = sorted(peak, key=lambda k: peak[k], reverse=True)[:6]

    panels = [
        (MARGIN["l"], 250, "深度剖面：离子浓度 vs 深度（表层在上）"),
        (360, 250, "高度带与雨后脉冲"),
        (650, 400, "候选迁移路径证据裁决"),
    ]
    parts: List[str] = []
    parts.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
                 f'viewBox="0 0 {W} {H}" font-family="sans-serif" font-size="12">')
    parts.append(f'<rect width="{W}" height="{H}" fill="white"/>')
    parts.append(f'<text x="{W/2}" y="28" text-anchor="middle" font-size="18" font-weight="bold">'
                 f'{_esc(wall.get("name", wall["wall_id"]))} — 盐分剖面复核图</text>')
    parts.append(f'<text x="{W/2}" y="46" text-anchor="middle" fill="#555">'
                 f'裁决：{_esc(_verdict_text(result))}</text>')

    _depth_panel(parts, panels[0], ions, prof, wall, result)
    _height_panel(parts, panels[1], m)
    _candidate_panel(parts, panels[2], result)

    parts.append(f'<text x="{MARGIN["l"]}" y="{H-14}" fill="#888" font-size="10">'
                 '实线=干阶段 虚线=湿阶段；空心点=半检出限代用；'
                 '浓度单位 mmol/kg_dry（固相）或 mmol/L（液相）</text>')
    parts.append("</svg>")
    return "\n".join(parts)


def _verdict_text(result: Dict[str, Any]) -> str:
    v = result["verdict"]
    if v == "unique_source":
        return f"唯一保留路径：{result['unique_source_name']}"
    return {"indeterminate_multiple": "多路径并列/证据不足，禁止唯一盐源",
            "indeterminate_none": "无路径达标，禁止唯一盐源",
            "indeterminate_conservative": "保守解释模式，禁止唯一盐源"}.get(v, v)


def _axes(parts, x, ytop, w, h, x_label, y_label, xmax, vmax_y):
    parts.append(f'<rect x="{x}" y="{ytop}" width="{w}" height="{h}" fill="#fafafa" '
                 'stroke="#ccc"/>')
    # x 刻度（4 格）
    for i in range(5):
        gx = x + w * i / 4
        parts.append(f'<line x1="{gx}" y1="{ytop+h}" x2="{gx}" y2="{ytop+h+4}" stroke="#333"/>')
        parts.append(f'<text x="{gx}" y="{ytop+h+16}" text-anchor="middle" fill="#333">'
                     f'{r4(xmax*i/4):g}</text>')
    for i in range(5):
        gy = ytop + h * i / 4
        parts.append(f'<line x1="{x-4}" y1="{gy}" x2="{x}" y2="{gy}" stroke="#333"/>')
        # 普通纵轴：顶端最大、底端为 0
        parts.append(f'<text x="{x-8}" y="{gy+4}" text-anchor="end" fill="#333">'
                     f'{r4(vmax_y*(1-i/4)):g}</text>')
    parts.append(f'<text x="{x+w/2}" y="{ytop+h+36}" text-anchor="middle">{_esc(x_label)}</text>')
    parts.append(f'<text x="18" y="{ytop+h/2}" text-anchor="middle" '
                 f'transform="rotate(-90 18 {ytop+h/2})">{_esc(y_label)}</text>')


def _depth_panel(parts, panel, ions, prof, wall, result):
    x, w, title = panel
    ytop, h = 80, 440
    parts.append(f'<text x="{x}" y="{ytop-16}" font-weight="bold">{_esc(title)}</text>')
    dmax = max([wall["thickness_mm"]]
               + [p["depth_mm"] for data in prof.values() for p in data["points"]] + [1.0])
    vmax = _nice_max(max([p["value"] for data in prof.values() for p in data["points"]] + [1.0]))

    def X(d):
        return x + w * d / dmax

    def Y(v):
        return ytop + h * (1 - v / vmax)

    _axes(parts, x, ytop, w, h, "深度 depth_mm（自表层向内 →）", "浓度", dmax, vmax)
    # 层位带
    for li, layer in enumerate(wall["layers"]):
        x1, x2 = X(layer["d_start_mm"]), X(layer["d_end_mm"])
        fill = "#eef4fb" if li % 2 == 0 else "#f4f0f8"
        parts.append(f'<rect x="{x1}" y="{ytop}" width="{max(x2-x1,0)}" height="{h}" '
                     f'fill="{fill}" opacity="0.6"/>')
        mid = (x1 + x2) / 2
        parts.append(f'<text x="{mid}" y="{ytop+h-6}" text-anchor="middle" fill="#666" '
                     f'font-size="10">{_esc(layer["layer_id"])}</text>')
    # 层界
    for layer in wall["layers"][1:]:
        gx = X(layer["d_start_mm"])
        parts.append(f'<line x1="{gx}" y1="{ytop}" x2="{gx}" y2="{ytop+h}" '
                     'stroke="#bbb" stroke-dasharray="3 3"/>')

    censored_pairs = {(ionn, s["sample_id"])
                      for s in result["samples"]["active"]
                      for ionn in (s["balance"] or {}).get("lod_censored_ions", [])}
    for idx, ion in enumerate(ions):
        color = ION_COLORS[idx % len(ION_COLORS)]
        data = prof[ion]
        # 按阶段聚合均值连线（避免同深度多孔样本产生竖直假线）
        for stage, dash in (("dry", ""), ("wet", "3 3")):
            seg = [p for p in data["series"] if p["stage"] == stage]
            if len(seg) >= 2:
                d = "M " + " L ".join(f"{X(p['depth_mm']):.1f},{Y(p['value']):.1f}"
                                      for p in seg)
                parts.append(f'<path d="{d}" fill="none" stroke="{color}" '
                             f'stroke-dasharray="{dash}" stroke-width="1.6"/>')
        for p in data["points"]:
            hollow = (ion, p["sample_id"]) in censored_pairs
            parts.append(f'<circle cx="{X(p["depth_mm"]):.1f}" cy="{Y(p["value"]):.1f}" '
                         f'r="3" fill="{"white" if hollow else color}" stroke="{color}"/>')
    # 图例
    lx, ly = x + 8, ytop + 8
    for idx, ion in enumerate(ions):
        color = ION_COLORS[idx % len(ION_COLORS)]
        yy = ly + idx * 15
        parts.append(f'<rect x="{lx}" y="{yy-8}" width="10" height="3" fill="{color}"/>')
        parts.append(f'<text x="{lx+14}" y="{yy-4}" font-size="10">'
                     f'{_esc(IONS[ion]["cn"])} {ion}</text>')


def _height_panel(parts, panel, m):
    x, w, title = panel
    ytop, h = 80, 440
    parts.append(f'<text x="{x}" y="{ytop-16}" font-weight="bold">{_esc(title)}</text>')
    data = [
        ("低带总盐(均)", m.get("low_band_total_mean"), "#7fb3d9"),
        ("高带干季总盐", m.get("high_band_dry_total"), "#e8a09a"),
        ("高带雨后总盐", m.get("high_band_wet_total"), "#c0392b"),
        ("修补旁总盐", m.get("near_patch_total_mean"), "#b594d0"),
        ("远处总盐", m.get("far_patch_total_mean"), "#d8c9e6"),
    ]
    data = [d for d in data if d[1] is not None]
    if not data:
        parts.append(f'<text x="{x+w/2}" y="{ytop+h/2}" text-anchor="middle" fill="#999">'
                     '有效样本不足</text>')
        return
    vmax = _nice_max(max(d[1] for d in data))
    # 纵轴刻度
    parts.append(f'<line x1="{x}" y1="{ytop}" x2="{x}" y2="{ytop+h}" stroke="#333"/>')
    for i in range(5):
        gy = ytop + h * i / 4
        parts.append(f'<line x1="{x-4}" y1="{gy}" x2="{x}" y2="{gy}" stroke="#333"/>')
        parts.append(f'<text x="{x-8}" y="{gy+4}" text-anchor="end" font-size="10">'
                     f'{r4(vmax*(1-i/4)):g}</text>')
    bw = w / (len(data) * 1.6)
    for i, (label, val, color) in enumerate(data):
        bx = x + w * (i + 0.3) / (len(data) + 0.2)
        bh = h * val / vmax
        parts.append(f'<rect x="{bx:.1f}" y="{ytop+h-bh:.1f}" width="{bw:.1f}" '
                     f'height="{bh:.1f}" fill="{color}" stroke="#555" stroke-width="0.5"/>')
        parts.append(f'<text x="{bx+bw/2:.1f}" y="{ytop+h-bh-4:.1f}" text-anchor="middle" '
                     f'font-size="10">{r4(val):g}</text>')
        parts.append(f'<text x="{bx+bw/2:.1f}" y="{ytop+h+16}" text-anchor="middle" '
                     f'font-size="10">{_esc(label)}</text>')
    parts.append(f'<line x1="{x}" y1="{ytop+h}" x2="{x+w}" y2="{ytop+h}" stroke="#333"/>')
    pulse = m.get("high_band_rain_pulse")
    if pulse:
        parts.append(f'<text x="{x+w/2}" y="{ytop+20}" text-anchor="middle" fill="#c0392b">'
                     f'高带雨后/干季脉冲比 = {r4(pulse)}</text>')


def _candidate_panel(parts, panel, result):
    x, w, title = panel
    ytop, h = 80, 440
    parts.append(f'<text x="{x}" y="{ytop-16}" font-weight="bold">{_esc(title)}</text>')
    rowh = h / 3
    for i, c in enumerate(CANDIDATES):
        info = result["candidates"][c]
        yy = ytop + i * rowh + 6
        color = {"retained": "#1e8449", "excluded": "#922b21",
                 "unresolved": "#b9770e"}[info["status"]]
        parts.append(f'<rect x="{x}" y="{yy}" width="{w}" height="{rowh-12}" rx="6" '
                     f'fill="white" stroke="{color}" stroke-width="1.5"/>')
        parts.append(f'<rect x="{x}" y="{yy}" width="6" height="{rowh-12}" rx="3" '
                     f'fill="{CAND_COLORS[c]}"/>')
        parts.append(f'<text x="{x+14}" y="{yy+18}" font-weight="bold" fill="{color}">'
                     f'{_esc(CANDIDATE_CN[c])}（{_esc(status_cn(info["status"]))}，'
                     f'证据分 {info["score"]}）</text>')
        ty = yy + 36
        for e in info["evidence_support"][:4]:
            parts.append(f'<text x="{x+14}" y="{ty}" font-size="10" fill="#1e8449">'
                         f'＋ [{e["weight"]}] {_esc(e["text"])}</text>')
            ty += 14
        for e in info["evidence_contradiction"][:3]:
            parts.append(f'<text x="{x+14}" y="{ty}" font-size="10" fill="#922b21">'
                         f'－ [{e["weight"]}] {_esc(e["text"])}</text>')
            ty += 14
        if not info["evidence_support"] and not info["evidence_contradiction"]:
            parts.append(f'<text x="{x+14}" y="{ty}" font-size="10" fill="#888">'
                         '（无 fired evidence）</text>')


def status_cn(status: str) -> str:
    return {"retained": "保留", "excluded": "被证据排除", "unresolved": "证据不足/无法区分"}[status]

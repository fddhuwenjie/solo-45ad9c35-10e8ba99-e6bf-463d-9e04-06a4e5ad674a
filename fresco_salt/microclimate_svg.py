"""微气候风险曲线 SVG：左—逐区 RH 风险曲线（叠 DRH/CRH 阈值带），
右—逐区循环统计与门控状态。

曲线面板把各区 RH 序列与盐类规则的潮解 RH（DRH，红色虚线）、析晶 RH
（CRH，蓝色虚线）叠加；待判序列以灰色绘制。统计面板按完整循环数与最长
湿润段排序，并列出各区门控缺口。
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict, List, Optional
from xml.sax.saxutils import escape

from .microclimate import GAP_CN
from .svg import _nice_max
from .util import parse_dt, r4

W, H = 1080, 640
ZONE_COLORS = ["#c0392b", "#2980b9", "#27ae60", "#8e44ad",
               "#d35400", "#16a085", "#7f8c8d"]
RISK_CN = {"high": "高风险", "moderate": "中风险", "low": "低风险",
           "current_wet": "当前湿润", "unknown": "待判"}
RISK_COLOR = {"high": "#922b21", "moderate": "#b9770e", "low": "#1e8449",
              "current_wet": "#c0392b", "unknown": "#888"}


def _esc(s: Any) -> str:
    return escape(str(s))


def render_risk_svg(monitor: Dict[str, Any], result: Dict[str, Any]) -> str:
    parts: List[str] = []
    parts.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
                 f'viewBox="0 0 {W} {H}" font-family="sans-serif" font-size="12">')
    parts.append(f'<rect width="{W}" height="{H}" fill="white"/>')
    parts.append(f'<text x="{W/2}" y="26" text-anchor="middle" font-size="18" '
                 f'font-weight="bold">{_esc(monitor.get("name", monitor["monitor_id"]))}'
                 ' — 微气候结晶循环风险曲线</text>')
    src = result["source"]
    parts.append(f'<text x="{W/2}" y="44" text-anchor="middle" fill="#555">'
                 f'来源：{_esc(src.get("kind_name"))} {_esc(src.get("version_id"))} '
                 f'（{_esc(src.get("summary"))}）；'
                 f'待判 {result["n_pending"]} 区 / 已判 {result["n_assessed"]} 区</text>')

    _curve_panel(parts, (60, 580), result)
    _stat_panel(parts, (680, 220), result)
    _gate_panel(parts, (920, 140), result)

    parts.append(f'<text x="60" y="{H-12}" fill="#888" font-size="10">'
                 'RH 越过 DRH 进入湿润，越过更低的 CRH 才析晶回返；'
                 '带内保持原态（滞回）；断档处曲线断开，灰=待判序列</text>')
    parts.append("</svg>")
    return "\n".join(parts)


# ---------------------------------------------------------------- 曲线面板

def _curve_panel(parts, panel, result):
    x, w = panel
    ytop, h = 70, 420
    parts.append(f'<text x="{x}" y="{ytop-14}" font-weight="bold">'
                 '逐区相对湿度曲线与潮解/析晶阈值</text>')
    zones = result["zones"]
    # 时间范围取全部序列点
    all_ts = [parse_dt(p["ts"]) for z in zones for p in z["series_points"]]
    if not all_ts:
        parts.append(f'<rect x="{x}" y="{ytop}" width="{w}" height="{h}" fill="#fafafa" '
                     'stroke="#ccc"/>')
        parts.append(f'<text x="{x+w/2}" y="{ytop+h/2}" text-anchor="middle" fill="#999">'
                     '无采样数据</text>')
        return
    t0, t1 = min(all_ts), max(all_ts)
    if t1 == t0:
        t1 = t0 + timedelta(hours=1)

    def X(t):
        return x + w * (t - t0).total_seconds() / (t1 - t0).total_seconds()

    def Y(rh):
        return ytop + h * (1 - rh / 100.0)

    parts.append(f'<rect x="{x}" y="{ytop}" width="{w}" height="{h}" fill="#fafafa" '
                 'stroke="#ccc"/>')
    for i in range(5):
        rhv = i * 25
        gy = Y(rhv)
        parts.append(f'<line x1="{x-4}" y1="{gy}" x2="{x}" y2="{gy}" stroke="#333"/>')
        parts.append(f'<text x="{x-8}" y="{gy+4}" text-anchor="end" font-size="10">'
                     f'{rhv}%</text>')
    # x 轴 5 个时间刻度
    for i in range(5):
        ts = t0 + (t1 - t0) * i / 4
        gx = X(ts)
        parts.append(f'<line x1="{gx}" y1="{ytop+h}" x2="{gx}" y2="{ytop+h+4}" '
                     'stroke="#333"/>')
        parts.append(f'<text x="{gx}" y="{ytop+h+16}" text-anchor="middle" font-size="9">'
                     f'{ts.strftime("%m-%d %H:%M")}</text>')

    # 阈值线：从规则目录取 DRH/CRH（相同阈值只画一次）
    seen_drh, seen_crh = set(), set()
    for r in result["rule_catalog"]:
        if r["drh_percent"] not in seen_drh:
            gy = Y(r["drh_percent"])
            parts.append(f'<line x1="{x}" y1="{gy:.1f}" x2="{x+w}" y2="{gy:.1f}" '
                         'stroke="#c0392b" stroke-width="0.8" stroke-dasharray="5 3"/>')
            parts.append(f'<text x="{x+w-4}" y="{gy-3:.1f}" text-anchor="end" '
                         f'font-size="9" fill="#c0392b">DRH {r["drh_percent"]:g}%</text>')
            seen_drh.add(r["drh_percent"])
        if r["crh_percent"] not in seen_crh:
            gy = Y(r["crh_percent"])
            parts.append(f'<line x1="{x}" y1="{gy:.1f}" x2="{x+w}" y2="{gy:.1f}" '
                         'stroke="#2471a3" stroke-width="0.8" stroke-dasharray="5 3"/>')
            parts.append(f'<text x="{x+4}" y="{gy-3:.1f}" font-size="9" '
                         f'fill="#2471a3">CRH {r["crh_percent"]:g}%</text>')
            seen_crh.add(r["crh_percent"])

    # 逐区 RH 折线（断点处不连接）
    lx, ly = x + 8, ytop + 12
    for zi, z in enumerate(zones):
        color = ZONE_COLORS[zi % len(ZONE_COLORS)]
        pending = z["status"] == "pending"
        stroke = "#b0b0b0" if pending else color
        pts = z["series_points"]
        run: List[tuple] = []

        def flush():
            if len(run) >= 2:
                d = "M " + " L ".join(f"{X(parse_dt(p['ts'])):.1f},{Y(p['rh_percent']):.1f}"
                                      for p in run)
                dash = ' stroke-dasharray="2 2"' if pending else ""
                parts.append(f'<path d="{d}" fill="none" stroke="{stroke}"{dash} '
                             'stroke-width="1.5"/>')

        prev = None
        for p in pts:
            if not p["valid"] or p["rh_percent"] is None:
                flush()
                run.clear()
                prev = None
                continue
            if prev is not None:
                dt = (parse_dt(p["ts"]) - parse_dt(prev["ts"])).total_seconds() / 3600
                if dt > result["params_effective"]["gap_max_hours"]:
                    flush()
                    run.clear()
            run.append(p)
            prev = p
        flush()
        # 完整循环起讫标记
        for rr in z["rules"]:
            for c in rr.get("cycles", []):
                if not c["complete"]:
                    continue
                parts.append(f'<circle cx="{X(parse_dt(c["wet_start_ts"])):.1f}" '
                             f'cy="{Y(rr["drh_percent"]):.1f}" r="2.6" fill="{color}"/>')
        parts.append(f'<rect x="{lx}" y="{ly-8}" width="12" height="3" fill="{stroke}"/>')
        parts.append(f'<text x="{lx+16}" y="{ly-4}" font-size="10">'
                     f'{_esc(z["name"])}'
                     f'{"（待判）" if pending else ""}</text>')
        ly += 14

    parts.append(f'<line x1="{x}" y1="{ytop+h}" x2="{x+w}" y2="{ytop+h}" stroke="#333"/>')


# ---------------------------------------------------------------- 统计面板

def _stat_panel(parts, panel, result):
    x, w = panel
    ytop, h = 70, 250
    parts.append(f'<text x="{x}" y="{ytop-14}" font-weight="bold">'
                 '逐区统计：完整循环 / 最长湿润段</text>')
    zones = sorted(result["zones"],
                   key=lambda z: (-z["n_complete_cycles"],
                                  -(z["longest_wet_hours"] or 0)))
    vmax_c = _nice_max(max([z["n_complete_cycles"] for z in zones] + [1]))
    vmax_h = _nice_max(max([z["longest_wet_hours"] or 0 for z in zones] + [1]))
    parts.append(f'<rect x="{x}" y="{ytop}" width="{w}" height="{h}" fill="#fafafa" '
                 'stroke="#ccc"/>')
    rowh = h / max(len(zones), 1)
    for i, z in enumerate(zones):
        ry = ytop + i * rowh + 4
        rc = RISK_COLOR.get(z["risk_level"], "#888")
        parts.append(f'<text x="{x+6}" y="{ry+12}" font-size="10" fill="{rc}">'
                     f'{_esc(z["name"])}：{RISK_CN.get(z["risk_level"], z["risk_level"])}</text>')
        # 循环数条
        bw_c = (w - 120) * z["n_complete_cycles"] / vmax_c
        parts.append(f'<rect x="{x+6}" y="{ry+18}" width="{max(bw_c,1):.1f}" height="8" '
                     f'fill="#c0392b"/>')
        parts.append(f'<text x="{x+12+max(bw_c,1):.1f}" y="{ry+26}" font-size="9" '
                     f'fill="#555">完整循环 {z["n_complete_cycles"]}</text>')
        # 最长湿润条
        lh = z["longest_wet_hours"] or 0
        bw_h = (w - 120) * lh / vmax_h
        parts.append(f'<rect x="{x+6}" y="{ry+30}" width="{max(bw_h,1):.1f}" height="6" '
                     f'fill="#2980b9"/>')
        parts.append(f'<text x="{x+12+max(bw_h,1):.1f}" y="{ry+36}" font-size="9" '
                     f'fill="#555">最长湿润 {r4(lh)}h · 首风险 '
                     f'{_esc((z["first_risk_ts"] or "—")[:16])}</text>')
        if z.get("open_wet"):
            parts.append(f'<text x="{x+6}" y="{ry+48:.0f}" font-size="9" fill="#c0392b">'
                         f'⚠ 开口湿润 {r4(z["open_wet"]["hours"])}h（断档/期末未析晶）</text>')


# ---------------------------------------------------------------- 门控面板

def _gate_panel(parts, panel, result):
    x, w = panel
    ytop, h = 70, 420
    parts.append(f'<text x="{x}" y="{ytop-14}" font-weight="bold">门控与缺口</text>')
    parts.append(f'<rect x="{x}" y="{ytop}" width="{w}" height="{h}" rx="6" '
                 'fill="white" stroke="#ccc"/>')
    ty = ytop + 18
    for z in result["zones"]:
        ok = z["all_gates_passed"]
        rc = RISK_COLOR.get(z["risk_level"], "#888")
        parts.append(f'<text x="{x+8}" y="{ty}" font-size="11" font-weight="bold" '
                     f'fill="{rc}">{_esc(z["name"])}</text>')
        ty += 14
        if ok:
            parts.append(f'<text x="{x+14}" y="{ty}" font-size="10" fill="#1e8449">'
                         f'✓ 六门控通过 ｜ {RISK_CN.get(z["risk_level"])}</text>')
            ty += 16
        for g in z["gates"]:
            if g["passed"]:
                continue
            parts.append(f'<text x="{x+14}" y="{ty}" font-size="10" fill="#922b21">'
                         f'✗ {_esc(g["name"])}</text>')
            ty += 13
        ty += 6
        if ty > ytop + h - 10:
            break

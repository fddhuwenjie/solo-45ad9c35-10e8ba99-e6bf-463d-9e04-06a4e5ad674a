"""敷贴试验离子收支 SVG：左—分离子收支，中—逐轮浸出，右—门控与决定。

收支面板把每种离子的处理前库存、处理后库存与累计浸出移出量并排放置，
直观区分“盐离墙（净移除）”与“盐只是退到深处（向内迁移）”。
"""
from __future__ import annotations

from typing import Any, Dict, List
from xml.sax.saxutils import escape

from .chemistry import IONS
from .poultice import CLASSIFICATION_CN
from .svg import ION_COLORS, _nice_max
from .util import r4

W, H = 1080, 640


def _esc(s: Any) -> str:
    return escape(str(s))


def render_balance_svg(trial: Dict[str, Any], result: Dict[str, Any]) -> str:
    parts: List[str] = []
    parts.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
                 f'viewBox="0 0 {W} {H}" font-family="sans-serif" font-size="12">')
    parts.append(f'<rect width="{W}" height="{H}" fill="white"/>')
    parts.append(f'<text x="{W/2}" y="28" text-anchor="middle" font-size="18" '
                 f'font-weight="bold">{_esc(trial.get("name", trial["trial_id"]))}'
                 ' — 敷贴脱盐离子收支图</text>')
    d = result["decision"]
    mark = "#1e8449" if d["can_end"] else "#922b21"
    parts.append(f'<text x="{W/2}" y="46" text-anchor="middle" fill="{mark}">'
                 f'判定：{_esc(result["classification_name"])} ｜ '
                 f'{_esc(d["name"])}（{_esc(d["reason"])}）</text>')

    _balance_panel(parts, (70, 400), result)
    _rounds_panel(parts, (520, 250), result)
    _gate_panel(parts, (810, 240), result)

    parts.append(f'<text x="70" y="{H-14}" fill="#888" font-size="10">'
                 '库存=层平均浓度×层干质量（体积×干密度），单位 mmol；'
                 '浸出=浓度×体积逐轮累计；灰柱=被修订剔除的污染液轮次</text>')
    parts.append("</svg>")
    return "\n".join(parts)


def _balance_panel(parts, panel, result):
    x, w = panel
    ytop, h = 80, 440
    parts.append(f'<text x="{x}" y="{ytop-16}" font-weight="bold">'
                 '分离子收支（mmol）：处理前库存 / 处理后库存 / 累计浸出</text>')
    inv_t = result["inventory"]["treatment"]["per_ion"]
    bal = result["balance"]["per_ion"]
    drift = result["inventory"]["control_drift_ratio"]
    ions = sorted(set(inv_t) | set(bal),
                  key=lambda i: -(bal.get(i, {}).get("extract_mmol") or 0))[:6]
    if not ions:
        parts.append(f'<text x="{x+w/2}" y="{ytop+h/2}" text-anchor="middle" '
                     'fill="#999">无收支数据</text>')
        return
    vmax = _nice_max(max(
        [inv_t.get(i, {}).get("pre_mmol") or 0 for i in ions]
        + [inv_t.get(i, {}).get("post_mmol") or 0 for i in ions]
        + [bal.get(i, {}).get("extract_mmol") or 0 for i in ions] + [1.0]))
    parts.append(f'<rect x="{x}" y="{ytop}" width="{w}" height="{h}" fill="#fafafa" '
                 'stroke="#ccc"/>')
    for i in range(5):
        gy = ytop + h * i / 4
        parts.append(f'<line x1="{x-4}" y1="{gy}" x2="{x}" y2="{gy}" stroke="#333"/>')
        parts.append(f'<text x="{x-8}" y="{gy+4}" text-anchor="end" font-size="10">'
                     f'{r4(vmax*(1-i/4)):g}</text>')
    series = [("处理前", "#7fb3d9", lambda i: inv_t.get(i, {}).get("pre_mmol") or 0),
              ("处理后", "#e8a09a", lambda i: inv_t.get(i, {}).get("post_mmol") or 0),
              ("累计浸出", "#27ae60", lambda i: bal.get(i, {}).get("extract_mmol") or 0)]
    gw = w / len(ions)
    bw = gw / 4.2
    for gi, ion in enumerate(ions):
        gx = x + gi * gw
        for si, (label, color, val) in enumerate(series):
            v = val(ion)
            bh = h * v / vmax
            bx = gx + (si + 0.35) * bw
            parts.append(f'<rect x="{bx:.1f}" y="{ytop+h-bh:.1f}" width="{bw*0.9:.1f}" '
                         f'height="{bh:.1f}" fill="{color}" stroke="#555" '
                         'stroke-width="0.4"/>')
        cn = IONS.get(ion, {}).get("cn", ion)
        parts.append(f'<text x="{gx+gw/2:.1f}" y="{ytop+h+14}" text-anchor="middle" '
                     f'font-size="10">{_esc(ion)}</text>')
        parts.append(f'<text x="{gx+gw/2:.1f}" y="{ytop+h+26}" text-anchor="middle" '
                     f'font-size="9" fill="#777">{_esc(cn)}</text>')
        dr = drift.get(ion)
        if dr is not None:
            parts.append(f'<text x="{gx+gw/2:.1f}" y="{ytop+h+38}" text-anchor="middle" '
                         f'font-size="9" fill="#8e44ad">对照漂移×{r4(dr):g}</text>')
        row = bal.get(ion)
        if row and not row.get("closed", True):
            parts.append(f'<text x="{gx+gw/2:.1f}" y="{ytop+12}" text-anchor="middle" '
                         'font-size="11" fill="#922b21">✗不闭合</text>')
    parts.append(f'<line x1="{x}" y1="{ytop+h}" x2="{x+w}" y2="{ytop+h}" stroke="#333"/>')
    lx, ly = x + 6, ytop + 10
    for label, color, _ in series:
        parts.append(f'<rect x="{lx}" y="{ly-8}" width="10" height="8" fill="{color}"/>')
        parts.append(f'<text x="{lx+14}" y="{ly}" font-size="10">{label}</text>')
        ly += 14


def _rounds_panel(parts, panel, result):
    x, w = panel
    ytop, h = 80, 440
    parts.append(f'<text x="{x}" y="{ytop-16}" font-weight="bold">'
                 '逐轮浸出（mmol）与累计</text>')
    rounds = result["rounds"]
    if not rounds:
        parts.append(f'<text x="{x+w/2}" y="{ytop+h/2}" text-anchor="middle" '
                     'fill="#999">无轮次</text>')
        return
    vmax = _nice_max(max(
        [r["removed_total_mmol"] or 0 for r in rounds]
        + [r["cumulative_total_mmol"] or 0 for r in rounds] + [1.0]))
    parts.append(f'<rect x="{x}" y="{ytop}" width="{w}" height="{h}" fill="#fafafa" '
                 'stroke="#ccc"/>')
    for i in range(5):
        gy = ytop + h * i / 4
        parts.append(f'<line x1="{x-4}" y1="{gy}" x2="{x}" y2="{gy}" stroke="#333"/>')
        parts.append(f'<text x="{x-8}" y="{gy+4}" text-anchor="end" font-size="10">'
                     f'{r4(vmax*(1-i/4)):g}</text>')
    bw = w / (len(rounds) * 1.8)
    pts = []
    for i, r in enumerate(rounds):
        bx = x + w * (i + 0.4) / (len(rounds) + 0.2)
        v = r["removed_total_mmol"] or 0
        bh = h * v / vmax
        if r["excluded"]:
            fill, dash = "#bbbbbb", ' stroke-dasharray="2 2"'
        else:
            fill, dash = ION_COLORS[2], ""
        parts.append(f'<rect x="{bx:.1f}" y="{ytop+h-bh:.1f}" width="{bw:.1f}" '
                     f'height="{bh:.1f}" fill="{fill}" stroke="#555"{dash}/>')
        label = r["round_id"] + ("（剔除）" if r["excluded"] else "")
        parts.append(f'<text x="{bx+bw/2:.1f}" y="{ytop+h+14}" text-anchor="middle" '
                     f'font-size="9">{_esc(label)}</text>')
        if not r["excluded"] and r["cumulative_total_mmol"] is not None:
            pts.append((bx + bw / 2, ytop + h * (1 - r["cumulative_total_mmol"] / vmax)))
    if len(pts) >= 2:
        dpath = "M " + " L ".join(f"{px:.1f},{py:.1f}" for px, py in pts)
        parts.append(f'<path d="{dpath}" fill="none" stroke="#c0392b" '
                     'stroke-width="1.6"/>')
    for px, py in pts:
        parts.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="3" fill="#c0392b"/>')
    parts.append(f'<line x1="{x}" y1="{ytop+h}" x2="{x+w}" y2="{ytop+h}" stroke="#333"/>')
    parts.append(f'<text x="{x}" y="{ytop+h+30}" font-size="10" fill="#c0392b">'
                 '红线=累计浸出总量</text>')


def _gate_panel(parts, panel, result):
    x, w = panel
    ytop, h = 80, 440
    parts.append(f'<text x="{x}" y="{ytop-16}" font-weight="bold">门控与决定</text>')
    parts.append(f'<rect x="{x}" y="{ytop}" width="{w}" height="{h}" rx="6" '
                 'fill="white" stroke="#ccc"/>')
    ty = ytop + 20
    for g in result["gates"]:
        ok = g["passed"]
        color = "#1e8449" if ok else "#922b21"
        parts.append(f'<text x="{x+10}" y="{ty}" fill="{color}" font-size="11">'
                     f'{"✓" if ok else "✗"} {_esc(g["name"])}</text>')
        ty += 16
        if not ok and g["detail"]:
            for chunk in _wrap(g["detail"], 26)[:3]:
                parts.append(f'<text x="{x+18}" y="{ty}" font-size="9" fill="#922b21">'
                             f'{_esc(chunk)}</text>')
                ty += 12
    ty += 6
    cls = result["classification"]
    parts.append(f'<text x="{x+10}" y="{ty}" font-weight="bold">'
                 f'分类：{_esc(CLASSIFICATION_CN[cls])}（{cls}）</text>')
    ty += 18
    d = result["decision"]
    color = "#1e8449" if d["can_end"] else "#922b21"
    parts.append(f'<text x="{x+10}" y="{ty}" font-weight="bold" fill="{color}">'
                 f'决定：{_esc(d["name"])}</text>')
    ty += 16
    for chunk in _wrap(d["reason"], 26)[:5]:
        parts.append(f'<text x="{x+10}" y="{ty}" font-size="9" fill="#555">'
                     f'{_esc(chunk)}</text>')
        ty += 12


def _wrap(text: str, width: int) -> List[str]:
    return [text[i:i + width] for i in range(0, len(text), width)] or [""]

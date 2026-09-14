# 壁画盐害来源复核 API（`fresco_salt`）

仅使用 Python 标准库（`http.server` / `json` / `sqlite3`，及 `datetime`、`hashlib` 等）实现的
古建筑壁画盐害来源复核服务。把**地基返潮（毛细上升）**、**屋面/墙体渗漏**、
**水泥修补层材料释放**表示为候选迁移路径，用离子当量闭合、浓度梯度与干湿循环响应作证据，
输出可被**排除 / 保留 / 无法区分**的解释和下一处补样建议。

> 设计红线：层位错绑、坐标越界、电荷账不闭合、检测限缺失、时序断档（雨后间隔与干湿阶段未对齐）
> 或候选并列时，**绝不输出唯一盐源**。

## 运行

```bash
python3 -m fresco_salt.app --host 127.0.0.1 --port 8000 --db fresco.db
python3 -m fresco_salt.demo           # 端到端演示（内存库，不落盘）
python3 -m unittest discover -s tests # 25 个单元 + HTTP 测试
```

## 单位统一

| 数据基准 | 统一单位 | 接受的输入单位 |
| --- | --- | --- |
| 固相干土/地杖 | `mmol/kg_dry` | `mmol/kg`, `meq/kg`, `mg/kg`, `ug/kg` |
| 孔隙水/浸出液 | `mmol/L` | `mmol/L`, `mM`, `meq/L`, `mg/L`, `ppm`, `ug/L` |

* 质量浓度按摩尔质量换算、`meq` 按离子电荷换算；**固相/液相基准不可混用**（否则 `basis` 门控失败）。
* 未检出离子（`value` 为 `null` 或低于检出限）**必须给 `lod`**，按 `LOD/2` 代用并标记 `censored`；
  代用离子占比超过 `censored_max_fraction` 时电荷账判为不可信。

电荷当量闭合：

```
CBE = (Σ 阳离子当量 − Σ 阴离子当量) / ((Σ+ + Σ−) / 2) × 100%
```

默认 `|CBE| ≤ 5%` 才闭合。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/walls` | 建立墙体分层模型（层位必须连续、不越墙厚） |
| GET | `/walls` / `/walls/{id}` | 列表 / 墙体与各类记录计数 |
| POST | `/walls/{id}/samples` | 提交样本（数组） |
| POST | `/walls/{id}/rainfall` | 降雨记录（`ts`, `rain_mm`） |
| POST | `/walls/{id}/repairs` | 修缮记录（`date`, `material`，可带层位/坐标） |
| GET/POST | `/walls/{id}/analysis` | 复核（按深度/时段的当量闭合、梯度、干湿响应、候选裁决、补样建议） |
| GET/POST | `/walls/{id}/revisions` | 查询修订账 / 提交修订 |
| POST | `/walls/{id}/lock` | **封版**：冻结墙体模型、样本与参数 |
| GET | `/walls/{id}/versions` | 封版版本列表 |
| GET | `/versions/{vid}` / `…/recompute.json` / `…/profile.svg` | 版本元数据 / 复算 JSON / 盐分剖面 SVG |

### 样本结构

```json
{
  "sample_id": "low3d",
  "layer_id": "L1_pigment",
  "x_mm": 500, "y_mm": 200, "z_mm": 3, "depth_mm": 3,
  "ts": "2026-06-01T09:00:00",
  "moisture_wt": 5.5, "temp_c": 22.0, "rh_percent": 60,
  "ions": {
    "Na+":  {"value": 40, "unit": "mmol/kg", "lod": 0.5},
    "Cl-":  {"value": 40, "unit": "mmol/kg", "lod": 0.5},
    "Ca2+": {"value": 8,  "unit": "mmol/kg", "lod": 0.5},
    "SO42-":{"value": 8,  "unit": "mmol/kg", "lod": 0.5}
  }
}
```

`stage`（`wet/dry/transition`）可省略：默认结合降雨记录推断——雨后 `wet_window_hours`（默认 48h）
内为湿阶段，`dry_window_hours`（默认 96h）后为干阶段，首场有效降雨之前达到 dry_window 也视为
干季基线；申报值与推断不一致时保留备注并采用申报值。

## 门控（全部通过且仅一条候选保留，才输出唯一盐源）

| code | 名称 | 失败来源 |
| --- | --- | --- |
| `layer_bind` | 层位绑定 | 深度不在所绑层位区间，或层位不在模型中 |
| `coord` | 三维坐标 | x/y/z 越出墙体宽/高/厚 |
| `lod` | 检出限完整性 | 有离子缺检出限 |
| `charge` | 电荷当量闭合 | `|CBE|` 超阈、缺极性离子或半检出限代用比例过高 |
| `basis` | 基准一致 | 同批固/液相浓度混用 |
| `time` | 时序与干湿覆盖 | 缺湿阶段、缺干季基线、缺同孔位干湿配对或无降雨记录 |
| `tie` | 候选并列 | ≥2 条候选保留（`tie_eps` 区分“势均力敌”与“主次并存”） |

## 修订与封版

所有更正都必须**留理由**，写进 SQLite 修订账并生成新修订（自增 `seq` + `rev_id`）：

* `correct_binding`：`{sample_id, new_layer_id}` —— 更正层位错绑；
* `exclude_sample`：`{sample_id}` —— 剔除污染样本（原始数据仍保留，复算中给出剔除理由）；
* `restore_sample`：恢复被剔除的样本；
* `adopt_conservative`：采纳保守解释，之后即使证据倾向单一来源也不输出唯一盐源。

封版（`/lock`）冻结墙体模型、样本、参数与全部结果，生成：

* `recompute.json` —— 冻结输入、生效参数、离子登记、`input_hash` / `params_hash` /
  `recompute_hash`；用 `fresco_salt.recompute.verify()` 可离线重跑复核并比对哈希；
* `profile.svg` —— 三面板：离子-深度剖面（层位带背景、干/湿实线虚线、空心点为半检出限代用）、
  高度带与雨后脉冲、候选证据裁决。

封版后任何写操作返回 `409`；需要修订时应新建墙体版本。

## 模块布局

```
fresco_salt/
  chemistry.py   离子登记、单位统一、电荷当量闭合
  analysis.py    门控、梯度/干湿指标、候选证据打分、裁决、补样建议（纯函数）
  svg.py         盐分剖面 SVG
  recompute.py   封版复算 JSON 与哈希校验
  db.py          SQLite 持久化（walls/samples/rains/repairs/revisions/versions）
  app.py         http.server 路由、录入校验、服务装配与 CLI
  demo.py        端到端演示
tests/           核心逻辑测试 + HTTP 端到端测试
```

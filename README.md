# 壁画盐害来源复核 API（`fresco_salt`）

仅使用 Python 标准库（`http.server` / `json` / `sqlite3`，及 `datetime`、`hashlib` 等）实现的
古建筑壁画盐害来源复核服务。把**地基返潮（毛细上升）**、**屋面/墙体渗漏**、
**水泥修补层材料释放**表示为候选迁移路径，用离子当量闭合、浓度梯度与干湿循环响应作证据，
输出可被**排除 / 保留 / 无法区分**的解释和下一处补样建议。

1.1 起接入**敷贴脱盐试验**：脱盐敷贴揭下后表面电导下降，不等于盐已离墙——盐可能只从
颜料层退到地仗深处，下一次受潮又返回表面。试验把每轮浸出液带走的离子量、处理区分层
库存变化与邻近对照区漂移放在同一本账上核算，区分**净移除 / 向内迁移 / 证据不足**；
试验修订在 SQLite 中独立建账，确认版固定引用的来源复核封版、轮次与决定。

> 设计红线：层位错绑、坐标越界、电荷账不闭合、检测限缺失、时序断档（雨后间隔与干湿阶段未对齐）
> 或候选并列时，**绝不输出唯一盐源**。
>
> 敷贴试验红线：样本无法配对、轮次重叠、固液基准混用、浸出液缺检测限、收支不闭合
> 或深层浓度升高时，**绝不建议结束处理**。

## 运行

```bash
python3 -m fresco_salt.app --host 127.0.0.1 --port 8000 --db fresco.db
python3 -m fresco_salt.demo           # 端到端演示（内存库，不落盘）
python3 -m unittest discover -s tests # 43 个单元 + HTTP 测试
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
| POST | `/walls/{id}/trials` | 建立敷贴脱盐试验（须引用本墙已封版的来源复核版本） |
| GET | `/walls/{id}/trials` | 试验列表 |
| GET | `/trials/{id}` | 试验定义、轮次与计数 |
| POST | `/trials/{id}/rounds` | 登记敷贴轮次（覆盖时段 + 浸出液体积与离子浓度） |
| GET/POST | `/trials/{id}/analysis` | 逐轮核算（移出量、库存变化、对照漂移、收支、分类与决定） |
| GET/POST | `/trials/{id}/revisions` | 查询试验修订账（独立保存）/ 提交试验修订 |
| POST | `/trials/{id}/confirm` | **确认版**：固定来源、轮次与决定，试验转只读 |
| GET | `/trials/{id}/versions` | 确认版列表 |
| GET | `/trial-versions/{vid}` / `…/rounds.json` / `…/balance.svg` | 确认版元数据 / 逐轮 JSON / 离子收支 SVG |

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
干季基线；落在两窗口之间（雨后 48–96h）或与任何有效降雨都无法对齐时判为 `transition`（时序断档）。
**申报阶段只用于对账，不覆盖推断结果**：申报值与推断冲突（如雨后 3850h 申报 wet）时保留冲突说明、
样本按推断阶段入账，`time` 门控失败并禁止输出唯一盐源，同时给出该孔位湿/干窗口重采建议。

## 门控（全部通过且仅一条候选保留，才输出唯一盐源）

| code | 名称 | 失败来源 |
| --- | --- | --- |
| `layer_bind` | 层位绑定 | 深度不在所绑层位区间，或层位不在模型中 |
| `coord` | 三维坐标 | x/y/z 越出墙体宽/高/厚 |
| `lod` | 检出限完整性 | 有离子缺检出限 |
| `charge` | 电荷当量闭合 | `|CBE|` 超阈、缺极性离子或半检出限代用比例过高 |
| `basis` | 基准一致 | 同批固/液相浓度混用 |
| `time` | 时序与干湿覆盖 | 缺湿阶段、缺干季基线、缺同孔位干湿配对、无降雨记录、**申报阶段与降雨推断冲突**，或样本落在雨后过渡窗口（时序断档） |
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

## 敷贴脱盐试验

修复师在来源复核封版之后建立试验（`POST /walls/{id}/trials`），一次登记：

```json
{
  "name": "北壁东段纤维素敷贴脱盐试验",
  "source_version_id": "v_5aba3048",
  "treatment_zone": {"x_mm": 500, "y_mm": 200, "radius_mm": 300},
  "control_zone":   {"x_mm": 1500, "y_mm": 200, "radius_mm": 300},
  "layers":         [{"layer_id": "L1_pigment", "volume_m3": 0.0005, "dry_density_kg_m3": 1200}],
  "control_layers": [{"layer_id": "L1_pigment", "volume_m3": 0.0005, "dry_density_kg_m3": 1200}],
  "poultice": {"material": "纤维素纸浆", "water_content_percent": 45, "contact_area_m2": 0.28},
  "bindings": [{"sample_id": "tpre3", "zone": "treatment", "phase": "pre"}],
  "env_rain_ids": ["rain_0609"]
}
```

* **处理区 / 对照区**：圆域（圆心 + 半径），两区邻近但不得重叠；对照区提供自然漂移基线。
* **层登记**：处理区与对照区分别登记各层 `volume_m3` 与 `dry_density_kg_m3`，
  层干质量 = 体积 × 干密度，库存 = 层平均浓度(mmol/kg) × 层干质量(kg)。
* **绑定**：`bindings` 引用墙体样本，须覆盖 处理区/对照区 × 处理前/处理后 四类；
  同位置分层配对 = 同区 + 同层位 + x/y 归并（`column_xy_bin_mm`）+ 深度容差（`depth_match_mm`）。
* **环境记录**：`env_rain_ids` 绑定降雨记录，随确认版冻结。
* **轮次**（`POST /trials/{id}/rounds`）：每轮登记覆盖时段（起止不得颠倒）与浸出液
  `volume_l` + 离子浓度（液相单位）；移出量 = 浓度 × 体积，逐轮累计。

### 核算与判定

* 逐轮核算各离子移出量（mmol）；处理区/对照区前后库存变化；对照漂移比（后/前）。
* 漂移校正墙内净减 = 处理前库存 × 对照漂移比 − 处理后库存；
  **收支闭合**：|累计浸出 − 净减| ≤ `balance_tolerance`（默认 25%）× 收支较大侧。
* 深层（≥`deep_layer_min_depth_mm`）浓度后/前 ≥ `deep_rise_ratio`（默认 1.10）且超过
  对照漂移 → 判**向内迁移**（盐退到地杖深处，受潮后可能返表）。
* 分类：`net_removal` 净移除 / `inward_migration` 向内迁移 / `insufficient_evidence` 证据不足。
* 决定：仅当六道门控全过、判为净移除、且处理区残盐 ≤ 对照区 × `end_excess_ratio`
  （默认 1.10）时才 `end_treatment`；净移除但残盐仍高 → `continue_treatment`；
  其余一律不得结束。

### 试验门控（任一失败即不得建议结束处理）

| code | 名称 | 失败来源 |
| --- | --- | --- |
| `pairing` | 前后样本配对 | 处理前/后样本无法同位置同层配对、绑定区与坐标不符、层位未登记 |
| `rounds` | 轮次覆盖时段 | 轮次覆盖时段重叠，或无有效浸出轮次 |
| `basis` | 固/液相基准 | 浸出液用固相单位，或绑定了液相墙样，固液混账 |
| `lod` | 浸出液检测限 | 浸出液离子缺检测限，或半检出限代用比例过高 |
| `balance` | 离子收支闭合 | 累计浸出与漂移校正净减的残差超容差 |
| `deep_rise` | 深层浓度变化 | 深层浓度升高超对照漂移（或深层无配对无法排除迁移） |

### 试验修订与确认版

试验修订**独立保存**于 `trial_revisions` 表（与墙体修订账分开），同样须留理由：

* `exclude_extract`：`{round_id}` —— 剔除污染浸出液（原始数据保留，核算中给出理由）；
* `restore_extract`：恢复被剔除的轮次；
* `rebind_sample`：`{sample_id, zone?, phase?}` —— 改动配对归属；
* `unbind_sample`：`{sample_id}` —— 解除绑定。

确认版（`POST /trials/{id}/confirm`）冻结试验定义、轮次、试验修订账、绑定样本与
环境记录快照，并**固定引用的来源复核封版**（`source_version_id` + 其 `recompute_hash`）、
轮次核算与决定，生成：

* `rounds.json` —— 逐轮核算与收支全量 JSON，含 `input_hash` / `params_hash` /
  `confirm_hash`；用 `fresco_salt.poultice_recompute.verify()` 可离线复算比对；
* `balance.svg` —— 三面板：分离子收支（处理前/处理后库存 vs 累计浸出，标注对照漂移）、
  逐轮浸出与累计曲线（剔除轮次灰色标记）、门控与决定。

确认版后试验任何写操作返回 `409`；需要变更时应新建试验。

## 模块布局

```
fresco_salt/
  chemistry.py          离子登记、单位统一、电荷当量闭合
  analysis.py           门控、梯度/干湿指标、候选证据打分、裁决、补样建议（纯函数）
  svg.py                盐分剖面 SVG
  recompute.py          封版复算 JSON 与哈希校验
  poultice.py           敷贴试验核算：配对、逐轮浸出、库存/漂移、收支、深层、判定（纯函数）
  poultice_svg.py       离子收支 SVG
  poultice_recompute.py 试验确认版 JSON 与哈希校验
  db.py                 SQLite 持久化（walls/samples/rains/repairs/revisions/versions
                        + trials/trial_rounds/trial_revisions/trial_versions）
  app.py                http.server 路由、录入校验、服务装配与 CLI
  demo.py               端到端演示（含敷贴试验场景）
tests/                  核心逻辑测试 + HTTP 端到端测试 + 敷贴试验测试
```

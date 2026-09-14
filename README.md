# 壁画盐害来源复核 API（`fresco_salt`）

仅使用 Python 标准库（`http.server` / `json` / `sqlite3`，及 `datetime`、`hashlib` 等）实现的
古建筑壁画盐害来源复核服务。把**地基返潮（毛细上升）**、**屋面/墙体渗漏**、
**水泥修补层材料释放**表示为候选迁移路径，用离子当量闭合、浓度梯度与干湿循环响应作证据，
输出可被**排除 / 保留 / 无法区分**的解释和下一处补样建议。

1.1 起接入**敷贴脱盐试验**：脱盐敷贴揭下后表面电导下降，不等于盐已离墙——盐可能只从
颜料层退到地仗深处，下一次受潮又返回表面。试验把每轮浸出液带走的离子量、处理区分层
库存变化与邻近对照区漂移放在同一本账上核算，区分**净移除 / 向内迁移 / 证据不足**；
试验修订在 SQLite 中独立建账，确认版固定引用的来源复核封版、轮次与决定。

1.2 起接入**微气候结晶循环**：脱盐刚完成时库房平均湿度看似平稳，墙角传感器却可能在
昼夜波动中反复越过某类盐的潮解临界（DRH）与析晶临界（CRH）。本模块按墙面分区登记
温湿度传感器、校准记录与采样时序，按盐类规则（潮解/析晶 RH、温度适用范围、最短持续
时间、滞回带）扫描“跨阈—停留—回返”，统计每区**完整循环数、最长湿润段与首个风险
时刻**；校准失效、时序断档、单位冲突、规则温区不足、映射多解或来源版仍无结论时，
对应分区保持**待判**并列出缺口。

> 设计红线：层位错绑、坐标越界、电荷账不闭合、检测限缺失、时序断档（雨后间隔与干湿阶段未对齐）
> 或候选并列时，**绝不输出唯一盐源**。
>
> 敷贴试验红线：样本无法配对、轮次重叠、固液基准混用、浸出液缺检测限、收支不闭合
> 或深层浓度升高时，**绝不建议结束处理**。
>
> 微气候红线：来源版无结论、传感器映射多解/缺失、校准失效、时序断档、单位冲突或
> 规则温区覆盖不足时，对应分区**绝不输出循环结论，只列待判缺口**。

## 运行

```bash
python3 -m fresco_salt.app --host 127.0.0.1 --port 8000 --db fresco.db
python3 -m fresco_salt.demo           # 端到端演示（内存库，不落盘）
python3 -m unittest discover -s tests # 74 个单元 + HTTP 测试
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
| POST | `/walls/{id}/monitors` | 建立微气候监测计划（分区/传感器/校准/盐类规则，须引用已封版来源） |
| GET | `/walls/{id}/monitors` | 监测计划列表 |
| GET | `/monitors/{id}` | 监测计划定义与读数/修订计数 |
| POST | `/monitors/{id}/readings` | 登记温湿度读数（数组，支持 ℃/℉/K、%/小数） |
| GET/POST | `/monitors/{id}/analysis` | 逐区循环分析（跨阈、停留、回返、完整循环、最长湿润段、首风险时刻） |
| GET/POST | `/monitors/{id}/revisions` | 查询微气候修订账（独立保存）/ 改绑、采用序列、保守规则修订 |
| POST | `/monitors/{id}/confirm` | **确认版**：冻结来源、规则与采用序列，监测转只读 |
| GET | `/monitors/{id}/versions` | 确认版列表 |
| GET | `/microclimate-versions/{vid}` / `…/zones.json` / `…/risk.svg` | 确认版元数据 / 逐区 JSON / 风险曲线 SVG |

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
  **收支闭合**：每种离子分项残差与总量残差均 ≤ `balance_tolerance`（默认 25%）×
  该侧较大值——任一离子分项不闭合即判不闭合，防止正/负分项残差相互抵消。
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
| `balance` | 离子收支闭合 | 任一离子分项残差或总量残差超容差（分项可相互抵消不视为闭合） |
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

## 微气候盐结晶循环

脱盐完成后，修复师按墙面分区建立监测计划（`POST /walls/{id}/monitors`），引用**已封版
来源**：`source_kind=review`（默认，来源复核封版，须 `unique_source`）或 `trial`
（敷贴试验确认版，须分类 `net_removal`）；来源仍无结论时全部循环统计保持待判。

```json
{
  "name": "脱盐后微气候监测",
  "source_version_id": "v_5aba3048",
  "zones": [
    {"zone_id": "z_corner", "name": "西北角", "x_mm": 0, "y_mm": 0,
     "width_mm": 900, "height_mm": 1200}
  ],
  "sensors": [
    {"sensor_id": "T_corner", "x_mm": 450, "y_mm": 600,
     "calibrations": [
       {"calibrated_ts": "2026-07-15T00:00:00",
        "valid_until_ts": "2027-07-15T00:00:00",
        "rh_offset_pct": 0.4, "temp_offset_c": -0.1}]}
  ],
  "rules": [
    {"rule_id": "nacl", "salt": "NaCl", "cn": "氯化钠",
     "drh_percent": 75.3, "crh_percent": 70.0,
     "temp_min_c": 10.0, "temp_max_c": 30.0,
     "min_wet_hours": 6.0, "min_dry_hours": 6.0}
  ],
  "zone_salts": [{"zone_id": "z_corner", "rule_id": "nacl"}]
}
```

* **分区**：矩形（x/y/宽高）或圆形（x/y/radius_mm），不得越出墙面；不提供 `zone_salts`
  时全部规则逐区评估。
* **传感器映射**：默认按坐标几何落区；落在分区重叠区构成**映射多解**，一区多条序列
  构成**多序列**，均待判。建账时可用 `zone_id` 显式声明；改绑到几何范围之外须在理由中
  说明现场布设依据。
* **校准**：每只传感器至少一条校准记录；读数时刻取 `calibrated_ts ≤ ts` 的最新一条，
  超过 `valid_until_ts`（或无任何校准）即**校准失效**；`rh_offset_pct`/`temp_offset_c`
  在扫描前施加。
* **读数**：`POST /monitors/{id}/readings`，每条 `{sensor_id, ts, temp, rh}`；
  温度支持 `C/F/K`（`temp_unit`，默认 `C`），湿度支持 `%`/`fraction`（`rh_unit`，
  默认 `%`）。同序列单位混录判**单位冲突**；`temp`/`rh` 可为 `null`（缺测）。
  相邻读数间隔超过 `gap_max_hours`（默认 6h）判**时序断档**，断档两侧湿润段不拼接；
  重复时间戳同样拦截。
* **盐类规则**：`crh_percent < drh_percent` 是硬约束（滞回带）。

### 循环状态机（含滞回）

对每条有效序列 × 每条适用规则：

1. RH 向上插值越过 **DRH** → 进入湿润；只有向下越过更低的 **CRH** 才回到干燥；
   RH 在 `[CRH, DRH]` 滞回带内波动**保持原态**，不伪造析晶；
2. 湿润停留 `< min_wet_hours` 即回落，只登记为**短时回返**（`short_returns`），
   不计循环；干燥停留 `< min_dry_hours` 又再湿润，循环不完整；
3. 断档/缺测/校准失效/温度越出规则温区处切段；断档时仍湿润记为**开口湿润段**
   （`open_wet`），不跨段拼接；
4. 逐区汇总每条规则的**完整循环数、最长湿润段（小时）、首个风险时刻**（第一次合格
   湿润跨阈时刻）与跨阈事件；多规则按盐类分别给数，区级取最早首风险。

风险分级：开口湿润 → `current_wet`；完整循环 ≥3（`risk_cycles_high`）→ `high`；
≥1 → `moderate`；否则 `low`。

### 待判门控（六类缺口）

| code | 名称 | 触发 |
| --- | --- | --- |
| `source` | 来源版结论 | 引用的复核封版非 `unique_source`，或敷贴确认版非 `net_removal` |
| `mapping` | 传感器—样本区映射 | 无传感器、几何跨区多解、一区多序列且未指定采用序列 |
| `calibration` | 校准有效性 | 读数落在校准有效期之外（含无校准记录） |
| `time` | 采样时序 | 断档、缺测、重复时间戳、有效点不足 |
| `units` | 单位一致 | 同序列 ℃/℉/K 或 %/fraction 混录 |
| `rule_range` | 规则温度范围 | 读数温度超出规则 `temp_min/max_c` |

任一门控失败，分区 `status=pending`、`risk_level=unknown`，不输出循环结论，
`gaps` 列出可操作的缺口说明；分析同时给出逐缺口的补测/改绑建议。

### 微气候修订（独立修订账，留理由派生修订）

* `rebind_sensor`：`{sensor_id, zone_id}` —— 改绑分区（几何外改绑须说明现场理由）；
* `unbind_sensor`：`{sensor_id}` —— 解除显式归属，回到几何落区；
* `adopt_sensor`：`{zone_id, sensor_id}` —— 多解/多序列时指定采用序列；
* `adopt_conservative_rule`：`{zone_id, rule_id, ...阈值覆盖}` —— 从已登记规则
  **派生保守规则**（id 后缀 `_conservative`）：DRH 只许下调、CRH 只许下调（滞回带
  只许加宽）、温区只许放宽、最短湿润时长只许缩短；任何收紧方向被拒绝。

### 确认版

`POST /monitors/{id}/confirm` 冻结分区/传感器/校准/规则目录、全部读数、微气候修订账
（含改绑与派生保守规则）与引用来源（版本号 + 内容哈希 + 结论），生成：

* `zones.json` —— 逐区门控、跨阈事件、完整/不完整循环、短时回返、开口湿润、最长
  湿润段、首风险时刻与建议全量 JSON，含 `input_hash` / `params_hash` /
  `confirm_hash`；用 `fresco_salt.microclimate_recompute.verify()` 离线复算比对；
* `risk.svg` —— 三面板：逐区 RH 曲线叠 DRH/CRH 阈值（断点断开、待判灰色）、
  逐区循环/湿润段统计条、门控缺口列表。

确认版后监测计划任何写操作返回 `409`；需要变更时应新建监测计划。

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
  microclimate.py       微气候循环：映射、校准/单位/时序核查、潮解—析晶滞回状态机（纯函数）
  microclimate_svg.py   风险曲线 SVG
  microclimate_recompute.py 微气候确认版 JSON 与哈希校验
  db.py                 SQLite 持久化（walls/samples/rains/repairs/revisions/versions
                        + trials/trial_rounds/trial_revisions/trial_versions
                        + monitors/monitor_readings/monitor_revisions/monitor_versions）
  app.py                http.server 路由、录入校验、服务装配与 CLI
  demo.py               端到端演示（含敷贴试验与微气候循环场景）
tests/                  核心逻辑测试 + HTTP 端到端测试 + 敷贴/微气候测试
```

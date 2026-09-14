"""敷贴脱盐试验测试：逐轮收支、六道门控、分类判定、试验修订与确认版。"""
import copy
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from fresco_salt.app import Handler, SaltApiApp
from fresco_salt.db import Store
from fresco_salt.poultice import analyze_trial
from fresco_salt.poultice_recompute import build_confirm, verify

WALL = {
    "wall_id": "w_p", "name": "敷贴测试墙",
    "width_mm": 3000, "height_mm": 4000, "thickness_mm": 80,
    "layers": [
        {"layer_id": "L1", "d_start_mm": 0, "d_end_mm": 5},
        {"layer_id": "L2", "d_start_mm": 5, "d_end_mm": 40},
        {"layer_id": "L3", "d_start_mm": 40, "d_end_mm": 80},
    ],
}
RAINS = [{"rain_id": "r1", "ts": "2026-06-09T02:00:00", "rain_mm": 18.0}]
LAYERS_REG = [
    {"layer_id": "L1", "volume_m3": 0.0005, "dry_density_kg_m3": 1200},  # 0.6 kg
    {"layer_id": "L2", "volume_m3": 0.0035, "dry_density_kg_m3": 1500},  # 5.25 kg
    {"layer_id": "L3", "volume_m3": 0.004, "dry_density_kg_m3": 1600},   # 6.4 kg
]
SRC_VERSION = {"version_id": "v_src",
               "recompute": {"recompute_hash": "abc123",
                             "result": {"verdict": "unique_source",
                                        "unique_source": "rising",
                                        "unique_source_name": "地基返潮（毛细上升）"}}}


def ion4(na, ca, unit="mmol/kg", lod=0.5):
    return {"Na+": {"value": na, "unit": unit, "lod": lod},
            "Cl-": {"value": na, "unit": unit, "lod": lod},
            "Ca2+": {"value": ca, "unit": unit, "lod": lod},
            "SO42-": {"value": ca, "unit": unit, "lod": lod}}


def ss(sid, x, y, d, na, ca, ts="2026-06-01T09:00:00", moisture=5.0):
    layer = "L1" if d <= 5 else "L2" if d <= 40 else "L3"
    return {"sample_id": sid, "layer_id": layer, "x_mm": x, "y_mm": y, "z_mm": d,
            "depth_mm": d, "ts": ts, "moisture_wt": moisture,
            "temp_c": 22, "rh_percent": 60, "ions": ion4(na, ca)}


def bound_samples():
    """处理区(500,200)：处理后各层净减且深层不升；对照区(1500,200)：漂移×1.05。"""
    return [
        ss("tpre3", 500, 200, 3, 80, 20), ss("tpre20", 500, 200, 20, 60, 15),
        ss("tpre55", 500, 200, 55, 40, 10),
        ss("tpost3", 500, 200, 3, 24, 6, "2026-08-20T09:00:00"),
        ss("tpost20", 500, 200, 20, 30, 8, "2026-08-20T09:00:00"),
        ss("tpost55", 500, 200, 55, 36, 9, "2026-08-20T09:00:00"),
        ss("cpre3", 1500, 200, 3, 30, 8), ss("cpre20", 1500, 200, 20, 25, 6),
        ss("cpre55", 1500, 200, 55, 20, 5),
        ss("cpost3", 1500, 200, 3, 31.5, 8.4, "2026-08-20T09:00:00"),
        ss("cpost20", 1500, 200, 20, 26.25, 6.3, "2026-08-20T09:00:00"),
        ss("cpost55", 1500, 200, 55, 21, 5.25, "2026-08-20T09:00:00"),
    ]


def bindings():
    out = []
    for z, prefix in (("treatment", "t"), ("control", "c")):
        for ph in ("pre", "post"):
            for d in (3, 20, 55):
                out.append({"sample_id": f"{prefix}{ph}{d}", "zone": z, "phase": ph})
    return out


def trial(**over):
    t = {"trial_id": "t_1", "wall_id": "w_p", "name": "北壁东段敷贴脱盐试验",
         "source_version_id": "v_src",
         "treatment_zone": {"x_mm": 500, "y_mm": 200, "radius_mm": 300},
         "control_zone": {"x_mm": 1500, "y_mm": 200, "radius_mm": 300},
         "layers": [dict(x) for x in LAYERS_REG],
         "control_layers": [dict(x) for x in LAYERS_REG],
         "poultice": {"material": "纤维素纸浆", "water_content_percent": 45,
                      "contact_area_m2": 0.28},
         "bindings": bindings(),
         "env_rain_ids": ["r1"]}
    t.update(over)
    return t


def rnd(rid, start, end, na=160, ca=40, vol=0.5, unit="mmol/L", drop_na_lod=False):
    ions = ion4(na, ca, unit=unit, lod=0.2)
    if drop_na_lod:
        ions["Na+"].pop("lod")
    return {"round_id": rid, "cover_start_ts": start, "cover_end_ts": end,
            "extract": {"volume_l": vol, "ions": ions}}


def rounds3():
    return [rnd("R1", "2026-07-01T09:00:00", "2026-07-03T09:00:00"),
            rnd("R2", "2026-07-08T09:00:00", "2026-07-10T09:00:00"),
            rnd("R3", "2026-07-15T09:00:00", "2026-07-17T09:00:00")]


def trev(seq, kind, payload, reason="污染/错绑更正（双人复核）"):
    return {"seq": seq, "rev_id": f"tr{seq}", "kind": kind, "reason": reason,
            "payload": payload, "actor": "tester", "created_ts": "2026-07-25T00:00:00"}


def gates_map(result):
    return {g["code"]: g for g in result["gates"]}


def run(rounds=None, revisions=None, smpl=None, tr=None):
    return analyze_trial(WALL, tr or trial(), rounds if rounds is not None else rounds3(),
                         revisions or [], smpl or bound_samples(), RAINS)


class PoulticeAnalysisTests(unittest.TestCase):
    def test_net_removal_continue(self):
        """收支闭合、深层未升 → 净移除；残盐仍高于对照 → 继续处理，不得结束。"""
        r = run()
        self.assertTrue(r["all_gates_passed"],
                        [g for g in r["gates"] if not g["passed"]])
        self.assertEqual(r["classification"], "net_removal")
        self.assertEqual(r["decision"]["code"], "continue_treatment")
        self.assertFalse(r["decision"]["can_end"])
        # 逐轮核算：3 轮 × 0.5L × (160+160+40+40) mmol/L = 600 mmol
        self.assertAlmostEqual(r["cumulative_removed_total_mmol"], 600.0, places=3)
        self.assertAlmostEqual(r["cumulative_removed_mmol"]["Na+"], 240.0, places=3)
        # 库存：处理区 1547.5 → 1011 mmol；对照漂移 ×1.05
        self.assertAlmostEqual(r["inventory"]["treatment"]["pre_total_mmol"],
                               1547.5, places=2)
        self.assertAlmostEqual(r["inventory"]["treatment"]["post_total_mmol"],
                               1011.0, places=2)
        self.assertAlmostEqual(r["inventory"]["control_drift_ratio"]["Na+"],
                               1.05, places=3)
        # 收支：漂移校正净减 613.875 mmol，残差 -13.875 在 25% 容差内
        self.assertAlmostEqual(r["balance"]["total"]["loss_corr_mmol"],
                               613.875, places=2)
        self.assertTrue(r["balance"]["closed"])
        self.assertTrue(r["balance"]["total"]["closed"])
        self.assertTrue(r["balance"]["total_closed"])
        self.assertEqual(r["balance"]["open_ions"], [])
        # 深层 L3：100 → 90，未升高
        deep = {d["layer_id"]: d for d in r["deep_layers"]}
        self.assertIn("L3", deep)
        self.assertFalse(deep["L3"]["rise"])
        self.assertEqual(r["pairs"]["treatment"], 3)
        self.assertEqual(r["pairs"]["control"], 3)

    def test_can_end_when_residual_near_control(self):
        """放宽残盐阈值（试验参数覆盖）→ 可建议结束。"""
        r = run(tr=trial(params={"end_excess_ratio": 1.5}))
        self.assertEqual(r["classification"], "net_removal")
        self.assertEqual(r["decision"]["code"], "end_treatment")
        self.assertTrue(r["decision"]["can_end"])

    def test_unpairable_samples_block_end(self):
        """处理后样本缺一层 → 配对门控失败，证据不足，不得结束。"""
        tr = trial()
        tr["bindings"] = [b for b in tr["bindings"]
                          if b["sample_id"] != "tpost55"]
        r = run(tr=tr)
        g = gates_map(r)
        self.assertFalse(g["pairing"]["passed"])
        self.assertIn("tpre55", json.dumps(r["pairs"]["unpaired"], ensure_ascii=False))
        self.assertEqual(r["classification"], "insufficient_evidence")
        self.assertFalse(r["decision"]["can_end"])

    def test_round_overlap_blocks_end(self):
        rr = rounds3()
        rr[1]["cover_start_ts"] = "2026-07-02T09:00:00"  # 与 R1 重叠
        r = run(rounds=rr)
        g = gates_map(r)
        self.assertFalse(g["rounds"]["passed"])
        self.assertIn("重叠", g["rounds"]["detail"])
        self.assertFalse(r["decision"]["can_end"])

    def test_no_rounds_blocks_end(self):
        r = run(rounds=[])
        self.assertFalse(gates_map(r)["rounds"]["passed"])
        self.assertEqual(r["classification"], "insufficient_evidence")

    def test_solid_extract_basis_blocks_end(self):
        """浸出液误用固相单位 → 固液基准混用门控失败。"""
        rr = rounds3()
        rr[0]["extract"]["ions"] = ion4(160, 40, unit="mmol/kg", lod=0.2)
        r = run(rounds=rr)
        g = gates_map(r)
        self.assertFalse(g["basis"]["passed"])
        self.assertIn("固相", g["basis"]["detail"])
        self.assertFalse(r["decision"]["can_end"])

    def test_extract_missing_lod_blocks_end(self):
        rr = rounds3()
        rr[0]["extract"]["ions"]["Na+"].pop("lod")
        r = run(rounds=rr)
        g = gates_map(r)
        self.assertFalse(g["lod"]["passed"])
        self.assertIn("Na+", g["lod"]["detail"])
        self.assertFalse(r["decision"]["can_end"])

    def test_balance_open_blocks_end(self):
        """浸出量远低于墙内净减 → 收支不闭合，证据不足。"""
        rr = [rnd("R1", "2026-07-01T09:00:00", "2026-07-03T09:00:00", na=4, ca=1)]
        r = run(rounds=rr)
        g = gates_map(r)
        self.assertFalse(g["balance"]["passed"])
        self.assertIn("不闭合", g["balance"]["detail"])
        self.assertEqual(r["classification"], "insufficient_evidence")
        self.assertFalse(r["decision"]["can_end"])

    def test_canceling_ion_residuals_block_end(self):
        """回归：Na+ 正残差与 Cl- 负残差相互抵消、总量闭合——分项不闭合仍须拦截。

        浸出 Na+ 260 / Cl- 60 mmol/L × 3 轮 × 0.5L：累计 Na+ 390、Cl- 90 mmol，
        分项残差 +142.35 / -157.65 mmol 各自超容差，但总量残差 -13.875 mmol
        与全部闭合时完全相同——旧逻辑只看总量会误判净移除并放行结束处理。
        """
        def split_round(rid, start, end):
            return {"round_id": rid, "cover_start_ts": start, "cover_end_ts": end,
                    "extract": {"volume_l": 0.5, "ions": {
                        "Na+": {"value": 260, "unit": "mmol/L", "lod": 0.2},
                        "Cl-": {"value": 60, "unit": "mmol/L", "lod": 0.2},
                        "Ca2+": {"value": 40, "unit": "mmol/L", "lod": 0.2},
                        "SO42-": {"value": 40, "unit": "mmol/L", "lod": 0.2}}}}

        rr = [split_round("R1", "2026-07-01T09:00:00", "2026-07-03T09:00:00"),
              split_round("R2", "2026-07-08T09:00:00", "2026-07-10T09:00:00"),
              split_round("R3", "2026-07-15T09:00:00", "2026-07-17T09:00:00")]
        r = run(rounds=rr)
        # 总量确实闭合（与 test_net_removal_continue 相同的 -13.875 mmol），
        # 拦截只能来自分项判定
        self.assertTrue(r["balance"]["total_closed"])
        self.assertAlmostEqual(r["balance"]["total"]["residual_mmol"],
                               -13.875, places=2)
        # Na+ / Cl- 分项残差相互抵消，各自超容差
        self.assertFalse(r["balance"]["closed"])
        self.assertEqual(set(r["balance"]["open_ions"]), {"Na+", "Cl-"})
        g = gates_map(r)
        self.assertFalse(g["balance"]["passed"])
        self.assertIn("分项", g["balance"]["detail"])
        self.assertIn("Na+", g["balance"]["detail"])
        self.assertIn("Cl-", g["balance"]["detail"])
        self.assertEqual(r["classification"], "insufficient_evidence")
        self.assertEqual(r["decision"]["code"], "hold_insufficient_evidence")
        self.assertFalse(r["decision"]["can_end"])

    def test_deep_rise_means_inward_migration(self):
        """深层 L3 浓度升高超对照漂移 → 向内迁移，不得结束（盐退到地杖深处）。"""
        smpl = bound_samples()
        for s in smpl:
            if s["sample_id"] == "tpost55":
                s["ions"] = ion4(50, 13)  # L3 总浓度 100 → 126（对照仅 ×1.05）
        r = run(smpl=smpl)
        g = gates_map(r)
        self.assertFalse(g["deep_rise"]["passed"])
        self.assertIn("L3", g["deep_rise"]["detail"])
        self.assertEqual(r["classification"], "inward_migration")
        self.assertEqual(r["decision"]["code"], "hold_inward_migration")
        self.assertFalse(r["decision"]["can_end"])

    def test_exclude_contaminated_extract_revision(self):
        """污染液撑破收支；留理由剔除后收支闭合（修订按 seq 派生）。"""
        rr = rounds3() + [rnd("R4", "2026-07-22T09:00:00", "2026-07-24T09:00:00",
                              na=900, ca=40)]
        before = run(rounds=rr)
        self.assertFalse(gates_map(before)["balance"]["passed"])
        revs = [trev(1, "exclude_extract", {"round_id": "R4"},
                     "R4 浸出液钠异常，疑敷贴材料本底污染（空白对照超标），剔除")]
        after = run(rounds=rr, revisions=revs)
        self.assertTrue(gates_map(after)["balance"]["passed"])
        self.assertEqual(after["classification"], "net_removal")
        r4 = [x for x in after["rounds"] if x["round_id"] == "R4"][0]
        self.assertTrue(r4["excluded"])
        self.assertIn("污染", r4["exclude_reason"])
        self.assertIsNone(r4["cumulative_total_mmol"])
        # 恢复剔除 → 收支再次不闭合（修订按序应用）
        revs2 = revs + [trev(2, "restore_extract", {"round_id": "R4"},
                             "空白复核为误报，恢复 R4 入账")]
        again = run(rounds=rr, revisions=revs2)
        self.assertFalse(gates_map(again)["balance"]["passed"])

    def test_rebind_sample_revision(self):
        """配对相位录错 → 配对失败；rebind 留理由更正后恢复。"""
        tr = trial()
        for b in tr["bindings"]:
            if b["sample_id"] == "tpost3":
                b["phase"] = "pre"  # 错录为处理前
        before = run(tr=tr)
        self.assertFalse(gates_map(before)["pairing"]["passed"])
        revs = [trev(1, "rebind_sample",
                     {"sample_id": "tpost3", "phase": "post"},
                     "tpost3 为处理后采样，录入相位有误，按采样记录更正")]
        after = run(tr=tr, revisions=revs)
        self.assertTrue(gates_map(after)["pairing"]["passed"])
        self.assertEqual(after["classification"], "net_removal")

    def test_unbind_sample_revision(self):
        """多绑一个无配对样本 → 配对失败；unbind 留理由解除后恢复。"""
        smpl = bound_samples() + [ss("tpostX", 550, 200, 3, 25, 6,
                                     "2026-08-20T09:00:00")]
        tr = trial()
        tr["bindings"] = tr["bindings"] + [
            {"sample_id": "tpostX", "zone": "treatment", "phase": "post"}]
        before = run(tr=tr, smpl=smpl)
        self.assertFalse(gates_map(before)["pairing"]["passed"])
        revs = [trev(1, "unbind_sample", {"sample_id": "tpostX"},
                     "tpostX 为旁路验证样，不属于本试验配对体系，解除绑定")]
        after = run(tr=tr, smpl=smpl, revisions=revs)
        self.assertTrue(gates_map(after)["pairing"]["passed"])
        self.assertEqual(after["bindings"]["n_effective"], 12)

    def test_confirm_build_and_verify(self):
        confirm = build_confirm(WALL, trial(), rounds3(), [], bound_samples(),
                                RAINS, SRC_VERSION)
        self.assertTrue(confirm["version_id"].startswith("tv_"))
        self.assertEqual(confirm["source"]["version_id"], "v_src")
        self.assertEqual(confirm["source"]["recompute_hash"], "abc123")
        self.assertEqual(confirm["result"]["classification"], "net_removal")
        self.assertTrue(verify(confirm))
        bad = copy.deepcopy(confirm)
        bad["frozen_input"]["rounds"][0]["extract"]["volume_l"] = 0.6
        self.assertFalse(verify(bad))


# ---------------------------------------------------------------- HTTP 端到端

def http(method, url, payload=None):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw)
                                 if "json" in resp.headers.get("Content-Type", "")
                                 else raw)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


HTTP_WALL = {k: v for k, v in WALL.items() if k != "wall_id"}


def wall_samples():
    """绑定样本 + 让来源复核闭环的干湿/高低带样本。"""
    out = bound_samples()
    for d in (3, 20, 55):
        out.append(ss(f"lw{d}", 500, 200, d, 40, 8, "2026-06-10T10:00:00", 10.0))
        out.append(ss(f"hd{d}", 500, 3700, d, 15, 3, "2026-06-01T11:00:00", 3.5))
        out.append(ss(f"hw{d}", 500, 3700, d, 15, 3, "2026-06-10T11:00:00", 4.0))
    return out


class PoulticeHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.httpd.app = SaltApiApp(Store(":memory:"))
        cls.port = cls.httpd.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.app.store.close()

    def setUp(self):
        st, res = http("POST", f"{self.base}/walls", HTTP_WALL)
        self.assertEqual(st, 200)
        self.wid = res["wall"]["wall_id"]
        self.assertEqual(http("POST", f"{self.base}/walls/{self.wid}/samples",
                              wall_samples())[0], 200)
        # rains 表 rain_id 全局唯一，按墙区分避免跨用例冲突
        self.rain_id = f"r_{self.wid}"
        st, res = http("POST", f"{self.base}/walls/{self.wid}/rainfall",
                       [dict(RAINS[0], rain_id=self.rain_id)])
        self.assertEqual(st, 200, res)
        st, res = http("GET", f"{self.base}/walls/{self.wid}/analysis")
        self.assertEqual(res["analysis"]["verdict"], "unique_source")
        st, locked = http("POST", f"{self.base}/walls/{self.wid}/lock",
                          {"actor": "张工"})
        self.assertEqual(st, 200)
        self.src_vid = locked["version_id"]

    def _trial_body(self, **over):
        body = trial(source_version_id=self.src_vid,
                     env_rain_ids=[self.rain_id])
        body.pop("trial_id", None)
        body.pop("wall_id", None)
        body.update(over)
        return body

    def _create_trial(self, **over):
        st, res = http("POST", f"{self.base}/walls/{self.wid}/trials",
                       self._trial_body(**over))
        self.assertEqual(st, 200, res)
        return res["trial"]["trial_id"]

    def test_trial_validation(self):
        st, res = http("POST", f"{self.base}/walls/{self.wid}/trials",
                       self._trial_body(source_version_id="v_nope"))
        self.assertEqual(st, 400)
        self.assertIn("封版", res["error"])
        st, res = http("POST", f"{self.base}/walls/{self.wid}/trials",
                       self._trial_body(control_zone={"x_mm": 600, "y_mm": 200,
                                                      "radius_mm": 300}))
        self.assertEqual(st, 400)
        self.assertIn("重叠", res["error"])
        bad = self._trial_body()
        bad["bindings"][0]["sample_id"] = "ghost"
        st, res = http("POST", f"{self.base}/walls/{self.wid}/trials", bad)
        self.assertEqual(st, 400)
        bad = self._trial_body()
        bad["layers"][0]["layer_id"] = "LX"
        st, res = http("POST", f"{self.base}/walls/{self.wid}/trials", bad)
        self.assertEqual(st, 400)
        bad = self._trial_body()
        bad["bindings"] = bad["bindings"][:6]  # 只剩处理区
        st, res = http("POST", f"{self.base}/walls/{self.wid}/trials", bad)
        self.assertEqual(st, 400)
        self.assertIn("四类", res["error"])

    def test_full_trial_flow(self):
        tid = self._create_trial()
        st, res = http("GET", f"{self.base}/walls/{self.wid}/trials")
        self.assertEqual(st, 200)
        self.assertTrue(any(t["trial_id"] == tid for t in res["trials"]))

        # 轮次：R1-R3 正常，R4 污染（钠异常高）
        rr = rounds3() + [rnd("R4", "2026-07-22T09:00:00", "2026-07-24T09:00:00",
                              na=900)]
        st, res = http("POST", f"{self.base}/trials/{tid}/rounds", rr)
        self.assertEqual(st, 200)
        self.assertEqual(len(res["round_ids"]), 4)
        # 轮次校验：起止颠倒 400
        st, res = http("POST", f"{self.base}/trials/{tid}/rounds",
                       [rnd("RX", "2026-07-05T09:00:00", "2026-07-04T09:00:00")])
        self.assertEqual(st, 400)

        # 含污染液：收支不闭合，不得结束
        st, res = http("GET", f"{self.base}/trials/{tid}/analysis")
        self.assertEqual(st, 200)
        a = res["analysis"]
        self.assertEqual(a["classification"], "insufficient_evidence")
        self.assertFalse(a["decision"]["can_end"])
        self.assertFalse({g["code"]: g for g in a["gates"]}["balance"]["passed"])

        # 无理由修订 400；留理由剔除污染液 → 净移除
        st, res = http("POST", f"{self.base}/trials/{tid}/revisions",
                       {"kind": "exclude_extract", "reason": "  ",
                        "payload": {"round_id": "R4"}})
        self.assertEqual(st, 400)
        st, res = http("POST", f"{self.base}/trials/{tid}/revisions",
                       {"kind": "exclude_extract",
                        "reason": "R4 浸出液钠异常，空白对照证实敷贴材料本底污染，剔除",
                        "payload": {"round_id": "R4"}, "actor": "李工"})
        self.assertEqual(st, 200)
        self.assertEqual(res["classification"], "net_removal")
        self.assertEqual(res["decision"]["code"], "continue_treatment")
        self.assertFalse(res["decision"]["can_end"])
        # 试验修订独立保存：墙体修订账不受影响
        st, res = http("GET", f"{self.base}/trials/{tid}/revisions")
        self.assertEqual(len(res["revisions"]), 1)
        self.assertEqual(res["revisions"][0]["kind"], "exclude_extract")
        st, res = http("GET", f"{self.base}/walls/{self.wid}/revisions")
        self.assertEqual(len(res["revisions"]), 0)

        # 确认版：固定来源、轮次与决定
        st, conf = http("POST", f"{self.base}/trials/{tid}/confirm",
                        {"actor": "张工"})
        self.assertEqual(st, 200)
        tvid = conf["version_id"]
        self.assertEqual(conf["source_version_id"], self.src_vid)
        self.assertEqual(conf["classification"], "net_removal")
        self.assertEqual(conf["decision"]["code"], "continue_treatment")

        # 产物：逐轮 JSON 可复算；收支 SVG 可下载
        st, rec = http("GET", f"{self.base}/trial-versions/{tvid}/rounds.json")
        self.assertEqual(st, 200)
        self.assertTrue(verify(rec))
        self.assertEqual(len(rec["result"]["rounds"]), 4)
        self.assertEqual(rec["source"]["version_id"], self.src_vid)
        st, svg = http("GET", f"{self.base}/trial-versions/{tvid}/balance.svg")
        self.assertEqual(st, 200)
        self.assertTrue(svg.lstrip().startswith("<svg"))
        st, meta = http("GET", f"{self.base}/trial-versions/{tvid}")
        self.assertEqual(st, 200)
        self.assertEqual(meta["version_id"], tvid)
        self.assertFalse(meta["can_end"])
        st, res = http("GET", f"{self.base}/trials/{tid}/versions")
        self.assertEqual(len(res["versions"]), 1)

        # 确认后只读
        self.assertEqual(http("POST", f"{self.base}/trials/{tid}/rounds",
                              rounds3())[0], 409)
        self.assertEqual(http("POST", f"{self.base}/trials/{tid}/revisions",
                              {"kind": "restore_extract", "reason": "x",
                               "payload": {"round_id": "R4"}})[0], 409)
        self.assertEqual(http("POST", f"{self.base}/trials/{tid}/confirm", {})[0],
                         409)
        st, res = http("GET", f"{self.base}/trial-versions/nope")
        self.assertEqual(st, 404)
        st, res = http("GET", f"{self.base}/trials/nope")
        self.assertEqual(st, 404)


if __name__ == "__main__":
    unittest.main()

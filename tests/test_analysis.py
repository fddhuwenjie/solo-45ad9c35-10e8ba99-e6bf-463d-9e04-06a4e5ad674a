"""核心复核引擎测试：门控、候选裁决、干湿、修订与封版语义。"""
import unittest

from fresco_salt.analysis import (
    CANDIDATE_CN, DEFAULTS, infer_stage, review,
)
from fresco_salt.chemistry import charge_balance, normalize_ions

WALL = {
    "wall_id": "w_test", "name": "测试墙",
    "width_mm": 3000, "height_mm": 4000, "thickness_mm": 80,
    "layers": [
        {"layer_id": "L1", "d_start_mm": 0, "d_end_mm": 5},
        {"layer_id": "L2", "d_start_mm": 5, "d_end_mm": 40},
        {"layer_id": "L3", "d_start_mm": 40, "d_end_mm": 80},
    ],
}
RAINS = [{"rain_id": "r1", "ts": "2026-06-09T02:00:00", "rain_mm": 18.0}]


def ion4(na, ca, unit="mmol/kg", lod=0.5):
    return {"Na+": {"value": na, "unit": unit, "lod": lod},
            "Cl-": {"value": na, "unit": unit, "lod": lod},
            "Ca2+": {"value": ca, "unit": unit, "lod": lod},
            "SO42-": {"value": ca, "unit": unit, "lod": lod}}


def sample(sid, x, y, depth, ts, na, ca, moisture=5.0, ions=None):
    layer = "L1" if depth <= 5 else "L2" if depth <= 40 else "L3"
    return {"sample_id": sid, "layer_id": layer, "x_mm": x, "y_mm": y, "z_mm": depth,
            "depth_mm": depth, "ts": ts, "moisture_wt": moisture,
            "temp_c": 22, "rh_percent": 60, "ions": ions or ion4(na, ca)}


def rising_set():
    out = []
    for d, na, ca in [(3, 40, 8), (20, 50, 10), (55, 60, 12)]:
        out.append(sample(f"l{d}d", 500, 200, d, "2026-06-01T09:00:00", na, ca, 5.5))
        out.append(sample(f"l{d}w", 500, 200, d, "2026-06-10T10:00:00", na, ca, 10.0))
    for d, na, ca in [(3, 15, 3), (20, 15, 3), (55, 15, 3)]:
        out.append(sample(f"h{d}d", 500, 3700, d, "2026-06-01T11:00:00", na, ca, 3.5))
        out.append(sample(f"h{d}w", 500, 3700, d, "2026-06-10T11:00:00", na, ca, 4.0))
    return out


def gates_map(result):
    return {g["code"]: g for g in result["gates"]}


class ChemistryTests(unittest.TestCase):
    def test_unit_unification(self):
        ions, basis = normalize_ions({
            "Na+": {"value": 22.99, "unit": "mg/kg", "lod": 1.0},
            "Cl-": {"value": 1.0, "unit": "meq/kg", "lod": 0.1},
        })
        self.assertEqual(basis, "solid")
        self.assertAlmostEqual(ions["Na+"].value, 1.0, places=6)
        self.assertAlmostEqual(ions["Cl-"].value, 1.0, places=6)

    def test_charge_balance_balanced(self):
        ions, _ = normalize_ions(ion4(10, 5))
        b = charge_balance(ions)
        self.assertAlmostEqual(b["cbe_percent"], 0.0, places=9)
        self.assertEqual(b["missing_lod"], [])

    def test_charge_balance_off(self):
        ions, _ = normalize_ions({"Na+": {"value": 20, "unit": "mmol/kg", "lod": 0.1},
                                  "Cl-": {"value": 2, "unit": "mmol/kg", "lod": 0.1}})
        b = charge_balance(ions)
        self.assertGreater(abs(b["cbe_percent"]), 80)

    def test_half_lod_and_missing_lod(self):
        ions, _ = normalize_ions({"Na+": {"value": None, "unit": "mmol/L", "lod": 0.8},
                                  "Cl-": {"value": 0.01, "unit": "mmol/L", "lod": 0.8}})
        self.assertTrue(all(i.censored for i in ions.values()))
        self.assertAlmostEqual(ions["Na+"].value, 0.4)
        with self.assertRaises(ValueError):
            normalize_ions({"K+": {"value": None, "unit": "mmol/L"}})

    def test_basis_mix_rejected(self):
        with self.assertRaises(ValueError):
            normalize_ions({"Na+": {"value": 1, "unit": "mmol/kg"},
                            "Cl-": {"value": 1, "unit": "mmol/L"}})


class StageTests(unittest.TestCase):
    def test_infer_stage_windows(self):
        from fresco_salt.util import parse_dt
        p = dict(DEFAULTS)
        self.assertEqual(infer_stage(parse_dt("2026-06-10T10:00:00"), RAINS, p)[0], "wet")
        self.assertEqual(infer_stage(parse_dt("2026-06-14T10:00:00"), RAINS, p)[0], "dry")
        self.assertEqual(infer_stage(parse_dt("2026-06-11T10:00:00"), RAINS, p)[0], "transition")
        # 首场雨前 8 天 -> 干季基线
        self.assertEqual(infer_stage(parse_dt("2026-06-01T02:00:00"), RAINS, p)[0], "dry")
        # 无降雨记录
        self.assertEqual(infer_stage(parse_dt("2026-06-01T02:00:00"), [], p)[0], "transition")


class GateTests(unittest.TestCase):
    def test_binding_gate(self):
        s = rising_set()
        s[0]["layer_id"] = "L3"  # 深度 3 错绑粗泥层
        r = review(WALL, s, RAINS, [])
        self.assertFalse(gates_map(r)["layer_bind"]["passed"])
        self.assertNotEqual(r["verdict"], "unique_source")

    def test_coord_gate(self):
        s = rising_set()
        s[0]["y_mm"] = 4001
        r = review(WALL, s, RAINS, [])
        self.assertFalse(gates_map(r)["coord"]["passed"])
        self.assertNotEqual(r["verdict"], "unique_source")

    def test_lod_gate(self):
        s = rising_set()
        s[0]["ions"]["Na+"].pop("lod")
        r = review(WALL, s, RAINS, [])
        self.assertFalse(gates_map(r)["lod"]["passed"])

    def test_charge_gate(self):
        s = rising_set()
        s[0]["ions"]["Cl-"]["value"] = 1.0  # 阴离子严重不足
        r = review(WALL, s, RAINS, [])
        self.assertFalse(gates_map(r)["charge"]["passed"])
        self.assertNotEqual(r["verdict"], "unique_source")

    def test_time_gate_missing_wet_and_pair(self):
        # 只有干季
        s = [x for x in rising_set() if x["sample_id"].endswith("d")]
        r = review(WALL, s, RAINS, [])
        self.assertFalse(gates_map(r)["time"]["passed"])
        self.assertNotEqual(r["verdict"], "unique_source")

    def test_basis_gate(self):
        s = rising_set()
        s[1]["ions"] = ion4(40, 8, unit="mmol/L")  # 与其余 mmol/kg 混相
        r = review(WALL, s, RAINS, [])
        self.assertFalse(gates_map(r)["basis"]["passed"])


class CandidateTests(unittest.TestCase):
    def test_unique_rising(self):
        r = review(WALL, rising_set(), RAINS,
                   [{"repair_id": "p1", "date": "2023-09-01", "material": "水泥砂浆",
                     "layer_id": "L2", "x_mm": 500, "y_mm": 2000}])
        self.assertTrue(r["all_gates_passed"], [g for g in r["gates"] if not g["passed"]])
        self.assertEqual(r["verdict"], "unique_source")
        self.assertEqual(r["unique_source"], "rising")
        self.assertEqual(r["candidates"]["leak"]["status"], "excluded")
        self.assertEqual(r["candidates"]["material"]["status"], "excluded")

    def test_tie_blocks_unique(self):
        s = []
        low = [(3, 30, 5), (20, 38, 7), (55, 48, 7)]
        for d, na, ca in low:
            s.append(sample(f"l{d}d", 600, 200, d, "2026-06-01T09:00:00", na, ca, 5.0))
            s.append(sample(f"l{d}w", 600, 200, d, "2026-06-10T10:00:00", na, ca, 9.5))
        for d, na, ca in [(3, 22, 8), (20, 22, 8), (55, 20, 10)]:
            s.append(sample(f"h{d}d", 600, 3700, d, "2026-06-01T11:00:00", na, ca, 3.2))
        for d, na, ca in [(3, 40, 15), (20, 32, 13), (55, 24, 11)]:
            s.append(sample(f"h{d}w", 600, 3700, d, "2026-06-10T11:00:00", na, ca, 7.4))
        r = review(WALL, s, RAINS, [])
        self.assertFalse(gates_map(r)["tie"]["passed"])
        self.assertNotEqual(r["verdict"], "unique_source")
        self.assertIsNone(r["unique_source"])
        detail = gates_map(r)["tie"]["detail"]
        self.assertTrue("势均力敌" in detail or "并列" in detail, detail)

    def test_material_release(self):
        # 修补点旁富集、标志离子高、干湿含水率不脉动
        s = []
        plan = [(3, 45, 10), (20, 45, 10)]
        for d, na, ca in plan:
            s.append(sample(f"n{d}d", 500, 2000, d, "2026-06-01T09:00:00", na, ca, 6.0))
            s.append(sample(f"n{d}w", 500, 2000, d, "2026-06-10T10:00:00", na, ca, 6.4))
        for d, na, ca in [(3, 15, 3), (20, 15, 3)]:
            s.append(sample(f"f{d}d", 2000, 2000, d, "2026-06-01T09:00:00", na, ca, 5.0))
            s.append(sample(f"f{d}w", 2000, 2000, d, "2026-06-10T10:00:00", na, ca, 8.0))
        repairs = [{"repair_id": "p1", "date": "2023-09-01", "material": "水泥砂浆",
                    "layer_id": "L2", "x_mm": 500, "y_mm": 2000, "radius_mm": 300}]
        r = review(WALL, s, RAINS, repairs)
        self.assertEqual(r["candidates"]["material"]["status"], "retained")
        # 返潮/渗漏在近地面/高带均无系统梯度，不应保留
        self.assertNotEqual(r["candidates"]["rising"]["status"], "retained")
        self.assertNotEqual(r["candidates"]["leak"]["status"], "retained")
        if r["all_gates_passed"]:
            self.assertEqual(r["unique_source"], "material")


class RevisionTests(unittest.TestCase):
    def _revs(self, payload, kind="exclude_sample", reason="污染/错绑更正（双人复核）"):
        return [{"seq": 1, "rev_id": "rev_x", "kind": kind, "reason": reason,
                 "payload": payload, "actor": "tester", "created_ts": "2026-06-11T00:00:00"}]

    def test_exclude_polluted_sample(self):
        # 高带真实剖面：雨后盐分被稀释（脉冲比 <1）；污染白霜虚增脉冲使渗漏被保留
        s = []
        for d, na, ca in [(3, 40, 8), (20, 50, 10), (55, 60, 12)]:
            s.append(sample(f"l{d}d", 500, 200, d, "2026-06-01T09:00:00", na, ca, 5.5))
            s.append(sample(f"l{d}w", 500, 200, d, "2026-06-10T10:00:00", na, ca, 10.0))
        for d, na, ca in [(3, 32, 5.5), (20, 32, 5.5), (55, 32, 5.5)]:
            s.append(sample(f"h{d}d", 500, 3700, d, "2026-06-01T11:00:00", na, ca, 3.5))
        for d, na, ca in [(3, 18, 3), (20, 18, 3), (55, 18, 3)]:
            s.append(sample(f"h{d}w", 500, 3700, d, "2026-06-10T11:00:00", na, ca, 4.2))
        s.append(sample("poll", 520, 3700, 3, "2026-06-10T12:00:00", 300, 100, 8.0))
        before = review(WALL, s, RAINS, [])
        self.assertNotEqual(before["verdict"], "unique_source")
        self.assertEqual(before["candidates"]["leak"]["status"], "retained")
        after = review(WALL, s, RAINS, [], self._revs({"sample_id": "poll"}))
        self.assertEqual(after["verdict"], "unique_source")
        self.assertEqual(after["samples"]["excluded"][0]["sample_id"], "poll")
        self.assertTrue(after["samples"]["excluded"][0]["reason"])

    def test_correct_binding(self):
        s = rising_set()
        s[0]["layer_id"] = "L3"  # 错绑
        before = review(WALL, s, RAINS, [])
        self.assertFalse(gates_map(before)["layer_bind"]["passed"])
        revs = self._revs({"sample_id": s[0]["sample_id"], "new_layer_id": "L1"},
                          kind="correct_binding",
                          reason="深度 3mm 落在 L1，原录入 L3 为层位错绑，按剖面照片更正")
        after = review(WALL, s, RAINS, [], revs)
        self.assertTrue(gates_map(after)["layer_bind"]["passed"])
        self.assertEqual(after["samples"]["rebound"][0]["new_layer_id"], "L1")

    def test_conservative_blocks_even_unique(self):
        r = review(WALL, rising_set(), RAINS, [],
                   self._revs({}, kind="adopt_conservative",
                              reason="证据依赖单一雨季，采纳保守解释，封版前不下唯一结论"))
        self.assertEqual(r["verdict"], "indeterminate_conservative")
        self.assertIsNone(r["unique_source"])
        self.assertTrue(r["conservative_mode"])

    def test_revision_requires_reason_in_app(self):
        from fresco_salt.app import ApiError, validate_revision
        from fresco_salt.db import Store
        from fresco_salt.app import SaltApiApp
        store = Store(":memory:")
        app = SaltApiApp(store)
        app.create_wall(WALL)
        with self.assertRaises(ApiError):
            validate_revision({"kind": "adopt_conservative", "reason": "  ",
                               "payload": {}}, WALL, store)
        store.close()


class AdviceTests(unittest.TestCase):
    def test_tie_advice_targets_pair(self):
        s = []
        # 构造无湿样本 + 弱并列
        for d in (3, 20):
            s.append(sample(f"l{d}", 600, 200, d, "2026-06-01T09:00:00", 40, 8))
            s.append(sample(f"h{d}", 600, 3700, d, "2026-06-14T09:00:00", 20, 4))
        r = review(WALL, s, RAINS, [])
        purposes = " ".join(a["purpose"] for a in r["next_sampling"])
        self.assertIn("雨后湿阶段", purposes)
        self.assertIn("干湿配对", purposes)


if __name__ == "__main__":
    unittest.main()

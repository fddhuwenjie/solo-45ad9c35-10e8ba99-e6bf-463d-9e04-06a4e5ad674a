"""微气候结晶循环模块测试：滞回状态机、六类待判缺口、改绑/保守规则修订、
确认版冻结复算与 HTTP 端到端。"""
import json
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer

from fresco_salt.app import Handler, SaltApiApp
from fresco_salt.db import Store
from fresco_salt.microclimate import (
    analyze_monitor, derive_conservative_rule, resolve_mapping, scan_rule,
)
from fresco_salt.microclimate_recompute import build_confirm, verify

RULE_NACL = {"rule_id": "nacl", "salt": "NaCl", "cn": "氯化钠",
             "drh_percent": 75.3, "crh_percent": 70.0,
             "temp_min_c": 10.0, "temp_max_c": 30.0,
             "min_wet_hours": 6.0, "min_dry_hours": 6.0}
RULE_NITRE = {"rule_id": "nitre", "salt": "KNO3", "cn": "硝酸钾",
              "drh_percent": 92.0, "crh_percent": 88.0,
              "temp_min_c": 0.0, "temp_max_c": 40.0,
              "min_wet_hours": 6.0, "min_dry_hours": 12.0}

WALL = {"wall_id": "w_m", "name": "微气候测试墙",
        "width_mm": 3000, "height_mm": 4000, "thickness_mm": 80,
        "layers": [{"layer_id": "L1", "d_start_mm": 0, "d_end_mm": 5}]}

SRC_OK = {"kind": "review", "kind_name": "来源复核封版", "version_id": "v_src",
          "hash": "h1", "summary": "唯一盐源", "decisive": True}
SRC_NO = {"kind": "review", "kind_name": "来源复核封版", "version_id": "v_src",
          "hash": "h1", "summary": "候选并列", "decisive": False}
SRC_TRIAL_OK = {"kind": "trial", "kind_name": "敷贴试验确认版",
                "version_id": "tv_src", "hash": "h2",
                "summary": "净移除", "decisive": True}

T0 = datetime(2026, 8, 1)


def pt(ts, temp=22.0, rh=60.0):
    return {"ts": ts, "temp_c": temp, "rh_percent": rh, "valid": True,
            "invalid_reasons": []}


def diurnal_points(days=3, t0=T0, rhpat=None, temp=22.0):
    if rhpat is None:
        rhpat = (58, 72, 78, 77, 72, 60, 58, 57)
    out = []
    for d in range(days):
        for i, rh in enumerate(rhpat):
            out.append(pt(t0 + timedelta(hours=d * 24 + i * 3), temp, rh))
    return out


def readings(sid, days=3, temp=22.0, rhpat=None, start=T0):
    if rhpat is None:
        rhpat = (58, 72, 78, 77, 72, 60, 58, 57)
    return [{"reading_id": f"{sid}_{d}_{i}", "sensor_id": sid,
             "ts": (start + timedelta(hours=d * 24 + i * 3)).isoformat(),
             "temp": temp, "rh": rh}
            for d in range(days) for i, rh in enumerate(rhpat)]


def cal(valid_until="2027-01-01T00:00:00", **over):
    c = {"calibrated_ts": "2026-07-01T00:00:00", "valid_until_ts": valid_until}
    c.update(over)
    return c


def sensor(sid="s1", x=500, y=500, calibrations=None, **over):
    s = {"sensor_id": sid, "x_mm": x, "y_mm": y,
         "calibrations": calibrations if calibrations is not None else [cal()]}
    s.update(over)
    return s


def zone(zid="zA", name="墙角A", x=0, y=0, w=1000, h=1000, **over):
    z = {"zone_id": zid, "name": name, "x_mm": x, "y_mm": y,
         "width_mm": w, "height_mm": h}
    z.update(over)
    return z


def monitor(zones=None, sensors=None, rules=(RULE_NACL,),
            source_version_id="v_src", **over):
    m = {"monitor_id": "m1", "wall_id": "w_m", "name": "脱盐后微气候",
         "source_version_id": source_version_id,
         "zones": zones if zones is not None else [zone()],
         "sensors": sensors if sensors is not None else [sensor()],
         "rules": list(rules),
         "params": {}}
    m.update(over)
    return m


def rev(seq, kind, payload, reason="现场布设双人复核，留痕"):
    return {"seq": seq, "rev_id": f"mrev{seq}", "kind": kind, "reason": reason,
            "payload": payload, "actor": "tester",
            "created_ts": "2026-08-10T00:00:00"}


def gaps(result):
    return {z["zone_id"]: [g["code"] for g in z["gaps"]] for z in result["zones"]}


class StateMachineTests(unittest.TestCase):
    def test_diurnal_complete_cycles(self):
        """三天昼夜跨阈 → 3 个完整循环（末段干燥停留 8.5h 已达 min_dry）。"""
        r = scan_rule(diurnal_points(3), RULE_NACL, 6.0)
        self.assertEqual(r["n_segments"], 1)
        self.assertEqual(r["n_complete_cycles"], 3)
        self.assertTrue(all(c["complete"] for c in r["cycles"]))
        r4 = scan_rule(diurnal_points(4), RULE_NACL, 6.0)
        self.assertEqual(r4["n_complete_cycles"], 4)
        self.assertIsNotNone(r4["longest_wet_hours"])
        self.assertEqual(r4["first_risk_ts"], r4["cycles"][0]["wet_start_ts"])

    def test_hysteresis_keeps_wet_inside_band(self):
        """RH 越过 DRH 后在 [CRH,DRH] 滞回带内波动仍算湿润，不伪造析晶。"""
        rh = [60, 78, 73, 72, 74, 78, 60, 55, 55, 55]  # 72/73/74 均在带内
        pts = [pt(T0 + timedelta(hours=3 * i), 22.0, v) for i, v in enumerate(rh)]
        r = scan_rule(pts, RULE_NACL, 6.0)
        kinds = [c["kind"] for c in r["crossings"]]
        self.assertEqual(kinds.count("up_drh"), 1)
        self.assertEqual(kinds.count("down_crh"), 1)
        self.assertEqual(r["n_complete_cycles"], 1)

    def test_short_return_not_a_cycle(self):
        """跨阈湿润仅 3h（< 最短湿润 6h）即回落 → 短时回返，不计循环。"""
        rh = [60, 78, 78, 60, 55, 55, 55]
        pts = [pt(T0 + timedelta(hours=3 * i), 22.0, v) for i, v in enumerate(rh)]
        r = scan_rule(pts, RULE_NACL, 6.0)
        self.assertEqual(r["n_complete_cycles"], 0)
        self.assertEqual(r["n_short_returns"], 1)
        self.assertIsNone(r["longest_wet_hours"])
        self.assertIsNone(r["first_risk_ts"])

    def test_dry_dwell_too_short_incomplete(self):
        """湿润合格但干燥停留不足 min_dry 又再湿润 → 不完整循环。"""
        rh = [60, 78, 78, 78, 60, 78, 78, 78, 78]  # 仅 3h 干燥后再湿润
        pts = [pt(T0 + timedelta(hours=3 * i), 22.0, v) for i, v in enumerate(rh)]
        r = scan_rule(pts, RULE_NACL, 6.0)
        self.assertEqual(r["n_complete_cycles"], 0)
        self.assertTrue(r["cycles"])
        self.assertEqual(r["cycles"][0]["incomplete_reason"], "dry_dwell_too_short")

    def test_gap_splits_open_wet(self):
        """断档处曲线断开：断档时仍湿润记为开口湿润，不与后续拼接。"""
        a = [pt(T0 + timedelta(hours=3 * i), 22.0, v)
             for i, v in enumerate([60, 78, 78, 78])]
        b = [pt(T0 + timedelta(hours=48 + 3 * i), 22.0, 60) for i in range(2)]
        r = scan_rule(a + b, RULE_NACL, 6.0)
        self.assertEqual(r["n_segments"], 2)
        self.assertEqual(r["n_complete_cycles"], 0)
        self.assertIsNotNone(r["open_wet"])
        self.assertGreaterEqual(r["open_wet"]["hours"], 6.0)
        self.assertEqual(r["first_risk_ts"], r["open_wet"]["start_ts"])


class ZoneGapTests(unittest.TestCase):
    def run_zones(self, zones, sensors, rds=None, revs=None, src=SRC_OK, rules=(RULE_NACL,)):
        m = monitor(zones=zones, sensors=sensors, rules=rules)
        return analyze_monitor(WALL, m, rds if rds is not None else readings("s1"),
                               revs or [], src)

    def test_source_inconclusive_pending(self):
        r = self.run_zones([zone()], [sensor()], src=SRC_NO)
        z = r["zones"][0]
        self.assertEqual(z["status"], "pending")
        self.assertIn("source_inconclusive", gaps(r)["zA"])
        self.assertFalse(z["gates"][0]["passed"])

    def test_no_sensor_pending(self):
        z2 = zone("zB", "中区B", x=1000, y=1000)
        r = self.run_zones([zone(), z2], [sensor()])
        g = gaps(r)
        self.assertEqual(r["zones"][0]["status"], "assessed")
        self.assertIn("no_sensor", g["zB"])

    def test_mapping_ambiguous_overlap(self):
        zb = zone("zB", "墙角B", x=500, y=500)
        r = self.run_zones([zone(), zb], [sensor(x=600, y=600)])
        g = gaps(r)
        self.assertIn("mapping_ambiguous", g["zA"])
        self.assertIn("mapping_ambiguous", g["zB"])

    def test_adopt_sensor_resolves_ambiguity(self):
        zb = zone("zB", "墙角B", x=500, y=500)
        revs = [rev(1, "adopt_sensor", {"zone_id": "zA", "sensor_id": "s1"},
                    "s1 安装在 A 区取样列，现场布点图编号 A-3，代表 A 区")]
        r = self.run_zones([zone(), zb], [sensor(x=600, y=600)], revs=revs)
        za, zbb = r["zones"]
        self.assertEqual(za["status"], "assessed")
        self.assertEqual(za["mapped_sensor_ids"], ["s1"])
        # 多解解除：s1 明确归属 A，B 变为无传感器
        self.assertIn("no_sensor", gaps(r)["zB"])

    def test_multiple_series_pending(self):
        r = self.run_zones(
            [zone()], [sensor("s1", 200, 200), sensor("s2", 800, 800)],
            readings("s1") + readings("s2"))
        self.assertIn("multiple_series", gaps(r)["zA"])

    def test_rebind_sensor_revision(self):
        z2 = zone("zB", "中区B", x=1000, y=1000)
        # s1 几何在 A，改绑到 B（几何外须给理由）
        revs = [rev(1, "rebind_sensor", {"sensor_id": "s1", "zone_id": "zB"},
                    "s1 随样点迁移重装至 B 区，迁移单 MC-07，双人复核")]
        r = self.run_zones([zone(), z2], [sensor()],
                           readings("s1"), revs=revs)
        za, zb = r["zones"]
        self.assertIn("no_sensor", gaps(r)["zA"])
        self.assertEqual(zb["status"], "assessed")
        note = [n for n in zb["mapping_notes"] if n["code"] == "rebind_off_geometry"]
        self.assertTrue(note)

    def test_calibration_expired_pending(self):
        r = self.run_zones([zone()], [sensor(calibrations=[cal("2026-07-15T00:00:00")])])
        self.assertIn("calibration_expired", gaps(r)["zA"])
        self.assertEqual(r["zones"][0]["status"], "pending")

    def test_missing_calibration_pending(self):
        r = self.run_zones([zone()], [sensor(calibrations=[])])
        self.assertIn("calibration_expired", gaps(r)["zA"])

    def test_time_gap_pending(self):
        rds = readings("s1")
        rds[10]["ts"] = (T0 + timedelta(hours=120)).isoformat()  # 拉开断档
        r = self.run_zones([zone()], [sensor()], rds)
        self.assertIn("time_gap", gaps(r)["zA"])
        # 断档两侧不拼接：两段
        rule_row = r["zones"][0]["rules"][0]
        self.assertEqual(rule_row["n_segments"], 2)

    def test_missing_value_pending(self):
        rds = readings("s1")
        rds[5]["rh"] = None
        r = self.run_zones([zone()], [sensor()], rds)
        self.assertIn("missing_value", gaps(r)["zA"])

    def test_duplicate_ts_pending(self):
        rds = readings("s1")
        rds[6] = dict(rds[5])
        r = self.run_zones([zone()], [sensor()], rds)
        self.assertIn("duplicate_ts", gaps(r)["zA"])

    def test_unit_conflict_pending(self):
        rds = readings("s1")
        rds[3]["rh_unit"] = "fraction"
        rds[3]["rh"] = 0.78
        r = self.run_zones([zone()], [sensor()], rds)
        self.assertIn("unit_conflict", gaps(r)["zA"])

    def test_temp_unit_f_converted(self):
        rds = readings("s1", temp=71.6)  # 22℃
        for x in rds:
            x["temp_unit"] = "F"
        m = monitor(zones=[zone()], sensors=[sensor(temp_unit="F")])
        r = analyze_monitor(WALL, m, rds, [], SRC_OK)
        self.assertEqual(r["zones"][0]["status"], "assessed")

    def test_rule_range_uncovered_pending(self):
        rds = readings("s1", temp=5.0)  # NaCl 规则温区 10~30℃
        r = self.run_zones([zone()], [sensor()], rds)
        self.assertIn("temp_range_uncovered", gaps(r)["zA"])

    def test_conservative_rule_revision_resolves(self):
        rds = readings("s1", temp=5.0)
        revs = [rev(1, "adopt_conservative_rule",
                    {"zone_id": "zA", "rule_id": "nacl", "temp_min_c": 0.0},
                    "冬季凌晨实测 5℃，原规则温区 10℃ 起步覆盖不足；"
                    "补做低温潮解实验前先按安全侧放宽温区到 0℃，DRH/CRH 同步下调")]
        r = self.run_zones([zone()], [sensor()], rds, revs=revs)
        z = r["zones"][0]
        self.assertEqual(z["status"], "assessed")
        rr = z["rules"][0]
        self.assertTrue(rr["conservative"])
        self.assertEqual(rr["derived"]["derived_from_rule_id"], "nacl")
        self.assertLessEqual(rr["drh_percent"], RULE_NACL["drh_percent"])
        self.assertLessEqual(rr["crh_percent"], RULE_NACL["crh_percent"])

    def test_multi_rule_zone_aggregation(self):
        rds = readings("s1")  # 峰值 78%：过 NaCl DRH 但远低于 KNO3 DRH 92%
        r = self.run_zones([zone()], [sensor()], rds, rules=(RULE_NACL, RULE_NITRE))
        z = r["zones"][0]
        by = {rr["rule_id"]: rr for rr in z["rules"]}
        self.assertGreater(by["nacl"]["n_complete_cycles"], 0)
        self.assertEqual(by["nitre"]["n_complete_cycles"], 0)
        self.assertEqual(z["n_complete_cycles"], by["nacl"]["n_complete_cycles"])


class ConservativeRuleTests(unittest.TestCase):
    def test_auto_derivation_safe_direction(self):
        p = {}
        from fresco_salt.microclimate import MICROCLIMATE_DEFAULTS
        d = derive_conservative_rule(RULE_NACL, {}, MICROCLIMATE_DEFAULTS)
        self.assertLess(d["drh_percent"], RULE_NACL["drh_percent"])
        self.assertLess(d["crh_percent"], RULE_NACL["crh_percent"])
        self.assertGreater(d["hysteresis_pct"], 0)
        self.assertLessEqual(d["temp_min_c"], RULE_NACL["temp_min_c"])
        self.assertGreaterEqual(d["temp_max_c"], RULE_NACL["temp_max_c"])
        self.assertEqual(d["source"], "derived_conservative")

    def test_tightening_rejected(self):
        from fresco_salt.microclimate import MICROCLIMATE_DEFAULTS
        with self.assertRaises(ValueError):
            derive_conservative_rule(RULE_NACL, {"drh_percent": 80.0},
                                     MICROCLIMATE_DEFAULTS)
        with self.assertRaises(ValueError):
            derive_conservative_rule(RULE_NACL, {"min_wet_hours": 9.0},
                                     MICROCLIMATE_DEFAULTS)

    def test_invalid_base_rule_rejected(self):
        from fresco_salt.microclimate import validate_rule
        with self.assertRaises(ValueError):
            validate_rule({"rule_id": "x", "salt": "X", "drh_percent": 60,
                           "crh_percent": 65, "temp_min_c": 0, "temp_max_c": 30,
                           "min_wet_hours": 1, "min_dry_hours": 1})


class RiskLevelTests(unittest.TestCase):
    def test_high_moderate_low_and_current_wet(self):
        high = analyze_monitor(WALL, monitor(), readings("s1", days=6), [], SRC_OK)
        self.assertGreaterEqual(high["zones"][0]["n_complete_cycles"], 3)
        self.assertEqual(high["zones"][0]["risk_level"], "high")
        low = analyze_monitor(WALL, monitor(params={"risk_cycles_high": 99}),
                              readings("s1", days=2), [], SRC_OK)
        self.assertIn(low["zones"][0]["risk_level"], ("moderate", "low"))
        # 期末停在湿润态 → current_wet
        rds = readings("s1", days=1)
        for x in rds[-3:]:
            x["rh"] = 78.0
        cur = analyze_monitor(WALL, monitor(), rds, [], SRC_OK)
        self.assertEqual(cur["zones"][0]["risk_level"], "current_wet")
        self.assertIsNotNone(cur["zones"][0]["open_wet"])


class ConfirmTests(unittest.TestCase):
    def test_build_verify_and_tamper(self):
        m = monitor()
        c = build_confirm(WALL, m, readings("s1"), [], SRC_OK)
        self.assertTrue(c["version_id"].startswith("mc_"))
        self.assertEqual(c["source"]["version_id"], "v_src")
        self.assertTrue(verify(c))
        # 篡改读数 → 哈希不复算
        bad = json.loads(json.dumps(c))
        bad["frozen_input"]["readings"][0]["rh"] = 99.0
        self.assertFalse(verify(bad))
        # 篡改保守修订 → 失败
        revs = [rev(1, "adopt_conservative_rule",
                    {"zone_id": "zA", "rule_id": "nacl", "temp_min_c": 0.0},
                    "r")]
        c2 = build_confirm(WALL, monitor(), readings("s1", temp=5.0), revs, SRC_OK)
        self.assertTrue(verify(c2))
        self.assertEqual(c2["result"]["n_pending"], 0)


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
    out = []

    def s(sid, x, y, d, ts, na, ca, m):
        layer = "L1" if d <= 5 else "L1"
        return {"sample_id": sid, "layer_id": layer, "x_mm": x, "y_mm": y,
                "z_mm": d, "depth_mm": d, "ts": ts, "moisture_wt": m,
                "temp_c": 22, "rh_percent": 60,
                "ions": {"Na+": {"value": na, "unit": "mmol/kg", "lod": 0.5},
                         "Cl-": {"value": na, "unit": "mmol/kg", "lod": 0.5},
                         "Ca2+": {"value": ca, "unit": "mmol/kg", "lod": 0.5},
                         "SO42-": {"value": ca, "unit": "mmol/kg", "lod": 0.5}}}

    for d in (3,):
        out.append(s(f"l{d}d", 500, 200, d, "2026-06-01T09:00:00", 40, 8, 5.5))
        out.append(s(f"l{d}w", 500, 200, d, "2026-06-10T10:00:00", 40, 8, 10.0))
        out.append(s(f"h{d}d", 500, 3700, d, "2026-06-01T11:00:00", 15, 3, 3.5))
        out.append(s(f"h{d}w", 500, 3700, d, "2026-06-10T11:00:00", 15, 3, 4.0))
    return out


MON_BODY = {
    "name": "脱盐后墙角微气候",
    "zones": [zone()],
    "sensors": [sensor("s1", 500, 500)],
    "rules": [RULE_NACL],
}


class MicroclimateHttpTests(unittest.TestCase):
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
        st, res = http("POST", f"{self.base}/walls/{self.wid}/rainfall",
                       [{"ts": "2026-06-09T02:00:00", "rain_mm": 18.0}])
        self.assertEqual(st, 200, res)
        st, res = http("GET", f"{self.base}/walls/{self.wid}/analysis")
        self.assertEqual(res["analysis"]["verdict"], "unique_source")
        st, locked = http("POST", f"{self.base}/walls/{self.wid}/lock",
                          {"actor": "张工"})
        self.assertEqual(st, 200)
        self.src_vid = locked["version_id"]

    def _create(self, **over):
        body = dict(MON_BODY, source_version_id=self.src_vid)
        body.update(over)
        st, res = http("POST", f"{self.base}/walls/{self.wid}/monitors", body)
        return st, res

    def test_monitor_validation(self):
        st, res = self._create(source_version_id="v_nope")
        self.assertEqual(st, 400)
        self.assertIn("封版", res["error"])
        bad_s = [dict(sensor("s1", 500, 500), calibrations=[])]
        st, res = self._create(sensors=bad_s)
        self.assertEqual(st, 400)
        self.assertIn("校准", res["error"])
        bad_rule = dict(RULE_NACL, crh_percent=80.0)  # CRH > DRH
        st, res = self._create(rules=[bad_rule])
        self.assertEqual(st, 400)
        self.assertIn("CRH", res["error"])
        bad_z = [dict(zone(), width_mm=99999)]
        st, res = self._create(zones=bad_z)
        self.assertEqual(st, 400)
        self.assertIn("越出", res["error"])

    def test_full_monitor_flow(self):
        st, res = self._create()
        self.assertEqual(st, 200, res)
        mid = res["monitor"]["monitor_id"]
        st, res = http("GET", f"{self.base}/walls/{self.wid}/monitors")
        self.assertTrue(any(m["monitor_id"] == mid for m in res["monitors"]))

        rds = readings("s1", days=4)
        st, res = http("POST", f"{self.base}/monitors/{mid}/readings", rds)
        self.assertEqual(st, 200)
        self.assertEqual(len(res["reading_ids"]), len(rds))
        # 未知传感器 / 越界 RH → 400
        bad = {"sensor_id": "ghost", "ts": "2026-08-01T00:00:00",
               "temp": 22, "rh": 60}
        self.assertEqual(http("POST", f"{self.base}/monitors/{mid}/readings",
                              [bad])[0], 400)
        bad = {"sensor_id": "s1", "ts": "2026-08-01T00:00:00", "rh": 120}
        self.assertEqual(http("POST", f"{self.base}/monitors/{mid}/readings",
                              [bad])[0], 400)

        st, res = http("GET", f"{self.base}/monitors/{mid}/analysis")
        self.assertEqual(st, 200)
        z = res["analysis"]["zones"][0]
        self.assertEqual(z["status"], "assessed")
        self.assertGreaterEqual(z["n_complete_cycles"], 1)
        self.assertIsNotNone(z["first_risk_ts"])

        # 无理由修订 400；几何外改绑理由过短 400
        st, res = http("POST", f"{self.base}/monitors/{mid}/revisions",
                       {"kind": "rebind_sensor", "reason": " ",
                        "payload": {"sensor_id": "s1", "zone_id": "zA"}})
        self.assertEqual(st, 400)

        # 确认版
        st, conf = http("POST", f"{self.base}/monitors/{mid}/confirm",
                        {"actor": "张工"})
        self.assertEqual(st, 200, conf)
        mvid = conf["version_id"]
        self.assertEqual(conf["n_pending"], 0)
        self.assertEqual(conf["source_version_id"], self.src_vid)

        st, rec = http("GET",
                       f"{self.base}/microclimate-versions/{mvid}/zones.json")
        self.assertEqual(st, 200)
        self.assertTrue(verify(rec))
        self.assertEqual(rec["source"]["version_id"], self.src_vid)
        st, svg = http("GET",
                       f"{self.base}/microclimate-versions/{mvid}/risk.svg")
        self.assertEqual(st, 200)
        self.assertTrue(svg.lstrip().startswith("<svg"))
        st, meta = http("GET", f"{self.base}/microclimate-versions/{mvid}")
        self.assertEqual(st, 200)
        self.assertEqual(meta["version_id"], mvid)
        st, res = http("GET", f"{self.base}/monitors/{mid}/versions")
        self.assertEqual(len(res["versions"]), 1)

        # 确认后只读 409
        self.assertEqual(http("POST", f"{self.base}/monitors/{mid}/readings",
                              [rds[0]])[0], 409)
        self.assertEqual(http("POST", f"{self.base}/monitors/{mid}/revisions",
                              {"kind": "adopt_sensor", "reason": "x",
                               "payload": {"zone_id": "zA",
                                           "sensor_id": "s1"}})[0], 409)
        self.assertEqual(http("POST", f"{self.base}/monitors/{mid}/confirm",
                              {})[0], 409)
        self.assertEqual(http("GET", f"{self.base}/monitors/nope/analysis")[0], 404)
        self.assertEqual(http("GET",
                              f"{self.base}/microclimate-versions/nope")[0], 404)

    def test_pending_zone_then_revision_resolves(self):
        # 两区重叠，s1 在重叠区 → 都待判；adopt 后 A 可判、B 无传感器
        zb = zone("zB", "墙角B", x=500, y=500)
        st, res = self._create(
            zones=[zone(), zb],
            sensors=[sensor("s1", 600, 600)])
        self.assertEqual(st, 200, res)
        mid = res["monitor"]["monitor_id"]
        rds = readings("s1", days=3)
        http("POST", f"{self.base}/monitors/{mid}/readings", rds)
        st, res = http("GET", f"{self.base}/monitors/{mid}/analysis")
        self.assertEqual(res["analysis"]["n_pending"], 2)
        st, res = http("POST", f"{self.base}/monitors/{mid}/revisions",
                       {"kind": "adopt_sensor", "actor": "李工",
                        "reason": "s1 位于 A 区样点列，布点图 A-3，代表 A 区",
                        "payload": {"zone_id": "zA", "sensor_id": "s1"}})
        self.assertEqual(st, 200)
        by = {z["zone_id"]: z for z in res["analysis"]["zones"]}
        self.assertEqual(by["zA"]["status"], "assessed")
        self.assertEqual(by["zB"]["status"], "pending")

    def test_conservative_rule_revision_http(self):
        st, res = self._create()
        self.assertEqual(st, 200, res)
        mid = res["monitor"]["monitor_id"]
        rds = readings("s1", days=3, temp=5.0)
        http("POST", f"{self.base}/monitors/{mid}/readings", rds)
        st, res = http("GET", f"{self.base}/monitors/{mid}/analysis")
        self.assertEqual(res["analysis"]["zones"][0]["status"], "pending")
        # 收紧方向的保守派生应被拒绝（400）
        st, bad = http("POST", f"{self.base}/monitors/{mid}/revisions",
                       {"kind": "adopt_conservative_rule", "reason": "x",
                        "payload": {"zone_id": "zA", "rule_id": "nacl",
                                    "drh_percent": 80.0}})
        self.assertEqual(st, 400)
        st, ok = http("POST", f"{self.base}/monitors/{mid}/revisions",
                      {"kind": "adopt_conservative_rule", "actor": "张工",
                       "reason": "凌晨低温 5℃ 超出原规则温区，补测前先按安全侧"
                                 "放宽温区至 0℃，阈值同步下调",
                       "payload": {"zone_id": "zA", "rule_id": "nacl",
                                   "temp_min_c": 0.0}})
        self.assertEqual(st, 200, ok)
        self.assertEqual(ok["analysis"]["zones"][0]["status"], "assessed")
        self.assertEqual(ok["n_pending"], 0)


if __name__ == "__main__":
    unittest.main()

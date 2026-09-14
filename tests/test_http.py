"""HTTP 端到端测试：线程内启动真实 http.server，经 urllib 打请求。"""
import json
import threading
import unittest
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

from fresco_salt.app import Handler, SaltApiApp
from fresco_salt.db import Store
from fresco_salt.recompute import verify


def body(method, url, payload=None, ctype="application/json"):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = f"{ctype}; charset=utf-8"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if "json" in resp.headers.get("Content-Type", "")
                                 else raw)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


WALL = {
    "name": "HTTP 测试墙", "width_mm": 3000, "height_mm": 4000, "thickness_mm": 80,
    "layers": [
        {"layer_id": "L1", "d_start_mm": 0, "d_end_mm": 5},
        {"layer_id": "L2", "d_start_mm": 5, "d_end_mm": 40},
        {"layer_id": "L3", "d_start_mm": 40, "d_end_mm": 80},
    ],
}


def s(sid, x, y, d, ts, na, ca, m):
    layer = "L1" if d <= 5 else "L2" if d <= 40 else "L3"
    return {"sample_id": sid, "layer_id": layer, "x_mm": x, "y_mm": y, "z_mm": d,
            "depth_mm": d, "ts": ts, "moisture_wt": m, "temp_c": 22, "rh_percent": 60,
            "ions": {
                "Na+": {"value": na, "unit": "mmol/kg", "lod": 0.5},
                "Cl-": {"value": na, "unit": "mmol/kg", "lod": 0.5},
                "Ca2+": {"value": ca, "unit": "mmol/kg", "lod": 0.5},
                "SO42-": {"value": ca, "unit": "mmol/kg", "lod": 0.5}}}


def rising_samples():
    out = []
    for d, na, ca in [(3, 40, 8), (20, 50, 10), (55, 60, 12)]:
        out.append(s(f"l{d}d", 500, 200, d, "2026-06-01T09:00:00", na, ca, 5.5))
        out.append(s(f"l{d}w", 500, 200, d, "2026-06-10T10:00:00", na, ca, 10.0))
    for d, na, ca in [(3, 15, 3), (20, 15, 3), (55, 15, 3)]:
        out.append(s(f"h{d}d", 500, 3700, d, "2026-06-01T11:00:00", na, ca, 3.5))
        out.append(s(f"h{d}w", 500, 3700, d, "2026-06-10T11:00:00", na, ca, 4.0))
    return out


class HttpTests(unittest.TestCase):
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
        st, res = body("POST", f"{self.base}/walls", WALL)
        self.assertEqual(st, 200)
        self.wid = res["wall"]["wall_id"]

    def test_01_health_and_list(self):
        st, res = body("GET", f"{self.base}/health")
        self.assertEqual(st, 200)
        self.assertEqual(res["status"], "ok")
        st, res = body("GET", f"{self.base}/walls")
        self.assertTrue(any(w["wall_id"] == self.wid for w in res["walls"]))

    def test_02_ingestion_validation(self):
        bad = dict(WALL, name="坏墙", layers=[{"layer_id": "X", "d_start_mm": 0,
                                               "d_end_mm": 999}])
        st, res = body("POST", f"{self.base}/walls", bad)
        self.assertEqual(st, 400)
        bad_sample = s("zz", 0, 0, 3, "2026-06-01T09:00:00", 1, 1, 5)
        bad_sample["layer_id"] = "L3"  # 层位错绑
        st, res = body("POST", f"{self.base}/walls/{self.wid}/samples", [bad_sample])
        self.assertEqual(st, 400)
        self.assertIn("层位错绑", res["error"])
        bad_sample["layer_id"] = "L1"
        bad_sample["y_mm"] = 99999
        st, res = body("POST", f"{self.base}/walls/{self.wid}/samples", [bad_sample])
        self.assertEqual(st, 400)
        self.assertIn("越界", res["error"])

    def test_03_full_flow_unique_lock_artifacts_readonly(self):
        w = self.wid
        self.assertEqual(body("POST", f"{self.base}/walls/{w}/samples", rising_samples())[0], 200)
        self.assertEqual(body("POST", f"{self.base}/walls/{w}/rainfall",
                              [{"ts": "2026-06-09T02:00:00", "rain_mm": 18}])[0], 200)
        self.assertEqual(body("POST", f"{self.base}/walls/{w}/repairs",
                              [{"date": "2023-09-01", "material": "水泥砂浆",
                                "layer_id": "L2", "x_mm": 500, "y_mm": 2000}])[0], 200)

        st, res = body("GET", f"{self.base}/walls/{w}/analysis")
        self.assertEqual(st, 200)
        self.assertEqual(res["analysis"]["verdict"], "unique_source")
        self.assertEqual(res["analysis"]["unique_source"], "rising")

        # 无理由修订 -> 400
        st, res = body("POST", f"{self.base}/walls/{w}/revisions",
                       {"kind": "adopt_conservative", "reason": "  ", "payload": {}})
        self.assertEqual(st, 400)
        # 有理由的保守修订
        st, res = body("POST", f"{self.base}/walls/{w}/revisions",
                       {"kind": "adopt_conservative",
                        "reason": "仅一个雨季观测，封版复核时采纳保守解释",
                        "payload": {}, "actor": "审核员甲"})
        self.assertEqual(st, 200)
        self.assertEqual(res["verdict"], "indeterminate_conservative")
        # 回退保守修订：恢复样本不适用，改用新墙体更贴切；这里直接验证修订账可查
        st, res = body("GET", f"{self.base}/walls/{w}/revisions")
        self.assertEqual(len(res["revisions"]), 1)
        self.assertTrue(res["revisions"][0]["reason"])

        # 封版
        st, locked = body("POST", f"{self.base}/walls/{w}/lock", {"actor": "甲"})
        self.assertEqual(st, 200)
        vid = locked["version_id"]
        self.assertTrue(locked["profile_svg"].endswith("/profile.svg"))

        # 封版后写操作 409
        st, res = body("POST", f"{self.base}/walls/{w}/samples",
                       [s("after", 1, 1, 3, "2026-06-10T13:00:00", 1, 1, 5)])
        self.assertEqual(st, 409)
        st, res = body("POST", f"{self.base}/walls/{w}/lock", {})
        self.assertEqual(st, 409)

        # 产物
        st, svg = body("GET", f"{self.base}/versions/{vid}/profile.svg")
        self.assertEqual(st, 200)
        self.assertTrue(svg.lstrip().startswith("<svg"))
        st, rec = body("GET", f"{self.base}/versions/{vid}/recompute.json")
        self.assertEqual(st, 200)
        self.assertTrue(verify(rec))
        self.assertEqual(rec["result"]["verdict"], "indeterminate_conservative")
        st, meta = body("GET", f"{self.base}/versions/{vid}")
        self.assertEqual(st, 200)
        self.assertEqual(meta["version_id"], vid)
        st, res = body("GET", f"{self.base}/versions/nope")
        self.assertEqual(st, 404)

    def test_04_gates_block_unique_via_api(self):
        # 另建墙：只有干季样本，时序门控失败
        st, res = body("POST", f"{self.base}/walls", dict(WALL, name="时序不全墙"))
        wid2 = res["wall"]["wall_id"]
        only_dry = [x for x in rising_samples() if x["sample_id"].endswith("d")]
        body("POST", f"{self.base}/walls/{wid2}/samples", only_dry)
        body("POST", f"{self.base}/walls/{wid2}/rainfall",
             [{"ts": "2026-06-09T02:00:00", "rain_mm": 18}])
        st, res = body("POST", f"{self.base}/walls/{wid2}/analysis", {})
        g = {x["code"]: x for x in res["analysis"]["gates"]}
        self.assertFalse(g["time"]["passed"])
        self.assertNotEqual(res["analysis"]["verdict"], "unique_source")

    def test_04b_declared_wet_conflict_blocks_lock_unique(self):
        # 申报 wet 但雨后 3850h（推断 dry）：经 API 复核不得返回唯一盐源，
        # 即使封版，封版产物中的裁决也必须是无法区分/多候选。
        import datetime as _dt
        st, res = body("POST", f"{self.base}/walls", dict(WALL, name="阶段冲突墙"))
        wid2 = res["wall"]["wall_id"]
        ts = (_dt.datetime(2026, 6, 9, 2) + _dt.timedelta(hours=3850)).isoformat()
        samples = rising_samples()
        for x in samples:
            if x["sample_id"].endswith("w"):
                x["ts"] = ts
                x["stage"] = "wet"
        self.assertEqual(body("POST", f"{self.base}/walls/{wid2}/samples", samples)[0], 200)
        self.assertEqual(body("POST", f"{self.base}/walls/{wid2}/rainfall",
                              [{"ts": "2026-06-09T02:00:00", "rain_mm": 18}])[0], 200)
        st, res = body("GET", f"{self.base}/walls/{wid2}/analysis")
        g = {x["code"]: x for x in res["analysis"]["gates"]}
        self.assertFalse(g["time"]["passed"])
        self.assertIn("冲突", g["time"]["detail"])
        self.assertNotEqual(res["analysis"]["verdict"], "unique_source")
        conflicted = [a for a in res["analysis"]["samples"]["active"]
                      if a["stage_conflict"]]
        self.assertEqual(len(conflicted), 6)
        self.assertTrue(all(a["stage"] == "dry" for a in conflicted))
        advice = json.dumps(res["analysis"]["next_sampling"], ensure_ascii=False)
        self.assertIn("冲突", advice)
        # 封版产物同样保留降级裁决
        st, locked = body("POST", f"{self.base}/walls/{wid2}/lock", {"actor": "乙"})
        self.assertEqual(st, 200)
        st, rec = body("GET",
                       f"{self.base}/versions/{locked['version_id']}/recompute.json")
        self.assertTrue(verify(rec))
        self.assertNotEqual(rec["result"]["verdict"], "unique_source")
        self.assertIsNone(rec["result"]["unique_source"])

    def test_05_unknown_route_and_bad_json(self):
        st, _ = body("GET", f"{self.base}/nope")
        self.assertEqual(st, 404)
        req = urllib.request.Request(f"{self.base}/walls",
                                     data=b"{bad json", method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("应返回 400")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 400)


if __name__ == "__main__":
    unittest.main()

"""SQLite 持久化：墙体模型 / 样本 / 降雨 / 修缮 / 修订账 / 封版版本 /
敷贴试验 / 试验轮次 / 试验修订账（独立保存）/ 试验确认版。"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def _uid() -> str:
    return uuid.uuid4().hex[:16]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


SCHEMA = """
CREATE TABLE IF NOT EXISTS walls (
    wall_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    model_json TEXT NOT NULL,
    locked INTEGER NOT NULL DEFAULT 0,
    locked_version_id TEXT,
    created_ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS samples (
    sample_id TEXT NOT NULL,
    wall_id TEXT NOT NULL REFERENCES walls(wall_id),
    data_json TEXT NOT NULL,
    created_ts TEXT NOT NULL,
    PRIMARY KEY (sample_id, wall_id)
);
CREATE TABLE IF NOT EXISTS rains (
    rain_id TEXT PRIMARY KEY,
    wall_id TEXT NOT NULL REFERENCES walls(wall_id),
    data_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS repairs (
    repair_id TEXT PRIMARY KEY,
    wall_id TEXT NOT NULL REFERENCES walls(wall_id),
    data_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS revisions (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    wall_id TEXT NOT NULL REFERENCES walls(wall_id),
    rev_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    reason TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    actor TEXT,
    created_ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS versions (
    version_id TEXT PRIMARY KEY,
    wall_id TEXT NOT NULL REFERENCES walls(wall_id),
    created_ts TEXT NOT NULL,
    recompute_json TEXT NOT NULL,
    svg TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trials (
    trial_id TEXT PRIMARY KEY,
    wall_id TEXT NOT NULL REFERENCES walls(wall_id),
    data_json TEXT NOT NULL,
    locked INTEGER NOT NULL DEFAULT 0,
    locked_version_id TEXT,
    created_ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trial_rounds (
    round_id TEXT NOT NULL,
    trial_id TEXT NOT NULL REFERENCES trials(trial_id),
    data_json TEXT NOT NULL,
    created_ts TEXT NOT NULL,
    PRIMARY KEY (round_id, trial_id)
);
CREATE TABLE IF NOT EXISTS trial_revisions (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    trial_id TEXT NOT NULL REFERENCES trials(trial_id),
    rev_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    reason TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    actor TEXT,
    created_ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trial_versions (
    version_id TEXT PRIMARY KEY,
    trial_id TEXT NOT NULL REFERENCES trials(trial_id),
    created_ts TEXT NOT NULL,
    confirm_json TEXT NOT NULL,
    svg TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS monitors (
    monitor_id TEXT PRIMARY KEY,
    wall_id TEXT NOT NULL REFERENCES walls(wall_id),
    data_json TEXT NOT NULL,
    locked INTEGER NOT NULL DEFAULT 0,
    locked_version_id TEXT,
    created_ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS monitor_readings (
    reading_id TEXT NOT NULL,
    monitor_id TEXT NOT NULL REFERENCES monitors(monitor_id),
    sensor_id TEXT NOT NULL,
    data_json TEXT NOT NULL,
    created_ts TEXT NOT NULL,
    PRIMARY KEY (reading_id, monitor_id)
);
CREATE TABLE IF NOT EXISTS monitor_revisions (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    monitor_id TEXT NOT NULL REFERENCES monitors(monitor_id),
    rev_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    reason TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    actor TEXT,
    created_ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS monitor_versions (
    version_id TEXT PRIMARY KEY,
    monitor_id TEXT NOT NULL REFERENCES monitors(monitor_id),
    created_ts TEXT NOT NULL,
    confirm_json TEXT NOT NULL,
    svg TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: str = ":memory:"):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate_samples_pk()
        self.conn.commit()

    def _migrate_samples_pk(self):
        """旧版本 samples 以 sample_id 为全局主键，迁移为 (sample_id, wall_id) 复合主键。"""
        cols = self.conn.execute("PRAGMA table_info(samples)").fetchall()
        pk_cols = [c["name"] for c in cols if c["pk"]]
        if pk_cols and pk_cols != ["sample_id", "wall_id"]:
            self.conn.executescript(
                "ALTER TABLE samples RENAME TO samples_legacy;"
                "CREATE TABLE samples ("
                "sample_id TEXT NOT NULL, wall_id TEXT NOT NULL REFERENCES walls(wall_id),"
                "data_json TEXT NOT NULL, created_ts TEXT NOT NULL,"
                "PRIMARY KEY (sample_id, wall_id));"
                "INSERT INTO samples(sample_id,wall_id,data_json,created_ts) "
                "SELECT sample_id,wall_id,data_json,created_ts FROM samples_legacy;"
                "DROP TABLE samples_legacy;")

    def close(self):
        self.conn.close()

    # ---------------------------------------------------------- walls
    def create_wall(self, model: Dict[str, Any]) -> Dict[str, Any]:
        wall_id = model.get("wall_id") or f"w_{_uid()}"
        if self.get_wall(wall_id):
            raise LookupError(f"墙体 {wall_id} 已存在")
        model = dict(model, wall_id=wall_id)
        self.conn.execute(
            "INSERT INTO walls(wall_id,name,model_json,created_ts) VALUES(?,?,?,?)",
            (wall_id, model.get("name", wall_id), json.dumps(model, ensure_ascii=False), _now()))
        self.conn.commit()
        return model

    def get_wall(self, wall_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM walls WHERE wall_id=?", (wall_id,)).fetchone()
        if not row:
            return None
        model = json.loads(row["model_json"])
        model["_locked"] = bool(row["locked"])
        model["_locked_version_id"] = row["locked_version_id"]
        return model

    def list_walls(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute("SELECT wall_id,name,locked,locked_version_id FROM walls").fetchall()
        return [{"wall_id": r["wall_id"], "name": r["name"], "locked": bool(r["locked"]),
                 "locked_version_id": r["locked_version_id"]} for r in rows]

    def assert_unlocked(self, wall_id: str) -> None:
        w = self.get_wall(wall_id)
        if w and w.get("_locked"):
            raise PermissionError(
                f"墙体 {wall_id} 已封版（版本 {w['_locked_version_id']}），模型、样本与参数只读；"
                "如需修订须新建墙体版本")

    # ---------------------------------------------------------- payloads
    def add_samples(self, wall_id: str, items: List[Dict[str, Any]]) -> List[str]:
        ids = []
        for it in items:
            sid = it.get("sample_id") or f"s_{_uid()}"
            it = dict(it, sample_id=sid)
            try:
                self.conn.execute(
                    "INSERT INTO samples(sample_id,wall_id,data_json,created_ts) VALUES(?,?,?,?)",
                    (sid, wall_id, json.dumps(it, ensure_ascii=False), _now()))
            except sqlite3.IntegrityError as exc:
                raise LookupError(f"墙体 {wall_id} 下样本 {sid} 已存在") from exc
            ids.append(sid)
        self.conn.commit()
        return ids

    def get_samples(self, wall_id: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute("SELECT data_json FROM samples WHERE wall_id=?",
                                 (wall_id,)).fetchall()
        return [json.loads(r["data_json"]) for r in rows]

    def add_rains(self, wall_id: str, items: List[Dict[str, Any]]) -> List[str]:
        ids = []
        for it in items:
            rid = it.get("rain_id") or f"r_{_uid()}"
            it = dict(it, rain_id=rid)
            self.conn.execute("INSERT INTO rains(rain_id,wall_id,data_json) VALUES(?,?,?)",
                              (rid, wall_id, json.dumps(it, ensure_ascii=False)))
            ids.append(rid)
        self.conn.commit()
        return ids

    def get_rains(self, wall_id: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute("SELECT data_json FROM rains WHERE wall_id=?", (wall_id,)).fetchall()
        return [json.loads(r["data_json"]) for r in rows]

    def add_repairs(self, wall_id: str, items: List[Dict[str, Any]]) -> List[str]:
        ids = []
        for it in items:
            rid = it.get("repair_id") or f"p_{_uid()}"
            it = dict(it, repair_id=rid)
            self.conn.execute("INSERT INTO repairs(repair_id,wall_id,data_json) VALUES(?,?,?)",
                              (rid, wall_id, json.dumps(it, ensure_ascii=False)))
            ids.append(rid)
        self.conn.commit()
        return ids

    def get_repairs(self, wall_id: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute("SELECT data_json FROM repairs WHERE wall_id=?",
                                 (wall_id,)).fetchall()
        return [json.loads(r["data_json"]) for r in rows]

    # ---------------------------------------------------------- revisions
    def add_revision(self, wall_id: str, kind: str, reason: str,
                     payload: Dict[str, Any], actor: Optional[str]) -> Dict[str, Any]:
        rev_id = f"rev_{_uid()}"
        cur = self.conn.execute(
            "INSERT INTO revisions(wall_id,rev_id,kind,reason,payload_json,actor,created_ts)"
            " VALUES(?,?,?,?,?,?,?)",
            (wall_id, rev_id, kind, reason, json.dumps(payload, ensure_ascii=False), actor, _now()))
        self.conn.commit()
        return {"seq": cur.lastrowid, "rev_id": rev_id, "wall_id": wall_id, "kind": kind,
                "reason": reason, "payload": payload, "actor": actor, "created_ts": _now()}

    def get_revisions(self, wall_id: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM revisions WHERE wall_id=? ORDER BY seq", (wall_id,)).fetchall()
        return [{"seq": r["seq"], "rev_id": r["rev_id"], "wall_id": wall_id,
                 "kind": r["kind"], "reason": r["reason"], "actor": r["actor"],
                 "created_ts": r["created_ts"],
                 "payload": json.loads(r["payload_json"])} for r in rows]

    # ---------------------------------------------------------- versions
    def save_version(self, version_id: str, wall_id: str,
                     recompute: Dict[str, Any], svg: str) -> None:
        self.conn.execute(
            "INSERT INTO versions(version_id,wall_id,created_ts,recompute_json,svg)"
            " VALUES(?,?,?,?,?)",
            (version_id, wall_id, _now(),
             json.dumps(recompute, ensure_ascii=False), svg))
        self.conn.execute("UPDATE walls SET locked=1, locked_version_id=? WHERE wall_id=?",
                          (version_id, wall_id))
        self.conn.commit()

    def get_version(self, version_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM versions WHERE version_id=?",
                                (version_id,)).fetchone()
        if not row:
            return None
        return {"version_id": version_id, "wall_id": row["wall_id"],
                "created_ts": row["created_ts"], "svg": row["svg"],
                "recompute": json.loads(row["recompute_json"])}

    def list_versions(self, wall_id: str) -> List[Dict[str, str]]:
        rows = self.conn.execute(
            "SELECT version_id,created_ts FROM versions WHERE wall_id=? ORDER BY created_ts",
            (wall_id,)).fetchall()
        return [{"version_id": r["version_id"], "created_ts": r["created_ts"]} for r in rows]

    def bundle(self, wall_id: str) -> Dict[str, List[Dict[str, Any]]]:
        return {"samples": self.get_samples(wall_id), "rains": self.get_rains(wall_id),
                "repairs": self.get_repairs(wall_id), "revisions": self.get_revisions(wall_id)}

    # ---------------------------------------------------------- trials
    def create_trial(self, wall_id: str, model: Dict[str, Any]) -> Dict[str, Any]:
        trial_id = model.get("trial_id") or f"t_{_uid()}"
        if self.get_trial(trial_id):
            raise LookupError(f"试验 {trial_id} 已存在")
        model = dict(model, trial_id=trial_id, wall_id=wall_id)
        self.conn.execute(
            "INSERT INTO trials(trial_id,wall_id,data_json,created_ts) VALUES(?,?,?,?)",
            (trial_id, wall_id, json.dumps(model, ensure_ascii=False), _now()))
        self.conn.commit()
        return model

    def get_trial(self, trial_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM trials WHERE trial_id=?",
                                (trial_id,)).fetchone()
        if not row:
            return None
        model = json.loads(row["data_json"])
        model["_locked"] = bool(row["locked"])
        model["_locked_version_id"] = row["locked_version_id"]
        return model

    def list_trials(self, wall_id: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT trial_id,data_json,locked,locked_version_id FROM trials"
            " WHERE wall_id=? ORDER BY created_ts", (wall_id,)).fetchall()
        out = []
        for r in rows:
            m = json.loads(r["data_json"])
            out.append({"trial_id": r["trial_id"], "name": m.get("name"),
                        "source_version_id": m.get("source_version_id"),
                        "locked": bool(r["locked"]),
                        "locked_version_id": r["locked_version_id"]})
        return out

    def assert_trial_unlocked(self, trial_id: str) -> None:
        t = self.get_trial(trial_id)
        if t and t.get("_locked"):
            raise PermissionError(
                f"试验 {trial_id} 已确认封版（版本 {t['_locked_version_id']}），"
                "轮次、绑定与修订只读；如需变更须新建试验")

    def add_rounds(self, trial_id: str, items: List[Dict[str, Any]]) -> List[str]:
        ids = []
        for it in items:
            rid = it.get("round_id") or f"rd_{_uid()}"
            it = dict(it, round_id=rid)
            try:
                self.conn.execute(
                    "INSERT INTO trial_rounds(round_id,trial_id,data_json,created_ts)"
                    " VALUES(?,?,?,?)",
                    (rid, trial_id, json.dumps(it, ensure_ascii=False), _now()))
            except sqlite3.IntegrityError as exc:
                raise LookupError(f"试验 {trial_id} 下轮次 {rid} 已存在") from exc
            ids.append(rid)
        self.conn.commit()
        return ids

    def get_rounds(self, trial_id: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT data_json FROM trial_rounds WHERE trial_id=?", (trial_id,)).fetchall()
        return [json.loads(r["data_json"]) for r in rows]

    # ---------------------------------------------------------- trial revisions
    def add_trial_revision(self, trial_id: str, kind: str, reason: str,
                           payload: Dict[str, Any], actor: Optional[str]) -> Dict[str, Any]:
        rev_id = f"trev_{_uid()}"
        cur = self.conn.execute(
            "INSERT INTO trial_revisions(trial_id,rev_id,kind,reason,payload_json,"
            "actor,created_ts) VALUES(?,?,?,?,?,?,?)",
            (trial_id, rev_id, kind, reason, json.dumps(payload, ensure_ascii=False),
             actor, _now()))
        self.conn.commit()
        return {"seq": cur.lastrowid, "rev_id": rev_id, "trial_id": trial_id,
                "kind": kind, "reason": reason, "payload": payload, "actor": actor,
                "created_ts": _now()}

    def get_trial_revisions(self, trial_id: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM trial_revisions WHERE trial_id=? ORDER BY seq",
            (trial_id,)).fetchall()
        return [{"seq": r["seq"], "rev_id": r["rev_id"], "trial_id": trial_id,
                 "kind": r["kind"], "reason": r["reason"], "actor": r["actor"],
                 "created_ts": r["created_ts"],
                 "payload": json.loads(r["payload_json"])} for r in rows]

    # ---------------------------------------------------------- trial versions
    def save_trial_version(self, version_id: str, trial_id: str,
                           confirm: Dict[str, Any], svg: str) -> None:
        self.conn.execute(
            "INSERT INTO trial_versions(version_id,trial_id,created_ts,confirm_json,svg)"
            " VALUES(?,?,?,?,?)",
            (version_id, trial_id, _now(),
             json.dumps(confirm, ensure_ascii=False), svg))
        self.conn.execute("UPDATE trials SET locked=1, locked_version_id=?"
                          " WHERE trial_id=?", (version_id, trial_id))
        self.conn.commit()

    def get_trial_version(self, version_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM trial_versions WHERE version_id=?",
                                (version_id,)).fetchone()
        if not row:
            return None
        return {"version_id": version_id, "trial_id": row["trial_id"],
                "created_ts": row["created_ts"], "svg": row["svg"],
                "confirm": json.loads(row["confirm_json"])}

    def list_trial_versions(self, trial_id: str) -> List[Dict[str, str]]:
        rows = self.conn.execute(
            "SELECT version_id,created_ts FROM trial_versions WHERE trial_id=?"
            " ORDER BY created_ts", (trial_id,)).fetchall()
        return [{"version_id": r["version_id"], "created_ts": r["created_ts"]}
                for r in rows]

    def trial_bundle(self, trial_id: str) -> Dict[str, List[Dict[str, Any]]]:
        return {"rounds": self.get_rounds(trial_id),
                "revisions": self.get_trial_revisions(trial_id)}

    # ---------------------------------------------------------- microclimate monitors
    def create_monitor(self, wall_id: str, model: Dict[str, Any]) -> Dict[str, Any]:
        monitor_id = model.get("monitor_id") or f"m_{_uid()}"
        if self.get_monitor(monitor_id):
            raise LookupError(f"监测计划 {monitor_id} 已存在")
        model = dict(model, monitor_id=monitor_id, wall_id=wall_id)
        self.conn.execute(
            "INSERT INTO monitors(monitor_id,wall_id,data_json,created_ts)"
            " VALUES(?,?,?,?)",
            (monitor_id, wall_id, json.dumps(model, ensure_ascii=False), _now()))
        self.conn.commit()
        return model

    def get_monitor(self, monitor_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM monitors WHERE monitor_id=?",
                                (monitor_id,)).fetchone()
        if not row:
            return None
        model = json.loads(row["data_json"])
        model["_locked"] = bool(row["locked"])
        model["_locked_version_id"] = row["locked_version_id"]
        return model

    def list_monitors(self, wall_id: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT monitor_id,data_json,locked,locked_version_id FROM monitors"
            " WHERE wall_id=? ORDER BY created_ts", (wall_id,)).fetchall()
        out = []
        for r in rows:
            m = json.loads(r["data_json"])
            out.append({"monitor_id": r["monitor_id"], "name": m.get("name"),
                        "source_version_id": m.get("source_version_id"),
                        "source_kind": m.get("source_kind", "review"),
                        "n_zones": len(m.get("zones") or []),
                        "n_sensors": len(m.get("sensors") or []),
                        "locked": bool(r["locked"]),
                        "locked_version_id": r["locked_version_id"]})
        return out

    def assert_monitor_unlocked(self, monitor_id: str) -> None:
        m = self.get_monitor(monitor_id)
        if m and m.get("_locked"):
            raise PermissionError(
                f"监测计划 {monitor_id} 已确认封版（版本 {m['_locked_version_id']}），"
                "传感器、读数、规则与修订只读；如需变更须新建监测计划")

    def add_readings(self, monitor_id: str, items: List[Dict[str, Any]]) -> List[str]:
        ids = []
        for it in items:
            rid = it.get("reading_id") or f"rdm_{_uid()}"
            it = dict(it, reading_id=rid)
            try:
                self.conn.execute(
                    "INSERT INTO monitor_readings(reading_id,monitor_id,sensor_id,"
                    "data_json,created_ts) VALUES(?,?,?,?,?)",
                    (rid, monitor_id, it.get("sensor_id"),
                     json.dumps(it, ensure_ascii=False), _now()))
            except sqlite3.IntegrityError as exc:
                raise LookupError(f"监测计划 {monitor_id} 下读数 {rid} 已存在") from exc
            ids.append(rid)
        self.conn.commit()
        return ids

    def get_readings(self, monitor_id: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT data_json FROM monitor_readings WHERE monitor_id=?",
            (monitor_id,)).fetchall()
        return [json.loads(r["data_json"]) for r in rows]

    # ---------------------------------------------------------- monitor revisions
    def add_monitor_revision(self, monitor_id: str, kind: str, reason: str,
                             payload: Dict[str, Any],
                             actor: Optional[str]) -> Dict[str, Any]:
        rev_id = f"mrev_{_uid()}"
        cur = self.conn.execute(
            "INSERT INTO monitor_revisions(monitor_id,rev_id,kind,reason,payload_json,"
            "actor,created_ts) VALUES(?,?,?,?,?,?,?)",
            (monitor_id, rev_id, kind, reason,
             json.dumps(payload, ensure_ascii=False), actor, _now()))
        self.conn.commit()
        return {"seq": cur.lastrowid, "rev_id": rev_id, "monitor_id": monitor_id,
                "kind": kind, "reason": reason, "payload": payload, "actor": actor,
                "created_ts": _now()}

    def get_monitor_revisions(self, monitor_id: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM monitor_revisions WHERE monitor_id=? ORDER BY seq",
            (monitor_id,)).fetchall()
        return [{"seq": r["seq"], "rev_id": r["rev_id"], "monitor_id": monitor_id,
                 "kind": r["kind"], "reason": r["reason"], "actor": r["actor"],
                 "created_ts": r["created_ts"],
                 "payload": json.loads(r["payload_json"])} for r in rows]

    # ---------------------------------------------------------- monitor versions
    def save_monitor_version(self, version_id: str, monitor_id: str,
                             confirm: Dict[str, Any], svg: str) -> None:
        self.conn.execute(
            "INSERT INTO monitor_versions(version_id,monitor_id,created_ts,"
            "confirm_json,svg) VALUES(?,?,?,?,?)",
            (version_id, monitor_id, _now(),
             json.dumps(confirm, ensure_ascii=False), svg))
        self.conn.execute("UPDATE monitors SET locked=1, locked_version_id=? "
                          "WHERE monitor_id=?", (version_id, monitor_id))
        self.conn.commit()

    def get_monitor_version(self, version_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM monitor_versions WHERE version_id=?",
                                (version_id,)).fetchone()
        if not row:
            return None
        return {"version_id": version_id, "monitor_id": row["monitor_id"],
                "created_ts": row["created_ts"], "svg": row["svg"],
                "confirm": json.loads(row["confirm_json"])}

    def list_monitor_versions(self, monitor_id: str) -> List[Dict[str, str]]:
        rows = self.conn.execute(
            "SELECT version_id,created_ts FROM monitor_versions WHERE monitor_id=? "
            "ORDER BY created_ts", (monitor_id,)).fetchall()
        return [{"version_id": r["version_id"], "created_ts": r["created_ts"]}
                for r in rows]

    def monitor_bundle(self, monitor_id: str) -> Dict[str, List[Dict[str, Any]]]:
        return {"readings": self.get_readings(monitor_id),
                "revisions": self.get_monitor_revisions(monitor_id)}

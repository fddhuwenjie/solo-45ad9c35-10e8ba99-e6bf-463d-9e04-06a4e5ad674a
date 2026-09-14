"""SQLite 持久化：墙体模型 / 样本 / 降雨 / 修缮 / 修订账 / 封版版本。"""
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

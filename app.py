#!/usr/bin/env python3
"""Airline disruption recovery engine using standard-library SQLite and HTTP."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, time, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

PORT = 8202
ROLES = {"viewer", "scheduler", "ops_manager", "auditor", "atc"}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details: Any = None):
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details


class WriteFault(Exception):
    """测试钩子：模拟事项处理到某一步时写入失败，便于按事项号重试。"""

    def __init__(self, item_no: str, step: str):
        super().__init__(f"write failed at {step} for {item_no}")
        self.item_no, self.step = item_no, step


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | None = None) -> str:
    return (value or utcnow()).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_time(value: str | None) -> datetime:
    if not value:
        raise ApiError(400, "time_required", "必须提供 ISO 8601 时间")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ApiError(400, "invalid_time", f"时间格式错误: {value}") from exc
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def parse_clock(value: str) -> time:
    try:
        return time.fromisoformat(value)
    except ValueError as exc:
        raise ApiError(400, "invalid_clock", f"时刻格式应为 HH:MM: {value}") from exc


def overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    return a_start < b_end and b_start < a_end


class Repository:
    def __init__(self, db_path: str | Path):
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._init()

    @contextmanager
    def tx(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def _init(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS airports(code TEXT PRIMARY KEY, country TEXT NOT NULL, curfew_start TEXT NOT NULL, curfew_end TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS aircraft(id TEXT PRIMARY KEY, model TEXT NOT NULL, maintenance_due TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active');
            CREATE TABLE IF NOT EXISTS crew(id TEXT PRIMARY KEY, name TEXT NOT NULL, base TEXT NOT NULL, duty_start TEXT NOT NULL, max_duty_minutes INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'active');
            CREATE TABLE IF NOT EXISTS permits(id INTEGER PRIMARY KEY AUTOINCREMENT, origin TEXT NOT NULL, destination TEXT NOT NULL, valid_from TEXT NOT NULL, valid_to TEXT NOT NULL, curfew_exempt INTEGER NOT NULL DEFAULT 0, UNIQUE(origin,destination,valid_from,valid_to));
            CREATE TABLE IF NOT EXISTS flights(
                id INTEGER PRIMARY KEY AUTOINCREMENT, flight_no TEXT NOT NULL UNIQUE, origin TEXT NOT NULL, destination TEXT NOT NULL,
                std TEXT NOT NULL, sta TEXT NOT NULL, aircraft_id TEXT NOT NULL REFERENCES aircraft(id), crew_id TEXT NOT NULL REFERENCES crew(id),
                passenger_count INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'scheduled', delay_minutes INTEGER NOT NULL DEFAULT 0,
                revision INTEGER NOT NULL DEFAULT 1, cancel_reason TEXT, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS disruptions(id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, resource_id TEXT NOT NULL, starts_at TEXT NOT NULL, ends_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS recovery_plans(
                id INTEGER PRIMARY KEY AUTOINCREMENT, disruption_id INTEGER NOT NULL REFERENCES disruptions(id), name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'draft', revision INTEGER NOT NULL DEFAULT 1, score_json TEXT, metrics_json TEXT,
                created_by TEXT NOT NULL, created_at TEXT NOT NULL, locked_at TEXT, locked_by TEXT
            );
            CREATE TABLE IF NOT EXISTS assignments(
                id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER NOT NULL REFERENCES recovery_plans(id) ON DELETE CASCADE,
                flight_id INTEGER NOT NULL REFERENCES flights(id), aircraft_id TEXT NOT NULL REFERENCES aircraft(id), crew_id TEXT NOT NULL REFERENCES crew(id),
                new_std TEXT NOT NULL, new_sta TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'planned', delay_minutes INTEGER NOT NULL DEFAULT 0,
                missed_connections INTEGER NOT NULL DEFAULT 0, UNIQUE(plan_id,flight_id)
            );
            CREATE TABLE IF NOT EXISTS audit_log(id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER, actor TEXT NOT NULL, role TEXT NOT NULL, action TEXT NOT NULL, detail_json TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS slot_pools(
                id INTEGER PRIMARY KEY AUTOINCREMENT, airport_code TEXT NOT NULL, window_start TEXT NOT NULL, window_end TEXT NOT NULL,
                capacity INTEGER NOT NULL, UNIQUE(airport_code,window_start,window_end)
            );
            CREATE TABLE IF NOT EXISTS slot_occupancy(
                id INTEGER PRIMARY KEY AUTOINCREMENT, pool_id INTEGER NOT NULL REFERENCES slot_pools(id),
                item_id INTEGER NOT NULL REFERENCES approval_items(id), purpose TEXT NOT NULL DEFAULT 'landing', UNIQUE(item_id,purpose)
            );
            CREATE TABLE IF NOT EXISTS approval_items(
                id INTEGER PRIMARY KEY AUTOINCREMENT, item_no TEXT NOT NULL UNIQUE,
                plan_id INTEGER NOT NULL REFERENCES recovery_plans(id), revision INTEGER NOT NULL,
                assignment_id INTEGER NOT NULL REFERENCES assignments(id), flight_id INTEGER NOT NULL REFERENCES flights(id),
                status TEXT NOT NULL DEFAULT 'pending_atc',
                decision TEXT, decided_at TEXT, decided_by TEXT, atc_ref TEXT, withdraw_reason TEXT,
                snapshot_json TEXT NOT NULL, check_codes_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_approval_plan ON approval_items(plan_id,revision);
            CREATE INDEX IF NOT EXISTS idx_approval_status ON approval_items(status);
            CREATE TABLE IF NOT EXISTS approval_steps(
                id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INTEGER NOT NULL REFERENCES approval_items(id),
                step TEXT NOT NULL, result TEXT NOT NULL DEFAULT 'done', detail_json TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(item_id,step)
            );
            """
        )

    @staticmethod
    def audit(conn: sqlite3.Connection, plan_id: int | None, actor: str, role: str, action: str, detail: dict[str, Any]) -> None:
        conn.execute("INSERT INTO audit_log(plan_id,actor,role,action,detail_json,created_at) VALUES(?,?,?,?,?,?)",
                     (plan_id, actor, role, action, json.dumps(detail, ensure_ascii=False, sort_keys=True), iso()))


class AirlineRecoveryService:
    def __init__(self, db_path: str | Path):
        self.repo = Repository(db_path)
        # 测试钩子：{item_no: {step: True}}，在对应步骤提交前抛出 WriteFault
        self.fault_before_step: dict[str, dict[str, str]] = {}

    @staticmethod
    def identity(headers: Any) -> tuple[str, str]:
        actor, role = headers.get("X-User-Id", "").strip(), headers.get("X-Role", "").strip()
        if not actor or role not in ROLES:
            raise ApiError(401, "unauthorized", "需要 X-User-Id 和有效 X-Role")
        return actor, role

    @staticmethod
    def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row else None

    def seed_airport(self, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "ops_manager":
            raise ApiError(403, "seed_forbidden", "只有运行经理可以维护机场数据")
        code = str(body.get("code", "")).upper().strip()
        country = str(body.get("country", "")).upper().strip()
        if not code or not country:
            raise ApiError(400, "missing_fields", "code 和 country 必填")
        start = str(body.get("curfew_start", "23:00")); end = str(body.get("curfew_end", "06:00"))
        parse_clock(start); parse_clock(end)
        with self.repo.tx() as conn:
            conn.execute("INSERT OR REPLACE INTO airports(code,country,curfew_start,curfew_end) VALUES(?,?,?,?)", (code, country, start, end))
            return dict(conn.execute("SELECT * FROM airports WHERE code=?", (code,)).fetchone())

    def seed_aircraft(self, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "ops_manager": raise ApiError(403, "seed_forbidden", "只有运行经理可以维护飞机")
        ident, model = str(body.get("id", "")).strip(), str(body.get("model", "")).strip()
        if not ident or not model: raise ApiError(400, "missing_fields", "id 和 model 必填")
        due = iso(parse_time(body.get("maintenance_due")))
        with self.repo.tx() as conn:
            conn.execute("INSERT OR REPLACE INTO aircraft(id,model,maintenance_due,status) VALUES(?,?,?,?)", (ident, model, due, body.get("status", "active")))
            return dict(conn.execute("SELECT * FROM aircraft WHERE id=?", (ident,)).fetchone())

    def seed_crew(self, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "ops_manager": raise ApiError(403, "seed_forbidden", "只有运行经理可以维护机组")
        ident, name, base = str(body.get("id", "")).strip(), str(body.get("name", "")).strip(), str(body.get("base", "")).upper().strip()
        duty = parse_time(body.get("duty_start")); maximum = body.get("max_duty_minutes")
        if not ident or not name or not base or not isinstance(maximum, int) or maximum <= 0:
            raise ApiError(400, "invalid_crew", "id、name、base 和正整数 max_duty_minutes 必填")
        with self.repo.tx() as conn:
            conn.execute("INSERT OR REPLACE INTO crew(id,name,base,duty_start,max_duty_minutes,status) VALUES(?,?,?,?,?,?)", (ident, name, base, iso(duty), maximum, body.get("status", "active")))
            return dict(conn.execute("SELECT * FROM crew WHERE id=?", (ident,)).fetchone())

    def create_permit(self, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "ops_manager": raise ApiError(403, "permit_forbidden", "只有运行经理可以维护航线许可")
        origin, destination = str(body.get("origin", "")).upper(), str(body.get("destination", "")).upper()
        if not origin or not destination: raise ApiError(400, "missing_fields", "origin 和 destination 必填")
        valid_from, valid_to = parse_time(body.get("valid_from")), parse_time(body.get("valid_to"))
        if valid_to <= valid_from: raise ApiError(400, "invalid_permit", "许可结束时间必须晚于开始时间")
        with self.repo.tx() as conn:
            try:
                cur = conn.execute("INSERT INTO permits(origin,destination,valid_from,valid_to,curfew_exempt) VALUES(?,?,?,?,?)",
                                   (origin, destination, iso(valid_from), iso(valid_to), int(bool(body.get("curfew_exempt")))))
            except sqlite3.IntegrityError as exc:
                raise ApiError(409, "permit_exists", "相同航线与有效期的许可已存在") from exc
            return dict(conn.execute("SELECT * FROM permits WHERE id=?", (cur.lastrowid,)).fetchone())

    def create_flight(self, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"scheduler", "ops_manager"}: raise ApiError(403, "flight_forbidden", "当前角色不能创建航班")
        required = ("flight_no", "origin", "destination", "std", "sta", "aircraft_id", "crew_id")
        if any(not body.get(k) for k in required): raise ApiError(400, "missing_fields", f"缺少字段: {', '.join(k for k in required if not body.get(k))}")
        std, sta = parse_time(body["std"]), parse_time(body["sta"])
        if sta <= std: raise ApiError(400, "invalid_times", "到达时间必须晚于起飞时间")
        passengers = body.get("passenger_count", 0)
        if not isinstance(passengers, int) or passengers < 0: raise ApiError(400, "invalid_passengers", "passenger_count 必须是非负整数")
        with self.repo.tx() as conn:
            for table, ident in (("aircraft", body["aircraft_id"]), ("crew", body["crew_id"])):
                row = conn.execute(f"SELECT status FROM {table} WHERE id=?", (ident,)).fetchone()
                if not row or row["status"] != "active": raise ApiError(409, "resource_unavailable", f"{table} {ident} 不可用")
            try:
                cur = conn.execute("""INSERT INTO flights(flight_no,origin,destination,std,sta,aircraft_id,crew_id,passenger_count,updated_at)
                                      VALUES(?,?,?,?,?,?,?,?,?)""",
                                   (body["flight_no"].upper(), body["origin"].upper(), body["destination"].upper(), iso(std), iso(sta),
                                    body["aircraft_id"], body["crew_id"], passengers, iso()))
            except sqlite3.IntegrityError as exc:
                raise ApiError(409, "flight_exists", "航班号已存在") from exc
            return dict(conn.execute("SELECT * FROM flights WHERE id=?", (cur.lastrowid,)).fetchone())

    def create_disruption(self, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"scheduler", "ops_manager"}: raise ApiError(403, "disruption_forbidden", "当前角色不能登记中断")
        kind, resource = str(body.get("kind", "")).strip(), str(body.get("resource_id", "")).strip()
        if kind not in {"airport_closure", "aircraft_fault", "crew_timeout"} or not resource:
            raise ApiError(400, "invalid_disruption", "kind 或 resource_id 无效")
        start, end = parse_time(body.get("starts_at")), parse_time(body.get("ends_at"))
        if end <= start: raise ApiError(400, "invalid_times", "中断结束时间必须晚于开始时间")
        with self.repo.tx() as conn:
            cur = conn.execute("INSERT INTO disruptions(kind,resource_id,starts_at,ends_at,created_at) VALUES(?,?,?,?,?)",
                               (kind, resource.upper() if kind == "airport_closure" else resource, iso(start), iso(end), iso()))
            return dict(conn.execute("SELECT * FROM disruptions WHERE id=?", (cur.lastrowid,)).fetchone())

    def create_plan(self, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"scheduler", "ops_manager"}: raise ApiError(403, "plan_forbidden", "当前角色不能创建恢复方案")
        disruption_id, name = body.get("disruption_id"), str(body.get("name", "")).strip()
        assignments = body.get("assignments", [])
        if not isinstance(disruption_id, int) or not name or not isinstance(assignments, list):
            raise ApiError(400, "invalid_plan", "disruption_id、name 和 assignments 必填")
        submit = bool(body.get("submit_for_approval"))
        with self.repo.tx() as conn:
            if not conn.execute("SELECT 1 FROM disruptions WHERE id=?", (disruption_id,)).fetchone():
                raise ApiError(404, "disruption_not_found", "中断事件不存在")
            cur = conn.execute("INSERT INTO recovery_plans(disruption_id,name,status,created_by,created_at) VALUES(?,?,?,?,?)",
                               (disruption_id, name, "submitted" if submit else "draft", actor, iso()))
            plan_id = cur.lastrowid
            for item in assignments:
                self._insert_assignment(conn, plan_id, item, replace=False)
            if submit:
                self._register_pending_items(conn, plan_id, 1, actor, role, "plan_submitted")
            else:
                Repository.audit(conn, plan_id, actor, role, "plan_created", {"disruption_id": disruption_id, "assignment_count": len(assignments)})
            return self.get_plan(plan_id)

    def submit_plan(self, plan_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        """提交方案：按当前版本为每个航班调整登记待批事项。"""
        if role not in {"scheduler", "ops_manager"}: raise ApiError(403, "submit_forbidden", "当前角色不能提交方案")
        expected = body.get("expected_revision")
        if not isinstance(expected, int): raise ApiError(400, "revision_required", "expected_revision 必须是整数")
        with self.repo.tx() as conn:
            plan = conn.execute("SELECT * FROM recovery_plans WHERE id=?", (plan_id,)).fetchone()
            if not plan: raise ApiError(404, "plan_not_found", "方案不存在")
            if plan["revision"] != expected: raise ApiError(409, "revision_conflict", "方案版本已变化")
            if plan["status"] == "locked": raise ApiError(409, "plan_locked", "已锁定方案不能提交")
            if plan["status"] == "submitted": return self.get_plan(plan_id)
            rows = conn.execute("SELECT id FROM assignments WHERE plan_id=? AND status!='canceled'", (plan_id,)).fetchall()
            if not rows: raise ApiError(409, "empty_plan", "方案没有飞行调整，不能提交")
            conn.execute("UPDATE recovery_plans SET status='submitted' WHERE id=?", (plan_id,))
            self._register_pending_items(conn, plan_id, plan["revision"], actor, role, "plan_submitted")
            return self.get_plan(plan_id)

    def _register_pending_items(self, conn: sqlite3.Connection, plan_id: int, revision: int, actor: str, role: str, action: str) -> None:
        rows = conn.execute("""SELECT a.id,a.flight_id,a.new_std,a.new_sta,a.aircraft_id,a.crew_id,a.status,a.delay_minutes,
                                      f.flight_no,f.origin,f.destination
                               FROM assignments a JOIN flights f ON f.id=a.flight_id
                               WHERE a.plan_id=? AND a.status!='canceled' ORDER BY a.id""", (plan_id,)).fetchall()
        count = 0
        for row in rows:
            item_no = f"P{plan_id:06d}-R{revision}-{count + 1:03d}"
            snapshot = {"flight_no": row["flight_no"], "origin": row["origin"], "destination": row["destination"],
                        "new_std": row["new_std"], "new_sta": row["new_sta"], "aircraft_id": row["aircraft_id"], "crew_id": row["crew_id"]}
            conn.execute("""INSERT INTO approval_items(item_no,plan_id,revision,assignment_id,flight_id,snapshot_json,created_at,updated_at)
                            VALUES(?,?,?,?,?,?,?,?)""",
                         (item_no, plan_id, revision, row["id"], row["flight_id"], json.dumps(snapshot, ensure_ascii=False), iso(), iso()))
            count += 1
        Repository.audit(conn, plan_id, actor, role, action, {"revision": revision, "item_count": count})

    @staticmethod
    def _void_items_for_revision(conn: sqlite3.Connection, plan_id: int, old_revision: int, actor: str, role: str, reason: str) -> int:
        """方案版本一变：旧版本未执行的批复立即失效并释放其额度。已执行部分保留。"""
        rows = conn.execute("SELECT id FROM approval_items WHERE plan_id=? AND revision=? AND status IN ('pending_atc','held')",
                            (plan_id, old_revision)).fetchall()
        if not rows:
            return 0
        ids = [r["id"] for r in rows]
        conn.execute(f"DELETE FROM slot_occupancy WHERE item_id IN ({','.join('?' * len(ids))})", ids)
        note = f"方案版本变更(R{old_revision}→R{old_revision + 1})，{reason}"
        conn.execute(f"UPDATE approval_items SET status='void',withdraw_reason=?,updated_at=? WHERE id IN ({','.join('?' * len(ids))})",
                     [note, iso()] + ids)
        conn.executemany(
            "INSERT OR IGNORE INTO approval_steps(item_id,step,result,detail_json,created_at) VALUES(?,?,?,?,?)",
            [(i, "void", "done", json.dumps({"reason": note}, ensure_ascii=False), iso()) for i in ids])
        Repository.audit(conn, plan_id, actor, role, "approvals_voided", {"revision": old_revision, "count": len(ids), "reason": reason})
        return len(ids)

    def _insert_assignment(self, conn: sqlite3.Connection, plan_id: int, item: dict[str, Any], replace: bool) -> None:
        required = ("flight_id", "aircraft_id", "crew_id", "new_std", "new_sta")
        if any(item.get(k) in (None, "") for k in required): raise ApiError(400, "invalid_assignment", f"飞行调整缺少字段: {', '.join(k for k in required if item.get(k) in (None, ''))}")
        plan = conn.execute("SELECT * FROM recovery_plans WHERE id=?", (plan_id,)).fetchone()
        if not plan: raise ApiError(404, "plan_not_found", "方案不存在")
        if plan["status"] not in ("draft", "submitted"): raise ApiError(409, "plan_locked", "已锁定方案不能修改")
        std, sta = parse_time(item["new_std"]), parse_time(item["new_sta"])
        if sta <= std: raise ApiError(400, "invalid_times", "新到达时间必须晚于新起飞时间")
        flight = conn.execute("SELECT * FROM flights WHERE id=?", (item["flight_id"],)).fetchone()
        if not flight: raise ApiError(404, "flight_not_found", "航班不存在")
        if flight["status"] == "canceled" and item.get("status", "planned") != "canceled":
            raise ApiError(409, "canceled_flight", "已取消航班不能安排执行")
        delay = int((std - parse_time(flight["std"])).total_seconds() // 60)
        missed = int(item.get("missed_connections", 0))
        if missed < 0: raise ApiError(400, "invalid_connections", "missed_connections 不能为负")
        try:
            if replace:
                conn.execute("""UPDATE assignments SET aircraft_id=?,crew_id=?,new_std=?,new_sta=?,status=?,delay_minutes=?,missed_connections=?
                                WHERE plan_id=? AND flight_id=?""",
                             (item["aircraft_id"], item["crew_id"], iso(std), iso(sta), item.get("status", "planned"), delay, missed, plan_id, item["flight_id"]))
                if conn.execute("SELECT changes()").fetchone()[0] == 0:
                    raise KeyError
            else:
                conn.execute("""INSERT INTO assignments(plan_id,flight_id,aircraft_id,crew_id,new_std,new_sta,status,delay_minutes,missed_connections)
                                VALUES(?,?,?,?,?,?,?,?,?)""",
                             (plan_id, item["flight_id"], item["aircraft_id"], item["crew_id"], iso(std), iso(sta), item.get("status", "planned"), delay, missed))
        except sqlite3.IntegrityError as exc:
            raise ApiError(409, "assignment_conflict", "方案中该航班已存在或资源无效") from exc
        except KeyError as exc:
            raise ApiError(404, "assignment_not_found", "待替换的航班调整不存在") from exc

    def add_assignment(self, plan_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"scheduler", "ops_manager"}: raise ApiError(403, "assignment_forbidden", "当前角色不能修改方案")
        expected = body.get("expected_revision")
        if not isinstance(expected, int): raise ApiError(400, "revision_required", "expected_revision 必须是整数")
        with self.repo.tx() as conn:
            plan = conn.execute("SELECT * FROM recovery_plans WHERE id=?", (plan_id,)).fetchone()
            if not plan: raise ApiError(404, "plan_not_found", "方案不存在")
            if plan["status"] not in ("draft", "submitted"): raise ApiError(409, "plan_locked", "已锁定方案不能修改")
            if plan["revision"] != expected: raise ApiError(409, "revision_conflict", "方案已被其他人更新")
            self._insert_assignment(conn, plan_id, body, replace=True)
            old_revision = plan["revision"]
            conn.execute("UPDATE recovery_plans SET revision=revision+1 WHERE id=?", (plan_id,))
            voided = self._void_items_for_revision(conn, plan_id, old_revision, actor, role, "方案改派")
            re_registered = 0
            if plan["status"] == "submitted":
                self._register_pending_items(conn, plan_id, old_revision + 1, actor, role, "plan_resubmitted")
                re_registered = conn.execute("SELECT COUNT(*) c FROM approval_items WHERE plan_id=? AND revision=?", (plan_id, old_revision + 1)).fetchone()["c"]
            Repository.audit(conn, plan_id, actor, role, "assignment_reassigned",
                             {"flight_id": body.get("flight_id"), "voided": voided, "re_registered": re_registered})
            return self.get_plan(plan_id)

    # ------------------------------------------------------------------
    # 临时起降额度（夜间换场机场的 slot pool）
    # ------------------------------------------------------------------
    def create_slot_pool(self, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "ops_manager": raise ApiError(403, "slot_forbidden", "只有运行经理可以设置临时起降额度")
        airport = str(body.get("airport_code", "")).upper().strip()
        start, end = parse_time(body.get("window_start")), parse_time(body.get("window_end"))
        capacity = body.get("capacity")
        if not airport or not isinstance(capacity, int) or capacity < 0:
            raise ApiError(400, "invalid_slot_pool", "airport_code 和非负整数 capacity 必填")
        if end <= start: raise ApiError(400, "invalid_window", "额度窗口结束必须晚于开始")
        if not self.repo.conn.execute("SELECT 1 FROM airports WHERE code=?", (airport,)).fetchone():
            raise ApiError(404, "airport_unknown", f"机场 {airport} 不存在")
        with self.repo.tx() as conn:
            try:
                cur = conn.execute("INSERT INTO slot_pools(airport_code,window_start,window_end,capacity) VALUES(?,?,?,?)",
                                   (airport, iso(start), iso(end), capacity))
            except sqlite3.IntegrityError as exc:
                raise ApiError(409, "slot_pool_exists", "该机场与时间窗口的额度池已存在") from exc
            Repository.audit(conn, None, actor, role, "slot_pool_created", {"pool_id": cur.lastrowid, "airport": airport, "capacity": capacity})
            return self._slot_pool_payload(conn, cur.lastrowid)

    @staticmethod
    def _find_slot_pool(conn: sqlite3.Connection, airport: str, at_iso: str) -> sqlite3.Row | None:
        return conn.execute("""SELECT * FROM slot_pools WHERE airport_code=? AND window_start<=? AND window_end>?
                               ORDER BY window_start LIMIT 1""", (airport, at_iso, at_iso)).fetchone()

    @staticmethod
    def _slot_pool_payload(conn: sqlite3.Connection, pool_id: int) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM slot_pools WHERE id=?", (pool_id,)).fetchone()
        if not row: raise ApiError(404, "slot_pool_not_found", "额度池不存在")
        used = conn.execute("SELECT COUNT(*) c FROM slot_occupancy WHERE pool_id=?", (pool_id,)).fetchone()["c"]
        payload = dict(row)
        payload["used"], payload["remaining"] = used, row["capacity"] - used
        return payload

    def _curfew_violation(self, conn: sqlite3.Connection, origin: str, destination: str, std_iso: str, sta_iso: str) -> str | None:
        """与方案校验一致的宵禁复核，命中返回 'airport_curfew'。"""
        airport = conn.execute("SELECT * FROM airports WHERE code=?", (destination,)).fetchone()
        if not airport: return "airport_unknown"
        permit = conn.execute("SELECT * FROM permits WHERE origin=? AND destination=? AND valid_from<=? AND valid_to>=?",
                              (origin, destination, sta_iso, sta_iso)).fetchone()
        if permit and permit["curfew_exempt"]: return None
        curfew_start, curfew_end = parse_clock(airport["curfew_start"]), parse_clock(airport["curfew_end"])
        arrival_clock = parse_time(sta_iso).timetz().replace(tzinfo=None)
        inside = arrival_clock >= curfew_start or arrival_clock < curfew_end if curfew_start > curfew_end else curfew_start <= arrival_clock < curfew_end
        return "airport_curfew" if inside else None

    def _recheck_item(self, conn: sqlite3.Connection, item: sqlite3.Row) -> list[str]:
        """批复到达后重新核对宵禁、维护、执勤和额度池存在性（占用容量在占额事务中再次校验）。"""
        snap = json.loads(item["snapshot_json"])
        codes: list[str] = []
        aircraft = conn.execute("SELECT * FROM aircraft WHERE id=?", (snap["aircraft_id"],)).fetchone()
        crew = conn.execute("SELECT * FROM crew WHERE id=?", (snap["crew_id"],)).fetchone()
        if not conn.execute("SELECT 1 FROM airports WHERE code=?", (snap["origin"],)).fetchone(): codes.append("airport_unknown:origin")
        if not aircraft or aircraft["status"] != "active":
            codes.append("aircraft_unavailable")
        elif parse_time(aircraft["maintenance_due"]) < parse_time(snap["new_sta"]):
            codes.append("maintenance_due")
        if not crew or crew["status"] != "active":
            codes.append("crew_unavailable")
        elif (parse_time(snap["new_sta"]) - parse_time(crew["duty_start"])).total_seconds() / 60 > crew["max_duty_minutes"]:
            codes.append("duty_limit")
        curfew = self._curfew_violation(conn, snap["origin"], snap["destination"], snap["new_std"], snap["new_sta"])
        if curfew: codes.append(curfew)
        if not self._find_slot_pool(conn, snap["destination"], snap["new_sta"]): codes.append("slot_pool_missing")
        return codes

    def _hit_fault(self, item_no: str, step: str) -> None:
        rules = self.fault_before_step.get(item_no) or {}
        if rules.pop(step, None) or rules.pop("*", None):
            raise WriteFault(item_no, step)

    def decide_approval(self, item_no: str, actor: str, role: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """空管批准/撤回同一事项：决定先落库，再推进未完成步骤；同一事项重复批复只采一次。"""
        if role not in {"atc", "ops_manager"}: raise ApiError(403, "approval_forbidden", "只有空管或运行经理可以批复")
        decision = str(body.get("decision", "")).strip()
        if decision not in ("approved", "withdrawn"): raise ApiError(400, "invalid_decision", "decision 必须是 approved 或 withdrawn")
        atc_ref = str(body.get("atc_ref", "")).strip() or None
        reason = str(body.get("reason", "")).strip()
        with self.repo.tx() as conn:
            item = conn.execute("SELECT * FROM approval_items WHERE item_no=?", (item_no,)).fetchone()
            if not item: raise ApiError(404, "item_not_found", f"待批事项 {item_no} 不存在")
            if decision == "withdrawn" and not reason: raise ApiError(400, "reason_required", "撤回必须提供原因")
            if item["status"] == "void":
                raise ApiError(409, "item_void", f"事项 {item_no} 属于旧方案版本，批复已失效")
            idempotent = False
            if item["decision"] is not None:
                if item["decision"] == decision:
                    if decision == "withdrawn" and item["status"] == "released":
                        raise ApiError(409, "already_released", "航班已执行，不能再次撤回")
                    idempotent = True  # 重复批准/重复撤回：只采第一次，返回当前状态
                elif item["decision"] == "approved" and decision == "withdrawn" and item["status"] in ("released", "held", "pending_atc"):
                    pass  # 晚到的撤回：退回未执行部分
                else:
                    raise ApiError(409, "decision_conflict", f"该事项已记录决定 {item['decision']}，不能改为 {decision}")
            if not idempotent:
                conn.execute("UPDATE approval_items SET decision=?,decided_at=?,decided_by=?,atc_ref=?,updated_at=? WHERE id=?",
                             (decision, iso(), actor, atc_ref, iso(), item["id"]))
                Repository.audit(conn, item["plan_id"], actor, role, f"approval_{decision}",
                                 {"item_no": item_no, "atc_ref": atc_ref, "reason": reason})
        write_failed = None
        if not idempotent:
            try:
                if decision == "approved":
                    self._advance_approved(item_no, actor, role)
                else:
                    self._advance_withdrawn(item_no, actor, role, reason)
            except WriteFault as exc:
                write_failed = {"step": exc.step, "message": str(exc)}
        return (202 if write_failed else 200), {"item": self.approval_payload(item_no), "idempotent": idempotent, "write_failed": write_failed}

    def _reserve_slot_tx(self, item: sqlite3.Row, actor: str, role: str) -> None:
        with self.repo.tx() as conn:
            self._hit_fault(item["item_no"], "reserve_slot")
            snap = json.loads(item["snapshot_json"])
            pool = self._find_slot_pool(conn, snap["destination"], snap["new_sta"])
            if not pool:
                conn.execute("UPDATE approval_items SET status='held',check_codes_json=?,updated_at=? WHERE id=?",
                             (json.dumps(["slot_pool_missing"]), iso(), item["id"]))
                conn.execute("INSERT OR IGNORE INTO approval_steps(item_id,step,result,detail_json,created_at) VALUES(?,?,?,?,?)",
                             (item["id"], "reserve_slot", "blocked", json.dumps({"codes": ["slot_pool_missing"]}), iso()))
                return
            used = conn.execute("SELECT COUNT(*) c FROM slot_occupancy WHERE pool_id=?", (pool["id"],)).fetchone()["c"]
            if used >= pool["capacity"]:
                conn.execute("UPDATE approval_items SET status='held',check_codes_json=?,updated_at=? WHERE id=?",
                             (json.dumps(["slot_capacity_exceeded"]), iso(), item["id"]))
                conn.execute("INSERT OR IGNORE INTO approval_steps(item_id,step,result,detail_json,created_at) VALUES(?,?,?,?,?)",
                             (item["id"], "reserve_slot", "blocked", json.dumps({"codes": ["slot_capacity_exceeded"], "pool_id": pool["id"]}), iso()))
                Repository.audit(conn, item["plan_id"], actor, role, "approval_blocked",
                                 {"item_no": item["item_no"], "codes": ["slot_capacity_exceeded"]})
                return
            conn.execute("INSERT OR IGNORE INTO slot_occupancy(pool_id,item_id,purpose) VALUES(?,?,?)", (pool["id"], item["id"], "landing"))
            conn.execute("INSERT OR IGNORE INTO approval_steps(item_id,step,result,detail_json,created_at) VALUES(?,?,?,?,?)",
                         (item["id"], "reserve_slot", "done", json.dumps({"pool_id": pool["id"], "airport": snap["destination"]}), iso()))

    def _execute_flight_tx(self, item: sqlite3.Row, actor: str, role: str) -> None:
        with self.repo.tx() as conn:
            self._hit_fault(item["item_no"], "execute_flight")
            snap = json.loads(item["snapshot_json"])
            assignment = conn.execute("SELECT * FROM assignments WHERE id=?", (item["assignment_id"],)).fetchone()
            conn.execute("""UPDATE flights SET std=?,sta=?,aircraft_id=?,crew_id=?,delay_minutes=?,revision=revision+1,updated_at=? WHERE id=?""",
                         (snap["new_std"], snap["new_sta"], snap["aircraft_id"], snap["crew_id"],
                          max(0, assignment["delay_minutes"]), iso(), item["flight_id"]))
            conn.execute("UPDATE assignments SET status='active' WHERE id=?", (item["assignment_id"],))
            conn.execute("UPDATE approval_items SET status='released',check_codes_json=NULL,updated_at=? WHERE id=?", (iso(), item["id"]))
            conn.execute("INSERT OR IGNORE INTO approval_steps(item_id,step,result,detail_json,created_at) VALUES(?,?,?,?,?)",
                         (item["id"], "execute_flight", "done", json.dumps({"flight_id": item["flight_id"]}), iso()))
            Repository.audit(conn, item["plan_id"], actor, role, "flight_released",
                             {"item_no": item["item_no"], "flight_id": item["flight_id"],
                              "aircraft": snap["aircraft_id"], "crew": snap["crew_id"]})

    def _hold_item(self, item: sqlite3.Row, codes: list[str], actor: str, role: str) -> None:
        with self.repo.tx() as conn:
            conn.execute("UPDATE approval_items SET status='held',check_codes_json=?,updated_at=? WHERE id=?",
                         (json.dumps(codes, ensure_ascii=False), iso(), item["id"]))
            Repository.audit(conn, item["plan_id"], actor, role, "approval_blocked", {"item_no": item["item_no"], "codes": codes})

    def _done_steps(self, item_id: int) -> set[str]:
        return {r["step"] for r in self.repo.conn.execute(
            "SELECT step FROM approval_steps WHERE item_id=? AND result='done'", (item_id,))}

    def _advance_approved(self, item_no: str, actor: str, role: str) -> None:
        """对已登记批准决定的事项做对账：占额前重新核对宵禁/维护/执勤，只放行通过的航班。"""
        item = self.repo.conn.execute("SELECT * FROM approval_items WHERE item_no=?", (item_no,)).fetchone()
        if not item: raise ApiError(404, "item_not_found", f"待批事项 {item_no} 不存在")
        if item["status"] == "released": return
        if item["status"] == "void": raise ApiError(409, "item_void", f"事项 {item_no} 属于旧方案版本，批复已失效")
        if item["status"] == "held":
            # 重试：清掉上次阻塞状态，重新核对并续做未完成步骤
            with self.repo.tx() as conn:
                conn.execute("UPDATE approval_items SET status='pending_atc',check_codes_json=NULL,updated_at=? WHERE id=?", (iso(), item["id"]))
                conn.execute("DELETE FROM approval_steps WHERE item_id=? AND result='blocked'", (item["id"],))
            item = self.repo.conn.execute("SELECT * FROM approval_items WHERE id=?", (item["id"],)).fetchone()
        if "reserve_slot" not in self._done_steps(item["id"]):
            codes = self._recheck_item(self.repo.conn, item)
            if codes:
                self._hold_item(item, codes, actor, role)
                return
            self._reserve_slot_tx(item, actor, role)
        item = self.repo.conn.execute("SELECT * FROM approval_items WHERE id=?", (item["id"],)).fetchone()
        if item["status"] == "held": return
        if "execute_flight" not in self._done_steps(item["id"]):
            codes = self._recheck_item(self.repo.conn, item)  # 执行前再核对一次
            if codes:
                self._hold_item(item, codes, actor, role)
                return
            self._execute_flight_tx(item, actor, role)

    def _advance_withdrawn(self, item_no: str, actor: str, role: str, reason: str | None = None) -> None:
        """撤回：退回未执行部分并释放额度；已执行的航班不回滚。"""
        with self.repo.tx() as conn:
            self._hit_fault(item_no, "withdraw")
            item = conn.execute("SELECT * FROM approval_items WHERE item_no=?", (item_no,)).fetchone()
            if not item: raise ApiError(404, "item_not_found", f"待批事项 {item_no} 不存在")
            executed = bool(conn.execute(
                "SELECT 1 FROM approval_steps WHERE item_id=? AND step='execute_flight' AND result='done'", (item["id"],)).fetchone())
            if executed:
                released_slots = 0  # 已执行航班保留其落地额度
                status, note = "released", "撤回时航班已执行，保留航班记录与已占额度"
            else:
                released_slots = conn.execute("DELETE FROM slot_occupancy WHERE item_id=?", (item["id"],)).rowcount
                status, note = "withdrawn", (reason or item["withdraw_reason"] or "空管撤回")
            conn.execute("UPDATE approval_items SET status=?,withdraw_reason=?,updated_at=? WHERE id=?",
                         (status, note, iso(), item["id"]))
            conn.execute("INSERT OR IGNORE INTO approval_steps(item_id,step,result,detail_json,created_at) VALUES(?,?,?,?,?)",
                         (item["id"], "withdraw", "done",
                          json.dumps({"executed": executed, "released_slots": released_slots, "reason": note}, ensure_ascii=False), iso()))
            Repository.audit(conn, item["plan_id"], actor, role, "approval_withdrawn",
                             {"item_no": item_no, "executed": executed, "released_slots": released_slots, "reason": note})

    def retry_pending(self, actor: str, role: str, item_no: str | None = None) -> dict[str, Any]:
        """写入失败后按事项号重试未完成项；不给事项号则对账所有未完成项。"""
        if role not in {"scheduler", "ops_manager", "atc"}: raise ApiError(403, "retry_forbidden", "当前角色不能重试对账")
        targets: list[sqlite3.Row]
        if item_no:
            row = self.repo.conn.execute("SELECT * FROM approval_items WHERE item_no=?", (item_no,)).fetchone()
            if not row: raise ApiError(404, "item_not_found", f"待批事项 {item_no} 不存在")
            targets = [row]
        else:
            targets = list(self.repo.conn.execute(
                "SELECT * FROM approval_items WHERE status IN ('pending_atc','held') ORDER BY id"))
        results = []
        for target in targets:
            result = {"item_no": target["item_no"], "status_before": target["status"]}
            item = self.repo.conn.execute("SELECT * FROM approval_items WHERE id=?", (target["id"],)).fetchone()
            if item["decision"] is None:
                result.update(status="pending_atc", note="空管尚未批复")
            else:
                try:
                    if item["decision"] == "approved":
                        self._advance_approved(item["item_no"], actor, role)
                    else:
                        self._advance_withdrawn(item["item_no"], actor, role)
                except WriteFault as exc:
                    result.update(status="write_failed", failed_step=exc.step)
                    results.append(result)
                    continue
                result["status"] = self.repo.conn.execute(
                    "SELECT status FROM approval_items WHERE id=?", (item["id"],)).fetchone()["status"]
            results.append(result)
        return {"retry_count": len(results), "results": results,
                "items": [self.approval_payload(r["item_no"]) for r in results]}

    def approval_payload(self, item_no: str) -> dict[str, Any]:
        conn = self.repo.conn
        item = conn.execute("SELECT * FROM approval_items WHERE item_no=?", (item_no,)).fetchone()
        if not item: raise ApiError(404, "item_not_found", f"待批事项 {item_no} 不存在")
        payload = {k: item[k] for k in item.keys()}
        payload["snapshot"] = json.loads(item["snapshot_json"])
        payload["check_codes"] = json.loads(item["check_codes_json"]) if item["check_codes_json"] else []
        payload.pop("snapshot_json", None); payload.pop("check_codes_json", None)
        steps = [dict(r) for r in conn.execute(
            "SELECT step,result,detail_json,created_at FROM approval_steps WHERE item_id=? ORDER BY id", (item["id"],))]
        for step in steps: step["detail"] = json.loads(step.pop("detail_json"))
        payload["steps"] = steps
        occ = conn.execute("""SELECT o.purpose,p.* FROM slot_occupancy o JOIN slot_pools p ON p.id=o.pool_id
                              WHERE o.item_id=?""", (item["id"],)).fetchone()
        payload["slot"] = {k: occ[k] for k in occ.keys()} if occ else None
        return payload

    def list_approvals(self, status_filter: str | None = None, plan_id: int | None = None) -> dict[str, Any]:
        conn = self.repo.conn
        sql = "SELECT item_no FROM approval_items WHERE 1=1"
        args: list[Any] = []
        if status_filter:
            if status_filter not in {"pending_atc", "released", "withdrawn", "void", "held"}:
                raise ApiError(400, "invalid_status", "状态过滤不合法")
            sql += " AND status=?"; args.append(status_filter)
        if plan_id is not None:
            sql += " AND plan_id=?"; args.append(plan_id)
        sql += " ORDER BY id"
        items = [self.approval_payload(r["item_no"]) for r in conn.execute(sql, args)]
        groups: dict[str, int] = {}
        reasons: dict[str, str] = {}
        for it in items:
            groups[it["status"]] = groups.get(it["status"], 0) + 1
            if it["status"] == "withdrawn": reasons[it["item_no"]] = it["withdraw_reason"] or ""
        pools = [self._slot_pool_payload(conn, r["id"]) for r in conn.execute("SELECT id FROM slot_pools ORDER BY id")]
        return {"items": items, "status_counts": groups, "withdraw_reasons": reasons, "slot_pools": pools}

    def _validate_plan(self, conn: sqlite3.Connection, plan_id: int) -> list[dict[str, Any]]:
        plan = conn.execute("SELECT * FROM recovery_plans WHERE id=?", (plan_id,)).fetchone()
        if not plan: raise ApiError(404, "plan_not_found", "方案不存在")
        rows = [dict(r) for r in conn.execute("""SELECT a.*, f.flight_no, f.origin, f.destination, f.passenger_count, f.status flight_status
                                                  FROM assignments a JOIN flights f ON f.id=a.flight_id WHERE a.plan_id=? ORDER BY a.new_std""", (plan_id,))]
        if not rows: raise ApiError(409, "empty_plan", "方案没有飞行调整")
        problems: list[dict[str, Any]] = []
        by_aircraft: dict[str, list[dict[str, Any]]] = {}
        by_crew: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            if row["status"] == "canceled": continue
            std, sta = parse_time(row["new_std"]), parse_time(row["new_sta"])
            aircraft = conn.execute("SELECT * FROM aircraft WHERE id=?", (row["aircraft_id"],)).fetchone()
            crew = conn.execute("SELECT * FROM crew WHERE id=?", (row["crew_id"],)).fetchone()
            origin = conn.execute("SELECT * FROM airports WHERE code=?", (row["origin"],)).fetchone()
            destination = conn.execute("SELECT * FROM airports WHERE code=?", (row["destination"],)).fetchone()
            if not aircraft or aircraft["status"] != "active": problems.append({"assignment_id": row["id"], "code": "aircraft_unavailable"})
            elif parse_time(aircraft["maintenance_due"]) < sta: problems.append({"assignment_id": row["id"], "code": "maintenance_due", "resource": aircraft["id"]})
            if not crew or crew["status"] != "active": problems.append({"assignment_id": row["id"], "code": "crew_unavailable"})
            if not origin or not destination: problems.append({"assignment_id": row["id"], "code": "airport_unknown"})
            if crew:
                duty_start, max_duty = parse_time(crew["duty_start"]), crew["max_duty_minutes"]
                if (sta - duty_start).total_seconds() / 60 > max_duty: problems.append({"assignment_id": row["id"], "code": "duty_limit", "resource": crew["id"]})
            if destination:
                curfew_start, curfew_end = parse_clock(destination["curfew_start"]), parse_clock(destination["curfew_end"])
                permit = conn.execute("""SELECT * FROM permits WHERE origin=? AND destination=? AND valid_from<=? AND valid_to>=?""",
                                      (row["origin"], row["destination"], row["new_sta"], row["new_sta"])).fetchone()
                arrival_clock = sta.timetz().replace(tzinfo=None)
                inside = arrival_clock >= curfew_start or arrival_clock < curfew_end if curfew_start > curfew_end else curfew_start <= arrival_clock < curfew_end
                if inside and not (permit and permit["curfew_exempt"]): problems.append({"assignment_id": row["id"], "code": "airport_curfew"})
                if row["origin"] != row["destination"] and not permit: problems.append({"assignment_id": row["id"], "code": "route_permit_missing"})
                elif row["origin"] != row["destination"] and not (parse_time(permit["valid_from"]) <= std <= parse_time(permit["valid_to"])):
                    problems.append({"assignment_id": row["id"], "code": "route_permit_window"})
            by_aircraft.setdefault(row["aircraft_id"], []).append(row)
            by_crew.setdefault(row["crew_id"], []).append(row)
        for bucket_name, buckets in (("aircraft", by_aircraft), ("crew", by_crew)):
            for resource, items in buckets.items():
                for i, left in enumerate(items):
                    for right in items[i + 1:]:
                        if overlaps(parse_time(left["new_std"]), parse_time(left["new_sta"]), parse_time(right["new_std"]), parse_time(right["new_sta"])):
                            problems.append({"code": f"{bucket_name}_overlap", "resource": resource, "assignments": [left["id"], right["id"]]})
        return problems

    def validate_plan(self, plan_id: int, actor: str, role: str) -> dict[str, Any]:
        if role not in {"scheduler", "ops_manager", "auditor"}: raise ApiError(403, "validate_forbidden", "当前角色不能校验方案")
        with self.repo.tx() as conn:
            problems = self._validate_plan(conn, plan_id)
            if not problems:
                metrics = self._metrics(conn, plan_id)
                conn.execute("UPDATE recovery_plans SET metrics_json=?,score_json=? WHERE id=?", (json.dumps(metrics, ensure_ascii=False), json.dumps(self._score(metrics)), plan_id))
            return {"valid": not problems, "problems": problems, "plan": self.get_plan(plan_id)}

    def _metrics(self, conn: sqlite3.Connection, plan_id: int) -> dict[str, Any]:
        rows = conn.execute("""SELECT a.*,f.passenger_count FROM assignments a JOIN flights f ON f.id=a.flight_id WHERE a.plan_id=?""", (plan_id,)).fetchall()
        canceled = sum(1 for row in rows if row["status"] == "canceled")
        return {"flight_count": len(rows), "canceled": canceled, "total_delay_minutes": sum(max(0, row["delay_minutes"]) for row in rows),
                "affected_passengers": sum(row["passenger_count"] for row in rows), "missed_connections": sum(row["missed_connections"] for row in rows)}

    @staticmethod
    def _score(metrics: dict[str, Any]) -> dict[str, int]:
        score = metrics["canceled"] * 100000 + metrics["missed_connections"] * 5000 + metrics["total_delay_minutes"] * 100 + metrics["affected_passengers"]
        return {"cost_score": score, "lower_is_better": 1}

    def lock_plan(self, plan_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "ops_manager": raise ApiError(403, "lock_forbidden", "只有运行经理可以锁定恢复方案")
        expected = body.get("expected_revision")
        if not isinstance(expected, int): raise ApiError(400, "revision_required", "expected_revision 必须是整数")
        with self.repo.tx() as conn:
            plan = conn.execute("SELECT * FROM recovery_plans WHERE id=?", (plan_id,)).fetchone()
            if not plan: raise ApiError(404, "plan_not_found", "方案不存在")
            if plan["status"] == "locked": return self.get_plan(plan_id)
            if plan["status"] == "submitted": raise ApiError(409, "plan_submitted", "方案已提交空管待批，放行由批复对账流程完成")
            if plan["revision"] != expected: raise ApiError(409, "revision_conflict", "方案版本已变化")
            problems = self._validate_plan(conn, plan_id)
            if problems: raise ApiError(409, "plan_invalid", "方案未通过约束校验", problems)
            conflicts = []
            for row in conn.execute("SELECT * FROM assignments WHERE plan_id=? AND status!='canceled'", (plan_id,)).fetchall():
                conflicting = conn.execute("""SELECT a.*,p.name plan_name FROM assignments a JOIN recovery_plans p ON p.id=a.plan_id
                    WHERE p.id!=? AND p.status='locked' AND a.status!='canceled' AND (a.aircraft_id=? OR a.crew_id=?)
                    AND a.new_std<? AND a.new_sta>?""",
                    (plan_id, row["aircraft_id"], row["crew_id"], row["new_sta"], row["new_std"])).fetchall()
                conflicts.extend({"assignment_id": row["id"], "conflict_plan_id": item["plan_id"], "conflict_plan": item["plan_name"], "resource": item["aircraft_id"] if item["aircraft_id"] == row["aircraft_id"] else item["crew_id"]} for item in conflicting)
            if conflicts: raise ApiError(409, "locked_resource_conflict", "与已锁定方案存在飞机或机组冲突", conflicts)
            metrics = self._metrics(conn, plan_id)
            conn.execute("""UPDATE recovery_plans SET status='locked',metrics_json=?,score_json=?,locked_at=?,locked_by=? WHERE id=?""",
                         (json.dumps(metrics, ensure_ascii=False), json.dumps(self._score(metrics)), iso(), actor, plan_id))
            for row in conn.execute("""SELECT a.*,f.flight_no FROM assignments a JOIN flights f ON f.id=a.flight_id WHERE a.plan_id=? AND a.status!='canceled'""", (plan_id,)):
                conn.execute("UPDATE flights SET std=?,sta=?,aircraft_id=?,crew_id=?,delay_minutes=?,revision=revision+1,updated_at=? WHERE id=?",
                             (row["new_std"], row["new_sta"], row["aircraft_id"], row["crew_id"], max(0, row["delay_minutes"]), iso(), row["flight_id"]))
                conn.execute("UPDATE assignments SET status='active' WHERE id=?", (row["id"],))
            Repository.audit(conn, plan_id, actor, role, "plan_locked", {"metrics": metrics})
            return self.get_plan(plan_id)

    def cancel_flight(self, flight_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"scheduler", "ops_manager"}: raise ApiError(403, "cancel_forbidden", "当前角色不能取消航班")
        reason = str(body.get("reason", "")).strip()
        if not reason: raise ApiError(400, "reason_required", "取消原因必填")
        with self.repo.tx() as conn:
            flight = conn.execute("SELECT * FROM flights WHERE id=?", (flight_id,)).fetchone()
            if not flight: raise ApiError(404, "flight_not_found", "航班不存在")
            if flight["status"] == "canceled": return {"flight": dict(flight), "idempotent": True}
            conn.execute("UPDATE flights SET status='canceled',cancel_reason=?,revision=revision+1,updated_at=? WHERE id=?", (reason, iso(), flight_id))
            Repository.audit(conn, None, actor, role, "flight_canceled", {"flight_id": flight_id, "reason": reason})
            return {"flight": dict(conn.execute("SELECT * FROM flights WHERE id=?", (flight_id,)).fetchone()), "idempotent": False}

    def recover_flight(self, flight_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"scheduler", "ops_manager"}: raise ApiError(403, "recover_forbidden", "当前角色不能恢复航班")
        expected = body.get("expected_revision")
        if not isinstance(expected, int): raise ApiError(400, "revision_required", "expected_revision 必须是整数")
        with self.repo.tx() as conn:
            flight = conn.execute("SELECT * FROM flights WHERE id=?", (flight_id,)).fetchone()
            if not flight: raise ApiError(404, "flight_not_found", "航班不存在")
            if flight["revision"] != expected: raise ApiError(409, "revision_conflict", "航班版本已变化")
            if flight["status"] != "canceled": raise ApiError(409, "not_canceled", "只有取消航班可以恢复")
            std, sta = parse_time(body.get("new_std")), parse_time(body.get("new_sta"))
            if sta <= std: raise ApiError(400, "invalid_times", "到达时间必须晚于起飞时间")
            aircraft_id, crew_id = body.get("aircraft_id", flight["aircraft_id"]), body.get("crew_id", flight["crew_id"])
            conn.execute("""UPDATE flights SET status='scheduled',std=?,sta=?,aircraft_id=?,crew_id=?,cancel_reason=NULL,
                            delay_minutes=0,revision=revision+1,updated_at=? WHERE id=?""",
                         (iso(std), iso(sta), aircraft_id, crew_id, iso(), flight_id))
            Repository.audit(conn, None, actor, role, "flight_recovered", {"flight_id": flight_id})
            return {"flight": dict(conn.execute("SELECT * FROM flights WHERE id=?", (flight_id,)).fetchone())}

    def get_plan(self, plan_id: int) -> dict[str, Any]:
        conn = self.repo.conn
        plan = conn.execute("SELECT * FROM recovery_plans WHERE id=?", (plan_id,)).fetchone()
        if not plan: raise ApiError(404, "plan_not_found", "方案不存在")
        assignments = [dict(r) for r in conn.execute("""SELECT a.*,f.flight_no,f.origin,f.destination,f.passenger_count FROM assignments a JOIN flights f ON f.id=a.flight_id WHERE a.plan_id=? ORDER BY a.new_std""", (plan_id,))]
        result = dict(plan)
        result["metrics"] = json.loads(plan["metrics_json"]) if plan["metrics_json"] else self._metrics(conn, plan_id)
        result["score"] = json.loads(plan["score_json"]) if plan["score_json"] else None
        result["assignments"] = assignments
        return result

    def compare_plans(self, disruption_id: int) -> dict[str, Any]:
        plans = []
        for row in self.repo.conn.execute("SELECT id FROM recovery_plans WHERE disruption_id=? ORDER BY id", (disruption_id,)):
            plan = self.get_plan(row["id"])
            if not plan["score"]:
                problems = self._validate_plan(self.repo.conn, row["id"])
                plan["valid"] = not problems
            else:
                plan["valid"] = True
            plans.append(plan)
        plans.sort(key=lambda item: item["score"]["cost_score"] if item["score"] else 10**18)
        return {"disruption_id": disruption_id, "recommended_plan_id": plans[0]["id"] if plans else None, "plans": plans}

    def state(self) -> dict[str, Any]:
        conn = self.repo.conn
        flights = [dict(r) for r in conn.execute("SELECT * FROM flights ORDER BY std")]
        plans = [self.get_plan(r["id"]) for r in conn.execute("SELECT id FROM recovery_plans ORDER BY id DESC LIMIT 20")]
        approval_rows = conn.execute("SELECT status,COUNT(*) c FROM approval_items GROUP BY status").fetchall()
        approvals = {"status_counts": {r["status"]: r["c"] for r in approval_rows},
                     "pending": [self.approval_payload(r["item_no"]) for r in conn.execute(
                         "SELECT item_no FROM approval_items WHERE status IN ('pending_atc','held') ORDER BY id")]}
        slot_pools = [self._slot_pool_payload(conn, r["id"]) for r in conn.execute("SELECT id FROM slot_pools ORDER BY id")]
        return {"flights": flights, "disruptions": [dict(r) for r in conn.execute("SELECT * FROM disruptions ORDER BY id DESC")],
                "plans": plans, "approvals": approvals, "slot_pools": slot_pools, "server_time": iso()}


def respond(handler: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
    raw = json.dumps(payload, ensure_ascii=False, default=str).encode()
    handler.send_response(status); handler.send_header("Content-Type", "application/json; charset=utf-8"); handler.send_header("Content-Length", str(len(raw))); handler.end_headers(); handler.wfile.write(raw)


class Handler(BaseHTTPRequestHandler):
    service: AirlineRecoveryService
    web_root: Path
    def log_message(self, fmt: str, *args: Any) -> None: print(f"{self.address_string()} - {fmt % args}")
    def body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if not length: return {}
        try: value = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc: raise ApiError(400, "invalid_json", "请求体不是有效 JSON") from exc
        if not isinstance(value, dict): raise ApiError(400, "invalid_json", "请求体必须是对象")
        return value
    def get_api(self, path: str) -> tuple[int, Any]:
        if path == "/health": return 200, {"status": "ok", "service": "airline-recovery"}
        actor, role = self.service.identity(self.headers)
        if path == "/api/state": return 200, self.service.state()
        query = parse_qs(urlparse(self.path).query)
        if path == "/api/approvals":
            status_filter = query.get("status", [None])[0]
            plan_param = query.get("plan_id", [None])[0]
            plan_id = int(plan_param) if plan_param and plan_param.isdigit() else None
            return 200, self.service.list_approvals(status_filter, plan_id)
        parts = [p for p in path.split("/") if p]
        if len(parts) == 3 and parts[:2] == ["api", "plans"] and parts[2].isdigit(): return 200, self.service.get_plan(int(parts[2]))
        if len(parts) == 4 and parts[:2] == ["api", "disruptions"] and parts[2].isdigit() and parts[3] == "compare": return 200, self.service.compare_plans(int(parts[2]))
        if len(parts) == 3 and parts[:2] == ["api", "approvals"] and parts[2] != "retry":
            return 200, self.service.approval_payload(parts[2])
        raise ApiError(404, "not_found", "接口不存在")
    def post_api(self, path: str) -> tuple[int, Any]:
        actor, role = self.service.identity(self.headers); body = self.body(); parts = [p for p in path.split("/") if p]
        table = {
            "/api/airports": lambda: (201, self.service.seed_airport(actor, role, body)),
            "/api/aircraft": lambda: (201, self.service.seed_aircraft(actor, role, body)),
            "/api/crew": lambda: (201, self.service.seed_crew(actor, role, body)),
            "/api/permits": lambda: (201, self.service.create_permit(actor, role, body)),
            "/api/flights": lambda: (201, self.service.create_flight(actor, role, body)),
            "/api/disruptions": lambda: (201, self.service.create_disruption(actor, role, body)),
            "/api/recovery-plans": lambda: (201, self.service.create_plan(actor, role, body)),
            "/api/slot-pools": lambda: (201, self.service.create_slot_pool(actor, role, body)),
            "/api/approvals/retry": lambda: (200, self.service.retry_pending(actor, role, body.get("item_no"))),
        }
        if path in table: return table[path]()
        if len(parts) == 3 and parts[:2] == ["api", "approvals"] and parts[2] != "retry":
            status, payload = self.service.decide_approval(parts[2], actor, role, body)
            return status, payload
        if len(parts) == 4 and parts[:2] == ["api", "plans"] and parts[2].isdigit():
            plan_id, action = int(parts[2]), parts[3]
            if action == "assignments": return 200, self.service.add_assignment(plan_id, actor, role, body)
            if action == "submit": return 200, self.service.submit_plan(plan_id, actor, role, body)
            if action == "validate": return 200, self.service.validate_plan(plan_id, actor, role)
            if action == "lock": return 200, self.service.lock_plan(plan_id, actor, role, body)
        if len(parts) == 4 and parts[:2] == ["api", "flights"] and parts[2].isdigit():
            flight_id, action = int(parts[2]), parts[3]
            if action == "cancel": return 200, self.service.cancel_flight(flight_id, actor, role, body)
            if action == "recover": return 200, self.service.recover_flight(flight_id, actor, role, body)
        raise ApiError(404, "not_found", "接口不存在")
    def handle_request(self, method: str) -> None:
        parsed = urlparse(self.path)
        try:
            if method == "GET" and parsed.path == "/":
                raw = (self.web_root / "index.html").read_bytes(); self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw); return
            status, payload = self.get_api(parsed.path) if method == "GET" else self.post_api(parsed.path)
            respond(self, status, payload)
        except ApiError as exc:
            payload = {"error": exc.code, "message": exc.message}
            if exc.details is not None: payload["details"] = exc.details
            respond(self, exc.status, payload)
        except Exception as exc:
            print(f"unhandled error: {exc!r}"); respond(self, 500, {"error": "internal_error", "message": str(exc)})
    def do_GET(self) -> None: self.handle_request("GET")
    def do_POST(self) -> None: self.handle_request("POST")


def create_server(db_path: str | Path, host: str = "127.0.0.1", port: int = PORT) -> ThreadingHTTPServer:
    service = AirlineRecoveryService(db_path)
    handler = type("AirlineHandler", (Handler,), {"service": service, "web_root": Path(__file__).resolve().parent / "static"})
    return ThreadingHTTPServer((host, port), handler)


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("--host", default="127.0.0.1"); parser.add_argument("--port", type=int, default=PORT); parser.add_argument("--db", default=os.environ.get("AIRLINE_DB", "airline_recovery.db")); args = parser.parse_args()
    server = create_server(args.db, args.host, args.port); print(f"airline-recovery listening on http://{args.host}:{args.port}")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()

if __name__ == "__main__": main()

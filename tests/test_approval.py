import sys
import tempfile
import unittest
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import AirlineRecoveryService, ApiError, iso, utcnow


class ApprovalReconciliationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = AirlineRecoveryService(Path(self.tmp.name) / "test.db")
        self.base = utcnow() + timedelta(days=1)
        # 夜间 23:30 到达 BBB，落在 23:00-05:00 宵禁窗口内
        self.night = datetime.combine(self.base.date(), time(23, 30), tzinfo=timezone.utc)
        self.svc.seed_airport("ops", "ops_manager", {"code": "AAA", "country": "CN", "curfew_start": "23:00", "curfew_end": "05:00"})
        self.svc.seed_airport("ops", "ops_manager", {"code": "BBB", "country": "CN", "curfew_start": "23:00", "curfew_end": "05:00"})
        self.svc.seed_aircraft("ops", "ops_manager", {"id": "AC1", "model": "A320", "maintenance_due": iso(self.base + timedelta(days=5))})
        self.svc.seed_aircraft("ops", "ops_manager", {"id": "AC2", "model": "A320", "maintenance_due": iso(self.base + timedelta(days=5))})
        self.svc.seed_crew("ops", "ops_manager", {"id": "CR1", "name": "甲组", "base": "AAA", "duty_start": iso(self.night - timedelta(hours=2)), "max_duty_minutes": 720})
        self.svc.seed_crew("ops", "ops_manager", {"id": "CR2", "name": "乙组", "base": "AAA", "duty_start": iso(self.night - timedelta(hours=2)), "max_duty_minutes": 720})
        self.svc.create_permit("ops", "ops_manager", {"origin": "AAA", "destination": "BBB", "valid_from": iso(self.base - timedelta(days=1)), "valid_to": iso(self.base + timedelta(days=2))})
        self.svc.seed_slot_quota("ops", "ops_manager", {"airport": "BBB", "slot_date": self.night.date().isoformat(), "total": 2})

    def tearDown(self):
        self.tmp.cleanup()

    def make_plan(self, flight_no, aircraft="AC2", crew="CR2", std=None, sta=None):
        std = std or iso(self.night - timedelta(hours=2))
        sta = sta or iso(self.night)
        flight = self.svc.create_flight("sched", "scheduler", {"flight_no": flight_no, "origin": "AAA", "destination": "BBB",
             "std": std, "sta": sta, "aircraft_id": "AC1", "crew_id": "CR1", "passenger_count": 150})
        disruption = self.svc.create_disruption("sched", "scheduler", {"kind": "aircraft_fault", "resource_id": "AC1",
             "starts_at": iso(self.base - timedelta(hours=1)), "ends_at": iso(self.base + timedelta(hours=3))})
        plan = self.svc.create_plan("sched", "scheduler", {"disruption_id": disruption["id"], "name": f"{flight_no} 恢复方案",
             "assignments": [{"flight_id": flight["id"], "aircraft_id": aircraft, "crew_id": crew,
                              "new_std": std, "new_sta": sta, "missed_connections": 2}]})
        return flight, plan

    def test_submit_registers_versioned_items_and_console_shows_pending(self):
        _, plan = self.make_plan("AB200")
        result = self.svc.submit_for_approval(plan["id"], "sched", "scheduler")
        self.assertEqual(result["plan_revision"], 1)
        self.assertEqual(len(result["items"]), 1)
        item = result["items"][0]
        self.assertTrue(item["item_no"].startswith("AP"))
        self.assertEqual(item["status"], "pending")
        # 重复提交不重复登记
        again = self.svc.submit_for_approval(plan["id"], "sched", "scheduler")
        self.assertEqual(len(again["items"]), 1)
        console = self.svc.approval_console("ops", "ops_manager")
        self.assertEqual(len(console["pending"]), 1)
        self.assertEqual(console["released"], [])
        self.assertEqual(console["withdrawn"], [])

    def test_approval_rechecks_and_executes_only_passing_flights(self):
        _, plan = self.make_plan("AB201")
        item = self.svc.submit_for_approval(plan["id"], "sched", "scheduler")["items"][0]
        result = self.svc.record_approval(item["item_no"], "ops", "ops_manager", {"approval_ref": "ATC-001"})
        self.assertTrue(result["executed"])
        self.assertEqual(result["item"]["status"], "executed")
        self.assertEqual(result["item"]["attempts"], 1)
        quota = self.svc.repo.conn.execute("SELECT * FROM slot_quotas WHERE airport='BBB'").fetchone()
        self.assertEqual(quota["used"], 1)
        console = self.svc.approval_console("ops", "ops_manager")
        self.assertEqual(len(console["released"]), 1)

    def test_duplicate_approval_ref_only_accepted_once(self):
        _, plan = self.make_plan("AB202")
        item = self.svc.submit_for_approval(plan["id"], "sched", "scheduler")["items"][0]
        first = self.svc.record_approval(item["item_no"], "ops", "ops_manager", {"approval_ref": "ATC-002"})
        again = self.svc.record_approval(item["item_no"], "ops", "ops_manager", {"approval_ref": "ATC-002"})
        self.assertTrue(again["idempotent"])
        self.assertEqual(again["item"]["status"], "executed")
        # 换文号重复批复同样不再执行
        other = self.svc.record_approval(item["item_no"], "ops", "ops_manager", {"approval_ref": "ATC-003"})
        self.assertTrue(other["idempotent"])
        quota = self.svc.repo.conn.execute("SELECT * FROM slot_quotas WHERE airport='BBB'").fetchone()
        self.assertEqual(quota["used"], 1)

    def test_plan_version_change_invalidates_old_approval(self):
        flight, plan = self.make_plan("AB203")
        item = self.svc.submit_for_approval(plan["id"], "sched", "scheduler")["items"][0]
        # 方案版本一变（临时改派），旧批复立即失效
        self.svc.add_assignment(plan["id"], "sched", "scheduler", {"expected_revision": 1, "flight_id": flight["id"],
             "aircraft_id": "AC2", "crew_id": "CR2",
             "new_std": iso(self.night - timedelta(hours=3)), "new_sta": iso(self.night - timedelta(hours=1))})
        with self.assertRaises(ApiError) as ctx:
            self.svc.record_approval(item["item_no"], "ops", "ops_manager", {"approval_ref": "ATC-004"})
        self.assertEqual(ctx.exception.code, "approval_stale")
        stale = self.svc.repo.conn.execute("SELECT * FROM approval_items WHERE item_no=?", (item["item_no"],)).fetchone()
        self.assertEqual(stale["status"], "superseded")
        # 按新版本重新登记后可正常批复
        new_item = self.svc.submit_for_approval(plan["id"], "sched", "scheduler")["items"][0]
        self.assertEqual(new_item["plan_revision"], 2)
        result = self.svc.record_approval(new_item["item_no"], "ops", "ops_manager", {"approval_ref": "ATC-005"})
        self.assertTrue(result["executed"])

    def test_failed_approval_can_be_retried_by_item_number(self):
        _, plan = self.make_plan("AB204")
        item = self.svc.submit_for_approval(plan["id"], "sched", "scheduler")["items"][0]
        # 维护到期：批复核对不通过，只记录失败原因，不放行
        self.svc.seed_aircraft("ops", "ops_manager", {"id": "AC2", "model": "A320", "maintenance_due": iso(self.night - timedelta(hours=1))})
        result = self.svc.record_approval(item["item_no"], "ops", "ops_manager", {"approval_ref": "ATC-006"})
        self.assertFalse(result["executed"])
        self.assertIn("maintenance_due", [r["code"] for r in result["reasons"]])
        self.assertEqual(result["item"]["status"], "failed")
        self.assertEqual(result["item"]["attempts"], 1)
        # 批复核对不通过：不放行，但已预留额度保留以便按事项号重试
        quota = self.svc.repo.conn.execute("SELECT * FROM slot_quotas WHERE airport='BBB'").fetchone()
        self.assertEqual(quota["used"], 1)
        # 故障排除后按事项号重试，未完成项继续
        self.svc.seed_aircraft("ops", "ops_manager", {"id": "AC2", "model": "A320", "maintenance_due": iso(self.base + timedelta(days=5))})
        retried = self.svc.retry_item(item["item_no"], "ops", "ops_manager")
        self.assertTrue(retried["executed"])
        self.assertEqual(retried["item"]["attempts"], 2)

    def test_quota_reserved_at_submit_and_released_on_withdraw(self):
        self.svc.seed_slot_quota("ops", "ops_manager", {"airport": "BBB", "slot_date": self.night.date().isoformat(), "total": 1})
        _, plan1 = self.make_plan("AB205")
        _, plan2 = self.make_plan("AB206")
        item1 = self.svc.submit_for_approval(plan1["id"], "sched", "scheduler")["items"][0]
        item2 = self.svc.submit_for_approval(plan2["id"], "sched", "scheduler")["items"][0]
        # 提交时按方案版本预留额度：plan1 占满唯一额度，plan2 未预留成功
        self.assertEqual(item1["occupied"], 1)
        self.assertEqual(item2["occupied"], 0)
        self.assertEqual(item2["last_error"], "quota_exhausted")
        # 批复到达后重新核对额度占用：plan2 因额度被占不通过，不放行
        result2 = self.svc.record_approval(item2["item_no"], "ops", "ops_manager", {"approval_ref": "ATC-008"})
        self.assertFalse(result2["executed"])
        self.assertIn("quota_exhausted", [r["code"] for r in result2["reasons"]])
        # 撤回未执行部分（plan1 尚未批复执行）并释放额度
        withdrawn = self.svc.withdraw_item(item1["item_no"], "ops", "ops_manager", {"reason": "军方占用夜间时刻"})
        self.assertEqual(withdrawn["item"]["status"], "withdrawn")
        self.assertEqual(withdrawn["item"]["withdraw_reason"], "军方占用夜间时刻")
        quota = self.svc.repo.conn.execute("SELECT * FROM slot_quotas WHERE airport='BBB'").fetchone()
        self.assertEqual(quota["used"], 0)
        # 额度释放后重试 plan2，放行成功
        retried = self.svc.retry_item(item2["item_no"], "ops", "ops_manager")
        self.assertTrue(retried["executed"])
        quota = self.svc.repo.conn.execute("SELECT * FROM slot_quotas WHERE airport='BBB'").fetchone()
        self.assertEqual(quota["used"], 1)
        console = self.svc.approval_console("ops", "ops_manager")
        self.assertEqual(len(console["released"]), 1)
        self.assertEqual(len(console["withdrawn"]), 1)
        self.assertEqual(console["withdrawn"][0]["withdraw_reason"], "军方占用夜间时刻")

    def test_withdraw_plan_returns_unexecuted_portion(self):
        _, plan1 = self.make_plan("AB207")
        _, plan2 = self.make_plan("AB208")
        self.svc.submit_for_approval(plan1["id"], "sched", "scheduler")
        self.svc.submit_for_approval(plan2["id"], "sched", "scheduler")
        result = self.svc.withdraw_plan(plan1["id"], "ops", "ops_manager", {"reason": "天气持续不适航"})
        self.assertEqual(result["withdrawn_count"], 1)
        self.assertEqual(result["items"][0]["status"], "withdrawn")
        # 另一方案的待批事项不受影响
        console = self.svc.approval_console("ops", "ops_manager")
        self.assertEqual(len(console["pending"]), 1)
        # 已放行事项不能撤回
        item = self.svc.submit_for_approval(plan2["id"], "sched", "scheduler")["items"][0]
        self.svc.record_approval(item["item_no"], "ops", "ops_manager", {"approval_ref": "ATC-009"})
        with self.assertRaises(ApiError) as ctx:
            self.svc.withdraw_item(item["item_no"], "ops", "ops_manager", {"reason": "重复撤回"})
        self.assertEqual(ctx.exception.code, "already_executed")

    def test_duty_limit_blocks_approval(self):
        _, plan = self.make_plan("AB209")
        item = self.svc.submit_for_approval(plan["id"], "sched", "scheduler")["items"][0]
        self.svc.seed_crew("ops", "ops_manager", {"id": "CR2", "name": "乙组", "base": "AAA",
             "duty_start": iso(self.night - timedelta(hours=2)), "max_duty_minutes": 60})
        result = self.svc.record_approval(item["item_no"], "ops", "ops_manager", {"approval_ref": "ATC-010"})
        self.assertFalse(result["executed"])
        self.assertIn("duty_limit", [r["code"] for r in result["reasons"]])


if __name__ == "__main__":
    unittest.main()

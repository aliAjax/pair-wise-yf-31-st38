import sys, tempfile, unittest
from datetime import timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import AirlineRecoveryService, ApiError, WriteFault, iso, utcnow


class AirlineFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.svc = AirlineRecoveryService(Path(self.tmp.name) / "test.db")
        base = utcnow() + timedelta(days=1)
        self.svc.seed_airport("ops", "ops_manager", {"code": "AAA", "country": "CN", "curfew_start": "00:00", "curfew_end": "00:00"})
        self.svc.seed_airport("bb", "ops_manager", {"code": "BBB", "country": "CN", "curfew_start": "00:00", "curfew_end": "00:00"})
        self.svc.seed_aircraft("ops", "ops_manager", {"id": "AC1", "model": "A320", "maintenance_due": iso(base + timedelta(days=5))})
        self.svc.seed_aircraft("ops", "ops_manager", {"id": "AC2", "model": "A320", "maintenance_due": iso(base + timedelta(days=5))})
        self.svc.seed_crew("ops", "ops_manager", {"id": "CR1", "name": "甲组", "base": "AAA", "duty_start": iso(base - timedelta(hours=2)), "max_duty_minutes": 720})
        self.svc.seed_crew("ops", "ops_manager", {"id": "CR2", "name": "乙组", "base": "AAA", "duty_start": iso(base - timedelta(hours=2)), "max_duty_minutes": 720})
        self.svc.create_permit("ops", "ops_manager", {"origin": "AAA", "destination": "BBB", "valid_from": iso(base - timedelta(days=1)), "valid_to": iso(base + timedelta(days=2))})
        self.base = base

    def tearDown(self): self.tmp.cleanup()

    def make_flight(self, number, aircraft, crew):
        return self.svc.create_flight("sched", "scheduler", {"flight_no": number, "origin": "AAA", "destination": "BBB", "std": iso(self.base), "sta": iso(self.base + timedelta(hours=2)), "aircraft_id": aircraft, "crew_id": crew, "passenger_count": 150})

    def test_complete_recovery_and_manual_recovery(self):
        flight = self.make_flight("AB100", "AC1", "CR1")
        disruption = self.svc.create_disruption("sched", "scheduler", {"kind": "aircraft_fault", "resource_id": "AC1", "starts_at": iso(self.base - timedelta(hours=1)), "ends_at": iso(self.base + timedelta(hours=3))})
        plan = self.svc.create_plan("sched", "scheduler", {"disruption_id": disruption["id"], "name": "换飞机并延误", "assignments": [{"flight_id": flight["id"], "aircraft_id": "AC2", "crew_id": "CR2", "new_std": iso(self.base + timedelta(hours=3)), "new_sta": iso(self.base + timedelta(hours=5)), "missed_connections": 4}]})
        check = self.svc.validate_plan(plan["id"], "auditor", "auditor")
        self.assertTrue(check["valid"], check["problems"])
        locked = self.svc.lock_plan(plan["id"], "ops", "ops_manager", {"expected_revision": 1})
        self.assertEqual(locked["status"], "locked")
        self.assertEqual(locked["metrics"]["affected_passengers"], 150)
        canceled = self.svc.cancel_flight(flight["id"], "sched", "scheduler", {"reason": "后续机务检查"})["flight"]
        recovered = self.svc.recover_flight(flight["id"], "sched", "scheduler", {"expected_revision": canceled["revision"], "new_std": iso(self.base + timedelta(hours=8)), "new_sta": iso(self.base + timedelta(hours=10))})["flight"]
        self.assertEqual(recovered["status"], "scheduled")

    def test_locked_resource_conflict_and_permissions(self):
        flight1 = self.make_flight("AB101", "AC1", "CR1")
        disruption1 = self.svc.create_disruption("sched", "scheduler", {"kind": "crew_timeout", "resource_id": "CR1", "starts_at": iso(self.base), "ends_at": iso(self.base + timedelta(hours=2))})
        plan1 = self.svc.create_plan("sched", "scheduler", {"disruption_id": disruption1["id"], "name": "方案一", "assignments": [{"flight_id": flight1["id"], "aircraft_id": "AC2", "crew_id": "CR2", "new_std": iso(self.base + timedelta(hours=2)), "new_sta": iso(self.base + timedelta(hours=4))}]})
        self.svc.lock_plan(plan1["id"], "ops", "ops_manager", {"expected_revision": 1})
        flight2 = self.make_flight("AB102", "AC2", "CR2")
        disruption2 = self.svc.create_disruption("sched", "scheduler", {"kind": "airport_closure", "resource_id": "AAA", "starts_at": iso(self.base), "ends_at": iso(self.base + timedelta(hours=1))})
        plan2 = self.svc.create_plan("sched", "scheduler", {"disruption_id": disruption2["id"], "name": "冲突方案", "assignments": [{"flight_id": flight2["id"], "aircraft_id": "AC2", "crew_id": "CR2", "new_std": iso(self.base + timedelta(hours=2, minutes=30)), "new_sta": iso(self.base + timedelta(hours=4, minutes=30))}]})
        with self.assertRaises(ApiError) as ctx:
            self.svc.lock_plan(plan2["id"], "ops", "ops_manager", {"expected_revision": 1})
        self.assertEqual(ctx.exception.code, "locked_resource_conflict")
        with self.assertRaises(ApiError) as ctx:
            self.svc.lock_plan(plan2["id"], "sched", "scheduler", {"expected_revision": 1})
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(ApiError) as ctx:
            self.svc.add_assignment(plan1["id"], "sched", "scheduler", {"expected_revision": 1, "flight_id": flight1["id"], "aircraft_id": "AC1", "crew_id": "CR1", "new_std": iso(self.base), "new_sta": iso(self.base + timedelta(hours=2))})
        self.assertEqual(ctx.exception.code, "plan_locked")


class AtcReconciliationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.svc = AirlineRecoveryService(Path(self.tmp.name) / "atc.db")
        self.base = utcnow() + timedelta(days=2)
        self.svc.seed_airport("ops", "ops_manager", {"code": "AAA", "country": "CN", "curfew_start": "00:00", "curfew_end": "00:00"})
        self.svc.seed_airport("ops", "ops_manager", {"code": "BBB", "country": "CN", "curfew_start": "00:00", "curfew_end": "00:00"})
        self.svc.seed_aircraft("ops", "ops_manager", {"id": "AC1", "model": "A320", "maintenance_due": iso(self.base + timedelta(days=5))})
        self.svc.seed_aircraft("ops", "ops_manager", {"id": "AC2", "model": "A320", "maintenance_due": iso(self.base + timedelta(days=5))})
        self.svc.seed_crew("ops", "ops_manager", {"id": "CR1", "name": "甲组", "base": "AAA", "duty_start": iso(self.base - timedelta(hours=2)), "max_duty_minutes": 720})
        self.svc.seed_crew("ops", "ops_manager", {"id": "CR2", "name": "乙组", "base": "AAA", "duty_start": iso(self.base - timedelta(hours=2)), "max_duty_minutes": 720})
        self.svc.create_permit("ops", "ops_manager", {"origin": "AAA", "destination": "BBB", "valid_from": iso(self.base - timedelta(days=1)), "valid_to": iso(self.base + timedelta(days=2))})
        self.svc.create_slot_pool("ops", "ops_manager", {"airport_code": "BBB", "window_start": iso(self.base - timedelta(hours=1)), "window_end": iso(self.base + timedelta(hours=12)), "capacity": 2})

    def tearDown(self): self.tmp.cleanup()

    def flight_plan(self, number="AB200"):
        flight = self.svc.create_flight("sched", "scheduler", {"flight_no": number, "origin": "AAA", "destination": "BBB",
            "std": iso(self.base), "sta": iso(self.base + timedelta(hours=2)), "aircraft_id": "AC1", "crew_id": "CR1", "passenger_count": 120})
        disruption = self.svc.create_disruption("sched", "scheduler", {"kind": "airport_closure", "resource_id": "AAA",
            "starts_at": iso(self.base), "ends_at": iso(self.base + timedelta(hours=1))})
        plan = self.svc.create_plan("sched", "scheduler", {"disruption_id": disruption["id"], "name": "夜间换场", "submit_for_approval": True,
            "assignments": [{"flight_id": flight["id"], "aircraft_id": "AC2", "crew_id": "CR2",
                             "new_std": iso(self.base + timedelta(hours=3)), "new_sta": iso(self.base + timedelta(hours=5))}]})
        return flight, plan

    def test_submit_approval_idempotent_release_and_release_only_passing(self):
        flight, plan = self.flight_plan()
        self.assertEqual(plan["status"], "submitted")
        item_no = f"P{plan['id']:06d}-R1-001"
        listing = self.svc.list_approvals()
        self.assertEqual(listing["status_counts"], {"pending_atc": 1})
        # 未批复前不能锁定
        with self.assertRaises(ApiError) as ctx:
            self.svc.lock_plan(plan["id"], "ops", "ops_manager", {"expected_revision": 1})
        self.assertEqual(ctx.exception.code, "plan_submitted")
        status, resp = self.svc.decide_approval(item_no, "tower", "atc", {"decision": "approved", "atc_ref": "ATC-9001"})
        self.assertEqual(status, 200)
        self.assertIsNone(resp["write_failed"]); self.assertFalse(resp["idempotent"])
        self.assertEqual(resp["item"]["status"], "released")
        self.assertEqual(resp["item"]["atc_ref"], "ATC-9001")
        self.assertIsNotNone(resp["item"]["slot"])
        # 航班按新时刻/资源执行
        released = self.svc.repo.conn.execute("SELECT * FROM flights WHERE id=?", (flight["id"],)).fetchone()
        self.assertEqual(released["aircraft_id"], "AC2"); self.assertEqual(released["crew_id"], "CR2")
        self.assertEqual(released["std"], iso(self.base + timedelta(hours=3)))
        # 重复批复只采一次
        status2, resp2 = self.svc.decide_approval(item_no, "tower", "atc", {"decision": "approved", "atc_ref": "ATC-OTHER"})
        self.assertTrue(resp2["idempotent"])
        self.assertEqual(self.svc.approval_payload(item_no)["atc_ref"], "ATC-9001")
        # scheduler 无权批复
        with self.assertRaises(ApiError) as ctx:
            self.svc.decide_approval(item_no, "sched", "scheduler", {"decision": "approved"})
        self.assertEqual(ctx.exception.status, 403)

    def test_revision_change_voids_old_items_old_approval_rejected_and_slots_freed(self):
        flight, plan = self.flight_plan()
        old_no = f"P{plan['id']:06d}-R1-001"
        self.svc.decide_approval(old_no, "tower", "atc", {"decision": "approved"})
        self.assertEqual(self.svc.list_approvals("released")["status_counts"], {"released": 1})
        # 空管晚到的撤回作用于已执行航班：航班保留
        status, withdrawn = self.svc.decide_approval(old_no, "tower", "atc", {"decision": "withdrawn", "reason": "跑道关闭"})
        self.assertEqual(withdrawn["item"]["status"], "released")
        self.assertIn("已执行", withdrawn["item"]["withdraw_reason"])
        # 方案版本一变：旧事项失效（已执行的保留为 released，其额度仍占用）
        self.svc.add_assignment(plan["id"], "sched", "scheduler", {"expected_revision": 1, "flight_id": flight["id"],
            "aircraft_id": "AC2", "crew_id": "CR2", "new_std": iso(self.base + timedelta(hours=4)), "new_sta": iso(self.base + timedelta(hours=6))})
        new_no = f"P{plan['id']:06d}-R2-001"
        self.assertEqual(self.svc.approval_payload(new_no)["status"], "pending_atc")
        listing = self.svc.list_approvals()
        self.assertEqual(listing["status_counts"].get("pending_atc"), 1)
        self.assertEqual(listing["status_counts"].get("released"), 1)
        # 旧事项对应的航班已执行：版本变更后保留执行结果与额度，旧批复不再接受新决定
        self.assertEqual(self.svc.approval_payload(old_no)["status"], "released")
        with self.assertRaises(ApiError) as ctx:
            self.svc.decide_approval(old_no, "tower2", "atc", {"decision": "approved"})
        self.assertEqual(ctx.exception.code, "decision_conflict")
        self.assertEqual(listing["slot_pools"][0]["used"], 1)

    def test_pending_item_voided_on_revision_releases_no_slot(self):
        flight, plan = self.flight_plan()
        old_no = f"P{plan['id']:06d}-R1-001"
        # 未批复直接改派：旧事项作废，不占额度
        self.svc.add_assignment(plan["id"], "sched", "scheduler", {"expected_revision": 1, "flight_id": flight["id"],
            "aircraft_id": "AC2", "crew_id": "CR2", "new_std": iso(self.base + timedelta(hours=4)), "new_sta": iso(self.base + timedelta(hours=6))})
        self.assertEqual(self.svc.approval_payload(old_no)["status"], "void")
        pools = self.svc.list_approvals()["slot_pools"]
        self.assertEqual(pools[0]["used"], 0)
        with self.assertRaises(ApiError) as ctx:
            self.svc.decide_approval(old_no, "tower", "atc", {"decision": "approved"})
        self.assertEqual(ctx.exception.code, "item_void")

    def test_recheck_blocks_maintenance_then_retry_releases(self):
        flight, plan = self.flight_plan()
        item_no = f"P{plan['id']:06d}-R1-001"
        # 批复到达时重新核对：AC2 维护到期 → 不予放行
        self.svc.seed_aircraft("ops", "ops_manager", {"id": "AC2", "model": "A320", "maintenance_due": iso(self.base)})
        status, resp = self.svc.decide_approval(item_no, "tower", "atc", {"decision": "approved"})
        self.assertEqual(resp["item"]["status"], "held")
        self.assertIn("maintenance_due", resp["item"]["check_codes"])
        self.assertIsNone(resp["item"]["slot"])  # 被拦截不占额度
        # 修复资源后按事项号重试
        self.svc.seed_aircraft("ops", "ops_manager", {"id": "AC2", "model": "A320", "maintenance_due": iso(self.base + timedelta(days=5))})
        retry = self.svc.retry_pending("sched", "scheduler", item_no)
        self.assertEqual(retry["results"][0]["status"], "released")
        self.assertEqual(self.svc.approval_payload(item_no)["status"], "released")

    def test_write_failure_after_reserve_then_retry_finishes(self):
        flight, plan = self.flight_plan()
        item_no = f"P{plan['id']:06d}-R1-001"
        self.svc.fault_before_step[item_no] = {"execute_flight": True}
        status, resp = self.svc.decide_approval(item_no, "tower", "atc", {"decision": "approved"})
        self.assertEqual(status, 202)
        self.assertEqual(resp["write_failed"]["step"], "execute_flight")
        # 额度已占但航班尚未执行
        self.assertEqual(self.svc.list_approvals()["slot_pools"][0]["used"], 1)
        still = self.svc.repo.conn.execute("SELECT aircraft_id FROM flights WHERE id=?", (flight["id"],)).fetchone()
        self.assertEqual(still["aircraft_id"], "AC1")
        # 按事项号重试，只续做未完成步骤，额度不重复占用
        retry = self.svc.retry_pending("sched", "scheduler", item_no)
        self.assertEqual(retry["results"][0]["status"], "released")
        self.assertEqual(self.svc.list_approvals()["slot_pools"][0]["used"], 1)
        done = self.svc.repo.conn.execute("SELECT aircraft_id FROM flights WHERE id=?", (flight["id"],)).fetchone()
        self.assertEqual(done["aircraft_id"], "AC2")

    def test_withdraw_before_execution_releases_slot_and_records_reason(self):
        flight, plan = self.flight_plan()
        item_no = f"P{plan['id']:06d}-R1-001"
        # 先批准占额，再在执行前注入故障，随后撤回
        self.svc.fault_before_step[item_no] = {"execute_flight": True}
        self.svc.decide_approval(item_no, "tower", "atc", {"decision": "approved"})
        status, resp = self.svc.decide_approval(item_no, "tower", "atc", {"decision": "withdrawn", "reason": "夜场容量收回"})
        self.assertEqual(resp["item"]["status"], "withdrawn")
        self.assertEqual(resp["item"]["withdraw_reason"], "夜场容量收回")
        self.assertEqual(self.svc.list_approvals()["slot_pools"][0]["used"], 0)
        still = self.svc.repo.conn.execute("SELECT aircraft_id FROM flights WHERE id=?", (flight["id"],)).fetchone()
        self.assertEqual(still["aircraft_id"], "AC1")  # 未执行不改动
        # 重复撤回只采一次
        status2, resp2 = self.svc.decide_approval(item_no, "tower", "atc", {"decision": "withdrawn", "reason": "又一个原因"})
        self.assertTrue(resp2["idempotent"])
        self.assertEqual(self.svc.approval_payload(item_no)["withdraw_reason"], "夜场容量收回")
        # 撤回必须带原因
        with self.assertRaises(ApiError) as ctx:
            self.svc.decide_approval(item_no, "tower", "atc", {"decision": "withdrawn"})
        self.assertEqual(ctx.exception.code, "reason_required")
        # 不存在的事项返回 404
        with self.assertRaises(ApiError) as ctx:
            self.svc.decide_approval("P999999-R1-001", "tower", "atc", {"decision": "approved"})
        self.assertEqual(ctx.exception.code, "item_not_found")

    def test_capacity_exceeded_holds_and_global_retry_picks_up(self):
        flight, plan = self.flight_plan()
        # 把额度池容量降为 0（重建一个临时 DB 之外的直接更新，仅用于制造满额）
        self.svc.repo.conn.execute("UPDATE slot_pools SET capacity=0 WHERE airport_code='BBB'")
        item_no = f"P{plan['id']:06d}-R1-001"
        status, resp = self.svc.decide_approval(item_no, "tower", "atc", {"decision": "approved"})
        self.assertEqual(resp["item"]["status"], "held")
        self.assertIn("slot_capacity_exceeded", resp["item"]["check_codes"])
        self.svc.repo.conn.execute("UPDATE slot_pools SET capacity=2 WHERE airport_code='BBB'")
        retry = self.svc.retry_pending("ops", "ops_manager")
        self.assertEqual(retry["results"][0]["status"], "released")

    def test_console_views_pending_released_withdrawn_and_filter(self):
        flight, plan = self.flight_plan()
        item_no = f"P{plan['id']:06d}-R1-001"
        self.svc.decide_approval(item_no, "tower", "atc", {"decision": "withdrawn", "reason": "演练取消"})
        pending = self.svc.list_approvals("pending_atc")
        withdrawn = self.svc.list_approvals("withdrawn")
        self.assertEqual(pending["status_counts"], {})
        self.assertEqual(withdrawn["status_counts"], {"withdrawn": 1})
        self.assertEqual(withdrawn["withdraw_reasons"][item_no], "演练取消")
        state = self.svc.state()
        self.assertIn("approvals", state)
        self.assertIn("slot_pools", state)


if __name__ == "__main__":
    unittest.main()

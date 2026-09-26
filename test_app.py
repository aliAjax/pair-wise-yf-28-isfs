import tempfile
import unittest
from collections import Counter
from pathlib import Path

from app import BusinessError, RandomizationStore


class RandomizationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RandomizationStore(Path(self.tmp.name) / "test.db")
        self.store.seed()
        self.trial = self.store.create_trial(
            "coord", "多中心降压研究", "v1.0", ["A", "B"], ["risk"], 4, "seed-2026-001"
        )
        self.store.start_trial("coord", self.trial["id"])

    def tearDown(self):
        self.tmp.cleanup()

    def test_stratified_block_randomization_and_two_person_unblinding(self):
        participants = [
            self.store.enroll("site1", self.trial["id"], f"S001-{i:03d}", {"risk": "low"})
            for i in range(1, 5)
        ]
        self.assertNotIn("arm", participants[0])
        with self.store.connect() as conn:
            arms = [r["arm"] for r in conn.execute(
                "SELECT a.arm FROM allocations a JOIN participants p ON p.allocation_id=a.id WHERE p.trial_id=? ORDER BY p.id",
                (self.trial["id"],),
            ).fetchall()]
        self.assertEqual(Counter(arms), Counter({"A": 2, "B": 2}))
        request = self.store.request_unblinding("site1", participants[0]["id"], "受试者发生严重不良事件需要紧急处理")
        first = self.store.approve_unblinding("monitor1", request["id"])
        self.assertEqual(first["status"], "pending")
        with self.assertRaises(BusinessError) as ctx:
            self.store.approve_unblinding("monitor1", request["id"])
        self.assertEqual(ctx.exception.code, "distinct_approver_required")
        second = self.store.approve_unblinding("monitor2", request["id"])
        self.assertEqual(second["status"], "approved")
        self.assertIn(second["arm"], {"A", "B"})

    def test_idempotent_enrollment_site_isolation_and_protocol_lock(self):
        first = self.store.enroll("site1", self.trial["id"], "S001-001", {"risk": "high"})
        again = self.store.enroll("site1", self.trial["id"], "S001-001", {"risk": "high"})
        self.assertEqual(first["id"], again["id"])
        self.assertTrue(again["idempotent"])
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM participants").fetchone()[0], 1)
        with self.assertRaises(BusinessError) as ctx:
            self.store.get_participant("site2", first["id"])
        self.assertEqual(ctx.exception.code, "site_isolation")
        with self.assertRaises(BusinessError) as ctx:
            self.store.update_protocol("coord", self.trial["id"], "v2")
        self.assertEqual(ctx.exception.code, "protocol_locked")

    def _enroll_one(self, code="S001-100"):
        return self.store.enroll("site1", self.trial["id"], code, {"risk": "low"})

    def test_incompatible_amendment_is_returned_and_enrollment_stays_open(self):
        self._enroll_one()
        result = self.store.submit_amendment(
            "coord", self.trial["id"], "v2.0", "评估新分组安全性需要修订方案",
            ["A", "C"], ["risk"], 4, "seed-2026-001",
        )
        self.assertEqual(result["status"], "returned")
        self.assertTrue(result["application_no"].startswith("AMD-"))
        self.assertFalse(result["compatibility"]["compatible"])
        self.assertTrue(result["compatibility"]["strata_factors_same"])
        self.assertFalse(result["compatibility"]["old_arms_preserved"])
        self.assertEqual(result["enrolled_count"], 1)
        # 被退回的申请不暂停入组
        again = self.store.enroll("site1", self.trial["id"], "S001-101", {"risk": "low"})
        self.assertFalse(again["idempotent"])

    def test_pending_amendment_pauses_enrollment_and_holds_unblinding(self):
        participant = self._enroll_one("S001-200")
        amendment = self.store.submit_amendment(
            "coord", self.trial["id"], "v1.1", "区组长度调整并保持既有分层因素",
            ["A", "B"], ["risk"], 4, "seed-2026-001",
        )
        self.assertEqual(amendment["status"], "pending_review")
        self.assertEqual(amendment["enrolled"][0]["external_id"], "S001-200")
        with self.assertRaises(BusinessError) as ctx:
            self.store.enroll("site1", self.trial["id"], "S001-201", {"risk": "low"})
        self.assertEqual(ctx.exception.code, "enrollment_paused")
        request = self.store.request_unblinding("monitor1", participant["id"], "严重不良事件需立即获知用药组别")
        self.assertEqual(request["status"], "materials_pending")
        self.assertEqual(request["amendment_application_no"], amendment["application_no"])
        # 审核未结束不能确认揭盲
        with self.assertRaises(BusinessError) as ctx:
            self.store.approve_unblinding("monitor2", request["id"])
        self.assertEqual(ctx.exception.code, "materials_pending")
        # 监查员平时看不到组别
        view = self.store.get_participant("monitor1", participant["id"])
        self.assertNotIn("arm", view)

    def test_amendment_approval_rewrites_protocol_and_releases_unblinding(self):
        participant = self._enroll_one("S001-300")
        amendment = self.store.submit_amendment(
            "coord", self.trial["id"], "v1.2", "仅扩展试验组，旧分层沿用",
            ["A", "B", "C"], ["risk"], 6, "seed-2026-001",
        )
        request = self.store.request_unblinding("monitor1", participant["id"], "受试者出现严重过敏反应")
        self.assertEqual(request["status"], "materials_pending")
        review = self.store.review_amendment("monitor1", amendment["id"], "approve")
        self.assertEqual(review["status"], "approved")
        self.assertEqual(review["released_requests"], [request["id"]])
        config = self.store.trial_config("coord", self.trial["id"])
        self.assertEqual(config["protocol_version"], "v1.2")
        self.assertEqual(config["arms"], ["A", "B", "C"])
        self.assertFalse(config["enrollment_paused"])
        # 挂起的揭盲转待审批，两名不同人员确认后显示组别
        first = self.store.approve_unblinding("monitor2", request["id"])
        self.assertEqual(first["status"], "pending")
        with self.assertRaises(BusinessError) as ctx:
            self.store.approve_unblinding("monitor1", request["id"])
        self.assertEqual(ctx.exception.code, "requester_cannot_approve")
        second = self.store.approve_unblinding("coord", request["id"])
        self.assertEqual(second["status"], "approved")
        self.assertIn(second["arm"], {"A", "B", "C"})

    def test_returned_amendment_reopens_enrollment_and_unblinding(self):
        participant = self._enroll_one("S001-400")
        amendment = self.store.submit_amendment(
            "coord", self.trial["id"], "v1.3", "保持分层不变等待伦理意见",
            ["A", "B"], ["risk"], 4, "seed-2026-001",
        )
        request = self.store.request_unblinding("site1", participant["id"], "严重不良事件紧急处理")
        review = self.store.review_amendment("coord", amendment["id"], "return", "伦理委员会要求补充安全性数据")
        self.assertEqual(review["status"], "returned")
        self.store.enroll("site1", self.trial["id"], "S001-401", {"risk": "low"})
        first = self.store.approve_unblinding("monitor1", request["id"])
        self.assertEqual(first["status"], "pending")
        second = self.store.approve_unblinding("monitor2", request["id"])
        self.assertEqual(second["status"], "approved")


if __name__ == "__main__":
    unittest.main()

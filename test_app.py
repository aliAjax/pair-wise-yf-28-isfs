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

    def _enroll_one(self, external_id="S001-100"):
        return self.store.enroll("site1", self.trial["id"], external_id, {"risk": "low"})

    def test_version_application_holds_enrollment_and_unblinding_blocked_until_complete(self):
        participant = self._enroll_one()
        # 提交兼容的版本申请（分层因素集合不变），进入审核并归档已入组名单
        app = self.store.submit_version_application(
            "coord", self.trial["id"], "v2.0", "SAE 处理流程更新，需要修订随访方案",
            block_size=4,
        )
        self.assertEqual(app["status"], "reviewing")
        self.assertEqual(app["number"], f"VA-{app['id']:04d}")
        self.assertEqual(app["enrolled_count"], 1)
        self.assertNotIn("enrolled_roster", app)
        detail = self.store.get_version_application("coord", app["id"], include_roster=True)
        self.assertEqual(detail["enrolled_roster"][0]["external_id"], "S001-100")

        # 审核期间暂停新入组
        with self.assertRaises(BusinessError) as ctx:
            self.store.enroll("site1", self.trial["id"], "S001-101", {"risk": "low"})
        self.assertEqual(ctx.exception.code, "enrollment_paused")
        self.assertEqual(ctx.exception.details["application_id"], app["number"])

        # 监查员遇到 SAE 发起揭盲：因版本申请未完成，停在“待补材料”并显示申请编号
        req = self.store.request_unblinding("monitor1", participant["id"], "受试者发生严重不良事件需紧急揭盲")
        self.assertEqual(req["state"], "awaiting_materials")
        self.assertEqual(req["blocking_application_id"], app["number"])
        self.assertNotIn("arm", req)
        with self.assertRaises(BusinessError) as ctx:
            self.store.approve_unblinding("monitor2", req["id"])
        self.assertEqual(ctx.exception.code, "awaiting_materials")
        # 版本审核未结束前，补交也会被挡回
        with self.assertRaises(BusinessError) as ctx:
            self.store.supplement_unblinding("monitor1", req["id"], "SAE 抢救记录")
        self.assertEqual(ctx.exception.code, "application_in_review")

        # 监查员批准新版本：方案生效、入组恢复
        reviewed = self.store.review_version_application(
            "monitor1", app["id"], "approve", "修订内容与统计原则一致，同意生效"
        )
        self.assertEqual(reviewed["status"], "approved")
        self.store.enroll("site1", self.trial["id"], "S001-101", {"risk": "low"})

        # 版本申请完成后补交材料，揭盲转为待审批
        supplemented = self.store.supplement_unblinding("monitor1", req["id"], "SAE 抢救记录与急诊病历")
        self.assertEqual(supplemented["state"], "pending")
        self.assertIsNone(supplemented["blocking_application_id"])

        # 双人确认：申请人不能审批，两人须不同，通过后才显示组别
        with self.assertRaises(BusinessError) as ctx:
            self.store.approve_unblinding("monitor1", req["id"])
        self.assertEqual(ctx.exception.code, "requester_cannot_approve")
        first = self.store.approve_unblinding("coord", req["id"])
        self.assertEqual(first["status"], "pending")
        with self.assertRaises(BusinessError) as ctx:
            self.store.approve_unblinding("coord", req["id"])
        self.assertEqual(ctx.exception.code, "distinct_approver_required")
        second = self.store.approve_unblinding("monitor2", req["id"])
        self.assertEqual(second["status"], "approved")
        self.assertIn(second["arm"], {"A", "B"})
        # 平时监查员依然看不到组别；批准后查询才显示
        self.assertNotIn("arm", self.store.list_participants("monitor1", self.trial["id"])[0])
        revealed = self.store.get_participant("monitor1", participant["id"])
        self.assertEqual(revealed["arm"], second["arm"])

    def test_incompatible_strata_returns_application_without_holding_enrollment(self):
        self._enroll_one()
        # 新增分层因素：旧受试者无取值，系统核对后直接退回，不暂停入组
        app = self.store.submit_version_application(
            "coord", self.trial["id"], "v2.1", "拟新增年龄分层以细化分析",
            strata_factors=["risk", "age"],
        )
        self.assertEqual(app["status"], "returned")
        self.assertIn("age", app["incompatibility_note"])
        self.store.enroll("site1", self.trial["id"], "S001-101", {"risk": "high"})

        # 替换分层因素（删除旧因素）同样不兼容
        app2 = self.store.submit_version_application(
            "coord", self.trial["id"], "v2.2", "拟改用年龄分层替换风险分层",
            strata_factors=["age"],
        )
        self.assertEqual(app2["status"], "returned")
        self.assertIn("risk", app2["incompatibility_note"])

        # 审核人必须是监查员，且审核意见必填
        good = self.store.submit_version_application(
            "coord", self.trial["id"], "v2.3", "仅更新随机种子，分层因素保持不变",
            seed="seed-2026-009",
        )
        with self.assertRaises(BusinessError) as ctx:
            self.store.review_version_application("coord", good["id"], "approve", "同意")
        self.assertEqual(ctx.exception.code, "forbidden")
        with self.assertRaises(BusinessError) as ctx:
            self.store.review_version_application("monitor1", good["id"], "return", "退")
        self.assertEqual(ctx.exception.code, "review_note_required")
        returned = self.store.review_version_application("monitor1", good["id"], "return", "文件不完整退回")
        self.assertEqual(returned["status"], "returned")
        # 退回后入组保持可用
        self.store.enroll("site1", self.trial["id"], "S001-102", {"risk": "low"})

    def test_coordinator_cancels_reviewing_application_and_releases_enrollment(self):
        self._enroll_one()
        app = self.store.submit_version_application(
            "coord", self.trial["id"], "v2.0", "申办方撤回修订，仅测试撤回流程"
        )
        self.assertEqual(app["status"], "reviewing")
        cancelled = self.store.cancel_version_application("coord", app["id"])
        self.assertEqual(cancelled["status"], "cancelled")
        # 撤回后可继续入组，且揭盲不再被该申请阻塞
        participant = self.store.list_participants("coord", self.trial["id"])[0]
        req = self.store.request_unblinding("site1", participant["id"], "受试者发生严重不良事件需要揭盲")
        self.assertEqual(req["state"], "pending")


if __name__ == "__main__":
    unittest.main()

import unittest

from creative_program_foundation.pilot.acceptance import run


class PilotAcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertTrue(result["dup_scan_blocked"])
        self.assertTrue(result["wo_balanced"])
        self.assertTrue(all(result["lot_balances"].values()))
        self.assertTrue(all(result["steps"]))
        # 精准召回：替代料只牵连 3 个组件，订单/包装不受影响
        self.assertEqual(3, result["recall_components"])
        self.assertEqual(0, result["recall_packages"])
        self.assertEqual("open", result["order_status_after_targeted_freeze"])
        # 重启后工单与冻结状态继续有效
        self.assertEqual("in_progress", result["wo_after_restart"])
        self.assertEqual("frozen", result["frozen_after_restart"])
        self.assertTrue(result["balanced_after_restart"])


if __name__ == "__main__":
    unittest.main()

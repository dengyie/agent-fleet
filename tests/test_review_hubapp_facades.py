"""评审修复 #16：移除无调用方的 legacy 外观（web.py 旧守护/采集助手、
hub/routes_observe re-export shim）；运行进程只走 bootstrap/应用服务。"""
import importlib
import unittest


class LegacyFacadeRemovalTests(unittest.TestCase):
    def test_web_has_no_legacy_daemon_helpers(self):
        from hub import web
        for name in ("load_hosts", "collect_all", "start_reconciliation",
                     "reconcile_leases_once", "start_lease_reconciler"):
            self.assertFalse(hasattr(web, name), name)
        self.assertTrue(callable(web.make_app))

    def test_routes_observe_shim_removed(self):
        with self.assertRaises(ModuleNotFoundError):
            importlib.import_module("hub.routes_observe")
        from hub.http import observe_routes
        self.assertTrue(callable(observe_routes._sse_event_name))

    def test_lease_sweep_available_via_reconciliation_service(self):
        from hub.application.reconciliation_service import ReconciliationService
        self.assertTrue(callable(ReconciliationService.reconcile_leases))


if __name__ == "__main__":
    unittest.main()

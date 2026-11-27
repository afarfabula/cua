import pathlib
import shutil
import sys
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch

from grade import InfraError, score_cmdb

SERVICES = pathlib.Path(__file__).resolve().parents[1] / "environment"
sys.path[:0] = [str(SERVICES / "services/portal"), str(SERVICES / "seed")]

import app as portal


class TopologyIsolationTest(unittest.TestCase):
    def setUp(self):
        root = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.baseline = root / "initial.db"
        self.database = root / "current.db"
        self.enterContext(patch.object(portal, "DB_PATH", self.database))
        portal.init_db()
        shutil.copyfile(self.database, self.baseline)
        self.truth = {
            "isolated_nodes": ["risk-node-02", "risk-node-03"],
            "active_nodes": [
                "gw-edge-01", "gw-edge-02", "settle-node-01", "settle-node-02",
                "risk-node-01", "risk-node-04", "risk-node-05", "pg-primary-01",
            ],
        }

    def set_status(self, node_name, status):
        return portal.update_node(node_name, portal.NodeUpdate(status=status), "sre_oncall")

    def score(self):
        with closing(portal.get_conn()) as conn:
            return score_cmdb(conn, self.baseline, self.truth)

    def test_no_action_does_not_score(self):
        sc, _, prot = self.score()
        self.assertEqual(sc, 0)
        self.assertTrue(prot)

    def test_rebalancing_active_weights_does_not_violate_status_protection(self):
        for node in self.truth["isolated_nodes"]:
            self.set_status(node, "ISOLATED")
        portal.update_traffic_weights(
            portal.TrafficWeightsUpdate(weights={
                "risk-node-01": 20, "risk-node-02": 0, "risk-node-03": 0,
                "risk-node-04": 40, "risk-node-05": 40,
            }),
            "sre_oncall",
        )
        sc, _, prot = self.score()
        self.assertEqual(sc, 1.0)
        self.assertTrue(prot)

    def test_reversing_a_wrong_isolation_still_loses_protection_credit(self):
        for node in self.truth["isolated_nodes"]:
            self.set_status(node, "ISOLATED")
        self.set_status("risk-node-04", "ISOLATED")
        self.set_status("risk-node-04", "ACTIVE")
        sc, _, prot = self.score()
        self.assertFalse(prot)
        self.assertEqual(sc, 0.5)

    def test_no_snapshot_is_an_infrastructure_error(self):
        self.baseline.unlink()
        with self.assertRaises(InfraError):
            self.score()

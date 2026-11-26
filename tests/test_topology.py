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
            "active_nodes": ["risk-node-01", "settle-node-01", "settle-node-02", "pg-primary-01"],
        }

    def set_status(self, node_name, status):
        return portal.update_node(node_name, portal.NodeUpdate(status=status), "sre_oncall")

    def score(self):
        with closing(portal.get_conn()) as conn:
            return score_cmdb(conn, self.baseline, self.truth)[0]

    def test_no_action_does_not_score(self):
        self.assertEqual(self.score(), 0)

    def test_two_persisted_isolations_and_untouched_non_targets_get_full_credit(self):
        for node in self.truth["isolated_nodes"]:
            self.set_status(node, "ISOLATED")
        # Retrying the same write leaves the audit count unchanged.
        self.set_status("risk-node-02", "ISOLATED")
        nodes = {row["node_name"]: row for row in portal.list_nodes("sre_oncall")["nodes"]}
        self.assertEqual(nodes["risk-node-02"]["status_change_count"], 1)
        self.assertEqual(nodes["settle-node-01"]["status_change_count"], 0)
        self.assertEqual(self.score(), 1)

    def test_reversing_a_wrong_isolation_still_loses_protection_credit(self):
        for node in self.truth["isolated_nodes"]:
            self.set_status(node, "ISOLATED")
        self.set_status("settle-node-01", "ISOLATED")
        self.set_status("settle-node-01", "ACTIVE")
        nodes = {row["node_name"]: row for row in portal.list_nodes("sre_oncall")["nodes"]}
        self.assertEqual(nodes["settle-node-01"]["status"], "ACTIVE")
        self.assertEqual(nodes["settle-node-01"]["status_change_count"], 2)
        self.assertEqual(self.score(), 0.5)

    def test_no_snapshot_is_an_infrastructure_error(self):
        self.baseline.unlink()
        with self.assertRaises(InfraError):
            self.score()

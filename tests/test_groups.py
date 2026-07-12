"""Regression tests for the named zone-group store: validation, dedupe,
duplicate-name rejection, and persistence round-trip."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from groups import GroupStore


class GroupStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "groups.json")
        self.s = GroupStore(store_path=self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_add_dedupes_members_and_assigns_id(self):
        g = self.s.add({"name": "Arcade", "members": ["TCC-1", "TCC-2", "TCC-1"]})
        self.assertEqual(g["members"], ["TCC-1", "TCC-2"])
        self.assertTrue(g["id"])
        self.assertEqual([x["name"] for x in self.s.list_groups()], ["Arcade"])

    def test_rejects_empty_name_or_members(self):
        for bad in ({"name": "", "members": ["A"]},
                    {"name": "X", "members": []},
                    {"name": "X", "members": "A"},
                    {"name": "X", "members": ["A", ""]}):
            with self.assertRaises(ValueError):
                self.s.add(bad)

    def test_rejects_duplicate_name_case_insensitive(self):
        self.s.add({"name": "Rink", "members": ["R1"]})
        with self.assertRaises(ValueError):
            self.s.add({"name": "rink", "members": ["R2"]})

    def test_update_and_delete(self):
        g = self.s.add({"name": "Lobby", "members": ["L1"]})
        gid = g["id"]
        upd = self.s.update(gid, {"name": "Lobby", "members": ["L1", "L2"]})
        self.assertEqual(upd["members"], ["L1", "L2"])
        self.assertEqual(upd["id"], gid, "update must keep the same id")
        self.assertIsNone(self.s.update("nope", {"name": "X", "members": ["A"]}))
        self.assertTrue(self.s.remove(gid))
        self.assertFalse(self.s.remove(gid))
        self.assertEqual(self.s.list_groups(), [])

    def test_update_allows_same_name_on_self(self):
        g = self.s.add({"name": "Bay", "members": ["B1"]})
        # Renaming to its own current name must not trip the duplicate check.
        upd = self.s.update(g["id"], {"name": "Bay", "members": ["B1", "B2"]})
        self.assertEqual(sorted(upd["members"]), ["B1", "B2"])

    def test_persistence_round_trip_and_skips_corrupt(self):
        self.s.add({"name": "Arcade", "members": ["TCC-1"]})
        self.s.add({"name": "Rink", "members": ["TCC-9"]})
        reloaded = GroupStore(store_path=self.path)
        self.assertEqual(sorted(x["name"] for x in reloaded.list_groups()),
                         ["Arcade", "Rink"])

    def test_load_tolerates_a_bad_entry(self):
        import json
        with open(self.path, "w") as fh:
            json.dump([{"name": "Good", "members": ["A"]},
                       {"name": "", "members": []}], fh)   # second is invalid
        s = GroupStore(store_path=self.path)
        self.assertEqual([x["name"] for x in s.list_groups()], ["Good"])


if __name__ == "__main__":
    unittest.main()

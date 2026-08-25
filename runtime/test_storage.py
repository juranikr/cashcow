import unittest
from copy import deepcopy
from types import SimpleNamespace

from storage import DynamoStore


class RecordingDynamoResourceClient:
    def __init__(self):
        self.calls = []

    def transact_write_items(self, **kwargs):
        self.calls.append(deepcopy(kwargs))


class DynamoStorePersistenceTests(unittest.TestCase):
    def test_persist_delta_leaves_values_for_resource_client_serialization(self):
        client = RecordingDynamoResourceClient()
        store = object.__new__(DynamoStore)
        store.table = SimpleNamespace(
            name="cashcow-test",
            meta=SimpleNamespace(client=client),
        )
        before = {
            "revision": 11,
            "world": {"status": "before"},
            "board": {"version": 1},
            "jobs": {},
            "messages": [],
            "reports": [],
            "boardVersions": {},
            "knowledge": [],
        }
        after = deepcopy(before)
        after["revision"] = 12
        after["world"] = {"status": "after"}

        store._persist_delta(before, after, expected_revision=11)

        transact_items = client.calls[0]["TransactItems"]
        self.assertEqual(len(transact_items), 2)
        world_item = transact_items[0]["Put"]["Item"]
        self.assertEqual(world_item["pk"], "WORLD#main")
        self.assertEqual(world_item["sk"], "STATE")
        self.assertEqual(world_item["entity"], "world")
        self.assertEqual(world_item["payload"], '{"status":"after"}')

        commit_put = transact_items[1]["Put"]
        self.assertEqual(commit_put["Item"]["revision"], 12)
        self.assertEqual(commit_put["ExpressionAttributeValues"], {":expected": 11})


if __name__ == "__main__":
    unittest.main()

import unittest
from copy import deepcopy
from types import SimpleNamespace

from botocore.session import get_session
from botocore.validate import validate_parameters

from storage import DynamoStore


class ShapeValidatingDynamoClient:
    def __init__(self):
        service_model = get_session().get_service_model("dynamodb")
        self.input_shape = service_model.operation_model("TransactWriteItems").input_shape
        self.calls = []

    def transact_write_items(self, **kwargs):
        validate_parameters(kwargs, self.input_shape)
        self.calls.append(deepcopy(kwargs))


class DynamoStorePersistenceTests(unittest.TestCase):
    def test_persist_delta_serializes_low_level_transaction_attribute_values(self):
        client = ShapeValidatingDynamoClient()
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
        self.assertEqual(world_item["pk"], {"S": "WORLD#main"})
        self.assertEqual(world_item["sk"], {"S": "STATE"})
        self.assertEqual(world_item["entity"], {"S": "world"})
        self.assertEqual(world_item["payload"], {"S": '{"status":"after"}'})

        commit_put = transact_items[1]["Put"]
        self.assertEqual(commit_put["Item"]["revision"], {"N": "12"})
        self.assertEqual(commit_put["ExpressionAttributeValues"], {":expected": {"N": "11"}})


if __name__ == "__main__":
    unittest.main()

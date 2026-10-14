import json
from pathlib import Path
import re
import unittest

from agy_adapter import _with_execution_contract


class AgyAdapterTests(unittest.TestCase):
    def test_execution_contract_uses_valid_json_and_forward_slash_broker(self):
        contract = _with_execution_contract(
            "Task",
            project_root=Path(r"C:\Project\Example"),
            request_dir=Path(r"C:\Project\Example\.requests"),
            broker_path=Path(r"C:\Users\Example\.codex\tools\bridge\reviewed_exec.cmd"),
            approval_id="a" * 32,
        )
        objects = re.findall(r"`(\{[^`]+\})`", contract)
        self.assertEqual(len(objects), 1)
        value = json.loads(objects[0])
        self.assertEqual(value["cwd"], r"C:\Project\Example")
        self.assertIn(
            "C:/Users/Example/.codex/tools/bridge/reviewed_exec.cmd " + "a" * 32,
            contract,
        )


if __name__ == "__main__":
    unittest.main()

"""sync.py 单元测试（纯本地模拟测试，不依赖外部网络或密钥）。"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import sync


class SyncUnitTests(unittest.TestCase):
    def test_infer_cell_payload_types(self) -> None:
        self.assertIsNone(sync.infer_cell_payload(0, 0, ""))
        self.assertEqual(
            sync.infer_cell_payload(0, 1, "42"),
            {"row": 0, "col": 1, "value_type": "NUMBER", "number_value": 42},
        )
        self.assertEqual(
            sync.infer_cell_payload(0, 2, "3.14"),
            {"row": 0, "col": 2, "value_type": "NUMBER", "number_value": 3.14},
        )
        self.assertEqual(
            sync.infer_cell_payload(0, 3, "TRUE"),
            {"row": 0, "col": 3, "value_type": "BOOL", "bool_value": True},
        )
        # QQ 号/前导零 ID 必须保留字符串
        self.assertEqual(
            sync.infer_cell_payload(1, 0, "00123"),
            {"row": 1, "col": 0, "value_type": "STRING", "string_value": "00123"},
        )
        self.assertEqual(
            sync.infer_cell_payload(1, 1, "12345678901"),
            {"row": 1, "col": 1, "value_type": "STRING", "string_value": "12345678901"},
        )

    def test_hash_ignores_trailing_empty_grid(self) -> None:
        a = [["轮次", "比分", ""], ["QF", "4", ""], ["", "", ""]]
        b = [["轮次", "比分"], ["QF", "4"]]
        self.assertEqual(sync.compute_matrix_hash(a), sync.compute_matrix_hash(b))

    def test_parse_mcp_response_json_and_sse(self) -> None:
        plain = '{"jsonrpc":"2.0","id":1,"result":{"ok":true}}'
        self.assertEqual(sync.parse_mcp_response_body(plain)["id"], 1)

        sse = (
            "event: message\n"
            'data: {"jsonrpc":"2.0","id":2,"result":{"content":[{"type":"text","text":"{}"}]}}\n\n'
        )
        self.assertEqual(sync.parse_mcp_response_body(sse)["id"], 2)

    def test_sync_one_task_skips_when_unchanged(self) -> None:
        calls: list[tuple[str, str, dict]] = []

        class FakeClient:
            def call_tool(self, endpoint: str, tool_name: str, args: dict) -> dict:
                calls.append((endpoint, tool_name, args))
                if tool_name == "sheet.get_sheet_info":
                    return {
                        "sheets": [
                            {
                                "sheet_id": "s0",
                                "sheet_name": "工作表1",
                                "used_range": {"row_count": 2, "col_count": 2},
                            }
                        ]
                    }
                if tool_name == "sheet.get_cell_data":
                    return {"csv_data": "A,B\n1,2\n"}
                raise AssertionError(f"意外写入工具被触发: {tool_name}")

        with patch.object(sync, "fetch_google_sheet_csv", return_value=[["A", "B"], ["1", "2"]]):
            res = sync.sync_one_task(
                FakeClient(),  # type: ignore[arg-type]
                {"google_spreadsheet_id": "g1", "tencent_file_id": "t1"},
                force=False,
            )
        self.assertTrue(res["skipped"])
        self.assertEqual([c[1] for c in calls], ["sheet.get_sheet_info", "sheet.get_cell_data"])

    def test_sync_one_task_creates_and_writes(self) -> None:
        calls: list[tuple[str, str, dict]] = []

        class FakeClient:
            def call_tool(self, endpoint: str, tool_name: str, args: dict) -> dict:
                calls.append((endpoint, tool_name, args))
                if tool_name == "manage.create_file":
                    return {"file_id": "new_t1", "url": "https://docs.qq.com/sheet/new_t1"}
                if tool_name == "sheet.get_sheet_info":
                    return {"sheets": [{"sheet_id": "sub_1", "sheet_name": "工作表1"}]}
                if tool_name in ("sheet.clear_range_cells", "sheet.set_range_value"):
                    return {}
                if tool_name == "sheet.get_cell_data":
                    return {"csv_data": "轮次,比分\nQF,4\n"}
                raise AssertionError(f"未知工具: {tool_name}")

        with patch.object(
            sync, "fetch_google_sheet_csv", return_value=[["轮次", "比分"], ["QF", "4"]]
        ):
            res = sync.sync_one_task(
                FakeClient(),  # type: ignore[arg-type]
                {"google_spreadsheet_id": "g1", "create_title": "测试表"},
                force=True,
            )
        self.assertFalse(res["skipped"])
        self.assertEqual(res["tencent_file_id"], "new_t1")
        self.assertEqual(res["written_cells"], 4)
        self.assertEqual(
            [c[1] for c in calls],
            [
                "manage.create_file",
                "sheet.get_sheet_info",
                "sheet.clear_range_cells",
                "sheet.set_range_value",
                "sheet.get_cell_data",
            ],
        )


if __name__ == "__main__":
    unittest.main()

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
                if tool_name == "manage.search_file":
                    return {"list": []}
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
                "manage.search_file",
                "manage.create_file",
                "sheet.get_sheet_info",
                "sheet.clear_range_cells",
                "sheet.set_range_value",
                "sheet.get_cell_data",
            ],
        )

    def test_client_resolves_unprefixed_tool_on_method_not_found(self) -> None:
        client = sync.TencentMcpClient("test-token")
        seen_methods: list[tuple[str, str]] = []

        def fake_post(
            endpoint: str, auth_header: str, session_id: str | None, payload: dict
        ) -> tuple[int, dict[str, str], str]:
            method = payload["method"]
            if method == "initialize":
                return 200, {"mcp-session-id": "sid-1"}, '{"jsonrpc":"2.0","id":1,"result":{}}'
            if method == "notifications/initialized":
                return 202, {}, ""
            if method == "tools/list":
                seen_methods.append((method, ""))
                return (
                    200,
                    {},
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": payload["id"],
                            "result": {
                                "tools": [
                                    {"name": "get_sheet_info", "inputSchema": {"properties": {"file_id": {}}}}
                                ]
                            },
                        }
                    ),
                )
            if method == "tools/call":
                name = payload["params"]["name"]
                seen_methods.append((method, name))
                if name == "sheet.get_sheet_info":
                    return (
                        200,
                        {},
                        '{"jsonrpc":"2.0","id":2,"error":{"code":-32601,"message":"tool not found"}}',
                    )
                if name == "get_sheet_info":
                    return (
                        200,
                        {},
                        '{"jsonrpc":"2.0","id":3,"result":{"structuredContent":{"sheets":[{"sheet_id":"s0"}]}}}',
                    )
            raise AssertionError(f"未预期请求: {payload}")

        with patch.object(client, "_post_raw", side_effect=fake_post):
            res = client.call_tool(sync.MCP_SHEET_URL, "sheet.get_sheet_info", {"file_id": "f1"})
        self.assertEqual(res["sheets"][0]["sheet_id"], "s0")
        self.assertEqual(
            seen_methods,
            [("tools/call", "sheet.get_sheet_info"), ("tools/list", ""), ("tools/call", "get_sheet_info")],
        )

    def test_extract_used_dimensions_variants(self) -> None:
        self.assertEqual(
            sync.extract_used_dimensions({"used_range": {"row_count": 4, "col_count": 5}}),
            (4, 5),
        )
        self.assertEqual(
            sync.extract_used_dimensions({"used_range": {"start_row": 0, "end_row": 3, "start_col": 0, "end_col": 4}}),
            (4, 5),
        )
        self.assertEqual(
            sync.extract_used_dimensions({"used_range": "工作表1!A1:E4"}),
            (4, 5),
        )
        self.assertEqual(
            sync.extract_used_dimensions({"row_count": 4, "col_count": 5}),
            (4, 5),
        )

    def test_read_sheet_matrix_chunked_preserves_empty_row_offsets(self) -> None:
        calls: list[tuple[int, int]] = []

        class FakeClient:
            def call_tool(self, endpoint: str, tool_name: str, args: dict) -> dict:
                calls.append((args["start_row"], args["end_row"]))
                if args["start_row"] == 0:
                    return {"csv_data": "h1,h2\n"}
                return {"csv_data": "\nv2\n"}

        rows, first_csv = sync.read_sheet_matrix_chunked(
            FakeClient(), "f1", "s1", total_rows=4, total_cols=2, chunk_cells=4
        )
        self.assertEqual(calls, [(0, 1), (2, 3)])
        self.assertEqual(first_csv, "h1,h2\n")
        self.assertEqual(rows, [["h1", "h2"], [], [], ["v2"]])

    def test_resolve_or_create_subsheet_creates_missing_named_sheet(self) -> None:
        seen: list[str] = []
        state = {"sheets": [{"sheet_id": "s0", "sheet_name": "工作表1"}]}

        class FakeClient:
            def call_tool(self, endpoint: str, tool_name: str, args: dict) -> dict:
                seen.append(tool_name)
                if tool_name == "sheet.get_sheet_info":
                    return {"sheets": list(state["sheets"])}
                if tool_name == "sheet.add_sheet":
                    state["sheets"].append({"sheet_id": "s1", "sheet_name": args["name"]})
                    return {"sheet_id": "s1"}
                raise AssertionError(f"未预期调用: {tool_name}")

        sub = sync.resolve_or_create_subsheet(FakeClient(), "f1", desired_name="_sync_图池")
        self.assertEqual(sub["sheet_id"], "s1")
        self.assertEqual(seen, ["sheet.get_sheet_info", "sheet.add_sheet", "sheet.get_sheet_info"])

    def test_explicit_sync_config_overrides_env_google_id(self) -> None:
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "custom.json"
            cfg.write_text('{"tasks": [{"google_spreadsheet_id": "from_cfg"}]}', encoding="utf-8")
            with patch.dict(os.environ, {"GOOGLE_SHEET_ID": "from_env"}):
                tasks = sync.load_tasks_from_env_or_config(cfg, explicit_config=True)
            self.assertEqual(tasks[0]["google_spreadsheet_id"], "from_cfg")

    def test_ensure_sheet_capacity_expands_rows_and_cols_in_batches(self) -> None:
        calls: list[dict] = []

        class FakeClient:
            def call_tool(self, endpoint: str, tool_name: str, args: dict) -> dict:
                calls.append({"tool": tool_name, **args})
                return {}

        new_r, new_c = sync.ensure_sheet_capacity(
            FakeClient(), "f1", "s1", current_rows=200, current_cols=26, needed_rows=205, needed_cols=150
        )
        self.assertEqual((new_r, new_c), (205, 150))
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0]["dimension_type"], "row")
        self.assertEqual((calls[0]["index"], calls[0]["count"], calls[0]["direction"]), (199, 5, "after"))
        self.assertEqual(calls[1]["dimension_type"], "col")
        self.assertEqual((calls[1]["index"], calls[1]["count"], calls[1]["direction"]), (25, 100, "after"))
        self.assertEqual((calls[2]["index"], calls[2]["count"], calls[2]["direction"]), (125, 24, "after"))

    def test_hash_normalizes_equivalent_numbers_and_bools_but_keeps_leading_zeros(self) -> None:
        self.assertEqual(
            sync.compute_matrix_hash([["0.70", "4.00", "true", "00789"]]),
            sync.compute_matrix_hash([["0.7", "4", "TRUE", "00789"]]),
        )
        self.assertNotEqual(
            sync.compute_matrix_hash([["00789"]]),
            sync.compute_matrix_hash([["789"]]),
        )

    def test_http_retry_recovers_after_transient_503(self) -> None:
        import io
        import urllib.error
        import urllib.request

        class FakeResp:
            status = 200
            headers = {"Content-Type": "text/csv; charset=utf-8"}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self) -> bytes:
                return "a,b\n1,2\n".encode("utf-8")

        attempts = {"n": 0}

        def fake_urlopen(req, timeout=30):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise urllib.error.HTTPError(
                    req.full_url, 503, "Service Unavailable", {}, io.BytesIO(b"busy")
                )
            return FakeResp()

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            rows = sync.fetch_google_sheet_csv("gid_ok", "0", retries=2, retry_delay=0)
        self.assertEqual(attempts["n"], 2)
        self.assertEqual(rows, [["a", "b"], ["1", "2"]])

    def test_clear_sheet_range_chunked_splits_large_ranges(self) -> None:
        ranges: list[tuple[int, int, int, int]] = []

        class FakeClient:
            def call_tool(self, endpoint: str, tool_name: str, args: dict) -> dict:
                self.last = tool_name
                ranges.append((args["start_row"], args["end_row"], args["start_col"], args["end_col"]))
                return {}

        sync.clear_sheet_range_chunked(
            FakeClient(), "f1", "s1", end_row=4, end_col=3, chunk_cells=8
        )
        self.assertEqual(ranges, [(0, 1, 0, 3), (2, 3, 0, 3), (4, 4, 0, 3)])

    def test_validate_task_config_checks_sheet_name_constraints(self) -> None:
        sync.validate_task_config({"google_spreadsheet_id": "g1", "tencent_sheet_name": "_sync_合法"})
        with self.assertRaises(ValueError):
            sync.validate_task_config({"google_spreadsheet_id": "", "tencent_sheet_name": "ok"})
        with self.assertRaises(ValueError):
            sync.validate_task_config({"google_spreadsheet_id": "g1", "tencent_sheet_name": "a" * 32})
        with self.assertRaises(ValueError):
            sync.validate_task_config({"google_spreadsheet_id": "g1", "tencent_sheet_name": "bad/name"})

    def test_main_isolates_task_failures_and_returns_nonzero(self) -> None:
        import contextlib
        import io
        import os

        tasks = [
            {"google_spreadsheet_id": "bad_g", "tencent_sheet_name": "tab1"},
            {"google_spreadsheet_id": "ok_g", "tencent_sheet_name": "tab2"},
        ]

        def fake_sync(client, task, force=False, title_cache=None, debug_preview=False):
            if task["google_spreadsheet_id"] == "bad_g":
                raise RuntimeError("boom")
            return {"skipped": True, "sheet_name": "tab2"}

        buf = io.StringIO()
        with (
            patch.dict(os.environ, {"TENCENT_DOCS_TOKEN": "tok", "SYNC_CONFIG": "dummy.json"}),
            patch.object(sync, "load_tasks_from_env_or_config", return_value=tasks),
            patch.object(sync, "sync_one_task", side_effect=fake_sync),
            contextlib.redirect_stdout(buf),
        ):
            code = sync.main()

        self.assertEqual(code, 1)
        out = json.loads(buf.getvalue())
        self.assertEqual(len(out), 2)
        self.assertFalse(out[0]["ok"])
        self.assertIn("boom", out[0]["error"])
        self.assertTrue(out[1]["ok"])
        self.assertTrue(out[1]["skipped"])

    def test_file_ref_and_google_url_normalization(self) -> None:
        self.assertEqual(
            sync.normalize_google_sheet_id(
                "https://docs.google.com/spreadsheets/d/1DEfk-_2Xfo5Ipo1dT6CL5JLc6zGUoX27vIk2RBQuqPk/edit?gid=0#gid=0"
            ),
            "1DEfk-_2Xfo5Ipo1dT6CL5JLc6zGUoX27vIk2RBQuqPk",
        )
        p1, f1, u1 = sync.build_tencent_file_ref("https://docs.qq.com/sheet/DVk9CRndKb3hhYW5v?tab=BB08J2")
        self.assertEqual(p1, {"file_url": "https://docs.qq.com/sheet/DVk9CRndKb3hhYW5v"})
        self.assertIsNone(f1)
        self.assertEqual(u1, "https://docs.qq.com/sheet/DVk9CRndKb3hhYW5v")

        p2, f2, _ = sync.build_tencent_file_ref("DVk9CRndKb3hhYW5v")
        self.assertEqual(p2, {"file_url": "https://docs.qq.com/sheet/DVk9CRndKb3hhYW5v"})
        self.assertEqual(f2, {"file_id": "DVk9CRndKb3hhYW5v"})

        p3, f3, _ = sync.build_tencent_file_ref("LrSgRxUBjDag")
        self.assertEqual(p3, {"file_id": "LrSgRxUBjDag"})
        self.assertEqual(f3, {"file_url": "https://docs.qq.com/sheet/LrSgRxUBjDag"})

    def test_file_url_fallback_and_preview_hidden_by_default(self) -> None:
        calls: list[tuple[str, dict]] = []

        class FakeClient:
            def call_tool(self, endpoint: str, tool_name: str, args: dict) -> dict:
                calls.append((tool_name, dict(args)))
                if tool_name == "sheet.get_sheet_info":
                    if "file_url" in args and "file_id" not in args:
                        raise RuntimeError("not found by url")
                    return {"sheets": [{"sheet_id": "s0", "sheet_name": "工作表1", "row_count": 2, "col_count": 2}]}
                if tool_name == "sheet.get_cell_data":
                    return {"csv_data": "A,B\n1,2\n"}
                return {}

        with patch.object(sync, "fetch_google_sheet_csv", return_value=[["A", "B"], ["1", "2"]]):
            res = sync.sync_one_task(
                FakeClient(),
                {"google_spreadsheet_id": "g1", "tencent_file_id": "D" + "x" * 16},
                force=True,
                debug_preview=False,
            )

        self.assertTrue(res["verified_hash_match"])
        self.assertNotIn("read_back_csv", res)
        self.assertEqual(calls[0][0], "sheet.get_sheet_info")
        self.assertIn("file_url", calls[0][1])
        self.assertEqual(calls[1][0], "sheet.get_sheet_info")
        self.assertIn("file_id", calls[1][1])


if __name__ == "__main__":
    unittest.main()

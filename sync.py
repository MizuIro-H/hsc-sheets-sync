#!/usr/bin/env python3
"""Google Sheets 到腾讯文档在线表格（官方 HTTP MCP）单向同步脚本。"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

MCP_GENERAL_URL = "https://docs.qq.com/openapi/mcp"
MCP_SHEET_URL = "https://docs.qq.com/api/v6/sheet/mcp"
DEFAULT_MAX_CELLS_PER_BATCH = 1500
MAX_READ_CELLS = 20000

_NUM_RE = re.compile(r"^-?(?:0|[1-9]\d*)(?:\.\d+)?$")


def infer_cell_payload(row: int, col: int, text: str) -> dict[str, Any] | None:
    """将文本单元格转换为腾讯 sheet-mcp 写入对象；空字符串返回 None。"""
    if text == "":
        return None

    upper = text.strip().upper()
    if upper == "TRUE":
        return {"row": row, "col": col, "value_type": "BOOL", "bool_value": True}
    if upper == "FALSE":
        return {"row": row, "col": col, "value_type": "BOOL", "bool_value": False}

    # 仅将标准短数字转为 NUMBER；超长纯整数（如 QQ 号、UID）或带前导零编号保留为 STRING，避免精度丢失
    stripped = text.strip()
    if _NUM_RE.match(stripped):
        int_digits = stripped.lstrip("-").split(".", 1)[0]
        if len(int_digits) <= 10:
            try:
                num_val = int(stripped) if "." not in stripped else float(stripped)
                return {
                    "row": row,
                    "col": col,
                    "value_type": "NUMBER",
                    "number_value": num_val,
                }
            except ValueError:
                pass

    return {"row": row, "col": col, "value_type": "STRING", "string_value": text}


def build_cell_values(rows: list[list[str]]) -> list[dict[str, Any]]:
    """把二维字符串矩阵转换成 sheet.set_range_value 所需的稀疏单元格列表。"""
    cells: list[dict[str, Any]] = []
    for r, row in enumerate(rows):
        for c, val in enumerate(row):
            item = infer_cell_payload(r, c, val)
            if item is not None:
                cells.append(item)
    return cells


def compute_matrix_hash(rows: list[list[str]]) -> str:
    """计算二维表格内容规范化后的 SHA-256 摘要，用于跳过未变更的同步。"""
    # 统一去除每行尾部纯空单元格与尾部纯空行，防止两平台空网格尺寸差异误触发写入
    normalized: list[list[str]] = []
    for row in rows:
        trimmed = list(row)
        while trimmed and trimmed[-1] == "":
            trimmed.pop()
        normalized.append(trimmed)
    while normalized and not normalized[-1]:
        normalized.pop()

    encoded = json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _col_letters_to_number(letters: str) -> int:
    """将 Excel 列字母（如 A、E、AA）转换为从 1 开始的列数。"""
    num = 0
    for ch in letters.upper():
        num = num * 26 + (ord(ch) - ord("A") + 1)
    return num


def _parse_range_dimensions(range_str: str) -> tuple[int, int]:
    """从 A1:E4 类范围字符串提取覆盖的最大行数与列数。"""
    matches = re.findall(r"([A-Za-z]+)(\d+)", range_str)
    if not matches:
        return 0, 0
    max_col = max(_col_letters_to_number(col) for col, _ in matches)
    max_row = max(int(row) for _, row in matches)
    return max_row, max_col


def extract_used_dimensions(sub: dict[str, Any]) -> tuple[int, int]:
    """兼容多种 get_sheet_info 返回结构，提取子表已用行列数。"""
    used = sub.get("used_range")
    if isinstance(used, str) and used.strip():
        r, c = _parse_range_dimensions(used)
        if r > 0 and c > 0:
            return r, c
    if isinstance(used, dict):
        r = used.get("row_count") or used.get("rows") or used.get("rowCount")
        c = used.get("col_count") or used.get("cols") or used.get("colCount")
        if r is None and used.get("end_row") is not None:
            r = int(used["end_row"]) + 1
        if c is None and used.get("end_col") is not None:
            c = int(used["end_col"]) + 1
        if r and c:
            return int(r), int(c)

    for r_key, c_key in (
        ("used_row_count", "used_col_count"),
        ("row_count", "col_count"),
        ("rowCount", "colCount"),
        ("max_row", "max_col"),
    ):
        r_val = sub.get(r_key)
        c_val = sub.get(c_key)
        if r_val and c_val:
            return int(r_val), int(c_val)
    return 0, 0


def parse_csv_rows(csv_text: str) -> list[list[str]]:
    """解析 UTF-8 CSV 文本（自动处理 BOM）为二维字符串数组。"""
    cleaned = csv_text.lstrip("\ufeff")
    if not cleaned.strip():
        return []
    reader = csv.reader(io.StringIO(cleaned))
    return [list(row) for row in reader]


def fetch_google_sheet_csv(sheet_id: str, gid: str = "0", timeout: int = 30) -> list[list[str]]:
    """通过 Google Sheets 公开只读导出地址拉取指定分页 CSV。"""
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"
    req = urllib.request.Request(url, headers={"User-Agent": "hsc-sheets-sync/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content_type = (resp.headers.get("Content-Type") or "").lower()
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"拉取 Google 表格失败 (HTTP {exc.code})；请确认源表已设为「知道链接的任何人可查看」或 Sheet ID / GID 正确。"
        ) from exc

    if "text/html" in content_type or raw.lstrip().lower().startswith("<!doctype html"):
        raise RuntimeError(
            "Google 表格返回了 HTML 登录页而非 CSV，说明该表尚未开启「知道链接的任何人可查看」权限。"
        )
    return parse_csv_rows(raw)


def parse_mcp_response_body(raw_text: str) -> dict[str, Any] | None:
    """兼容解析纯 JSON 与 SSE (text/event-stream) 格式的 MCP 响应体。"""
    trimmed = raw_text.strip()
    if not trimmed:
        return None
    if trimmed.startswith("{"):
        try:
            return json.loads(trimmed)
        except json.JSONDecodeError:
            return None

    # 从 SSE data: 行逆序提取最后一条有效 JSON-RPC 负载
    for line in reversed(trimmed.splitlines()):
        stripped = line.strip()
        if stripped.startswith("data:"):
            payload = stripped[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                return json.loads(payload)
            except json.JSONDecodeError:
                continue
    return None


class TencentMcpClient:
    """腾讯文档官方 HTTP MCP 客户端封装（共用 TENCENT_DOCS_TOKEN）。"""

    def __init__(self, token: str, timeout: int = 30) -> None:
        self._token = token.strip()
        if not self._token:
            raise ValueError("缺少环境变量 TENCENT_DOCS_TOKEN")
        self._timeout = timeout
        self._sessions: dict[str, str | None] = {}
        self._tools_cache: dict[str, list[dict[str, Any]]] = {}
        self._auth_header: str = self._token
        self._req_id = 0

    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    def _post_raw(
        self, endpoint: str, auth_header: str, session_id: str | None, payload: dict[str, Any]
    ) -> tuple[int, dict[str, str], str]:
        headers = {
            "Authorization": auth_header,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if session_id:
            headers["Mcp-Session-Id"] = session_id

        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(endpoint, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                resp_headers = {k.lower(): v for k, v in resp.headers.items()}
                return resp.status, resp_headers, resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            resp_headers = {k.lower(): v for k, v in (exc.headers.items() if exc.headers else [])}
            body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            return exc.code, resp_headers, body

    def _ensure_session(self, endpoint: str) -> str | None:
        if endpoint in self._sessions:
            return self._sessions[endpoint]

        init_payload = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "hsc-sheets-sync", "version": "1.0.0"},
            },
        }
        status, headers, body = self._post_raw(endpoint, self._auth_header, None, init_payload)
        # 若原样 Token 返回 401，自动兼容补加 Bearer 前缀重试
        if status == 401 and not self._auth_header.lower().startswith("bearer "):
            candidate = f"Bearer {self._token}"
            status, headers, body = self._post_raw(endpoint, candidate, None, init_payload)
            if status < 400:
                self._auth_header = candidate

        if status >= 400:
            raise RuntimeError(f"MCP initialize 失败 ({endpoint}) HTTP {status}: {body[:300]}")

        session_id = headers.get("mcp-session-id")
        self._sessions[endpoint] = session_id

        if session_id:
            # 按 MCP 规范发送 initialized 通知，忽略非关键返回值
            self._post_raw(
                endpoint,
                self._auth_header,
                session_id,
                {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            )
        return session_id

    def list_tools(self, endpoint: str) -> list[dict[str, Any]]:
        """拉取并缓存指定 MCP 端点已注册的工具清单与参数 Schema。"""
        if endpoint in self._tools_cache:
            return self._tools_cache[endpoint]

        session_id = self._ensure_session(endpoint)
        payload = {"jsonrpc": "2.0", "id": self._next_id(), "method": "tools/list", "params": {}}
        status, _, body = self._post_raw(endpoint, self._auth_header, session_id, payload)
        if status >= 400:
            raise RuntimeError(f"MCP tools/list({endpoint}) HTTP {status}: {body[:300]}")

        rpc = parse_mcp_response_body(body)
        if not rpc or rpc.get("error"):
            raise RuntimeError(f"MCP tools/list({endpoint}) 错误: {rpc or body[:300]}")

        tools = (rpc.get("result") or {}).get("tools") or []
        self._tools_cache[endpoint] = tools
        summary = [
            {
                "name": t.get("name"),
                "props": sorted(((t.get("inputSchema") or {}).get("properties") or {}).keys()),
            }
            for t in tools
        ]
        print(
            f"[MCP tools/list] {endpoint}: {json.dumps(summary, ensure_ascii=False)}",
            file=sys.stderr,
        )
        return tools

    def _resolve_tool_name(self, endpoint: str, desired_name: str) -> str:
        """在已缓存的 tools/list 中按精确/无前缀/下划线等候选规则匹配真实工具名。"""
        tools = self._tools_cache.get(endpoint)
        if not tools:
            return desired_name
        available = [t.get("name", "") for t in tools]
        short = desired_name.split(".", 1)[-1]
        candidates = [
            desired_name,
            short,
            desired_name.replace(".", "_"),
        ]
        for cand in candidates:
            if cand in available:
                return cand
        for name in available:
            if name.endswith(f".{short}") or name.endswith(f"_{short}"):
                return name
        return desired_name

    def _invoke_tool_once(
        self, endpoint: str, tool_name: str, arguments: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """发送单次 tools/call；若命中 JSON-RPC 错误则返回 (None, rpc_error)。"""
        session_id = self._ensure_session(endpoint)
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }
        status, _, body = self._post_raw(endpoint, self._auth_header, session_id, payload)
        if status >= 400:
            raise RuntimeError(f"MCP tools/call({tool_name}) HTTP {status}: {body[:300]}")

        rpc = parse_mcp_response_body(body)
        if not rpc:
            raise RuntimeError(f"MCP 响应无法解析 ({tool_name}): {body[:300]}")
        if rpc.get("error"):
            return None, rpc["error"]
        return rpc, None

    def call_tool(self, endpoint: str, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """调用指定 MCP 端点上的工具并统一处理工具名兼容、JSON-RPC 及业务错误。"""
        actual_name = self._resolve_tool_name(endpoint, tool_name)
        rpc, rpc_err = self._invoke_tool_once(endpoint, actual_name, arguments)

        # 若工具未找到（-32601），主动查询 tools/list 自动适配真实工具名后重试一次
        if rpc_err and rpc_err.get("code") == -32601 and endpoint not in self._tools_cache:
            self.list_tools(endpoint)
            resolved = self._resolve_tool_name(endpoint, tool_name)
            if resolved != actual_name:
                actual_name = resolved
                rpc, rpc_err = self._invoke_tool_once(endpoint, actual_name, arguments)

        if rpc_err:
            raise RuntimeError(f"MCP JSON-RPC 错误 ({actual_name}): {rpc_err}")

        result = (rpc or {}).get("result") or {}
        if result.get("isError"):
            raise RuntimeError(f"MCP 工具执行错误 ({actual_name}): {result}")

        parsed: dict[str, Any] = {}
        if "structuredContent" in result and isinstance(result["structuredContent"], dict):
            parsed = result["structuredContent"]
        elif isinstance(result.get("content"), list) and result["content"]:
            first_text = result["content"][0].get("text")
            if isinstance(first_text, str):
                try:
                    loaded = json.loads(first_text)
                    parsed = loaded if isinstance(loaded, dict) else {"text": first_text}
                except json.JSONDecodeError:
                    parsed = {"text": first_text}
        elif isinstance(result, dict):
            parsed = result

        if parsed.get("error") or (parsed.get("ret") not in (None, 0)):
            raise RuntimeError(f"腾讯文档返回业务错误 ({actual_name}): {parsed}")
        return parsed


def pick_target_subsheet(
    sheets: list[dict[str, Any]], desired_id: str | None = None, desired_name: str | None = None
) -> dict[str, Any]:
    """根据子表 ID 或名称匹配目标子表；若均未指定则回退至首个子表。"""
    if desired_id:
        for s in sheets:
            if s.get("sheet_id") == desired_id:
                return s
        raise RuntimeError(f"未找到指定的腾讯子表 ID: {desired_id}")
    if desired_name:
        for s in sheets:
            if s.get("sheet_name") == desired_name:
                return s
        raise RuntimeError(f"未找到指定的腾讯子表名称: {desired_name}")
    if not sheets:
        raise RuntimeError("腾讯表格未包含任何子表")
    return sheets[0]


def sync_one_task(
    client: TencentMcpClient, task: dict[str, Any], force: bool = False
) -> dict[str, Any]:
    """执行单个 Google 工作表到腾讯文档子表的单向覆盖同步。"""
    google_id = task["google_spreadsheet_id"].strip()
    google_gid = str(task.get("google_gid", "0")).strip()
    rows = fetch_google_sheet_csv(google_id, google_gid)
    src_hash = compute_matrix_hash(rows)

    tencent_file_id = (task.get("tencent_file_id") or "").strip()
    tencent_url = ""
    if not tencent_file_id:
        title = (task.get("create_title") or "[回传测试] 腾讯目标表")[:36]
        # 优先按标题复用已有同名测试表，避免重复调试产生多个副本
        search_res = client.call_tool(
            MCP_GENERAL_URL, "manage.search_file", {"search_key": title}
        )
        for item in search_res.get("list") or []:
            if item.get("title") == title and item.get("file_id"):
                tencent_file_id = item["file_id"]
                tencent_url = item.get("url") or f"https://docs.qq.com/sheet/{tencent_file_id}"
                break
        if not tencent_file_id:
            created = client.call_tool(
                MCP_GENERAL_URL, "manage.create_file", {"title": title, "file_type": "sheet"}
            )
            tencent_file_id = created.get("file_id", "")
            tencent_url = created.get("url") or f"https://docs.qq.com/sheet/{tencent_file_id}"
        if not tencent_file_id:
            raise RuntimeError("自动获取或创建腾讯空白表失败")
    else:
        tencent_url = f"https://docs.qq.com/sheet/{tencent_file_id}"
    print(f"[目标腾讯表] {tencent_file_id} -> {tencent_url}", file=sys.stderr)

    info = client.call_tool(MCP_SHEET_URL, "sheet.get_sheet_info", {"file_id": tencent_file_id})
    sub = pick_target_subsheet(
        info.get("sheets") or [],
        desired_id=task.get("tencent_sheet_id") or None,
        desired_name=task.get("tencent_sheet_name") or None,
    )
    sub_id = sub["sheet_id"]
    old_rows, old_cols = extract_used_dimensions(sub)
    print(
        f"[子表元数据] sheet_id={sub_id}, dims=({old_rows}, {old_cols}), raw={json.dumps(sub, ensure_ascii=False)}",
        file=sys.stderr,
    )

    # 通过回读目标当前内容做变化检测，未改动则直接跳过写入，减少版本碎片
    if not force and old_rows > 0 and old_cols > 0 and old_rows * old_cols <= MAX_READ_CELLS:
        existing = client.call_tool(
            MCP_SHEET_URL,
            "sheet.get_cell_data",
            {
                "file_id": tencent_file_id,
                "sheet_id": sub_id,
                "start_row": 0,
                "start_col": 0,
                "end_row": old_rows - 1,
                "end_col": old_cols - 1,
                "return_csv": True,
            },
        )
        existing_rows = parse_csv_rows(existing.get("csv_data") or "")
        existing_hash = compute_matrix_hash(existing_rows)
        if existing_hash == src_hash:
            return {
                "skipped": True,
                "reason": "目标表格内容已与源表一致",
                "tencent_file_id": tencent_file_id,
                "tencent_url": tencent_url,
                "sheet_id": sub_id,
            }
        print(
            f"[变化检测未命中] src_rows={len(rows)}, existing_rows={len(existing_rows)}, "
            f"src_hash={src_hash[:12]}, existing_hash={existing_hash[:12]}",
            file=sys.stderr,
        )
    else:
        print(
            f"[跳过变化检测] force={force}, old_rows={old_rows}, old_cols={old_cols}",
            file=sys.stderr,
        )

    max_src_cols = max((len(r) for r in rows), default=0)
    clear_end_row = max(old_rows - 1, len(rows) - 1, 0)
    clear_end_col = max(old_cols - 1, max_src_cols - 1, 0)
    if old_rows > 0 or rows:
        client.call_tool(
            MCP_SHEET_URL,
            "sheet.clear_range_cells",
            {
                "file_id": tencent_file_id,
                "sheet_id": sub_id,
                "start_row": 0,
                "start_col": 0,
                "end_row": clear_end_row,
                "end_col": clear_end_col,
            },
        )

    values = build_cell_values(rows)
    for start in range(0, len(values), DEFAULT_MAX_CELLS_PER_BATCH):
        batch = values[start : start + DEFAULT_MAX_CELLS_PER_BATCH]
        client.call_tool(
            MCP_SHEET_URL,
            "sheet.set_range_value",
            {"file_id": tencent_file_id, "sheet_id": sub_id, "values": batch},
        )

    read_back_csv = ""
    if rows and max_src_cols > 0 and len(rows) * max_src_cols <= MAX_READ_CELLS:
        rb = client.call_tool(
            MCP_SHEET_URL,
            "sheet.get_cell_data",
            {
                "file_id": tencent_file_id,
                "sheet_id": sub_id,
                "start_row": 0,
                "start_col": 0,
                "end_row": len(rows) - 1,
                "end_col": max_src_cols - 1,
                "return_csv": True,
            },
        )
        read_back_csv = rb.get("csv_data") or ""

    return {
        "skipped": False,
        "tencent_file_id": tencent_file_id,
        "tencent_url": tencent_url,
        "sheet_id": sub_id,
        "sheet_name": sub.get("sheet_name"),
        "source_rows": len(rows),
        "written_cells": len(values),
        "read_back_csv": read_back_csv,
    }


def load_tasks_from_env_or_config(config_path: Path) -> list[dict[str, Any]]:
    """从环境变量或配置文件加载同步任务列表，环境变量优先生效。"""
    env_google_id = os.getenv("GOOGLE_SHEET_ID", "").strip()
    if env_google_id:
        return [
            {
                "google_spreadsheet_id": env_google_id,
                "google_gid": os.getenv("GOOGLE_GID", "0").strip() or "0",
                "tencent_file_id": os.getenv("TENCENT_FILE_ID", "").strip(),
                "tencent_sheet_id": os.getenv("TENCENT_SHEET_ID", "").strip(),
                "tencent_sheet_name": os.getenv("TENCENT_SHEET_NAME", "").strip(),
                "create_title": os.getenv("TENCENT_CREATE_TITLE", "[回传测试] 腾讯目标表").strip(),
            }
        ]

    if not config_path.exists():
        raise FileNotFoundError(f"未找到配置文件且未设置 GOOGLE_SHEET_ID：{config_path}")
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    tasks = raw.get("tasks") if isinstance(raw, dict) else raw
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("配置文件中必须包含至少一个同步任务（tasks）")
    return tasks


def main() -> int:
    """命令行主入口：按配置依次同步全部任务。"""
    token = os.getenv("TENCENT_DOCS_TOKEN", "").strip()
    if not token:
        print("错误：未设置环境变量 TENCENT_DOCS_TOKEN", file=sys.stderr)
        return 2

    force = os.getenv("FORCE_SYNC", "").strip().lower() in {"1", "true", "yes"}
    config_file = Path(os.getenv("SYNC_CONFIG", "config.json"))
    tasks = load_tasks_from_env_or_config(config_file)
    client = TencentMcpClient(token)

    results = []
    for idx, task in enumerate(tasks, start=1):
        res = sync_one_task(client, task, force=force)
        results.append({"task": idx, **res})
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

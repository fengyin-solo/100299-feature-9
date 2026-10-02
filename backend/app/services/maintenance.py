"""维保记录业务规则：状态流转、字段校验、清单导入与筛选口径都收在这里。"""
from __future__ import annotations

import uuid
from typing import Any

from app.store import store

MODULE = "maintenance"
REQUIRED_FIELDS = ["维保编号", "维保设备", "维保单位"]
STATUS_ORDER = ["待维保", "维保中", "已完成", "需返工"]
ACTION_RULES = {"安排维保": "维保中", "开始维保": "已完成", "返工登记": "需返工"}
NEGATIVE_ACTIONS = []

IMPORT_FIELDS = ["维保编号", "维保设备", "维保单位", "维保内容", "维保日期", "维保人员", "更换部件", "维保状态"]
# 同一维保编号下允许跨多行累积的字段：逐行拼接，已读入的内容不丢、不留占位
MERGED_FIELDS = ["维保内容", "更换部件"]
DEFAULT_CHUNK = 50
MAX_CHUNK = 500


def _clean(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _merge_lines(values: list[str]) -> str:
    merged: list[str] = []
    for value in values:
        if value and value not in merged:
            merged.append(value)
    return "；".join(merged)


def prepare_import(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """整表校验并按维保编号合并，返回 (待入库记录, 行级退回, 批级错误)。

    - 缺维保编号或维保单位的行整行退回，并标明是清单第几行（从 1 数）；
    - 同一维保编号的多行合并成一条：维保内容、更换部件逐行拼接，其余字段取首个非空值；
    - 同一编号下维保设备不一致、或设备与内容缺一边，视为设备与内容对不上，整批拒绝。
    """
    rejected: list[dict[str, Any]] = []
    groups: dict[str, list[tuple[int, dict[str, str]]]] = {}
    for line_no, raw in enumerate(rows, start=1):
        row = {field: _clean(raw.get(field)) for field in IMPORT_FIELDS}
        if not row["维保编号"]:
            rejected.append({"row": line_no, "reason": "缺少维保编号，无法归入任何一批"})
            continue
        if not row["维保单位"]:
            rejected.append({"row": line_no, "reason": "缺少维保单位"})
            continue
        groups.setdefault(row["维保编号"], []).append((line_no, row))

    errors: list[str] = []
    records: list[dict[str, Any]] = []
    for code, members in groups.items():
        lines = "、".join(str(line_no) for line_no, _ in members)
        devices = {row["维保设备"] for _, row in members}
        if "" in devices:
            errors.append(f"维保编号 {code}（第{lines}行）存在只有维保内容、没有维保设备的行，设备与内容对不上")
            continue
        if len(devices) > 1:
            errors.append(f"维保编号 {code}（第{lines}行）对应了 {len(devices)} 台不同设备，设备与内容对不上")
            continue
        missing_content = [str(line_no) for line_no, row in members if not row["维保内容"]]
        if missing_content:
            errors.append(f"维保编号 {code}（第{'、'.join(missing_content)}行）有维保设备却没有维保内容，设备与内容对不上")
            continue
        entry = {
            field: _merge_lines([row[field] for _, row in members])
            if field in MERGED_FIELDS
            else next((row[field] for _, row in members if row[field]), "")
            for field in IMPORT_FIELDS
        }
        status = entry["维保状态"] if entry["维保状态"] in STATUS_ORDER else STATUS_ORDER[0]
        entry["维保状态"] = status
        records.append({"source_row": members[0][0], "entry": entry, "status": status})
    if errors:
        return [], rejected, errors
    return records, rejected, []


class MaintenanceService:
    def __init__(self) -> None:
        # 导入会话只活在内存里，重启即失效；已落库的记录靠维保编号去重，重传不会重复
        self._import_sessions: dict[str, dict[str, Any]] = {}

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        device: str | None = None,
        unit: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("维保编号", ""))]
        if device:
            rows = [row for row in rows if device in str(row.get("维保设备", ""))]
        if unit:
            rows = [row for row in rows if unit in str(row.get("维保单位", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"维保记录 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于维保记录可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"维保记录已{action}"

    def start_import(self, rows: list[dict[str, Any]], *, chunk_size: int = DEFAULT_CHUNK) -> dict[str, Any]:
        """校验整表并开立导入会话；批级校验不过时整批拒绝，一行都不写。"""
        records, rejected, errors = prepare_import(rows)
        if errors:
            return {
                "ok": False,
                "message": f"维保设备与维保内容对不上，整批 {len(rows)} 行已拒绝，未写入任何记录",
                "done": True,
                "rejected_rows": rejected,
                "errors": errors,
            }
        session_id = uuid.uuid4().hex
        session = {"records": records, "rejected": rejected, "cursor": 0, "created": 0, "skipped": 0}
        self._import_sessions[session_id] = session
        return self._commit_chunk(session_id, session, chunk_size)

    def resume_import(self, session_id: str, *, chunk_size: int = DEFAULT_CHUNK) -> dict[str, Any] | None:
        """从断掉的位置继续落库；会话不存在时返回 None，由调用方提示重新上传。"""
        session = self._import_sessions.get(session_id)
        if session is None:
            return None
        return self._commit_chunk(session_id, session, chunk_size)

    def _commit_chunk(self, session_id: str, session: dict[str, Any], chunk_size: int) -> dict[str, Any]:
        """按游标落库一批记录：已录过的维保编号跳过不新增，字段整组写入不留占位。"""
        chunk = max(1, min(int(chunk_size or DEFAULT_CHUNK), MAX_CHUNK))
        rows = store.rows(MODULE)
        existing = {str(row.get("维保编号", "")) for row in rows}
        next_id = max((int(row.get("id", 0)) for row in rows), default=0)
        records = session["records"]
        end = min(session["cursor"] + chunk, len(records))
        while session["cursor"] < end:
            record = records[session["cursor"]]
            code = record["entry"]["维保编号"]
            if code in existing:
                session["skipped"] += 1
            else:
                next_id += 1
                entry = {"id": next_id}
                entry.update(record["entry"])
                entry["status"] = record["status"]
                entry["pending"] = record["status"] != STATUS_ORDER[-1]
                entry["abnormal"] = False
                rows.append(entry)
                existing.add(code)
                session["created"] += 1
            session["cursor"] += 1
        done = session["cursor"] >= len(records)
        next_row = None if done else records[session["cursor"]]["source_row"]
        if done:
            message = f"导入完成：新增 {session['created']} 条，跳过已存在的 {session['skipped']} 条，退回 {len(session['rejected'])} 行"
        else:
            last_row = records[session["cursor"] - 1]["source_row"]
            message = f"已处理到第 {last_row} 行，如中断请从第 {next_row} 行继续"
        return {
            "ok": True,
            "message": message,
            "session_id": session_id,
            "done": done,
            "next_row": next_row,
            "created": session["created"],
            "skipped": session["skipped"],
            "rejected_rows": session["rejected"],
            "errors": [],
        }

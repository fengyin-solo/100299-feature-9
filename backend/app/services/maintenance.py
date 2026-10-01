"""维保记录业务规则：状态流转、字段校验、批量导入与筛选口径都收在这里。"""
from __future__ import annotations

import csv
import io
import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.store import store

MODULE = "maintenance"
REQUIRED_FIELDS = ["维保编号", "维保设备", "维保单位"]
# 导入/导出清单列，顺序即外委单位交表时的列顺序
SHEET_FIELDS = ["维保编号", "维保设备", "维保单位", "维保内容", "维保日期", "维保人员", "更换部件", "维保状态"]
# 同一维保编号下需要逐行收集、合并的字段（一行设备对一行内容）
MULTI_FIELDS = ["维保设备", "维保内容", "维保人员", "更换部件"]
STATUS_ORDER = ["待维保", "维保中", "已完成", "需返工"]
ACTION_RULES = {"安排维保": "维保中", "开始维保": "已完成", "返工登记": "需返工"}
NEGATIVE_ACTIONS = []


def _clean(value: Any) -> str:
    """把表格里的 None / 数字统一成去空白的字符串，避免读出的真实值变成占位。"""
    if value is None:
        return ""
    return str(value).strip()


def _join_values(values: list[str]) -> str:
    """合并同一编号下多行的值：保留去重后的真实值，用顿号连接；空值一律不进结果。"""
    seen: list[str] = []
    for value in values:
        text = _clean(value)
        if text and text not in seen:
            seen.append(text)
    return "、".join(seen)


def _build_group(number: str, rows: list[dict[str, str]]) -> dict[str, str]:
    """把同一维保编号的多行聚成一条记录；多值字段收集全部，单值字段取第一个非空值。"""
    group: dict[str, str] = {"维保编号": number}
    for name in SHEET_FIELDS[1:]:
        if name in MULTI_FIELDS:
            group[name] = _join_values([row.get(name, "") for row in rows])
        else:
            group[name] = next((_clean(row.get(name)) for row in rows if _clean(row.get(name))), "")
    return group


@dataclass
class ImportSession:
    """一次导入的断点现场：已校验通过的行按序放好，游标记住处理到第几行。

    跨同一编号的多行先在 group_buffer 里攒齐，碰到下一个编号（或文件结束）才落库，
    因此中断时不会写入“设备已进、内容没进”的半成品。
    """

    session_id: str
    rows: list[dict[str, str]]
    accepted_lines: list[int]
    reject_errors: list[str]
    groups: dict[str, list[dict[str, str]]]
    cursor: int = 0
    group_buffer: list[dict[str, str]] = field(default_factory=list)
    completed_groups: set[str] = field(default_factory=set)
    added: int = 0
    updated: int = 0
    batch_rejected: bool = False
    rejected_count: int = 0

    def result(self, *, rejected: int, batch_rejected: bool, message: str) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "done": self.done,
            "next_row": self.cursor + 1 if not self.done else 0,
            "processed_rows": self.cursor,
            "total_rows": len(self.rows),
            "added": self.added,
            "updated": self.updated,
            "rejected_rows": rejected,
            "reject_errors": self.reject_errors,
            "batch_rejected": batch_rejected,
            "message": message,
        }

    @property
    def done(self) -> bool:
        return self.cursor >= len(self.rows)

    @property
    def total_rows(self) -> int:
        return len(self.rows)


class MaintenanceService:
    def __init__(self) -> None:
        # 导入会话保存在内存里，续导时凭 session_id 找回断点现场
        self._sessions: dict[str, ImportSession] = {}

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        unit: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("维保编号", ""))]
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
        missing = [field_name for field_name in REQUIRED_FIELDS if not str(values.get(field_name) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field_name: values.get(field_name) for field_name in REQUIRED_FIELDS})
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

    # ------------------------------------------------------------------
    # 批量导入
    # ------------------------------------------------------------------
    def start_import(self, content: str, *, filename: str | None = None, limit: int | None = None) -> ImportSession:
        """解析外委单位交来的清单，先做整批校验，再建立可续导的会话。

        - 缺维保单位（或缺维保编号）的行整行退回，注明表格中的行号；
        - 同一维保编号下维保设备与维保内容不成一对一的，整批拒绝、一条不落库；
        - 校验通过后按 limit 推进一段，没推完可凭 session_id 续导。
        """
        rows = self._parse_sheet(content, filename)

        valid: list[dict[str, str]] = []
        accepted_lines: list[int] = []
        reject_errors: list[str] = []
        for line_no, raw in rows:
            values = {name: _clean(raw.get(name)) for name in SHEET_FIELDS}
            # 完全空白的行（表格尾部空行）直接跳过，不算退回
            if not any(values.values()):
                continue
            missing = [name for name in ("维保编号", "维保单位") if not values[name]]
            if missing:
                reject_errors.append(f"第{line_no}行缺少{'、'.join(missing)}，已整行退回")
                continue
            values["_line"] = line_no
            valid.append(values)
            accepted_lines.append(line_no)

        # 按维保编号聚合，校验“维保设备 ↔ 维保内容”是否一对一；对不上整批拒绝
        groups: dict[str, list[dict[str, str]]] = {}
        mismatch_errors: list[str] = []
        for values in valid:
            groups.setdefault(values["维保编号"], []).append(values)
        for number, grouped in groups.items():
            devices = [v["维保设备"] for v in grouped if v["维保设备"]]
            contents = [v["维保内容"] for v in grouped if v["维保内容"]]
            if len(devices) != len(contents):
                mismatch_errors.append(
                    f"维保编号「{number}」维保设备 {len(devices)} 项与维保内容 {len(contents)} 项对不上，整批拒绝导入"
                )

        session = ImportSession(
            session_id=uuid.uuid4().hex,
            rows=valid,
            accepted_lines=accepted_lines,
            reject_errors=reject_errors,
            groups=groups,
            rejected_count=len(reject_errors),
        )

        if mismatch_errors:
            # 整批拒绝：会话不登记、不可续导，提示调用方改完重新上传；
            # 缺维保单位的退回说明一并带回，但不写入任何记录。
            session.rows = []
            session.accepted_lines = []
            session.groups = {}
            session.batch_rejected = True
            session.rejected_count = len(reject_errors) + len(mismatch_errors)
            session.reject_errors.extend(mismatch_errors)
            return session

        self._sessions[session.session_id] = session
        # 按维保编号稳定排序，保证同编号行连续，交错排列也能正确合并成一条
        session.rows.sort(key=lambda item: item["维保编号"])
        if valid:
            self._advance(session, limit)
        return session

    def get_session(self, session_id: str) -> ImportSession | None:
        return self._sessions.get(session_id)

    def resume_import(self, session_id: str, *, limit: int | None = None) -> ImportSession | None:
        """从中断的那一行接着推进；游标之前的编号不会重复落库。"""
        session = self._sessions.get(session_id)
        if session is None:
            return None
        self._advance(session, limit)
        if session.done:
            self._sessions.pop(session_id, None)
        return session

    def _advance(self, session: ImportSession, limit: int | None) -> None:
        if not session.rows:
            return
        stop = len(session.rows) if limit is None or limit <= 0 else min(session.cursor + limit, len(session.rows))
        while session.cursor < stop:
            row = session.rows[session.cursor]
            number = row["维保编号"]
            if session.group_buffer and session.group_buffer[0]["维保编号"] != number:
                self._commit_group(session, _build_group(
                    session.group_buffer[0]["维保编号"], session.group_buffer
                ))
                session.group_buffer = []
            session.group_buffer.append(row)
            session.cursor += 1
        # 只有在整份清单全部读完时，才提交末尾这一组，杜绝半成品落库
        if session.cursor >= len(session.rows) and session.group_buffer:
            self._commit_group(session, _build_group(
                session.group_buffer[0]["维保编号"], session.group_buffer
            ))
            session.group_buffer = []

    def _commit_group(self, session: ImportSession, merged: dict[str, str]) -> None:
        number = merged["维保编号"]
        if number in session.completed_groups:
            return
        existing = self._find_by_number(number)
        if existing is None:
            entry = {"id": self._next_id()}
            for name in SHEET_FIELDS:
                entry[name] = merged.get(name, "")
            entry["status"] = merged.get("维保状态") or STATUS_ORDER[0]
            entry["pending"] = entry["status"] != STATUS_ORDER[-1]
            entry["abnormal"] = False
            store.rows(MODULE).append(entry)
            session.added += 1
        else:
            # 已录过的编号再来一次：用非空的新值更新，空值不覆盖，避免真实字段被抹成占位
            for name in SHEET_FIELDS:
                if name == "维保编号":
                    continue
                value = merged.get(name, "")
                if value:
                    existing[name] = value
            if merged.get("维保状态"):
                existing["status"] = merged["维保状态"]
                existing["pending"] = existing["status"] != STATUS_ORDER[-1]
            session.updated += 1
        session.completed_groups.add(number)

    def _find_by_number(self, number: str) -> dict[str, Any] | None:
        for row in store.rows(MODULE):
            if _clean(row.get("维保编号")) == number:
                return row
        return None

    def _next_id(self) -> int:
        return max((int(row.get("id", 0)) for row in store.rows(MODULE)), default=0) + 1

    def _parse_sheet(self, content: str, filename: str | None) -> list[tuple[int, dict[str, str]]]:
        """把上传内容解析成 (表格行号, 行字段) 列表；支持 JSON 数组和 CSV。

        表格行号含表头：第一条数据是第 2 行，便于外委单位照表定位问题行。
        """
        text = content.lstrip("﻿").strip()
        if not text:
            return []
        is_json = text[0] in "[{" or (filename or "").lower().endswith(".json")
        if is_json:
            data = json.loads(text)
            if isinstance(data, dict):
                data = data.get("items") or data.get("rows") or []
            return [(index + 2, {name: _clean(item.get(name)) for name in SHEET_FIELDS})
                    for index, item in enumerate(data) if isinstance(item, dict)]

        reader = csv.reader(io.StringIO(text))
        records = list(reader)
        if not records:
            return []
        header = [_clean(cell) for cell in records[0]]
        if "维保编号" in header:
            index_map = {name: header.index(name) for name in SHEET_FIELDS if name in header}

            def to_fields(cells: list[str]) -> dict[str, str]:
                return {name: _clean(cells[pos]) if pos < len(cells) else ""
                        for name, pos in index_map.items()}
        else:
            # 没有可识别表头时按约定列顺序解析
            def to_fields(cells: list[str]) -> dict[str, str]:
                return {name: _clean(cells[pos]) if pos < len(cells) else ""
                        for pos, name in enumerate(SHEET_FIELDS)}
        return [(line_no, to_fields(cells)) for line_no, cells in enumerate(records[1:], start=2)]

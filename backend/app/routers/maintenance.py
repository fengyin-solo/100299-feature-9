"""维保记录接口：维护维保记录，覆盖安排维保、开始维保、返工登记、批量导入导出等动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, ImportPayload, ImportResult, PageResult, ResumePayload
from app.services.maintenance import SHEET_FIELDS, ImportSession, MaintenanceService

router = APIRouter(prefix="/api/maintenance", tags=["维保记录"])

service = MaintenanceService()

LIST_FIELDS = ["维保编号", "维保设备", "维保单位", "维保内容", "维保日期", "维保人员", "更换部件", "维保状态"]
STATUSES = ["待维保", "维保中", "已完成", "需返工"]


def _summarize(session: ImportSession) -> ImportResult:
    """把导入会话整理成接口返回。"""
    if session.batch_rejected:
        message = "维保设备与维保内容对不上，整批拒绝导入，未写入任何记录"
        ok = False
    elif session.done:
        message = (
            f"导入完成：新增 {session.added} 条、更新 {session.updated} 条，退回 {session.rejected_count} 行"
        )
        ok = session.rejected_count == 0
    else:
        message = (
            f"已处理到第 {session.cursor} 行（新增 {session.added}、更新 {session.updated}），"
            f"可携带会话标识从第 {session.cursor + 1} 行续导"
        )
        ok = True
    return ImportResult(
        ok=ok,
        message=message,
        session_id=session.session_id,
        done=session.done,
        next_row=session.cursor + 1 if not session.done else 0,
        processed_rows=session.cursor,
        total_rows=session.total_rows,
        added=session.added,
        updated=session.updated,
        rejected_rows=session.rejected_count,
        batch_rejected=session.batch_rejected,
        reject_errors=session.reject_errors,
    )


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按维保编号检索"),
    unit: str | None = Query(default=None, description="按维保单位检索"),
    status: str | None = Query(default=None, description="待维保、维保中、已完成、需返工"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按维保编号、维保单位与状态过滤维保记录列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, unit=unit, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries(
    keyword: str | None = Query(default=None, description="按维保编号检索"),
    unit: str | None = Query(default=None, description="按维保单位检索，与页面筛选项一致"),
    status: str | None = Query(default=None, description="按维保状态检索"),
) -> dict[str, Any]:
    """导出维保记录清单：沿用页面当前筛选条件，返回全量，条数与页面看到的 total 一致。"""
    items, total = service.list_entries(keyword=keyword, unit=unit, status=status, page=1, size=100000)
    return {
        "module": "maintenance",
        "fields": SHEET_FIELDS,
        "filters": {"keyword": keyword, "unit": unit, "status": status},
        "total": total,
        "items": items,
    }


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条维保记录明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"维保记录 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条维保记录，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="维保记录已登记", entry=entry)


@router.post("/import", response_model=ImportResult)
def import_entries(payload: ImportPayload) -> ImportResult:
    """导入外委单位交来的维保清单。

    - 按维保编号合并同一批多行；
    - 缺维保单位（或缺维保编号）的整行退回，并说明是表格第几行；
    - 维保设备与维保内容对不上的整批拒绝、一条不写；
    - 已录过的编号再次导入只更新、不新增重复记录；
    - 传 limit 可分批，未导完时返回会话标识，调用 /import/resume 从断点行继续。
    """
    if not payload.content.strip():
        return ImportResult(ok=False, message="导入内容为空，请先选择或粘贴维保清单")
    session = service.start_import(payload.content, filename=payload.filename, limit=payload.limit)
    return _summarize(session)


@router.post("/import/resume", response_model=ImportResult)
def resume_import(payload: ResumePayload) -> ImportResult:
    """断点续导：从上次中断的那一行接着走，已读入的字段与编号不会重复落库。"""
    session = service.resume_import(payload.session_id, limit=payload.limit)
    if session is None:
        raise HTTPException(status_code=404, detail="导入会话不存在或已结束，请重新上传维保清单")
    return _summarize(session)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条维保记录执行安排维保、开始维保、返工登记；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)

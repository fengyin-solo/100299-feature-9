"""维保记录接口：维护维保记录，覆盖安排维保、开始维保、返工登记与清单导入导出。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, ImportPayload, ImportResult, PageResult
from app.services.maintenance import MaintenanceService

router = APIRouter(prefix="/api/maintenance", tags=["维保记录"])

service = MaintenanceService()

LIST_FIELDS = ["维保编号", "维保设备", "维保单位", "维保内容", "维保日期", "维保人员", "更换部件", "维保状态"]
STATUSES = ["待维保", "维保中", "已完成", "需返工"]


def _strip(value: str | None) -> str | None:
    """空白筛选条件视为未填，避免把空串当成过滤值。"""
    if value is None:
        return None
    value = value.strip()
    return value or None


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, alias="维保编号", description="按维保编号检索"),
    device: str | None = Query(default=None, alias="维保设备", description="按维保设备检索"),
    unit: str | None = Query(default=None, alias="维保单位", description="按维保单位检索"),
    status: str | None = Query(default=None, description="待维保、维保中、已完成、需返工"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按维保编号、维保设备、维保单位与状态过滤维保记录列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(
        keyword=_strip(keyword),
        device=_strip(device),
        unit=_strip(unit),
        status=_strip(status),
        page=page,
        size=size,
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries(
    keyword: str | None = Query(default=None, alias="维保编号", description="按维保编号检索"),
    device: str | None = Query(default=None, alias="维保设备", description="按维保设备检索"),
    unit: str | None = Query(default=None, alias="维保单位", description="按维保单位检索"),
    status: str | None = Query(default=None, description="待维保、维保中、已完成、需返工"),
) -> dict[str, Any]:
    """导出维保清单：与列表页共用同一套筛选口径，导出条数与页面上看到的总数一致。"""
    items, total = service.list_entries(
        keyword=_strip(keyword),
        device=_strip(device),
        unit=_strip(unit),
        status=_strip(status),
        page=1,
        size=10000,
    )
    return {"module": "maintenance", "total": total, "items": items}


@router.post("/import", response_model=ImportResult)
def import_entries(payload: ImportPayload) -> ImportResult:
    """导入维保清单：整表校验、按维保编号合并、分批落库；中断后凭会话号从断掉的那一行接着走。"""
    if payload.session_id:
        result = service.resume_import(payload.session_id, chunk_size=payload.chunk_size)
        if result is None:
            return ImportResult(ok=False, message="导入会话已过期，请重新提交清单；已导入的维保编号不会重复录入")
        return ImportResult(**result)
    if not payload.rows:
        return ImportResult(ok=False, message="清单里没有可导入的数据行")
    return ImportResult(**service.start_import(payload.rows, chunk_size=payload.chunk_size))


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


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条维保记录执行安排维保、开始维保、返工登记；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)

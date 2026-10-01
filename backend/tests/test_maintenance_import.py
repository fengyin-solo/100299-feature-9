"""维保记录批量导入导出的验收测试。

覆盖：
1. 按维保编号合并同一批数据；
2. 缺维保单位的整行退回并说明第几行；
3. 维保设备与维保内容对不上时整批拒绝；
4. 已录过的编号再来一次不新增、只更新；
5. 中断后从断掉的那一行续导；
6. 已读入的字段保留真实值，不被抹成占位；
7. 导出按当前维保单位筛选，条数与页面一致。
"""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture()
def service():
    # 每个用例重新载入 store，拿到干净的种子数据
    import app.store as store_module
    importlib.reload(store_module)
    import app.services.maintenance as service_module
    importlib.reload(service_module)
    return service_module.MaintenanceService()


HEADER = "维保编号,维保设备,维保单位,维保内容,维保日期,维保人员,更换部件,维保状态"


def _import(service, text, limit=None):
    session = service.start_import(text, limit=limit)
    return session


def test_merge_rows_by_number(service):
    text = "\n".join([
        HEADER,
        "M-100,锅炉A,甲外委,清灰检查,2026-10-01,张三,滤芯,",
        "M-100,锅炉A,甲外委,换滤芯,2026-10-01,李四,,",
        "M-200,电梯B,乙外委,润滑保养,2026-10-02,王五,,",
    ])
    session = _import(service, text)
    assert session.done
    assert session.added == 2

    merged = service._find_by_number("M-100")
    assert merged is not None
    # 多值字段逐行合并，去重后保留全部真实值
    assert merged["维保内容"] == "清灰检查、换滤芯"
    assert merged["维保人员"] == "张三、李四"
    assert merged["更换部件"] == "滤芯"
    assert merged["维保设备"] == "锅炉A"
    assert merged["维保单位"] == "甲外委"
    assert merged["维保日期"] == "2026-10-01"


def test_missing_unit_row_rejected_with_line_number(service):
    text = "\n".join([
        HEADER,
        "M-1,设备1,甲单位,内容1,,,,",   # 第2行：正常
        "M-2,设备2,,内容2,,,,",         # 第3行：缺维保单位 -> 退回
        "M-3,设备3,丙单位,内容3,,,,",   # 第4行：正常
        ",设备4,丁单位,内容4,,,,",       # 第5行：缺维保编号 -> 退回
    ])
    session = _import(service, text)
    assert session.done
    assert session.added == 2
    assert service._find_by_number("M-2") is None
    assert session.rejected_count == 2
    joined = " ".join(session.reject_errors)
    assert "第3行" in joined and "维保单位" in joined
    assert "第5行" in joined and "维保编号" in joined


def test_device_content_mismatch_rejects_whole_batch(service):
    before = len(service.list_entries(page=1, size=1000)[0])
    text = "\n".join([
        HEADER,
        "M-1,设备1,甲单位,内容1,,,,",
        "M-1,设备2,甲单位,,,,,",  # 第3行：有设备没内容 -> 设备2项/内容1项
        "M-9,设备9,乙单位,内容9,,,,",  # 即使本行合法也不允许写入
    ])
    session = _import(service, text)
    assert session.batch_rejected is True
    assert session.added == 0
    assert service.get_session(session.session_id) is None  # 整批拒绝不可续导
    after = len(service.list_entries(page=1, size=1000)[0])
    assert after == before  # 一条都没落库
    assert service._find_by_number("M-9") is None


def test_existing_number_updates_instead_of_duplicate(service):
    existing = service._find_by_number("MAIN-0001")
    assert existing is not None
    before = len(service.list_entries(page=1, size=1000)[0])

    text = "\n".join([
        HEADER,
        "MAIN-0001,新设备,新单位,新内容,2026-10-05,新人员,新部件,已完成",
    ])
    session = _import(service, text)
    assert session.added == 0
    assert session.updated == 1
    after = len(service.list_entries(page=1, size=1000)[0])
    assert after == before  # 不新增重复记录

    updated = service._find_by_number("MAIN-0001")
    assert updated["id"] == existing["id"]
    assert updated["维保单位"] == "新单位"
    assert updated["维保内容"] == "新内容"
    assert updated["status"] == "已完成"


def test_resume_from_interrupted_row(service):
    text = "\n".join([
        HEADER,
        "R-1,d1,u1,c1,,,,",
        "R-2,d2,u2,c2,,,,",
        "R-3,d3,u3,c3,,,,",
        "R-4,d4,u4,c4,,,,",
    ])
    session = _import(service, text, limit=2)
    assert not session.done
    assert session.cursor == 2
    # R-1 已提交；R-2 因还没走到下一组边界仍在缓冲，尚未落库
    assert session.added == 1

    # 再续导 1 行：走到 R-3 时把缓冲里的 R-2 提交
    session = service.resume_import(session.session_id, limit=1)
    assert session.cursor == 3
    assert session.added == 2
    # 之前已写入的编号不会重复
    numbers = [row["维保编号"] for row in service.list_entries(page=1, size=1000)[0]]
    assert numbers.count("R-1") == 1

    # 一次续完：R-3、R-4 在文件结束时提交
    session = service.resume_import(session.session_id, limit=10)
    assert session.done
    assert session.added == 4
    assert service._find_by_number("R-4")["维保内容"] == "c4"


def test_no_partial_group_when_cut_mid_group(service):
    # 同一编号两行，limit=1 正好切在中间：此时不得落下半成品
    text = "\n".join([
        HEADER,
        "G-1,设备1,u1,内容1,,,,",
        "G-1,设备2,u1,内容2,,,,",
        "G-2,设备3,u2,内容3,,,,",
    ])
    session = _import(service, text, limit=1)
    assert session.cursor == 1
    assert service._find_by_number("G-1") is None  # 还没攒齐，不落库

    session = service.resume_import(session.session_id, limit=2)
    assert session.done
    g1 = service._find_by_number("G-1")
    assert g1["维保设备"] == "设备1、设备2"
    assert g1["维保内容"] == "内容1、内容2"


def test_imported_fields_keep_real_values_not_placeholders(service):
    # 第二次导入只带部分字段，空字段不能把已有真实值抹成空白
    _import(service, "\n".join([HEADER, "P-1,设备1,甲单位,内容1,2026-10-01,张三,轴承,待维保"]))
    _import(service, "\n".join(["维保编号,维保单位,维保日期", "P-1,甲单位,2026-10-09"]))
    row = service._find_by_number("P-1")
    assert row["维保设备"] == "设备1"
    assert row["维保内容"] == "内容1"
    assert row["维保人员"] == "张三"
    assert row["更换部件"] == "轴承"
    assert row["维保日期"] == "2026-10-09"  # 非空的新值照常更新


def test_export_matches_current_unit_filter(service):
    # 两个不同维保单位
    _import(service, "\n".join([
        HEADER,
        "E-1,设备A,华东维保,内容A,,,,",
        "E-2,设备B,华北维保,内容B,,,,",
        "E-3,设备C,华东维保,内容C,,,,",
    ]))
    page_items, page_total = service.list_entries(unit="华东维保", page=1, size=1000)
    export_items, export_total = service.list_entries(unit="华东维保", page=1, size=100000)
    assert page_total == export_total
    assert export_total == len(export_items)
    assert all("华东维保" in str(item["维保单位"]) for item in export_items)
    assert {item["维保编号"] for item in export_items} >= {"E-1", "E-3"}


def test_parse_json_array_supported(service):
    text = '[{"维保编号": "J-1", "维保设备": "d", "维保单位": "u", "维保内容": "c"}]'
    session = _import(service, text)
    assert session.done
    assert session.added == 1
    assert service._find_by_number("J-1")["维保内容"] == "c"

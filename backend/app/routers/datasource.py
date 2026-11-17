"""采集数据源池接口：数据源条目维护，以及代理降级切换、主备清单与失败核对。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult, WorklistResult
from app.services.datasource import DatasourceService

router = APIRouter(prefix="/api/datasource", tags=["采集数据源池"])

service = DatasourceService()

LIST_FIELDS = ["条目编号", "数据源名称", "源类型", "接入协议", "认证方式", "优先级", "最近采集", "条目状态"]
STATUSES = ["草稿", "已接入", "已暂停", "已移除"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按条目编号检索"),
    status: str | None = Query(default=None, description="草稿、已接入、已暂停、已移除"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按条目编号与状态过滤采集数据源池列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出采集数据源池清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "datasource", "total": total, "items": items}


@router.get("/standby", response_model=WorklistResult)
def standby_entries() -> WorklistResult:
    """数据源主备清单：每个区域唯一降级版本，已关闭代理保留但标为已退出。"""
    items = service.standby_entries()
    if not items:
        return WorklistResult(
            items=[], total=0, state="empty",
            message="暂无主备清单数据，请先登记代理配置与数据源条目",
        )
    return WorklistResult(
        items=items,
        total=len(items),
        state="ok",
        summary={
            "区域数": len(items),
            "已降级区域": sum(1 for row in items if row.get("降级状态") == "已降级"),
            "降级待复核": sum(1 for row in items if row.get("降级状态") == "降级待复核"),
            "恢复等待": sum(1 for row in items if row.get("恢复状态") == "恢复等待"),
            "已关闭代理": sum(1 for row in items if row.get("代理状态") == "已关闭"),
        },
    )


@router.get("/degrade/results", response_model=WorklistResult)
def degrade_results(
    version: str | None = Query(default=None, description="按降级版本过滤"),
    region: str | None = Query(default=None, description="按区域标签过滤"),
    page: int = 1,
    size: int = 20,
) -> WorklistResult:
    """降级切换结果：同一版本同一区域只有一条，读取顺序固定。"""
    items, total = service.degrade_results(version=version, region=region, page=page, size=size)
    if not items:
        if version or region:
            message = f"降级版本「{version or '全部'}」在区域「{region or '全部'}」暂无切换结果"
        else:
            message = "暂无降级切换结果，可先发起一次代理降级切换"
        return WorklistResult(items=[], total=0, page=page, size=size, state="empty", message=message)
    return WorklistResult(
        items=items,
        total=total,
        page=page,
        size=size,
        state="ok",
        summary={
            "结果条数": total,
            "已降级": sum(1 for row in items if row.get("降级状态") == "已降级"),
            "切换失败": sum(1 for row in items if row.get("降级状态") == "切换失败"),
            "恢复等待": sum(1 for row in items if row.get("恢复状态") == "恢复等待"),
        },
    )


@router.get("/degrade/failures", response_model=WorklistResult)
def degrade_failures(
    version: str | None = Query(default=None, description="按降级版本过滤"),
    region: str | None = Query(default=None, description="按区域标签过滤"),
    review_status: str | None = Query(default=None, description="待复核、已复核、已排除"),
    page: int = 1,
    size: int = 20,
) -> WorklistResult:
    """代理降级切换失败核对清单：失败与空路由原因都写在这里，供页面空态展示。"""
    items, total = service.degrade_failures(
        version=version, region=region, review_status=review_status, page=page, size=size,
    )
    if not items:
        if version or region or review_status:
            message = "当前筛选条件下暂无降级切换失败记录"
        else:
            message = "暂无降级切换失败记录，最近一次切换未产生失败项"
        return WorklistResult(items=[], total=0, page=page, size=size, state="empty", message=message)
    return WorklistResult(
        items=items,
        total=total,
        page=page,
        size=size,
        state="ok",
        summary={
            "失败条数": total,
            "待复核": sum(1 for row in items if row.get("复核状态") == "待复核"),
            "已排除": sum(1 for row in items if row.get("复核状态") == "已排除"),
            "空路由": sum(1 for row in items if row.get("空路由说明")),
        },
    )


@router.get("/degrade/route-diff", response_model=WorklistResult)
def route_diff() -> WorklistResult:
    """采集代理路由与采集数据源池的差异：定位空路由、代理关闭与备用源不可用。"""
    items = service.route_diff()
    if not items:
        return WorklistResult(
            items=[], total=0, state="empty",
            message="暂无可比较的区域，请先登记带区域标签的代理配置",
        )
    issues = [row for row in items if row.get("路由结论") != "路由可用"]
    return WorklistResult(
        items=items,
        total=len(items),
        state="ok",
        summary={
            "区域数": len(items),
            "路由可用": len(items) - len(issues),
            "存在差异": len(issues),
        },
    )


@router.post("/degrade/request", response_model=ActionResult)
def request_degrade(payload: EntryPayload) -> ActionResult:
    """发起区域降级切换；同区域重复请求复用既有降级版本，不重复迁移。"""
    entry, ok, message = service.request_degrade(payload.values)
    return ActionResult(ok=ok, message=message, entry=entry)


@router.post("/degrade/retry", response_model=ActionResult)
def retry_degrade(payload: EntryPayload) -> ActionResult:
    """按降级版本续做：已完成区域跳过，关闭代理不会被重新迁入主备清单。"""
    entry, ok, message = service.retry_degrade(payload.values)
    return ActionResult(ok=ok, message=message, entry=entry)


@router.post("/degrade/recovery", response_model=ActionResult)
def receive_recovery(payload: EntryPayload) -> ActionResult:
    """接收恢复回执：按降级是否完成，分别落到降级待复核或恢复等待。"""
    entry, ok, message = service.receive_recovery(payload.values)
    return ActionResult(ok=ok, message=message, entry=entry)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条数据源条目明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"数据源条目 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条数据源条目，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="数据源条目已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条数据源条目执行接入源、暂停源、移除源；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)

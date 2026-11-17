"""采集代理路由接口：维护代理配置，覆盖启用代理、降级代理、关闭代理等动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult, WorklistResult
from app.services.proxy import ProxyService
from app.store import store

router = APIRouter(prefix="/api/proxy", tags=["采集代理路由"])

service = ProxyService()

LIST_FIELDS = ["配置编号", "代理地址", "代理协议", "区域标签", "可用比例", "切换阈值", "响应延迟", "配置状态"]
STATUSES = ["草稿", "已启用", "已降级", "已关闭"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按配置编号检索"),
    status: str | None = Query(default=None, description="草稿、已启用、已降级、已关闭"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按配置编号与状态过滤采集代理路由列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出采集代理路由清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "proxy", "total": total, "items": items}


@router.get("/health-samples", response_model=WorklistResult)
def health_samples(
    region: str | None = Query(default=None, description="按区域标签过滤"),
    page: int = 1,
    size: int = 20,
) -> WorklistResult:
    """读取代理健康采样；没有采样时返回空态并说明原因，不伪造数据。"""
    rows = store.aux_rows("proxy_health_samples")
    if region:
        rows = [row for row in rows if row.get("区域标签") == region]
    rows = list(rows)
    total = len(rows)
    start = max(page - 1, 0) * size
    items = rows[start:start + size]
    if not items:
        message = (
            f"区域「{region}」暂无代理健康采样" if region else "暂无代理健康采样，等待采集节点上报"
        )
        return WorklistResult(items=[], total=0, page=page, size=size, state="empty", message=message)
    below = sum(1 for row in items if str(row.get("采样结论") or "").startswith("低于阈值"))
    return WorklistResult(
        items=items,
        total=total,
        page=page,
        size=size,
        state="ok",
        message="",
        summary={"采样条数": total, "低于阈值": below},
    )


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条代理配置明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"代理配置 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条代理配置，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="代理配置已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条代理配置执行启用代理、降级代理、关闭代理；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)

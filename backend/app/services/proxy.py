"""采集代理路由业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "proxy"
REQUIRED_FIELDS = ["配置编号", "代理地址", "代理协议"]
STATUS_ORDER = ["草稿", "已启用", "已降级", "已关闭"]
# 动作 -> 执行后的目标状态
ACTION_RESULTS = {"启用代理": "已启用", "降级代理": "已降级", "关闭代理": "已关闭"}
# 动作 -> 允许执行的前置状态
ACTION_FROM = {
    "启用代理": {"草稿"},
    "降级代理": {"已启用"},
    "关闭代理": {"已启用", "已降级"},
}
NEGATIVE_ACTIONS = ["降级代理"]
CLOSED_STATUS = "已关闭"


class ProxyService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("配置编号", ""))]
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
        entry["区域标签"] = values.get("区域标签")
        entry["可用比例"] = values.get("可用比例")
        entry["切换阈值"] = values.get("切换阈值")
        entry["响应延迟"] = values.get("响应延迟")
        entry["配置状态"] = "待启用"
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        from app.services.datasource import DatasourceService  # 延迟导入，避免模块级循环依赖

        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"代理配置 {entry_id} 不存在或已归档"
        if action not in ACTION_RESULTS:
            return None, f"动作「{action}」不属于采集代理路由可执行范围"
        if entry.get("status") == CLOSED_STATUS:
            # 已关闭是终态：任何动作（包括降级重试）都不能把它重新拉回主备清单。
            return None, (
                f"代理配置 {entry.get('配置编号')} 已关闭，不能再次执行「{action}」，"
                "如需恢复请重新登记代理配置"
            )
        if entry.get("status") not in ACTION_FROM[action]:
            return None, f"代理配置当前状态「{entry.get('status')}」不能执行「{action}」"

        datasource_service = DatasourceService()
        region = str(entry.get("区域标签") or "").strip()

        if action == "降级代理":
            if not region:
                return None, f"代理配置 {entry.get('配置编号')} 缺少区域标签，无法执行降级切换"
            state, ok, message = datasource_service.request_degrade({
                "区域标签": region,
                "代理配置编号": entry.get("配置编号"),
            })
            if not ok:
                return None, message
            entry["pending"] = state.get("降级状态") != "已降级"
            entry["abnormal"] = True
            return entry, message

        if action == "关闭代理" and region:
            # 关闭前先退役区域降级状态，保留代理快照，但不删除主备清单记录。
            datasource_service.retire_closed_proxy(region, str(entry.get("配置编号") or ""))

        entry["status"] = ACTION_RESULTS[action]
        entry["pending"] = entry["status"] != CLOSED_STATUS
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        if entry["status"] == "已启用":
            entry["配置状态"] = "可用"
        elif entry["status"] == CLOSED_STATUS:
            entry["配置状态"] = "已关闭"
        return entry, f"代理配置已{action}"

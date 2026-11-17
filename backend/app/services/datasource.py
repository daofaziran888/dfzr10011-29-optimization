"""采集数据源池业务规则：数据源流转、代理降级切换事务与主备流量口径都收在这里。

降级切换的关键约束：
1. 每个代理区域只保留一份降级状态，同一个降级版本在同一区域只落一条结果；
2. 重试按降级版本续做，已完成区域直接跳过，不重复迁移；
3. 切换事务失败时保留原代理快照，空路由要写清原因；
4. 同一区域同时收到恢复回执与新的降级请求时，按事件完成先后分别落到
   「降级待复核」或「恢复等待」两条路径上；
5. 已关闭代理保留在主备清单里，但重试不能把它重新变成主用线路。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.store import store

MODULE = "datasource"
REQUIRED_FIELDS = ["条目编号", "数据源名称", "源类型"]
STATUS_ORDER = ["草稿", "已接入", "已暂停", "已移除"]
ACTION_RULES = {"接入源": "已接入", "暂停源": "已暂停", "移除源": "已移除"}
ACTION_FROM = {
    "接入源": {"草稿"},
    "暂停源": {"已接入"},
    "移除源": {"已暂停"},
}
NEGATIVE_ACTIONS = []

PROXY_MODULE = "proxy"
REGION_TABLE = "degrade_regions"
BATCH_TABLE = "degrade_batches"
FAILURE_TABLE = "degrade_failures"
RESULT_TABLE = "degrade_results"
HEALTH_TABLE = "proxy_health_samples"

PROXY_CLOSED = "已关闭"
DATASOURCE_READY = "已接入"
DEGRADE_ACTIVE = {"切换中", "已降级", "降级待复核", "切换失败"}
RECOVERY_PENDING = {"恢复中", "恢复等待"}

PROXY_SNAPSHOT_FIELDS = [
    "配置编号",
    "代理地址",
    "代理协议",
    "区域标签",
    "可用比例",
    "切换阈值",
    "响应延迟",
    "status",
    "配置状态",
]


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _next_id(rows: list[dict[str, Any]]) -> int:
    return max((int(row.get("id", 0)) for row in rows), default=0) + 1


def _proxy_snapshot(proxy: dict[str, Any]) -> dict[str, Any]:
    return {field: proxy.get(field) for field in PROXY_SNAPSHOT_FIELDS}


def _region_proxies(region: str) -> list[dict[str, Any]]:
    """同一区域的全部代理配置，可用代理排在已关闭代理前面，保证选主稳定。"""
    rows = [row for row in store.rows(PROXY_MODULE) if str(row.get("区域标签") or "") == region]
    rows.sort(key=lambda row: (row.get("status") == PROXY_CLOSED, int(row.get("id", 0))))
    return rows


def _region_proxy(region: str, proxy_code: str | None = None) -> dict[str, Any] | None:
    rows = _region_proxies(region)
    if proxy_code:
        for row in rows:
            if str(row.get("配置编号") or "") == proxy_code:
                return row
    return rows[0] if rows else None


def _region_datasource(region: str) -> dict[str, Any] | None:
    """优先取已接入且优先级最高的数据源；没有可用源时回退到同区域任意条目用于展示。"""
    rows = [row for row in store.rows(MODULE) if str(row.get("区域标签") or "") == region]
    if not rows:
        return None
    rows.sort(
        key=lambda row: (
            row.get("status") != DATASOURCE_READY,
            _priority(row),
            int(row.get("id", 0)),
        )
    )
    return rows[0]


def _priority(row: dict[str, Any]) -> int:
    try:
        return int(row.get("优先级") or 999)
    except (TypeError, ValueError):
        return 999


def _healthy_datasource(region: str) -> dict[str, Any] | None:
    row = _region_datasource(region)
    if row is None or row.get("status") != DATASOURCE_READY:
        return None
    return row


def _state(region: str) -> dict[str, Any] | None:
    for row in store.aux_rows(REGION_TABLE):
        if str(row.get("区域标签") or "") == region:
            return row
    return None


def _batch(version: str) -> dict[str, Any] | None:
    for row in store.aux_rows(BATCH_TABLE):
        if str(row.get("降级版本") or "") == version:
            return row
    return None


def _result(version: str, region: str) -> dict[str, Any] | None:
    for row in store.aux_rows(RESULT_TABLE):
        if str(row.get("降级版本") or "") == version and str(row.get("区域标签") or "") == region:
            return row
    return None


def _failure(version: str, region: str) -> dict[str, Any] | None:
    for row in store.aux_rows(FAILURE_TABLE):
        if str(row.get("降级版本") or "") == version and str(row.get("区域标签") or "") == region:
            return row
    return None


def _new_version() -> str:
    """按天生成不重复的降级版本号；重试复用同一版本，不因失败另起新版本。"""
    prefix = f"DEGR-{datetime.now().strftime('%Y%m%d')}-"
    rows = store.aux_rows(BATCH_TABLE)
    used = set()
    for row in rows:
        version = str(row.get("降级版本") or "")
        if version.startswith(prefix):
            suffix = version[len(prefix):]
            if suffix.isdigit():
                used.add(int(suffix))
    seq = max(used, default=0) + 1
    return f"{prefix}{seq:02d}"


class DatasourceService:
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
            rows = [row for row in rows if keyword in str(row.get("条目编号", ""))]
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
        entry = {"id": _next_id(rows)}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["区域标签"] = values.get("区域标签")
        entry["主备角色"] = values.get("主备角色") or "备用源"
        entry["当前流量"] = "空闲"
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"数据源条目 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于采集数据源池可执行范围"
        if entry.get("status") == STATUS_ORDER[-1]:
            return None, f"数据源条目 {entry.get('条目编号')} 已移除，不能再次执行「{action}」"
        allowed = ACTION_FROM.get(action, set())
        if entry.get("status") not in allowed:
            return None, f"数据源条目当前状态「{entry.get('status')}」不能执行「{action}」"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        if action == "暂停源" and entry.get("当前流量") == "承载备用流量":
            return None, f"数据源条目 {entry.get('条目编号')} 正在承载备用流量，请先完成降级恢复再暂停"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"数据源条目已{action}"

    def retire_closed_proxy(self, region: str, proxy_code: str) -> dict[str, Any] | None:
        """代理关闭后退役区域降级状态：保留代理记录，撤销备用流量，避免重试复活。"""
        state = _state(region)
        if state is None:
            return None
        if state.get("主用线路") == "备用源":
            datasource = _region_datasource(region)
            if datasource is not None and datasource.get("当前流量") == "承载备用流量":
                datasource["当前流量"] = "空闲"
        state.update({
            "代理配置编号": proxy_code or state.get("代理配置编号") or "",
            "降级状态": "已停止",
            "主用线路": "代理",
            "恢复状态": "无",
            "最近事件": f"代理配置 {proxy_code} 已关闭，区域降级状态停止，代理记录保留在主备清单",
            "更新时间": _now(),
        })
        version = str(state.get("降级版本") or "")
        if version:
            self._upsert_result(
                version, region, _region_proxy(region), _region_datasource(region),
                "已停止", "无", state["最近事件"], state.get("原代理快照") or {},
            )
            # 关闭代理不再算作已完成区域，重试时会重新校验并记录关闭原因，而不是被当作已完成跳过。
            batch = _batch(version)
            if batch is not None:
                batch["已完成区域"] = [
                    item for item in batch.get("已完成区域") or [] if str(item) != region
                ]
                batch["状态"] = "部分失败" if batch.get("失败区域") else "待复核"
                batch["更新时间"] = _now()
        return state

    # ------------------------------------------------------------------
    # 主备清单 / 失败核对清单 / 降级切换结果
    # ------------------------------------------------------------------
    def standby_entries(self) -> list[dict[str, Any]]:
        """把代理配置、数据源池和区域降级状态拼成主备清单。

        已关闭代理保留在清单里并标记为已退出，但不会成为主用线路；
        每个区域最多展示一个降级版本，取自该区域唯一的降级状态行。
        """
        regions: set[str] = set()
        for row in store.rows(PROXY_MODULE):
            if row.get("区域标签"):
                regions.add(str(row["区域标签"]))
        for row in store.rows(MODULE):
            if row.get("区域标签"):
                regions.add(str(row["区域标签"]))
        for row in store.aux_rows(REGION_TABLE):
            if row.get("区域标签"):
                regions.add(str(row["区域标签"]))

        items: list[dict[str, Any]] = []
        for region in sorted(regions):
            proxy = _region_proxy(region)
            datasource = _region_datasource(region)
            state = _state(region) or {}
            proxy_closed = bool(proxy and proxy.get("status") == PROXY_CLOSED)
            if proxy_closed:
                role = "已退出（保留代理）"
                traffic = "空闲"
            elif datasource and datasource.get("status") == DATASOURCE_READY:
                role = "备用承载" if state.get("主用线路") == "备用源" else "待命"
                traffic = str(datasource.get("当前流量") or "空闲")
            else:
                role = "无可用备用源"
                traffic = "空闲"
            items.append({
                "区域标签": region,
                "代理配置编号": (proxy or {}).get("配置编号") or "",
                "代理状态": (proxy or {}).get("status") or "未登记",
                "数据源条目编号": (datasource or {}).get("条目编号") or "",
                "数据源名称": (datasource or {}).get("数据源名称") or "",
                "数据源状态": (datasource or {}).get("status") or "未登记",
                "主备角色": role,
                "当前流量": traffic,
                "主用线路": state.get("主用线路") or "代理",
                "降级版本": state.get("降级版本") or "",
                "降级状态": state.get("降级状态") or "未降级",
                "恢复状态": state.get("恢复状态") or "无",
                "最近事件": state.get("最近事件") or "",
                "更新时间": state.get("更新时间") or "",
            })
        return items

    def degrade_results(
        self,
        *,
        version: str | None = None,
        region: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = list(store.aux_rows(RESULT_TABLE))
        if version:
            rows = [row for row in rows if row.get("降级版本") == version]
        if region:
            rows = [row for row in rows if row.get("区域标签") == region]
        # 固定排序，保证同一版本多次读取结果顺序一致。
        rows.sort(key=lambda row: (str(row.get("降级版本") or ""), str(row.get("区域标签") or "")))
        total = len(rows)
        start = max(page - 1, 0) * size
        items: list[dict[str, Any]] = []
        for row in rows[start:start + size]:
            item = dict(row)
            batch = _batch(str(row.get("降级版本") or ""))
            # 批次状态在读取时补齐：重试中断时能直接看到该版本停在哪一步。
            item["批次状态"] = (batch or {}).get("状态") or "未登记"
            item["重试次数"] = int((batch or {}).get("重试次数") or 0)
            items.append(item)
        return items, total

    def degrade_failures(
        self,
        *,
        version: str | None = None,
        region: str | None = None,
        review_status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = list(store.aux_rows(FAILURE_TABLE))
        if version:
            rows = [row for row in rows if row.get("降级版本") == version]
        if region:
            rows = [row for row in rows if row.get("区域标签") == region]
        if review_status:
            rows = [row for row in rows if row.get("复核状态") == review_status]
        rows.sort(key=lambda row: (str(row.get("降级版本") or ""), str(row.get("区域标签") or "")))
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def route_diff(self) -> list[dict[str, Any]]:
        """比较采集代理路由与采集数据源池，定位无法承载降级流量的区域。"""
        regions: set[str] = set()
        for row in store.rows(PROXY_MODULE):
            if row.get("区域标签"):
                regions.add(str(row["区域标签"]))
        for row in store.rows(MODULE):
            if row.get("区域标签"):
                regions.add(str(row["区域标签"]))

        items: list[dict[str, Any]] = []
        for region in sorted(regions):
            proxies = _region_proxies(region)
            proxy = proxies[0] if proxies else None
            datasource = _region_datasource(region)
            if proxy is None:
                conclusion, detail = "缺代理", f"数据源 {datasource.get('条目编号')} 所在区域没有代理配置"
            elif proxy.get("status") == PROXY_CLOSED:
                conclusion, detail = "代理已关闭", f"代理 {proxy.get('配置编号')} 已关闭，保留记录但不参与切换"
            elif datasource is None:
                conclusion, detail = "空路由", f"代理 {proxy.get('配置编号')} 未匹配到数据源条目，降级事务会失败"
            elif datasource.get("status") != DATASOURCE_READY:
                conclusion = "备用源不可用"
                detail = (
                    f"数据源 {datasource.get('条目编号')} 当前状态为{datasource.get('status')}，"
                    "不能承载备用流量"
                )
            elif len(proxies) > 1:
                conclusion = "多代理同区域"
                detail = f"区域存在 {len(proxies)} 条代理配置，只允许保留一个降级状态"
            else:
                conclusion, detail = "路由可用", ""
            items.append({
                "区域标签": region,
                "代理配置编号": (proxy or {}).get("配置编号") or "",
                "代理状态": (proxy or {}).get("status") or "未登记",
                "数据源条目编号": (datasource or {}).get("条目编号") or "",
                "数据源状态": (datasource or {}).get("status") or "未登记",
                "路由结论": conclusion,
                "差异说明": detail,
            })
        return items

    # ------------------------------------------------------------------
    # 降级切换事务
    # ------------------------------------------------------------------
    def request_degrade(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, bool, str]:
        """发起一次区域降级；同一区域重复请求复用既有降级版本，不重复迁移。"""
        region = str(values.get("区域标签") or "").strip()
        if not region:
            return None, False, "缺少区域标签，无法发起代理降级切换"
        proxy_code = str(values.get("代理配置编号") or "").strip() or None
        proxy = _region_proxy(region, proxy_code)
        if proxy is None:
            return None, False, f"代理区域「{region}」不存在，降级切换未执行"
        version = str(values.get("降级版本") or "").strip()
        state = _state(region)
        if version == "" and state and state.get("降级状态") in DEGRADE_ACTIVE:
            version = str(state.get("降级版本") or "")
        batch = _batch(version) if version else None
        if batch is None:
            version = version or _new_version()
            batch = self._ensure_batch(version, region, proxy)
        return self._switch_region(region, version, batch, proxy=proxy, retry=False)

    def retry_degrade(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, bool, str]:
        """按降级版本续做未完成区域；已完成区域跳过，关闭代理不会被重新迁入。"""
        version = str(values.get("降级版本") or "").strip()
        if not version:
            state_version = str((_state(str(values.get("区域标签") or "")) or {}).get("降级版本") or "")
            version = state_version
        if not version:
            return None, False, "缺少降级版本，无法确定续做范围"
        batch = _batch(version)
        if batch is None:
            return None, False, f"降级版本「{version}」不存在或已中断，无法续做"

        completed = [str(item) for item in batch.get("已完成区域") or []]
        migrated: list[str] = []
        skipped: list[str] = []
        pending_review: list[str] = []
        retired: list[str] = []
        failures: list[str] = []
        for region in [str(item) for item in batch.get("区域范围") or []]:
            if region in completed:
                skipped.append(region)
                continue
            proxy = _region_proxy(region)
            if proxy is None:
                failures.append(f"{region}：代理配置不存在")
                continue
            if proxy.get("status") == PROXY_CLOSED or (_state(region) or {}).get("降级状态") == "已停止":
                # 关闭代理是终态：只补一条核对说明，重试范围里永久排除，不算本次续做失败。
                self._switch_region(region, version, batch, proxy=proxy, retry=True)
                retired.append(region)
                continue
            state, ok, message = self._switch_region(region, version, batch, proxy=proxy, retry=True)
            if ok and state and state.get("降级状态") == "已降级":
                migrated.append(region)
            elif ok and state and state.get("降级状态") == "降级待复核":
                pending_review.append(region)
            else:
                failures.append(f"{region}：{message}")

        batch["重试次数"] = int(batch.get("重试次数") or 0) + 1
        batch["更新时间"] = _now()

        pending = [
            region
            for region in [str(item) for item in batch.get("区域范围") or []]
            if (_state(region) or {}).get("降级状态") not in ("已降级", "降级待复核", "已停止")
        ]
        if failures:
            # 一个区域都没迁移成功时标成整体失败，避免「部分失败」让人误以为已有区域可用。
            batch["状态"] = "部分失败" if batch.get("已完成区域") else "切换失败"
        elif pending:
            batch["状态"] = "待复核"
        else:
            batch["状态"] = "已完成"
        # 空路由/不可迁移原因每次都刷新为本次续做的最新结论，避免残留旧说明。
        if failures:
            batch["空路由说明"] = "；".join(failures)
        if retired:
            batch["排除区域说明"] = (
                f"已关闭代理区域不再续做：{'、'.join(retired)}；保留代理记录，不会重新进入主备清单"
            )
        # 续做成功后补登记该版本已完成降级的区域，下一次重试直接跳过；
        # 关闭代理（已停止）与切换失败的区域不登记，下次重试仍会校验并把原因写进核对清单。
        batch["已完成区域"] = [
            region
            for region in [str(item) for item in batch.get("区域范围") or []]
            if (_state(region) or {}).get("降级状态") in ("已降级", "降级待复核")
        ]

        if migrated:
            lead = f"降级版本 {version} 重试完成：新迁移 {len(migrated)} 个区域（{'、'.join(migrated)}）"
        else:
            lead = f"降级版本 {version} 重试未产生新的迁移"
        if skipped:
            lead += f"；已完成区域 {len(skipped)} 个保持原状、不重复迁移"
        if pending_review:
            lead += f"；待复核区域 {len(pending_review)} 个未重复切换流量（{'、'.join(pending_review)}）"
        if retired:
            lead += f"；已关闭代理区域 {len(retired)} 个永久排除续做（{'、'.join(retired)}）"
        if failures:
            return batch, False, f"{lead}；续做中断，仍有区域失败：{'；'.join(failures)}"
        return batch, True, lead

    def receive_recovery(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, bool, str]:
        """接收恢复回执，按降级是否已经完成决定是切回代理还是显示恢复等待。"""
        region = str(values.get("区域标签") or "").strip()
        if not region:
            return None, False, "缺少区域标签，无法确认恢复回执"
        proxy = _region_proxy(region)
        if proxy is None:
            return None, False, f"代理区域「{region}」不存在，恢复回执未生效"
        state = _state(region)
        receipt = str(values.get("恢复回执编号") or "").strip() or "RECV-未登记"

        if proxy.get("status") == PROXY_CLOSED:
            if state:
                state["最近事件"] = f"恢复回执 {receipt} 到达，但代理 {proxy.get('配置编号')} 已关闭，未切回主用"
                state["更新时间"] = _now()
            return proxy, False, f"代理配置 {proxy.get('配置编号')} 已关闭，恢复回执不生效，关闭代理记录继续保留"

        if state is None or state.get("降级状态") not in DEGRADE_ACTIVE:
            return self._complete_recovery(region, proxy, state, receipt)

        if state.get("降级状态") == "降级待复核":
            # 恢复先完成、降级被延后：待复核结论保持不变，不因重复回执改成未降级。
            state["最近事件"] = (
                f"恢复回执 {receipt} 已收到；该区域降级此前被延后，仍显示降级待复核"
            )
            state["更新时间"] = _now()
            return state, True, (
                f"代理区域「{region}」恢复已先完成，降级仍待复核，主备流量保持代理配置"
            )

        if state.get("降级状态") == "切换失败":
            state["恢复状态"] = "已恢复"
            state["主用线路"] = "代理"
            state["最近事件"] = f"切换事务此前失败，恢复回执 {receipt} 确认继续使用原代理"
            state["更新时间"] = _now()
            self._upsert_result(
                str(state.get("降级版本") or ""), region, proxy, None,
                "切换失败", "已恢复", state["最近事件"], state.get("原代理快照") or _proxy_snapshot(proxy),
            )
            return state, True, f"代理区域「{region}」原代理仍在承载流量，恢复回执已确认"

        # 降级已经完成：恢复延后执行，继续使用备用源。
        state["恢复状态"] = "恢复等待"
        state["最近事件"] = f"恢复回执 {receipt} 已收到，但降级已完成，恢复延后执行，继续使用备用源"
        state["更新时间"] = _now()
        self._upsert_result(
            str(state.get("降级版本") or ""), region, proxy, _healthy_datasource(region),
            "已降级", "恢复等待", state["最近事件"], state.get("原代理快照") or _proxy_snapshot(proxy),
        )
        return state, True, (
            f"降级已先完成，恢复延后执行：代理区域「{region}」继续使用备用源，"
            "数据源主备清单显示恢复等待"
        )

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------
    def _ensure_batch(
        self, version: str, region: str, proxy: dict[str, Any]
    ) -> dict[str, Any]:
        batch = _batch(version)
        if batch is not None:
            if region not in batch.setdefault("区域范围", []):
                batch["区域范围"].append(region)
            batch["原代理快照"][region] = _proxy_snapshot(proxy)
            return batch
        rows = store.aux_rows(BATCH_TABLE)
        batch = {
            "id": _next_id(rows),
            "降级版本": version,
            "区域范围": [region],
            "已完成区域": [],
            "状态": "待切换",
            "重试次数": 0,
            "原代理快照": {region: _proxy_snapshot(proxy)},
            "空路由说明": "",
            "创建时间": _now(),
            "更新时间": _now(),
        }
        rows.append(batch)
        return batch

    def _switch_region(
        self,
        region: str,
        version: str,
        batch: dict[str, Any],
        *,
        proxy: dict[str, Any],
        retry: bool,
    ) -> tuple[dict[str, Any] | None, bool, str]:
        state = _state(region)
        proxy_code = str(proxy.get("配置编号") or "")
        snapshot = _proxy_snapshot(proxy)

        if retry and proxy.get("status") == PROXY_CLOSED:
            # 关闭代理是终态：重试只记录原因，不重新进入主备流量。
            reason = f"代理配置 {proxy_code} 已关闭，不能因重试重新进入数据源主备清单"
            self._upsert_failure(
                version, region, proxy, "重试校验", reason, reason, snapshot, review_status="已排除",
            )
            batch["空路由说明"] = batch.get("空路由说明") or reason
            batch["更新时间"] = _now()
            return state, False, reason

        if proxy.get("status") == PROXY_CLOSED:
            reason = f"代理配置 {proxy_code} 已关闭，不能进入数据源主备清单"
            self._upsert_failure(
                version, region, proxy, "代理状态校验", reason, reason, snapshot, review_status="已排除",
            )
            batch["空路由说明"] = batch.get("空路由说明") or reason
            batch["更新时间"] = _now()
            return state, False, reason

        if state and state.get("降级状态") == "已降级" and state.get("主用线路") == "备用源":
            state["更新时间"] = _now()
            state["最近事件"] = f"区域已完成降级（降级版本 {state.get('降级版本') or version}），本次未重复迁移"
            batch["更新时间"] = _now()
            return state, True, (
                f"代理区域「{region}」已处于降级状态（降级版本 {state.get('降级版本') or version}），"
                "本次未重复迁移"
            )

        if state and state.get("降级状态") == "降级待复核":
            state["更新时间"] = _now()
            state["最近事件"] = f"降级版本 {version} 处于待复核，恢复回执到达前不重复切换流量"
            return state, True, (
                f"代理区域「{region}」的降级处于待复核状态（降级版本 "
                f"{state.get('降级版本') or version}），本次未重复迁移"
            )

        datasource = _healthy_datasource(region)
        if datasource is None:
            fallback = _region_datasource(region)
            if fallback is None:
                detail = f"代理区域「{region}」未匹配到任何数据源条目，切换事务保留原代理快照"
            else:
                detail = (
                    f"数据源 {fallback.get('条目编号')} 当前状态为{fallback.get('status')}，"
                    "不能承载备用流量，切换事务保留原代理快照"
                )
            self._upsert_failure(version, region, proxy, "可用路由校验", detail, detail, snapshot)
            state = self._set_state(
                region, proxy, None, version,
                degrade_status="切换失败",
                main_line="代理",
                recovery_status="无",
                event=detail,
                snapshot=snapshot,
            )
            self._upsert_result(
                version, region, proxy, fallback, "切换失败", "无", detail, snapshot,
            )
            batch["空路由说明"] = detail
            batch["更新时间"] = _now()
            if region not in batch.setdefault("区域范围", []):
                batch["区域范围"].append(region)
            batch["原代理快照"][region] = snapshot
            return state, False, f"降级切换失败：{detail}"

        recovery_status = str((state or {}).get("恢复状态") or "")
        if recovery_status == "已恢复" and (state or {}).get("主用线路") == "代理":
            # 路径一：恢复先完成，降级请求延后落地，只标记待复核。
            event = "恢复先完成，降级延后执行；流量保持代理配置，等待降级复核"
            state = self._set_state(
                region, proxy, datasource, version,
                degrade_status="降级待复核",
                main_line="代理",
                recovery_status="待复核",
                event=event,
                snapshot=snapshot,
            )
            datasource["当前流量"] = "空闲"
            self._upsert_result(
                version, region, proxy, datasource, "降级待复核", "待复核", event, snapshot,
            )
            if region not in batch.setdefault("区域范围", []):
                batch["区域范围"].append(region)
            batch["原代理快照"][region] = snapshot
            batch["状态"] = "待复核"
            batch["更新时间"] = _now()
            return state, True, (
                f"恢复已完成，降级延后执行：代理区域「{region}」流量已切回代理配置，"
                "数据源主备清单显示降级待复核"
            )

        recovery_waiting = recovery_status in RECOVERY_PENDING
        if recovery_waiting:
            # 路径二：降级先完成，恢复回执延后，继续使用备用源并标记恢复等待。
            event = "降级先完成，恢复回执延后；继续使用备用源，等待恢复"
            state = self._set_state(
                region, proxy, datasource, version,
                degrade_status="已降级",
                main_line="备用源",
                recovery_status="恢复等待",
                event=event,
                snapshot=snapshot,
            )
        else:
            event = "健康采样低于切换阈值，已切换到备用源"
            state = self._set_state(
                region, proxy, datasource, version,
                degrade_status="已降级",
                main_line="备用源",
                recovery_status="无",
                event=event,
                snapshot=snapshot,
            )

        datasource["当前流量"] = "承载备用流量"
        datasource["最近采集"] = datasource.get("最近采集") or _now()
        proxy["status"] = "已降级"
        proxy["pending"] = True
        proxy["abnormal"] = True
        proxy["配置状态"] = "降级中"
        self._upsert_result(
            version, region, proxy, datasource, "已降级",
            str(state.get("恢复状态") or "无"), event, snapshot,
        )
        batch["原代理快照"][region] = snapshot
        if region not in batch.setdefault("区域范围", []):
            batch["区域范围"].append(region)
        if region not in batch.setdefault("已完成区域", []):
            batch["已完成区域"].append(region)
        # 该区域重试成功后，旧的失败核对记录转为已复核，核对清单只保留仍待处理的区域。
        self._resolve_failure(version, region, "重试已完成迁移，失败记录自动归档为已复核")
        if not batch.get("已完成区域"):
            batch["状态"] = "待切换"
        elif len(batch["已完成区域"]) == len(batch["区域范围"]):
            batch["状态"] = "已完成"
        else:
            batch["状态"] = "部分失败"
        batch["更新时间"] = _now()
        suffix = "，主备清单显示恢复等待" if recovery_waiting else ""
        return state, True, (
            f"代理区域「{region}」已降级，流量切换到备用源 {datasource.get('条目编号')}{suffix}"
        )

    def _complete_recovery(
        self,
        region: str,
        proxy: dict[str, Any],
        state: dict[str, Any] | None,
        receipt: str,
    ) -> tuple[dict[str, Any] | None, bool, str]:
        datasource = _region_datasource(region)
        if datasource is not None:
            datasource["当前流量"] = "空闲"
        proxy["status"] = "已启用"
        proxy["pending"] = False
        proxy["abnormal"] = False
        proxy["配置状态"] = "可用"
        event = f"恢复回执 {receipt} 已确认，流量切回代理配置"
        if state is None:
            state = self._set_state(
                region, proxy, datasource, "", "未降级", "代理", "已恢复", event,
                snapshot=_proxy_snapshot(proxy),
            )
        else:
            state["降级状态"] = "未降级"
            state["主用线路"] = "代理"
            state["恢复状态"] = "已恢复"
            state["最近事件"] = event
            state["更新时间"] = _now()
        version = str(state.get("降级版本") or "")
        if version:
            self._upsert_result(
                version, region, proxy, datasource, "未降级", "已恢复", event,
                state.get("原代理快照") or _proxy_snapshot(proxy),
            )
        return state, True, (
            f"恢复回执 {receipt} 已完成：代理区域「{region}」流量切回代理配置，"
            "数据源主备清单恢复状态为已恢复"
        )

    def _set_state(
        self,
        region: str,
        proxy: dict[str, Any] | None,
        datasource: dict[str, Any] | None,
        version: str,
        degrade_status: str,
        main_line: str,
        recovery_status: str,
        event: str,
        snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        state = _state(region)
        if state is None:
            rows = store.aux_rows(REGION_TABLE)
            state = {"id": _next_id(rows), "区域标签": region}
            rows.append(state)
        state.update({
            "代理配置编号": (proxy or {}).get("配置编号") or state.get("代理配置编号") or "",
            "数据源条目编号": (datasource or {}).get("条目编号") or state.get("数据源条目编号") or "",
            "降级版本": version if version else (state.get("降级版本") or ""),
            "降级状态": degrade_status,
            "主用线路": main_line,
            "恢复状态": recovery_status,
            "最近事件": event,
            "原代理快照": snapshot if snapshot is not None else (state.get("原代理快照") or {}),
            "更新时间": _now(),
        })
        return state

    def _resolve_failure(self, version: str, region: str, note: str) -> None:
        """区域补做成功后把失败核对记录标为已复核，重试只维护一条记录，不新增副本。"""
        row = _failure(version, region)
        if row is None:
            return
        row["复核状态"] = "已复核"
        row["复核说明"] = note
        row["更新时间"] = _now()

    def _upsert_failure(
        self,
        version: str,
        region: str,
        proxy: dict[str, Any],
        stage: str,
        reason: str,
        empty_route: str,
        snapshot: dict[str, Any],
        *,
        review_status: str = "待复核",
    ) -> dict[str, Any]:
        """同一降级版本 + 区域只保留一条核对记录，重试只刷新原因，不重复堆积。"""
        row = _failure(version, region)
        if row is None:
            rows = store.aux_rows(FAILURE_TABLE)
            row = {
                "id": _next_id(rows),
                "核对编号": f"DCHK-{_next_id(rows):04d}",
                "降级版本": version,
                "区域标签": region,
                "代理配置编号": proxy.get("配置编号") or "",
                "复核状态": review_status,
                "创建时间": _now(),
            }
            rows.append(row)
        row.update({
            "失败环节": stage,
            "失败原因": reason,
            "空路由说明": empty_route,
            "原代理快照": snapshot,
            # 关闭代理记为已排除（终态，不再续做）；其他失败会重新打开核对记录。
            "复核状态": review_status,
            "复核说明": "",
            "更新时间": _now(),
        })
        return row

    def _upsert_result(
        self,
        version: str,
        region: str,
        proxy: dict[str, Any] | None,
        datasource: dict[str, Any] | None,
        degrade_status: str,
        recovery_status: str,
        note: str,
        snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        """同一降级版本 + 区域只保留一条结果，读取顺序固定，避免结果抖动。"""
        row = _result(version, region)
        if row is None:
            rows = store.aux_rows(RESULT_TABLE)
            row = {"id": _next_id(rows), "降级版本": version, "区域标签": region}
            rows.append(row)
        row.update({
            "代理配置编号": (proxy or {}).get("配置编号") or "",
            "数据源条目编号": (datasource or {}).get("条目编号") or "",
            "主用线路": "备用源" if degrade_status == "已降级" else "代理",
            "降级状态": degrade_status,
            "恢复状态": recovery_status,
            "结果说明": note,
            "原代理快照": snapshot,
            "更新时间": _now(),
        })
        return row

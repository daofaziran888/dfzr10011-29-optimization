# 分布式采集调度引擎

面向大规模数据采集团队的分布式采集调度引擎。覆盖采集节点集群管理、任务分片调度、结果聚合归并、节点健康监控、采集流量均衡、断点续采恢复与全局采集看板的完整分布式采集链路。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 采集节点集群 | `cluster` | 集群节点 | 节点编号、节点地址、节点角色 |
| 任务分片调度 | `shard` | 分片任务 | 分片编号、采集计划、分片范围 |
| 结果聚合归并 | `merge` | 聚合任务 | 任务编号、采集计划、分片总数 |
| 节点状态巡检 | `nodecheck` | 巡检记录 | 记录编号、节点编号、CPU使用率 |
| 采集流量均衡 | `balance` | 均衡策略 | 策略编号、负载算法、权重因子 |
| 断点续采恢复 | `resume` | 断点记录 | 断点编号、采集计划、断点位置 |
| 全局采集看板 | `dashboard` | 看板面板 | 面板编号、面板名称、显示指标 |
| 采集配置模板 | `crawlconf` | 配置模板 | 模板编号、模板名称、目标类型 |
| 采集数据源池 | `datasource` | 数据源条目 | 条目编号、数据源名称、源类型、区域标签、主备角色 |
| 采集日志解析 | `logparse` | 日志模板 | 模板编号、日志类型、解析规则 |
| 采集频率控制 | `ratelimit` | 频率规则 | 规则编号、目标站点、请求间隔 |
| 采集代理路由 | `proxy` | 代理配置 | 配置编号、代理地址、代理协议、区域标签、可用比例 |
| RPA执行记录 | `robot` | 执行记录 | 记录编号、流程模板、执行节点 |
| 采集结果校验 | `verify` | 校验规则 | 规则编号、采集计划、校验类型 |
| 采集操作审计 | `audit` | 审计记录 | 记录编号、操作类型、操作对象 |
| 采集存储分层 | `storage` | 存储策略 | 策略编号、存储层级、文件格式 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 清单类接口（主备清单、失败核对清单、切换结果、健康采样）额外返回
  `state`（`ok` / `empty` / `failed`）与 `message`：空态说明原因，失败或重试中断也走 `message`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 代理降级切换

- 降级以「降级版本」为单位：`POST /api/datasource/degrade/request` 发起，
  同一区域重复发起复用既有版本；`POST /api/datasource/degrade/retry` 按版本续做，
  已完成区域跳过，不会重复迁移。
- 每个代理区域只保留一个降级状态：`GET /api/datasource/standby` 的数据源主备清单
  按区域聚合，同区域只展示唯一的降级版本与恢复状态；已关闭代理保留记录但标为已退出。
- 切换事务失败时保留原代理快照，空路由/备用源不可用的原因写入
  `GET /api/datasource/degrade/failures`（降级切换失败核对清单），
  切换结果在 `GET /api/datasource/degrade/results`，同一版本同一区域只有一条。
- 同一区域同时收到恢复回执与降级请求时：
  - 恢复先完成、降级延后 → 主备清单流量切回代理，显示「降级待复核」；
  - 降级先完成、恢复延后 → 主备清单继续使用备用源，显示「恢复等待」。
- `PROX-0003` 这类已关闭代理是终态：再次降级会被拒绝，重试不会把它重新迁入主备清单，
  只在失败核对清单里补一条「已排除」说明。
- 采集代理路由页与采集数据源池页共用同一份失败核对清单与切换结果，
  前端组件在 `frontend/src/components/DegradeWorklist.vue`。

领域：数据工程
子方向：数据采集、爬虫与RPA

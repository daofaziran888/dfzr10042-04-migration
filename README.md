# 洞察工坊 · 多维交互分析与实验度量平台

面向数据分析师的多维下钻分析、指标口径管理、A/B实验评估、群体圈选分群、归因分析与数据看板构建的交互分析平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
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
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 指标定义 | `metric_def` | 指标口径 | 指标编号、指标名称、计算口径 |
| 维度定义 | `dimension_def` | 分析维度 | 维度编号、维度名称、字段来源 |
| 多维查询 | `olap_query` | 查询方案 | 方案编号、查询名称、选择指标 |
| 群体圈选 | `segment_builder` | 人群包 | 人群编号、人群名称、圈选条件 |
| A/B实验 | `ab_experiment` | 实验方案 | 方案编号、实验名称、实验分组 |
| 实验分析 | `experiment_result` | 实验报告 | 报告编号、关联实验、指标名称 |
| 归因分析 | `attribution_model` | 归因模型 | 模型编号、模型名称、归因算法 |
| 看板定义 | `dashboard_def` | 数据看板 | 看板编号、看板名称、布局配置 |
| 下钻路径 | `drill_path` | 下钻方案 | 方案编号、父级维度、子级维度 |
| 同期群分析 | `cohort_analysis` | 同期群 | 群组编号、群组名称、划分周期 |
| 数据导出 | `data_export` | 导出任务 | 任务编号、数据来源、导出格式 |
| 查询缓存 | `query_cache` | 缓存条目 | 条目编号、查询哈希、结果行数 |
| 定时分析 | `scheduled_analysis` | 调度任务 | 任务编号、分析方案、Cron表达式 |
| 指标对比 | `metric_compare` | 对比方案 | 方案编号、主指标、对比指标 |
| 异常检测 | `anomaly_detect` | 检测规则 | 规则编号、目标指标、检测算法 |
| 分析快照 | `data_snapshot` | 数据快照 | 快照编号、分析方案、数据行数 |
| 分群比较 | `user_segment` | 比较方案 | 方案编号、群体A、群体B |
| 事件追踪 | `event_track` | 事件定义 | 事件编号、事件名称、事件属性 |
| 排行榜 | `ranking_board` | 排行方案 | 方案编号、排行指标、排行维度 |
| 漏斗分析 | `funnel_def` | 漏斗方案 | 方案编号、步骤序列、转化窗口 |
| 数据集关联 | `dataset_join` | 关联方案 | 方案编号、主数据集、关联数据集 |
| 报告生成 | `report_builder` | 报告模板 | 模板编号、报告名称、引用指标 |

## 分析快照字段清单迁移（snapshot_migration）

把"只有字段摘要和查询编号"的旧导出迁移为带**投影顺序、列类型、实际行数、
文件校验值**的分析快照字段清单，代码在 `backend/app/snapshot_migration/`：

- 迁移分 `build → verify → enable-reader` 三段，journal 断点可接续；
  清单原子写盘，进程在核验落盘后、写日志前被杀也能从断点重启续跑；
  启用新版读入是独立的升级确认，与清单构建、文件核验分开验证。
- 历史日报始终按旧证据文件回算；新版取数只引用核验过的文件
  （待核 `pending_verification`、缺文件 `missing_file` 都明确拒绝）；
  新版日报钉住当次采用的一个投影和一个真实文件来源，取数后可按该投影回算。
- 缺文件的同名快照停留在可说明状态，摘要里"声称的行数"仅作未证实声明，
  不借用其他快照的文件伪造数据行。

```bash
cd backend
# 端到端演练（真实子进程中断/重启 + 中断点并发回算与取数）
.venv/bin/python -m app.snapshot_migration.cli --state var/demo story
# 单步命令：seed-demo / migrate build|verify|enable-reader|resume|status /
#           report recompute-legacy|fetch-new|daily-new|recompute-new / audit
.venv/bin/python -m pytest tests/test_snapshot_migration.py   # 15 个场景测试
```

HTTP 接口挂在 `/api/snapshot-migration/`（status、build、verify、resume、
enable-reader、audit、fetch、daily-report、recompute 等）；状态目录默认
`backend/var/snapshot_migration`（可用 `SNAPSHOT_MIGRATION_STATE` 覆盖），
为空时自动写入演示数据：同名查询 Q-1001 的两份快照（一份有旧文件 8/12 元，
一份只有摘要）加一份历史日报。

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数据分析与数据应用

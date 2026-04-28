# 黑猫核校 — Web 化部署演进路线（EdgeOne + 大书稿 + 大 bib + 慢网络）

> 本文档为**未来路线规划**，暂不开发。为后期上线 EdgeOne 服务化做准备。

## Context

CLI 已稳定：纯 LLM 异步流水线，6949 字 18.4s / 114k 字 95s。下一步上线 EdgeOne：

- 用户 Web 上传**最大 30 万字**书稿 + **最多 10 本、单本上百万字**参考文献
- 网络可能很慢（上传 + 结果回传都需要韧性）
- EdgeOne Pages Functions 单次执行 ≤ 60s，**不能跑全流程**——必须前端 + 边缘 + 源站三层切分

目标：把现有核心从「单进程 CLI」演进成「可水平扩展的服务后端」，同时保留 CLI 入口（开发与本地批处理仍有用）。

## 三层架构

```
浏览器
  ↑↓ 分片上传 / SSE / 下载
EdgeOne (CDN + Pages Functions)
  ├── 静态前端 (上传 / 进度 / 报告 viewer)
  ├── /api/upload-init     → 预签名 COS 直传 URL
  ├── /api/jobs/*          → 透传到源站（SSE 关闭缓冲）
  └── /report/*            → CDN 缓存已完成报告
源站 (FastAPI + 后台 worker)
  ├── 接单 + 任务编排
  ├── 调用 core.api.check()
  └── 写状态 / 事件 / 报告到 Redis + COS
COS (对象存储)
  └── manuscripts/  bibs/  reports/  index-cache/
```

**关键决策**：核心算法**不改逻辑**，只做接口扩展（流式、可中断、可恢复）+ 持久化对接。

## 改动清单

### A. 核心层（`core/`）—— 让算法支持服务化

> **范围调整**：bib 文献由用户运行时上传，不做预处理 / 不做注册表。仅保留 `register_bib()` 形态的接口占位，留待后期管理员内置常用古籍时启用。本轮 P0 不实现 bib registry。

#### A1. Bib 索引（保留现状 + 留接口）

现状（`core/index.py`）：
- 已有 SHA256 → pickle 缓存到 `~/.cache/heimao_index/<sha>.v5.pkl` ✓
- 用户每次上传 bib 仍走 `build_corpus(paths)`，但 sha 命中缓存就秒级返回

改进（仅留接口）：
- `core/api.py::check` 已有 `bibs: list[Path]` 入参，**不变**
- 留 TODO：`core/bib_registry.py`（后期管理员内置常用古籍时实现 `register_bib(path) -> bib_id` / `load_corpus(bib_ids)`），现在不写

**用户上传的 bib 处理流程**：
1. 服务端接收 bib 文件 → 写入 `cos://bibs/<job_id>/...`
2. 同任务首次 `build_corpus()` 触发索引 + 缓存到 pickle（同 sha 后续任务命中复用）
3. 不预热、不注册表，每个任务独立处理

#### A2. 流式 verdict 输出

现状：`check()` 一次性返回 `Report.verdicts`，前端必须等全跑完才能看结果。

改进：
- `core/api.py` 增加 `check_stream(...) -> Iterator[Event]`
  - 事件流：`parse_done` → `extract_chunk(citations=[...])` → `match_partial(verdicts=[...])` → `judge_done(verdict=...)` → `done`
  - 不破坏老 `check()`：`check()` 内部消费 `check_stream` 把结果聚合返回
- `core/match.py::match` 增加 `on_partial=callback` 参数（A/B 自动判定的项目立即回调，C/D 走 LLM 后再回调）
- `core/llm.py::judge_batch` 已有 `on_progress`，扩展为 per-verdict 完成回调

**收益**：30 万字稿，用户 5s 后就能开始看 A/B 自动通过的引文，不必等 4 分钟全部跑完。

#### A3. Chunk 失败防御（已知 bug 修）

`core/llm.py::_extract_json` 偶尔被 LLM 输出的未转义控制字符卡住（实测 71 chunks 失败 1 个）：
- 在 json.loads 前 `re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", content)`
- 仍失败则尝试 `response_format={"type": "json_object"}`

#### A4. 取消令牌

服务端用户关闭页面 / 任务超时时要能停掉正在跑的 LLM 调用：
- `extract()` / `match()` / `judge_batch()` 全部接受 `cancel: asyncio.Event | None`
- 每个 chunk 完成后检查 `cancel.is_set()`，已发出的 LLM 请求让 httpx 自然超时

#### A5. CLI 不变

`cli.py` 继续用 `core.api.check()` 跑同步流水线；新加的 `check_stream` 只服务 Web 后端。

### B. 服务层（新建 `server/`）—— FastAPI 后端

#### B1. `server/app.py`

```
POST   /api/jobs                     创建任务 (manuscript_key, bib_keys[]) -> {job_id}
GET    /api/jobs/{id}                状态 + 已完成 verdict 数
GET    /api/jobs/{id}/events         SSE 流（progress + 增量 verdict）
GET    /api/jobs/{id}/report?fmt=md  最终报告（流式 chunked transfer）
POST   /api/jobs/{id}/cancel         取消
POST   /api/upload-init              获取预签名 COS 直传 URL
```

#### B2. `server/worker.py`

- 单进程 asyncio worker（小规模够用）；后期可换 Celery / arq
- 任务队列用 Redis（`jobs:queue`），状态用 Redis hash（`job:{id}:state`），事件用 Redis Stream（`job:{id}:events`）

#### B3. `server/storage.py`

- COS 客户端封装：上传 / 下载 / 预签名 URL
- 路径约定：
  - `manuscripts/<job_id>/manuscript.docx`
  - `bibs/<job_id>/<filename>.txt`
  - `reports/<job_id>/report.{md,html,json}`

#### B4. 鉴权（轻量）

- 上传 / 任务创建需要 token（预签名或 session cookie）
- 不做完整账号系统：MVP 阶段邀请码 / 短链分享

### C. 上传 / 下载层（应对慢网络）

#### C1. 大文件分片上传

- 前端用 **tus-js-client** 或 COS 直传 SDK（推荐后者，省一跳）
- 流程：
  1. 前端 `POST /api/upload-init`（边缘函数）→ 返回预签名 COS URL
  2. 前端直接分片 PUT 到 COS（断点续传，每片 1MB）
  3. 上传完成 → 前端 `POST /api/jobs` 携带 COS object key

#### C2. SSE 进度流（断线自动重连）

- 后端发事件用 `id:` header；前端 EventSource 断线自动从 `Last-Event-ID` 续传
- 每 15s 发一次 `heartbeat` 事件防止中间代理切链路
- EdgeOne 透传 SSE 需要：`X-Accel-Buffering: no` + `Cache-Control: no-cache`

#### C3. 报告下载

- 短报告（< 1MB）：直接返回字节
- 长报告（30 万字稿可能 5-10MB md）：CDN 缓存 + Range 请求支持
- HTML / JSON 报告也走 CDN，前端按需懒加载

### D. 前端（独立工程，单独规划，本计划只勾轮廓）

- React + Vite + Tailwind（与 EdgeOne Pages 模板贴合）
- 主要页面：上传 → 任务列表 → 进度面板（SSE）→ 报告 viewer（A/B/C/D tab + 搜索）
- 上传组件支持拖拽、断点续传、bib 多选 + 显示 chars

## 文件清单（按优先级）

### P0（不做服务跑不起来）
- `core/api.py::check_stream` 新建
- `core/llm.py::_extract_json` 加控制字符防御
- `server/app.py` `server/worker.py` `server/storage.py` 新建

### P1（提升体验）
- `core/match.py` `on_partial` 回调
- 取消令牌贯穿 extract/match/judge
- 前端原型

### P2（规模化）
- 多 worker / Celery
- `core/bib_registry.py` 后期管理员内置常用古籍时实现
- CDN 缓存策略调优

## 验证步骤

### 步骤 1：核心层流式接口（CLI 层无需改）

写测试 `tests/test_check_stream.py`：

```python
events = list(check_stream("manuscript/幺弟解惑-部分章节.docx", ["bib/Dao De Jing.txt"]))
# 断言：第一个 verdict 事件出现的 wall-clock 时间 < 5s
# 断言：事件类型覆盖 parse_done / extract_chunk / match_partial / judge_done / done
```

### 步骤 2：FastAPI 端到端

```bash
uv run uvicorn server.app:app --reload
curl -F manuscript=@manuscript.docx -F bibs=lunyu,daodejing /api/jobs
# 返回 {job_id}
curl -N /api/jobs/{job_id}/events  # SSE 流
curl /api/jobs/{job_id}/report?fmt=md > report.md
```

### 步骤 3：30 万字 + 10 bib 压力测试

- 用 `manuscript/260427-幺弟解惑-完整书稿.docx`（115k 字，扩 3 倍模拟 300k）
- 全部 10 个 bib（合计 ~5MB raw / ~50MB indexed）
- 通过标准：
  - 内存峰值 ≤ 4GB
  - 同 sha bib 第二次任务加载 ≤ 5s（命中 pickle 缓存）
  - 端到端 ≤ 6 分钟
  - SSE 持续推送，无 30s+ 静默
  - 取消任务能 ≤ 2s 内停掉所有 LLM 调用

### 步骤 4：网络劣化测试

用 Chrome DevTools "Slow 3G" 验证：
- 10MB 上传可恢复
- SSE 断线自动续传，事件不重复
- 大报告（5MB）能边下边看

## 风险

| 风险 | 应对 |
|---|---|
| EdgeOne SSE 透传被 buffer 卡死 | 显式设 `X-Accel-Buffering: no`；不行就用 WebSocket |
| 用户上传同名 bib 导致冲突 | 用 sha256 当 bib_id；同内容上传去重 |
| 单 worker 跑不动 10 并发任务 | 上 arq + Redis；首 MVP 限 1 并发任务 |
| LLM 配额耗尽（30 万字 ~150-200 LLM 调用 × 多任务） | 每任务前 token 估算 + 配额检查；超额提前拒绝 |
| 报告 5MB Markdown 浏览器卡顿 | 报告分页（按章节）+ 虚拟滚动 |
| EdgeOne Pages Functions 60s 超时 | 所有耗时操作放源站；边缘函数只做签名 / 透传 |

## 时间评估

- P0 核心改动 + FastAPI 雏形：3-5 天
- 前端原型 + EdgeOne 部署：3-5 天
- 大书稿 + 大 bib 压测调优：2-3 天
- 总计：约 2 周到「可用 demo」

## 与上一轮的关系

上一轮（异步 LLM 提取流水线）已完成并 freeze 在 `core/extract_llm.py` / `core/llm.py`。本轮**不动这些**，只在外围加：
- 流式 check 包装（不破坏旧签名）
- HTTP 服务层（全新）
- chunk JSON 解析防御（小补丁）
- bib registry 接口占位（不实现）

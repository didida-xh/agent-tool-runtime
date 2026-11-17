# agent-tool-runtime

带工具调用的业务助手后端。模型通过 function calling 调用真实业务接口（订单 / 库存 / 天气 / 退款），
多轮循环、失败自愈、熔断主备，全过程可落库、可追溯、可回放。

## 实测结果

| # | 指标 | 目标 | 实测 |
|---|---|---|---|
| 1 | 端到端成功率 | ≥95% | **100%**（100/100） |
| 2 | 平均工具调用轮次 | ≤3 | **1.01** |
| 3 | 工具超时率 | ≤1% | **0.00%**（129 次调用） |
| 4 | 参数缺字段自愈率 | ≥70% | **95%**（19/20） |
| 5 | 调用链可追溯 | 100% | **100%**（100/100 有 trace） |

`python -m scripts.acceptance` 一键复现上表，全 PASS 时退出码为 0。

## 一次请求走了哪些路

```
POST /chat
  │
  ├─ 幂等检查  X-Idempotency-Key → Redis SET NX
  │     └─ 已存在 ──▶ 回放上次结果（replayed: true），不重复执行
  │
  ├─ tasks 表落 running
  ├─ observe.trace 开始（Langfuse 优先，失败降级本地 JSON）
  │
  └─ run_agent 循环（最多 5 轮）
       │
       ├─ call_model ── 熔断器 CLOSED / OPEN / HALF_OPEN ──▶ 主模型 或 备模型
       │     └─ span: llm（round、model、circuit）
       │
       ├─ 无 tool_calls ──▶ 直接作答，任务结束
       │
       └─ 有 tool_calls（一轮可能多个）
            └─ 逐个 registry.execute
                 ├─ Pydantic 校验 → 缺字段 / 类型错 → invalid
                 ├─ 超时 → timeout；权限不足 → denied
                 ├─ 任何失败都转成文本返回，绝不抛异常
                 ├─ span: tool（name、round、status、duration）
                 └─ tool_calls 表落库（入参、耗时、结果截断 512 字）
            └─ 回填 role=tool，进入下一轮

  第 5 轮仍未收敛 ──▶ 注入「停止调工具」系统消息 + tools=None ──▶ 强制总结
```

## 能力一览

| 能力 | 实现位置 |
|---|---|
| 工具注册表：JSON Schema 给模型看，Pydantic 给代码把关 | `app/tools/registry.py` |
| 多轮循环引擎：最多 5 轮，超限注入强制总结 | `app/agent_loop.py` |
| 工具失败不抛异常，错误文本回填给模型自愈 | `registry._error_content` |
| 每次调用落库（入参 / 耗时 / 结果截断 / 状态） | `app/db.py` |
| 模型熔断 + 主备切换（CLOSED → OPEN → HALF_OPEN） | `app/llm.py` |
| 调用链可观测：优先 Langfuse，失败降级本地 JSON | `app/observe.py` |
| 调用链 HTML 瀑布页 | `app/chain.py` |
| 请求级幂等（Redis SET NX + 故障降级） | `app/cache.py` |
| 100 条评测集 + 跑批 + 自愈专项 + 一键验收 | `scripts/` |

## 目录结构

```
app/
  main.py            FastAPI 入口：/chat、幂等、trace 包裹
  agent_loop.py      循环引擎（唯一的任务执行入口）
  llm.py             模型调用 + 熔断主备
  observe.py         trace / span，三级降级
  chain.py           调用链页面与 JSON 接口
  cache.py           Redis 幂等状态位
  db.py              PG 访问层
  tools/
    registry.py      注册表、装饰器、校验、执行、超时
    order.py         订单 / 库存查询（只读）
    weather.py       天气查询（Open-Meteo，含内置坐标表）
    refund.py        退款（写操作，三道业务校验）
migrations/          001 建表、002 补 status 约束
scripts/
  init_db.py         按序执行迁移
  run_eval.py        100 条评测跑批
  run_selfheal.py    缺参注入式自愈评测
  acceptance.py      五项指标对照
data/
  eval/tasks.jsonl   评测集
  eval/report.json   跑批结果（提交入库，作为验收证据）
  traces/            本地 trace（gitignore）
tests/               单元测试
```

## 快速开始

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env        # 填 DEEPSEEK_API_KEY / DATABASE_URL
set -a && source .env && set +a

python -m scripts.init_db   # 建表 / 执行迁移
uvicorn app.main:app --port 8000
```

`.env` 关键项：

```
DEEPSEEK_API_KEY=sk-xxx
DEEPSEEK_BASE_URL=https://api.deepseek.com
MODEL_NAME=deepseek-chat
MODEL_FALLBACK=deepseek-reasoner
DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/agent
REDIS_URL=redis://127.0.0.1:6379/0
# 可选：不配则 trace 只落本地 JSON
LANGFUSE_PUBLIC_KEY=
LANGFUSE_SECRET_KEY=
LANGFUSE_HOST=https://cloud.langfuse.com
```

> uvicorn 不会自动读 `.env`，启动前必须 `set -a && source .env && set +a`。

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/chat` | 跑一次完整任务；写操作需在 body 里带 `confirm_token`。支持 `X-Idempotency-Key`，同 key 只真正执行一次 |
| GET | `/chain?task_id=` | 调用链 HTML 页面（耗时条 / 状态色 / 入参与输出） |
| GET | `/tasks/{task_id}` | 同一份数据的 JSON 版本 |
| GET | `/health` | 存活探针 |

```bash
curl -s -X POST http://127.0.0.1:8000/chat \
  -H 'Content-Type: application/json' \
  -d '{"question":"查一下订单 A1001 的状态，再看看杭州明天的天气"}'

# 打开调用链（把 task_id 换成上一步返回值）
open "http://127.0.0.1:8000/chain?task_id=<task_id>"
```

## 验收

```bash
pytest -q                                        # 单元测试

set -a && source .env && set +a
python -m scripts.run_eval --reset               # 100 条评测 → data/eval/report.json
python -m scripts.run_selfheal                   # 20 条缺参注入 → data/eval/selfheal.json
python -m scripts.acceptance                     # 五项指标对照，全 PASS 退出码 0
```

常用参数：`run_eval --ids e001,e081` 只跑指定用例，`-n 10` 只跑前 10 条。

## 指标口径

| # | 指标 | 目标 | 怎么算 |
|---|---|---|---|
| 1 | 端到端成功率 | ≥95% | 工具序列正确 + 关键参数正确 + 终答命中关键事实/正则；不判文本相似度 |
| 2 | 平均工具调用轮次 | ≤3 | 每任务 `max(round)` 求均值，按任务数平均 |
| 3 | 工具超时率 | ≤1% | 超时次数 / 工具调用总次数 |
| 4 | 参数缺字段自愈率 | ≥70% | 注入缺参 → 首轮 `invalid` → 后续补齐并成功 |
| 5 | 调用链可追溯 | 100% | 每条任务有 trace，含 llm span 与 tool span |

自愈率靠**真实注入**得到：`run_selfheal.py` 包住 `call_model`，在第一次命中目标工具时删掉一个必填参数，
让 registry 真的返回校验错误，再看模型能否补齐——不是拿假数据算出来的。

## 评测集构成

100 条：订单 20 / 库存 15 / 天气 12 / 组合 20 / 负例 13 / 退款 10 / 闲聊 10。

- 退款类带 `reset_before`，每条执行前把种子订单恢复原状；
- 退款类带 `confirm_turns: 2`，第一轮模型请求确认，第二轮用「我回复：确认」走完写操作；
- 负例判据用正则而非固定关键词，避免模型的同义表达（没有找到 / 无此记录）被判错；
- 闲聊类要求 `expect_tools: []`，专门防止模型无脑调工具。

## 已知限制

- **熔断状态是进程内的**，多 worker 各自一份；生产环境需要放 Redis 共享。
- **Langfuse 为可选项**，当前实测走本地 JSON 降级路径；配好 key 即自动切换。
- **超时率 0% 是外部依赖稳定的结果**，未做故障注入压测；`tests/test_registry.py` 覆盖了超时分支的单测。
- **天气工具依赖公网** Open-Meteo，离线环境下应换 fixture 或 mock。
- **幂等 TTL 24 小时**，且只做任务级去重；更细粒度的接口级幂等需要在工具层再落一层。
- 业务数据是 `migrations/001_init.sql` 里的种子数据，非真实订单系统。

## 踩坑记录

1. **不能靠 `content` 判断要不要继续调工具**。`deepseek-reasoner` 带 `tool_calls` 时 `content` 也非空，
   唯一可靠判据是 `msg.tool_calls` 是否为空。
2. **`tool_calls[].function.arguments` 是字符串**，不是 dict。落 jsonb 前要先 `json.loads` 再 `dumps`，
   否则双重编码存成字符串，`->>'key'` 查不出来。
3. **`ToolRegistry` 实现了 `__len__`，空表是 falsy**。`registry or REGISTRY` 会把空表换掉，
   必须写 `registry if registry is not None else REGISTRY`。
4. **退款用例最初 0/10 不是模型不会调工具**。SYSTEM_PROMPT 要求写操作先请求确认，
   而第二轮确认语只复述了订单号和金额、丢了 `reason`，模型于是停下来追问枚举值。
   把原因带上、并明确「用户已确认就立即执行」后变成 10/10。
5. **Open-Meteo 地理编码会把「火星」匹配到同名村庄**，返回一份看似正常的数据。
   加 `countryCode=CN` 且只接受 `PPLC/PPLA*` 行政中心后，才能正确返回「未找到城市」。
6. **`tasks.status` 的 CHECK 漏了 `max_rounds`**，超轮次任务落库会被约束挡住，见 `migrations/002_task_status.sql`。
7. **`asyncio.wait_for` 只能取消协程**，中断不了同步阻塞调用；工具必须用 `httpx.AsyncClient` / `asyncpg`。
8. **`load_dotenv()` 不会覆盖已存在的环境变量**，所以 `MODEL_NAME=xxx uvicorn ...` 这种临时覆盖是有效的。
9. **zsh 交互模式默认不认行内 `#` 注释**，粘贴带注释的命令会把 `#` 后面的词当成参数。

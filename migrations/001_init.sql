-- 001_init.sql  幂等建表 + 种子数据（可重复执行）

-- ---------- 业务表 ----------
CREATE TABLE IF NOT EXISTS orders (
    order_id     TEXT PRIMARY KEY,
    customer     TEXT NOT NULL,
    city         TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('paid','shipped','delivered','refunding','refunded')),
    amount       NUMERIC(10,2) NOT NULL CHECK (amount > 0),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS inventory (
    sku            TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    stock          INTEGER NOT NULL CHECK (stock >= 0),
    warehouse_city TEXT NOT NULL
);

-- ---------- 任务与调用记录 ----------
CREATE TABLE IF NOT EXISTS tasks (
    id            TEXT PRIMARY KEY,           -- uuid4 hex，应用生成
    request       TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'running'
                  CHECK (status IN ('running','success','failed')),
    final_answer  TEXT,
    rounds        INTEGER NOT NULL DEFAULT 0, -- 工具调用轮次（max round）
    trace_id      TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tool_calls (
    id                BIGSERIAL PRIMARY KEY,
    task_id           TEXT NOT NULL REFERENCES tasks(id),
    round             INTEGER NOT NULL,
    tool_name         TEXT NOT NULL,
    arguments         JSONB NOT NULL,
    result_truncated  TEXT,                   -- 结果截断 ≤512 字符
    status            TEXT NOT NULL
                      CHECK (status IN ('success','invalid','timeout','denied','error')),
    duration_ms       INTEGER NOT NULL,
    error             TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_tool_calls_task ON tool_calls(task_id, round);

-- ---------- 种子数据（幂等） ----------
INSERT INTO orders (order_id, customer, city, status, amount) VALUES
    ('A1001', '张伟', '杭州', 'paid',      299.00),
    ('A1002', '李娜', '成都', 'delivered',  89.50),
    ('A1003', '王强', '广州', 'shipped',   459.00),
    ('A1004', '赵敏', '北京', 'refunding',1299.00)
ON CONFLICT (order_id) DO NOTHING;

INSERT INTO inventory (sku, name, stock, warehouse_city) VALUES
    ('SKU-001', '保温杯',   120, '杭州仓'),
    ('SKU-002', '电热毯',     8, '成都仓'),
    ('SKU-003', '空气炸锅',   0, '广州仓')
ON CONFLICT (sku) DO NOTHING;

-- tasks.status 的 CHECK 漏了 max_rounds。
-- 循环引擎超轮次收尾时返回 max_rounds，直接落库会被约束挡住。
ALTER TABLE tasks DROP CONSTRAINT IF EXISTS tasks_status_check;
ALTER TABLE tasks ADD CONSTRAINT tasks_status_check
    CHECK (status IN ('running', 'success', 'failed', 'max_rounds'));

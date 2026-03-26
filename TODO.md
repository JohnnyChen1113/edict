# Claude Code Backend — TODO

`feat/claude-code-backend` 分支遗留改进项。

## Done (this session)

- [x] dispatch 失败时 publish `task.dispatch.failed` 到 `TOPIC_TASK_STATUS`
- [x] `asyncio.get_event_loop()` → `asyncio.get_running_loop()`
- [x] sync 过滤逻辑：补上 `"Next"` 状态，避免会话从看板消失

## TODO

### SOUL.md 改用 `--system-prompt`

`dispatch_worker.py` 把 SOUL.md 内容拼进 `-p` prompt，应改用 Claude Code CLI 的 `--system-prompt` 参数，分离系统指令和用户任务。

**文件**: `edict/backend/app/workers/dispatch_worker.py` `_call_claude_code()`

### CLAUDE.md / SOUL.md 统一

当前 dispatch worker 同时：
1. 手动加载 SOUL.md 拼入 prompt
2. 设 cwd 到 agent 目录，Claude Code 自动加载 CLAUDE.md

两份指令可能冲突。建议统一为只用 CLAUDE.md（Claude Code 自动加载），将 SOUL.md 中独有的内容合并进去，然后从 dispatch worker 中移除 `_load_soul()` 逻辑。

**文件**:
- `edict/backend/app/workers/dispatch_worker.py`
- `agents/*/SOUL.md` → 合并到 `agents/*/CLAUDE.md`

### 添加测试

- `dispatch_worker.py`: prompt 构建逻辑、失败状态发布、SOUL.md 加载
- `sync_from_claude_code.py`: session 解析、agent 检测、状态推断、过滤逻辑

### 清理 OpenClaw legacy 配置

`config.py` 中的 `openclaw_*` 字段在切换到 Claude Code 后可标记为 deprecated 或移除。

**文件**: `edict/backend/app/config.py`

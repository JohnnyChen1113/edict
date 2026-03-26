"""Dispatch Worker — 消费 task.dispatch 事件，执行 Claude Code agent 调用。

核心解决旧架构痛点：
- 旧: daemon 线程 + subprocess.run → kill -9 丢失一切
- 新: Redis Streams ACK 保证 → 崩溃后自动重新投递

流程:
1. 从 task.dispatch stream 消费事件
2. 调用 Claude Code CLI: `claude -p "..." --output-format json`
   每个 agent 的 SOUL.md 作为 system prompt 注入
3. 解析 agent 输出（kanban_update.py 调用结果）
4. ACK 事件
"""

import asyncio
import json
import logging
import os
import signal
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ..config import get_settings
from ..services.event_bus import (
    EventBus,
    TOPIC_TASK_DISPATCH,
    TOPIC_TASK_STATUS,
    TOPIC_AGENT_THOUGHTS,
    TOPIC_AGENT_HEARTBEAT,
)

log = logging.getLogger("edict.dispatcher")

GROUP = "dispatcher"
CONSUMER = "disp-1"


class DispatchWorker:
    """Agent 派发 Worker — 调用 Claude Code CLI 执行 agent 任务。"""

    def __init__(self, max_concurrent: int = 3):
        self.bus = EventBus()
        self._running = False
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._active_tasks: dict[str, asyncio.Task] = {}
        self._soul_cache: dict[str, str] = {}

    async def start(self):
        await self.bus.connect()
        await self.bus.ensure_consumer_group(TOPIC_TASK_DISPATCH, GROUP)
        self._running = True
        log.info("🚀 Dispatch worker started")

        # 恢复崩溃遗留
        await self._recover_pending()

        while self._running:
            try:
                await self._poll_cycle()
            except Exception as e:
                log.error(f"Dispatch poll error: {e}", exc_info=True)
                await asyncio.sleep(2)

    async def stop(self):
        self._running = False
        # 等待进行中的 agent 调用完成
        if self._active_tasks:
            log.info(f"Waiting for {len(self._active_tasks)} active dispatches...")
            await asyncio.gather(*self._active_tasks.values(), return_exceptions=True)
        await self.bus.close()
        log.info("Dispatch worker stopped")

    async def _recover_pending(self):
        events = await self.bus.claim_stale(
            TOPIC_TASK_DISPATCH, GROUP, CONSUMER, min_idle_ms=60000, count=20
        )
        if events:
            log.info(f"Recovering {len(events)} stale dispatch events")
            for entry_id, event in events:
                await self._dispatch(entry_id, event)

    async def _poll_cycle(self):
        events = await self.bus.consume(
            TOPIC_TASK_DISPATCH, GROUP, CONSUMER, count=3, block_ms=2000
        )
        for entry_id, event in events:
            # 每个派发在独立任务中执行，带并发控制
            task = asyncio.create_task(self._dispatch(entry_id, event))
            task_id = event.get("payload", {}).get("task_id", entry_id)
            self._active_tasks[task_id] = task
            task.add_done_callback(lambda t, tid=task_id: self._active_tasks.pop(tid, None))

    async def _dispatch(self, entry_id: str, event: dict):
        """执行一次 agent 派发。"""
        async with self._semaphore:
            payload = event.get("payload", {})
            task_id = payload.get("task_id", "")
            agent = payload.get("agent", "")
            message = payload.get("message", "")
            trace_id = event.get("trace_id", "")
            state = payload.get("state", "")

            log.info(f"🔄 Dispatching task {task_id} → agent '{agent}' state={state}")

            # 发布心跳
            await self.bus.publish(
                topic=TOPIC_AGENT_HEARTBEAT,
                trace_id=trace_id,
                event_type="agent.dispatch.start",
                producer="dispatcher",
                payload={"task_id": task_id, "agent": agent},
            )

            try:
                result = await self._call_claude_code(agent, message, task_id, trace_id)

                # 发布 agent 输出
                await self.bus.publish(
                    topic=TOPIC_AGENT_THOUGHTS,
                    trace_id=trace_id,
                    event_type="agent.output",
                    producer=f"agent.{agent}",
                    payload={
                        "task_id": task_id,
                        "agent": agent,
                        "output": result.get("stdout", ""),
                        "return_code": result.get("returncode", -1),
                    },
                )

                if result.get("returncode") == 0:
                    log.info(f"✅ Agent '{agent}' completed task {task_id}")
                else:
                    log.warning(
                        f"⚠️ Agent '{agent}' returned non-zero for task {task_id}: "
                        f"rc={result.get('returncode')}"
                    )

                # ACK — 事件处理完毕
                await self.bus.ack(TOPIC_TASK_DISPATCH, GROUP, entry_id)

            except Exception as e:
                log.error(f"❌ Dispatch failed: task {task_id} → {agent}: {e}", exc_info=True)
                # 不 ACK → Redis 会重新投递给其他消费者

    def _load_soul(self, agent: str) -> str:
        """加载 agent 的 SOUL.md 作为 system prompt。"""
        if agent in self._soul_cache:
            return self._soul_cache[agent]

        settings = get_settings()
        # 在项目目录中查找 agents/{agent}/SOUL.md
        base_dir = Path(settings.claude_project_dir or ".")
        soul_path = base_dir / "agents" / agent / "SOUL.md"

        if soul_path.exists():
            soul_content = soul_path.read_text(encoding="utf-8")
            self._soul_cache[agent] = soul_content
            return soul_content

        log.warning(f"SOUL.md not found for agent '{agent}' at {soul_path}")
        return f"你是 {agent} agent，请根据任务指示执行工作。"

    async def _call_claude_code(
        self,
        agent: str,
        message: str,
        task_id: str,
        trace_id: str,
    ) -> dict:
        """异步调用 Claude Code CLI — 在线程池中执行。

        每个 agent 的 SOUL.md 通过 --system-prompt 注入角色定义，
        任务内容通过 -p (print/prompt) 传入。
        """
        settings = get_settings()
        soul = self._load_soul(agent)

        # 构建完整的 prompt：SOUL.md 角色定义 + 任务上下文
        full_prompt = (
            f"{soul}\n\n"
            f"---\n\n"
            f"## 当前任务\n\n"
            f"任务ID: {task_id}\n"
            f"追踪ID: {trace_id}\n\n"
            f"{message}"
        )

        cmd = [
            settings.claude_bin,
            "-p", full_prompt,
            "--output-format", "json",
            "--max-turns", str(settings.claude_max_turns),
        ]

        # 如果配置了 allowedTools，添加限制
        if settings.claude_allowed_tools:
            cmd.extend(["--allowedTools", settings.claude_allowed_tools])

        env = os.environ.copy()
        env["EDICT_TASK_ID"] = task_id
        env["EDICT_TRACE_ID"] = trace_id
        env["EDICT_API_URL"] = f"http://localhost:{settings.port}"

        # Claude Code 使用 agent 工作目录作为 cwd，使 CLAUDE.md 生效
        agent_cwd = None
        if settings.claude_project_dir:
            agent_workspace = Path(settings.claude_project_dir) / "agents" / agent
            if agent_workspace.exists():
                agent_cwd = str(agent_workspace)
            else:
                agent_cwd = settings.claude_project_dir

        log.debug(f"Executing: {' '.join(cmd[:4])}... (cwd={agent_cwd})")

        def _run():
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=settings.dispatch_timeout_sec,
                    env=env,
                    cwd=agent_cwd,
                )

                stdout = proc.stdout[-5000:] if proc.stdout else ""

                # Claude Code --output-format json 输出 JSON，尝试解析
                try:
                    output_json = json.loads(proc.stdout) if proc.stdout else {}
                    # 提取 result 字段作为主要输出
                    if "result" in output_json:
                        stdout = output_json["result"][-5000:]
                except (json.JSONDecodeError, TypeError):
                    pass  # 非 JSON 输出，使用原始 stdout

                return {
                    "returncode": proc.returncode,
                    "stdout": stdout,
                    "stderr": proc.stderr[-2000:] if proc.stderr else "",
                }
            except subprocess.TimeoutExpired:
                return {
                    "returncode": -1,
                    "stdout": "",
                    "stderr": f"TIMEOUT after {settings.dispatch_timeout_sec}s",
                }
            except FileNotFoundError:
                return {
                    "returncode": -1,
                    "stdout": "",
                    "stderr": f"claude command not found at '{settings.claude_bin}'",
                }

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _run)


async def run_dispatcher():
    """入口函数 — 用于直接运行 worker。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    worker = DispatchWorker()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(worker.stop()))

    await worker.start()


if __name__ == "__main__":
    asyncio.run(run_dispatcher())

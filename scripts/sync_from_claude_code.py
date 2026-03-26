#!/usr/bin/env python3
"""Sync worker — 从 Claude Code 会话数据同步 agent 状态到看板。

替代 sync_from_openclaw_runtime.py，读取 ~/.claude/projects/ 下的
JSONL 会话文件，解析 agent 活动状态并写入 tasks_source.json。

Claude Code JSONL 格式:
  每行一个 JSON 对象，包含:
  - type: "summary" | "progress" | "user" | "assistant" | "tool_use" | "tool_result"
  - data: 事件数据
  - timestamp: ISO 格式时间戳
  - uuid: 事件唯一 ID
"""

import json
import os
import pathlib
import time
import datetime
import traceback
import logging
from file_lock import atomic_json_write, atomic_json_read

log = logging.getLogger('sync_claude')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(message)s',
    datefmt='%H:%M:%S',
)

BASE = pathlib.Path(__file__).resolve().parent.parent
DATA = BASE / 'data'
DATA.mkdir(exist_ok=True)
SYNC_STATUS = DATA / 'sync_status.json'

# Claude Code 项目会话目录
# 当 edict 在 /Users/johnny/edict 时，会话存储在:
# ~/.claude/projects/-Users-johnny-edict/*.jsonl
CLAUDE_HOME = pathlib.Path.home() / '.claude'
PROJECT_DIR = os.environ.get('CLAUDE_PROJECT_DIR', str(BASE))
# Claude Code 将路径中的 / 替换为 -，首字符也是 -
PROJECT_KEY = PROJECT_DIR.replace('/', '-')
SESSIONS_DIR = CLAUDE_HOME / 'projects' / PROJECT_KEY

# Agent ID 列表 — 用于从会话内容中识别是哪个 agent 在工作
KNOWN_AGENTS = {
    'taizi', 'zhongshu', 'menxia', 'shangshu',
    'gongbu', 'bingbu', 'hubu', 'libu', 'xingbu', 'libu_hr', 'zaochao',
}


def write_status(**kwargs):
    atomic_json_write(SYNC_STATUS, kwargs)


def ts_to_str(ts_iso):
    """ISO 时间戳转显示字符串。"""
    if not ts_iso:
        return '-'
    try:
        dt = datetime.datetime.fromisoformat(ts_iso.replace('Z', '+00:00'))
        return dt.strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        return '-'


def ts_to_ms(ts_iso):
    """ISO 时间戳转毫秒。"""
    if not ts_iso:
        return 0
    try:
        dt = datetime.datetime.fromisoformat(ts_iso.replace('Z', '+00:00'))
        return int(dt.timestamp() * 1000)
    except Exception:
        return 0


def detect_official(agent_id):
    mapping = {
        'taizi':    ('储君', '太子'),
        'zhongshu': ('中书令', '中书省'),
        'menxia':   ('侍中', '门下省'),
        'shangshu': ('尚书令', '尚书省'),
        'hubu':     ('户部尚书', '户部'),
        'libu':     ('礼部尚书', '礼部'),
        'bingbu':   ('兵部尚书', '兵部'),
        'xingbu':   ('刑部尚书', '刑部'),
        'gongbu':   ('工部尚书', '工部'),
        'libu_hr':  ('吏部尚书', '吏部'),
        'zaochao':  ('钦天监', '钦天监'),
    }
    return mapping.get(agent_id, ('尚书令', '尚书省'))


def detect_agent_from_content(events):
    """从会话内容中检测是哪个 agent。

    检查方式：
    1. 查找 prompt 中包含的 agent 关键词
    2. 查找 SOUL.md 或 CLAUDE.md 的引用
    3. 查找 kanban_update.py 调用中的 agent 线索
    """
    for ev in events[:20]:  # 只看前 20 个事件
        data = ev.get('data', {})

        # 检查 user 消息中的 agent 关键词
        if ev.get('type') == 'user':
            text = ''
            if isinstance(data, dict):
                text = json.dumps(data)
            elif isinstance(data, str):
                text = data

            text_lower = text.lower()
            for agent_id in KNOWN_AGENTS:
                if agent_id in text_lower:
                    return agent_id

                # 也检查中文名
                cn_map = {
                    'taizi': '太子', 'zhongshu': '中书省', 'menxia': '门下省',
                    'shangshu': '尚书省', 'gongbu': '工部', 'bingbu': '兵部',
                    'hubu': '户部', 'libu': '礼部', 'xingbu': '刑部',
                    'libu_hr': '吏部', 'zaochao': '钦天监',
                }
                if cn_map.get(agent_id, '') in text:
                    return agent_id

        # 检查 tool 调用中的 kanban_update.py
        if ev.get('type') == 'tool_use':
            tool_input = str(data.get('input', ''))
            if 'kanban_update' in tool_input:
                for agent_id in KNOWN_AGENTS:
                    if agent_id in tool_input.lower():
                        return agent_id

    return None


def load_session_events(session_file, limit=50):
    """加载 Claude Code JSONL 会话文件中的事件。"""
    events = []
    try:
        lines = session_file.read_text(errors='ignore').splitlines()
        for ln in lines:
            try:
                events.append(json.loads(ln))
            except (json.JSONDecodeError, ValueError):
                continue
    except Exception:
        return []
    return events[-limit:] if len(events) > limit else events


def extract_activity(events, limit=12):
    """从 Claude Code 事件中提取 agent 活动日志。"""
    rows = []
    for ev in reversed(events):
        ev_type = ev.get('type', '')
        ts = ev.get('timestamp', '')
        data = ev.get('data', {})

        if ev_type == 'assistant':
            # assistant 消息
            text = ''
            if isinstance(data, dict):
                content = data.get('content', [])
                for c in (content if isinstance(content, list) else []):
                    if isinstance(c, dict) and c.get('type') == 'text':
                        text = c.get('text', '').strip()
                        break
            if text:
                summary = text.split('\n')[0]
                if len(summary) > 200:
                    summary = summary[:200] + '...'
                rows.append({'at': ts, 'kind': 'assistant', 'text': summary})

        elif ev_type == 'tool_result':
            tool = data.get('name', '-') if isinstance(data, dict) else '-'
            rows.append({'at': ts, 'kind': 'tool', 'text': f"Tool '{tool}' finished"})

        elif ev_type == 'user':
            text = ''
            if isinstance(data, dict):
                content = data.get('content', [])
                for c in (content if isinstance(content, list) else []):
                    if isinstance(c, dict) and c.get('type') == 'text':
                        text = c.get('text', '')[:100]
                        break
            if text:
                rows.append({'at': ts, 'kind': 'user', 'text': f"User: {text}..."})

        if len(rows) >= limit:
            break

    return rows


def state_from_age(age_ms):
    """根据最后活动时间推断状态。"""
    if age_ms <= 2 * 60 * 1000:
        return 'Doing'
    if age_ms <= 60 * 60 * 1000:
        return 'Review'
    return 'Next'


def build_task_from_session(session_file, now_ms):
    """从一个 Claude Code 会话文件构建看板任务。"""
    events = load_session_events(session_file)
    if not events:
        return None

    # 获取时间信息
    first_ts = events[0].get('timestamp', '')
    last_ts = events[-1].get('timestamp', '')
    last_ms = ts_to_ms(last_ts)
    age_ms = max(0, now_ms - last_ms) if last_ms else 99 * 24 * 3600 * 1000

    # 检测 agent
    agent_id = detect_agent_from_content(events)
    if not agent_id:
        return None  # 非 edict agent 的会话，跳过

    session_id = session_file.stem  # UUID
    official, org = detect_official(agent_id)
    state = state_from_age(age_ms)

    # 提取活动日志
    acts = extract_activity(events, limit=10)
    latest_act = '等待指令'
    if acts:
        first_act = acts[0]
        if first_act['kind'] == 'assistant':
            latest_act = f"思考中: {first_act['text'][:80]}"
        elif first_act['kind'] == 'tool':
            latest_act = first_act['text'][:60]
        else:
            latest_act = acts[0]['text'][:60]

    return {
        'id': f"CC-{agent_id}-{session_id[:8]}",
        'title': f"{org}会话",
        'official': official,
        'org': org,
        'state': state,
        'now': latest_act,
        'eta': ts_to_str(last_ts),
        'block': '无',
        'output': str(session_file),
        'flow': {
            'draft': f"agent={agent_id}",
            'review': f"lastActive={ts_to_str(last_ts)}",
            'dispatch': f"session={session_id[:8]}",
        },
        'ac': '来自 Claude Code 会话的实时映射',
        'activity': acts,
        'sourceMeta': {
            'agentId': agent_id,
            'sessionId': session_id,
            'updatedAt': last_ms,
            'ageMs': age_ms,
            'backend': 'claude-code',
        },
    }


def main():
    start = time.time()
    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    now_ms = int(time.time() * 1000)

    try:
        tasks = []
        scan_files = 0

        if SESSIONS_DIR.exists():
            for session_file in sorted(SESSIONS_DIR.glob('*.jsonl')):
                scan_files += 1
                task = build_task_from_session(session_file, now_ms)
                if task:
                    tasks.append(task)
        else:
            log.warning(f'Claude Code sessions dir not found: {SESSIONS_DIR}')

        # merge mission control tasks
        mc_tasks_file = DATA / 'mission_control_tasks.json'
        if mc_tasks_file.exists():
            try:
                mc_tasks = json.loads(mc_tasks_file.read_text())
                if isinstance(mc_tasks, list):
                    tasks.extend(mc_tasks)
            except Exception:
                pass

        # merge manual parallel tasks
        manual_tasks_file = DATA / 'manual_parallel_tasks.json'
        if manual_tasks_file.exists():
            try:
                manual_tasks = json.loads(manual_tasks_file.read_text())
                if isinstance(manual_tasks, list):
                    tasks.extend(manual_tasks)
            except Exception:
                pass

        tasks.sort(
            key=lambda x: x.get('sourceMeta', {}).get('updatedAt', 0),
            reverse=True,
        )

        # 去重
        seen_ids = set()
        deduped = []
        for t in tasks:
            if t['id'] not in seen_ids:
                seen_ids.add(t['id'])
                deduped.append(t)
        tasks = deduped

        # 过滤非活跃会话
        one_day_ago = now_ms - 24 * 3600 * 1000
        filtered = []
        for t in tasks:
            if str(t['id']).startswith('JJC'):
                filtered.append(t)
                continue
            updated = t.get('sourceMeta', {}).get('updatedAt', 0)
            if updated < one_day_ago:
                continue
            state = t.get('state')
            if state in ('Doing', 'Review', 'Blocked', 'Next'):
                filtered.append(t)
        tasks = filtered

        # 保留已有 JJC 任务
        existing_tasks_file = DATA / 'tasks_source.json'
        if existing_tasks_file.exists():
            try:
                existing = json.loads(existing_tasks_file.read_text())
                jjc_existing = [
                    t for t in existing
                    if str(t.get('id', '')).startswith('JJC')
                ]
                tasks = [
                    t for t in tasks
                    if not str(t.get('id', '')).startswith('JJC')
                ]
                tasks = jjc_existing + tasks
            except Exception as e:
                log.error(f'merge existing JJC tasks failed: {e}')

        atomic_json_write(DATA / 'tasks_source.json', tasks)

        duration_ms = int((time.time() - start) * 1000)
        write_status(
            ok=True,
            lastSyncAt=now,
            durationMs=duration_ms,
            source='claude_code_sessions',
            recordCount=len(tasks),
            scannedSessionFiles=scan_files,
            missingFields={},
            error=None,
        )
        log.info(f'synced {len(tasks)} tasks from Claude Code in {duration_ms}ms')

    except Exception as e:
        duration_ms = int((time.time() - start) * 1000)
        write_status(
            ok=False,
            lastSyncAt=now,
            durationMs=duration_ms,
            source='claude_code_sessions',
            recordCount=0,
            missingFields={},
            error=f'{type(e).__name__}: {e}',
            traceback=traceback.format_exc(limit=3),
        )
        raise


if __name__ == '__main__':
    main()

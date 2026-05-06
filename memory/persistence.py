import os
import sqlite3
import time
from datetime import datetime
from typing import Dict, List, Any

class PersistentStateManager:
    """
    管理任务状态、执行日志、文件产出和指标的持久化存储。
    """
    def __init__(self, db_path: str, output_dir: str):
        self.db_path = db_path
        self.output_dir = output_dir
        
        # 确保目录存在
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        os.makedirs(output_dir, exist_ok=True)
        
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        cursor = conn.cursor()
        
        # 任务表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                user_query TEXT,
                status TEXT, 
                created_at TEXT,
                updated_at TEXT
            )
        ''')
        
        # 执行记录表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS executions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT,
                step_index INTEGER,
                code TEXT,
                language TEXT,
                stdout TEXT,
                stderr TEXT,
                returncode INTEGER,
                duration REAL,
                timestamp TEXT,
                output_file_path TEXT
            )
        ''')
        
        # 产物表 (文件/指标)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS artifacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT,
                step_index INTEGER,
                type TEXT,
                key TEXT,
                value TEXT,
                path TEXT,
                timestamp TEXT
            )
        ''')
        conn.commit()
        conn.close()

    def create_task(self, task_id: str, query: str):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT OR REPLACE INTO tasks (task_id, user_query, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (task_id, query, 'running', datetime.now().isoformat(), datetime.now().isoformat())
        )
        conn.commit()
        conn.close()

    def update_task_status(self, task_id: str, status: str):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE tasks SET status = ?, updated_at = ? WHERE task_id = ?",
            (status, datetime.now().isoformat(), task_id)
        )
        conn.commit()
        conn.close()

    def save_execution(self, task_id: str, step_index: int, code: str, language: str, 
                      stdout: str, stderr: str, returncode: int, duration: float) -> Dict[str, Any]:
        """保存执行结果，智能处理大输出"""
        timestamp = datetime.now().isoformat()
        output_file_path = None
        
        # 智能处理: 如果输出超过 2000 字符，保存到文件
        is_large_output = len(stdout) > 2000
        summary_text = stdout
        
        if is_large_output:
            filename = f"{task_id}_step{step_index}_{int(time.time())}.txt"
            output_file_path = os.path.join(self.output_dir, filename)
            with open(output_file_path, 'w', encoding='utf-8') as f:
                f.write(stdout)
            
            # 生成摘要
            head = stdout[:500]
            tail = stdout[-500:]
            summary_text = f"{head}\n... [Skipped {len(stdout)-1000} chars] ...\n{tail}"
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.execute(
            '''INSERT INTO executions (task_id, step_index, code, language, stdout, stderr, returncode, duration, timestamp, output_file_path)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (task_id, step_index, code, language, summary_text, stderr, returncode, duration, timestamp, output_file_path)
        )
        execution_id = cursor.lastrowid
        conn.commit()
        conn.close()
        
        return {
            'type': 'output_ref' if is_large_output else 'inline',
            'content': stdout,
            'summary': summary_text,
            'file_path': output_file_path,
            'size_bytes': len(stdout),
            'output_id': execution_id
        }

    def save_artifact(self, task_id: str, step_index: int, artifact_type: str, key: str, value: str, path: str = None):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO artifacts (task_id, step_index, type, key, value, path, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (task_id, step_index, artifact_type, key, str(value), path, datetime.now().isoformat())
        )
        conn.commit()
        conn.close()

    def get_task_context(self, task_id: str) -> Dict[str, Any]:
        """获取任务上下文，用于恢复"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        
        task = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        executions = conn.execute("SELECT * FROM executions WHERE task_id = ? ORDER BY step_index", (task_id,)).fetchall()
        artifacts = conn.execute("SELECT * FROM artifacts WHERE task_id = ?", (task_id,)).fetchall()
        conn.close()
        
        if not task:
            return {"task_info": None}
            
        return {
            "task_info": dict(task),
            "executions": [dict(e) for e in executions],
            "artifacts": [dict(a) for a in artifacts]
        }

    def list_tasks(self, limit: int = 10) -> List[Dict]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        tasks = conn.execute("SELECT * FROM tasks ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        conn.close()
        return [dict(t) for t in tasks]

    def resume_task(self, task_id: str) -> str:
        """生成用于恢复任务的 Prompt"""
        ctx = self.get_task_context(task_id)
        if not ctx['task_info']:
            return "Task not found."
            
        history = []
        for exc in ctx['executions']:
            status = "✅ Success" if exc['returncode'] == 0 else "❌ Failed"
            history.append(f"Step {exc['step_index']} [{exc['language']}] {status}:\nCode: {exc['code'][:100]}...\nOutput Summary: {exc['stdout'][:200]}...")
        
        files = [f"{a['key']}: {a['path']}" for a in ctx['artifacts'] if a['type'] == 'file']
        
        prompt = f"""
**🔄 RESUMING TASK: {task_id}**
Original Query: {ctx['task_info']['user_query']}

**📜 Execution History (Previously Completed):**
{chr(10).join(history)}

**📂 Known Files:**
{chr(10).join(files)}

Please continue from the last step. Do not repeat successful steps unless necessary.
"""
        return prompt
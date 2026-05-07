import time
import json
import logging
import re
from datetime import datetime
from typing import List, Dict, Any, Tuple, Optional
from dataclasses import dataclass
from langchain_core.messages import HumanMessage

logger = logging.getLogger(__name__)

class ExecutionMonitor:
    """执行监控器 (记录历史、统计耗时)"""
    def __init__(self):
        self.history: List[Dict[str, Any]] = []
        self.start_time = time.time()
    
    def record_execution(
        self,
        code: str,
        language: str,
        stdout: str,
        stderr: str,
        returncode: int,
        duration: float,
        validation_result: Optional[Dict] = None
    ):
        self.history.append({
            'timestamp': datetime.now().isoformat(),
            'code': code,
            'language': language,
            'stdout': stdout[:1000],
            'stderr': stderr[:500],
            'returncode': returncode,
            'duration': duration,
            'success': returncode == 0,
            'validation': validation_result
        })
    
    def get_summary(self) -> Dict[str, Any]:
        total = len(self.history)
        successful = sum(1 for h in self.history if h['success'])
        
        return {
            'total_executions': total,
            'successful': successful,
            'failed': total - successful,
            'success_rate': (successful / total * 100) if total > 0 else 0,
            'total_duration': time.time() - self.start_time,
            'avg_duration': (sum(h['duration'] for h in self.history) / total) if total > 0 else 0
        }
    
    def export_log(self, filepath: str = "execution_log.json"):
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump({'summary': self.get_summary(), 'history': self.history}, f, indent=2)
        logger.info(f"✓ Execution log exported to {filepath}")

class LogCleaner:
    """日志清洗器 (提取报错指纹)"""
    ERROR_KEYWORDS = [
        "error", "exception", "fatal", "fail", "killed", 
        "panic", "segmentation fault", "core dumped", 
        "no such file", "permission denied", "command not found",
        "outofmemory", "java.lang", "traceback", "std::bad_alloc"
    ]

    @staticmethod
    def extract_error_fingerprint(stderr: str, max_lines: int = 5, max_chars: int = 800) -> str:
        if not stderr:
            return "Unknown Error (No stderr output)"
            
        lines = stderr.split('\n')
        critical_lines = []
        
        for i, line in enumerate(lines):
            clean_line = line.strip().lower()
            if not clean_line: continue
            for kw in LogCleaner.ERROR_KEYWORDS:
                if kw in clean_line:
                    critical_lines.append(line.strip())
                    if i + 1 < len(lines):
                        critical_lines.append(lines[i+1].strip())
                    break
        
        if len(critical_lines) < 2:
            tail_lines = [l.strip() for l in lines[-max_lines:] if l.strip()]
            critical_lines.extend(tail_lines)
            
        seen = set()
        final_lines = [x for x in critical_lines if not (x in seen or seen.add(x))]
        result = "\n".join(final_lines)
        
        return result[:max_chars] + "..." if len(result) > max_chars else result

@dataclass
class ErrorPattern:
    pattern: str
    category: str
    suggestion: str
    auto_fix: bool = False

class ErrorRecoverySystem:
    """错误恢复系统 (Regex + LLM)"""
    ERROR_PATTERNS = [
        ErrorPattern(r"No such file or directory: '(.+?)'", "FileNotFound", "文件 {0} 不存在。将自动创建目录。", True),
        ErrorPattern(r"ModuleNotFoundError: No module named '(.+?)'", "MissingModule", "Python 模块 {0} 未安装。建议执行: pip install {0}", True),
        ErrorPattern(r"command not found: (.+)", "CommandNotFound", "命令 {0} 未找到。请检查软件是否已安装。", False),
        ErrorPattern(r"MemoryError", "OutOfMemory", "内存不足。建议使用分块处理。", False),
        ErrorPattern(r"PermissionError", "Permission", "权限不足。将自动添加执行权限。", True),
    ]
    
    def __init__(self, max_retries: int = 3, llm=None):
        self.max_retries = max_retries
        self.llm = llm
    
    def analyze_error(self, stderr: str, code: str) -> Tuple[str, str, bool]:
        for pattern in self.ERROR_PATTERNS:
            match = re.search(pattern.pattern, stderr)
            if match:
                suggestion = pattern.suggestion.format(*match.groups())
                return pattern.category, suggestion, pattern.auto_fix
        
        return "Unknown", self._llm_analyze_error(stderr, code), False
    
    def _llm_analyze_error(self, stderr: str, code: str) -> str:
        if not self.llm:
            return "未知错误。请检查错误信息。"
        
        prompt = f"""分析错误并给出修复建议(简洁):
代码: {code[:200]}
错误: {stderr[:300]}"""
        
        try:
            response = self.llm.invoke([HumanMessage(content=prompt)])
            return response.content[:200]
        except:
            return "LLM 分析失败"
    
    def generate_fix_code(self, category: str, error_detail: str, original_code: str) -> str:
        """根据错误类型生成自动修复代码"""
        if category == "FileNotFound":
            match = re.search(r"'(.+?)'", error_detail)
            if match:
                path = match.group(1)
                return f"""# Auto-fix: Create directory
import os
from pathlib import Path
Path("{path}").parent.mkdir(parents=True, exist_ok=True)

{original_code}"""
        elif category == "MissingModule":
            match = re.search(r"'(.+?)'", error_detail)
            if match:
                module = match.group(1)
                return f"#!BASH\npip install {module} -q\n\n# Retry original\n{original_code}"
        elif category == "Permission":
             return f"#!BASH\nchmod +x temp_script.sh\n\n{original_code}"
        
        return original_code
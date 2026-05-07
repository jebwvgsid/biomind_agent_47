import shutil
import glob
from enum import Enum
from dataclasses import dataclass
from typing import List, Dict, Any, Callable, Tuple

class ValidationLevel(Enum):
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"

@dataclass
class ValidationRule:
    name: str
    description: str
    level: ValidationLevel
    # check_function: (stdout, stderr, command_script) -> (passed, message)
    check_function: Callable[[str, str, str], Tuple[bool, str]]
    suggestion: str = ""

class ResultValidator:
    """自动化结果验证器"""
    
    def __init__(self):
        self.rules: List[ValidationRule] = []
        self._register_default_rules()
    
    def _register_default_rules(self):
        # 1. 通用生信报错检测 (Segmentation fault 等)
        def check_bio_errors(stdout: str, stderr: str, code: str) -> Tuple[bool, str]:
            combined_log = (stdout + stderr).lower()
            fatal_keywords = [
                "segmentation fault", "core dumped", 
                "command not found", "no such file or directory",
                "syntax error", "permission denied"
            ]
            for kw in fatal_keywords:
                if kw in combined_log:
                    return False, f"检测到致命错误关键词: '{kw}'"
            return True, ""

        self.register_rule(ValidationRule(
            name="Generic Error Check",
            description="扫描通用系统级错误",
            level=ValidationLevel.CRITICAL,
            check_function=check_bio_errors,
            suggestion="检查输入路径、文件权限或软件安装状态。"
        ))

        # 2. 内存溢出 (OOM) 检测
        def check_oom(stdout: str, stderr: str, code: str) -> Tuple[bool, str]:
            combined_log = (stdout + stderr).lower()
            oom_keywords = [
                "java.lang.outofmemoryerror", "std::bad_alloc",
                "killed", "insufficient memory", "out of memory"
            ]
            for kw in oom_keywords:
                if kw in combined_log:
                    return False, f"检测到内存溢出信号: '{kw}'"
            return True, ""

        self.register_rule(ValidationRule(
            name="OOM Detector",
            description="检测内存溢出",
            level=ValidationLevel.CRITICAL,
            check_function=check_oom,
            suggestion="尝试减少线程数 (-t)，或切换到大内存节点运行。"
        ))
        
        # 3. FastQC 特定检测
        def check_fastqc_output(stdout: str, stderr: str, code: str) -> Tuple[bool, str]:
            if "fastqc" not in code.lower():
                return True, ""
            html_files = glob.glob("**/*_fastqc.html", recursive=True)
            if not html_files:
                if "failed" in stderr.lower():
                    return False, f"FastQC 执行报错: {stderr[:100]}..."
                return False, "未找到生成的 HTML 报告"
            return True, f"✓ 已检测到 {len(html_files)} 个 FastQC 报告"
        
        self.register_rule(ValidationRule(
            name="Tool Specific: FastQC",
            description="验证 FastQC 产物",
            level=ValidationLevel.WARNING,
            check_function=check_fastqc_output,
            suggestion="确保 -o 输出目录存在。"
        ))
        
        # 4. 磁盘空间检查
        def check_disk_space(stdout: str, stderr: str, code: str) -> Tuple[bool, str]:
            if "no space left" in (stdout + stderr).lower():
                return False, "磁盘写入失败：空间不足"
            try:
                stat = shutil.disk_usage('.')
                free_gb = stat.free / (1024**3)
                if free_gb < 1:
                    return False, f"磁盘极度危急: 剩余 {free_gb:.2f} GB"
                elif free_gb < 10:
                    return True, f"⚠️ 磁盘警告: 剩余 {free_gb:.2f} GB"
            except:
                pass
            return True, ""
        
        self.register_rule(ValidationRule(
            name="Resource Monitor",
            description="系统资源检查",
            level=ValidationLevel.CRITICAL,
            check_function=check_disk_space,
            suggestion="清理中间文件或临时目录。"
        ))
    
    def register_rule(self, rule: ValidationRule):
        self.rules.append(rule)
    
    def validate(self, stdout: str, stderr: str, code: str, returncode: int) -> Dict[str, Any]:
        result = {
            'passed': returncode == 0,
            'critical_failures': [],
            'warnings': [],
            'info': [],
            'suggestions': []
        }
        
        if returncode != 0:
            result['critical_failures'].append(f"Process exited with error code: {returncode}")
        
        for rule in self.rules:
            try:
                passed, message = rule.check_function(stdout, stderr, code)
                if not passed:
                    if rule.level == ValidationLevel.CRITICAL:
                        result['passed'] = False
                        result['critical_failures'].append(f"[{rule.name}] {message}")
                        if rule.suggestion:
                            result['suggestions'].append(rule.suggestion)
                    elif rule.level == ValidationLevel.WARNING:
                        result['warnings'].append(f"[{rule.name}] {message}")
                elif message:
                    if rule.level == ValidationLevel.WARNING:
                        result['warnings'].append(message)
                    else:
                        result['info'].append(message)
            except Exception as e:
                result['warnings'].append(f"Validator '{rule.name}' crashed: {str(e)}")
        
        result['suggestions'] = list(set(result['suggestions']))
        return result
    
    def format_report(self, validation_result: Dict[str, Any]) -> str:
        if validation_result['passed'] and not validation_result['warnings']:
            return "✅ Execution Validated: No critical errors found."

        report = "\n📋 Automated Validation Report\n" + "─"*40 + "\n"
        
        if validation_result['passed']:
            report += "✅ Status: PASSED (with warnings)\n"
        else:
            report += "❌ Status: FAILED\n"
        
        if validation_result['critical_failures']:
            report += "\n🚨 Critical Failures:\n"
            for failure in validation_result['critical_failures']:
                report += f"  • {failure}\n"
        
        if validation_result['warnings']:
            report += "\n⚠️ Warnings:\n"
            for warning in validation_result['warnings']:
                report += f"  • {warning}\n"
        
        if validation_result['suggestions']:
            report += "\n💡 AI Suggestions:\n"
            for suggestion in validation_result['suggestions']:
                report += f"  -> {suggestion}\n"
        
        return report
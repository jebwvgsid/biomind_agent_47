import os
import re
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
                "syntax error", "permission denied",
                "biomind shim error",
                "there is no package called",
                "rscript: command not found",
                "calder not available",
                "calder2 not available",
            ]
            for kw in fatal_keywords:
                if kw in combined_log:
                    return False, f"检测到致命错误关键词: '{kw}'"
            if re.search(r"gene_density(?:\.bed)?[^\n]*(?:sum|总和)[^\n]*(?:0|为 0)", combined_log, re.IGNORECASE):
                return False, "检测到 gene_density 第4列总和为 0"
            return True, ""

        self.register_rule(ValidationRule(
            name="Generic Error Check",
            description="扫描通用系统级错误",
            level=ValidationLevel.CRITICAL,
            check_function=check_bio_errors,
            suggestion="检查输入路径、文件权限或软件安装状态。"
        ))

        # 1b. Failed availability checks hidden behind `|| echo ...`
        def check_swallowed_availability_failure(stdout: str, stderr: str, code: str) -> Tuple[bool, str]:
            lowered_code = str(code or "").lower()
            combined = (stdout + "\n" + stderr).lower()
            swallows_failure = "||" in lowered_code and "echo" in lowered_code
            hides_stderr = "2>/dev/null" in lowered_code or "2> /dev/null" in lowered_code
            if not (swallows_failure or hides_stderr):
                return True, ""
            failure_patterns = [
                r"\bnot available\b",
                r"\bnot found\b",
                r"\bfailed\b",
                r"\bcommand not found\b",
                r"\bthere is no package called\b",
            ]
            for pattern in failure_patterns:
                if re.search(pattern, combined, re.IGNORECASE):
                    return False, "依赖检查输出包含失败信号，但命令用 echo/重定向吞掉了真实失败退出码"
            return True, ""

        self.register_rule(ValidationRule(
            name="Swallowed Availability Failure Check",
            description="检测被 `|| echo` 或 stderr 重定向隐藏的依赖检查失败",
            level=ValidationLevel.CRITICAL,
            check_function=check_swallowed_availability_failure,
            suggestion="依赖检查不要用 `|| echo not available` 吞失败；让 `which`、`--version`、`library()` 原样返回非零退出码。"
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

        # 5. CALDER2 gene-density feature-track semantic checks
        def check_gene_density_semantics(stdout: str, stderr: str, code: str) -> Tuple[bool, str]:
            combined = stdout + "\n" + stderr
            lowered_code = code.lower()
            if "gene_density" not in lowered_code and "gene density" not in combined.lower():
                return True, ""

            if re.search(r"print\s+chr\s+.*start\s*-\s*1.*end.*len", code, re.IGNORECASE | re.DOTALL):
                return False, "gene_density 输出疑似为 gene 区间 + gene length，不是真正按分辨率分箱的 gene density"

            sum_match = re.search(r"Total gene count \(sum of column 4\):\s*\n?\s*([0-9.]+)", combined, re.IGNORECASE)
            nonzero_match = re.search(r"Number of bins with non-zero gene count:\s*\n?\s*([0-9.]+)", combined, re.IGNORECASE)
            if sum_match and float(sum_match.group(1)) <= 0:
                return False, "gene_density 第4列总和为 0，不能用于 A/B 方向判定"
            if nonzero_match and float(nonzero_match.group(1)) <= 0:
                return False, "gene_density 没有 non-zero bins，说明基因与 bins 没有有效重叠"

            path_match = re.search(r"(?P<path>\S*gene_density\.bed)", code)
            if path_match:
                path = path_match.group("path")
                if os.path.exists(path):
                    try:
                        total = 0.0
                        nonzero = 0
                        checked = 0
                        bad_rows = 0
                        with open(path, encoding="utf-8", errors="replace") as handle:
                            for raw in handle:
                                if not raw.strip() or raw.startswith("#"):
                                    continue
                                fields = raw.rstrip("\n").split()
                                if len(fields) < 4:
                                    bad_rows += 1
                                    continue
                                try:
                                    start = int(fields[1])
                                    end = int(fields[2])
                                    value = float(fields[3])
                                except ValueError:
                                    bad_rows += 1
                                    continue
                                if end <= start:
                                    bad_rows += 1
                                    continue
                                checked += 1
                                total += value
                                if value > 0:
                                    nonzero += 1
                        if checked and total <= 0:
                            return False, f"{path} 第4列总和为 0"
                        if checked and nonzero == 0:
                            return False, f"{path} 没有 non-zero bins"
                        if bad_rows:
                            return False, f"{path} 含有 {bad_rows} 行非法 BED/数值记录"
                    except OSError as exc:
                        return False, f"无法读取 gene_density 文件进行语义验证: {exc}"
            return True, ""

        self.register_rule(ValidationRule(
            name="CALDER2 Gene Density Semantic Check",
            description="验证 CALDER2 gene_density 不是空壳文件或 gene length 列表",
            level=ValidationLevel.CRITICAL,
            check_function=check_gene_density_semantics,
            suggestion="重新生成按分辨率分箱的 gene_density.bed，并验证第4列 sum>0 且有 non-zero bins。"
        ))

        # 6. CALDER2 official R runtime preflight
        def check_calder2_r_preflight(stdout: str, stderr: str, code: str) -> Tuple[bool, str]:
            lowered = code.lower()
            if "run_calder_compartments.py" not in lowered or "--calder-mode official-r" not in lowered:
                return True, ""
            has_rscript_check = "rscript --version" in lowered or "which rscript" in lowered
            has_calder_check = "library(calder)" in lowered or "require(calder)" in lowered
            if not (has_rscript_check and has_calder_check):
                return False, "official-r 模式运行前缺少 Rscript 与 CALDER R 包预检"
            return True, ""

        self.register_rule(ValidationRule(
            name="CALDER2 R Runtime Preflight",
            description="运行 official-r 前确认 Rscript 和 CALDER R 包可用",
            level=ValidationLevel.WARNING,
            check_function=check_calder2_r_preflight,
            suggestion="先运行 `conda run -n <env> which Rscript`, `Rscript --version`, `Rscript -e \"library(CALDER); packageVersion('CALDER')\"`。"
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

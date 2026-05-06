"""
智能人工确认系统 - 严格白名单版 + 富交互UI
"""

import re
import sys
import time
from typing import Dict, Any, List, Optional
from enum import Enum
from dataclasses import dataclass

#--- 兼容性处理：如果没有 colorama，定义伪类 ---
try:
    from colorama import init, Fore, Back, Style
    init(autoreset=True)
except ImportError:
    class MockStyle: RESET_ALL = ""
    class MockColor: CYAN = ""; GREEN = ""; RED = ""; YELLOW = ""; WHITE = ""; MAGENTA = ""
    class MockBack: RED = ""
    init = lambda autoreset=True: None
    Fore = MockColor(); Back = MockBack(); Style = MockStyle()

#================================
#1. 基础定义
#================================

class ConfirmationMode(Enum):
    ALWAYS = "always"           # 每步都确认
    SMART = "smart"             # 智能判断 (仅放行白名单)
    BATCH = "batch"             # 批量预览
    NEVER = "never"             # 从不确认

class RiskLevel(Enum):
    SAFE = "safe"               # 白名单内的只读操作 (绿色)
    LOW = "low"                 # (本模式下暂不使用，视同Medium)
    MEDIUM = "medium"           # 未知命令 / 修改操作 (黄色)
    HIGH = "high"               # 安装 / 删除 (红色)
    CRITICAL = "critical"       # 系统破坏 / 格式化 (红底白字)

@dataclass
class ConfirmationConfig:
    mode: ConfirmationMode = ConfirmationMode.SMART
    auto_approve_safe: bool = True          # 仅自动批准 SAFE 等级          
    require_reason_for_high_risk: bool = True
    timeout_seconds: int = 300
    show_code_preview: bool = True

#================================
#2. 风险分析器 (严格逻辑核心)
#================================

class RiskAnalyzer:
    """代码风险分析器 - 严格白名单模式"""

    # 黑名单：明确的危险操作
    RISK_PATTERNS = {
        RiskLevel.CRITICAL: [
            r'\bsudo\b', 
            r'\brm\s+-rf\b', 
            r'\bdd\b.*\bif=', # 更严格的 dd 匹配
            r'\bchmod\b.*777', 
            r'\bmkfs\b',      # Linux 格式化命令
            r'>\s*/dev/sd',   # 写入物理设备
        ],
        RiskLevel.HIGH: [
            r'\brm\b', 
            r'\bmv\b.*\*', 
            r'\bkill\b',
            # 拦截包管理操作
            r'\bconda\s+install\b', r'\bconda\s+create\b', r'\bconda\s+remove\b',
            r'\bpip\s+install\b', r'\bpip\s+uninstall\b',
            r'os\.remove', r'shutil\.rmtree',
        ],
        RiskLevel.MEDIUM: [
            r'\bmkdir\b', r'\btouch\b', r'\bcp\b', r'\bmv\b',
            r'\bgit clone\b', r'\bwget\b', r'\bcurl\b',
            r'\bsed\b.*-i', r'>', r'\.to_csv', r'open\(.*w',
        ]
    }

    # 白名单：明确的安全操作
    SAFE_WHITELIST = [
        # 基础文件
        r"^ls\b", r"^ll\b", r"^la\b", r"^dir\b", r"^pwd\b", r"^tree\b",
        # 系统信息
        r"^whoami\b", r"^date\b", r"^cal\b", r"^uptime\b", r"^free\b", r"^df\b", r"^du\b",
        # 文本查看
        r"^cat\b", r"^head\b", r"^tail\b", r"^more\b", r"^less\b", 
        r"^grep\b", r"^wc\b", r"^sort\b", r"^uniq\b",
        r"^echo\b", 
        # 软件检查
        r"^which\b", r"^type\b", 
        r"^conda list\b", r"^conda info\b", r"^pip list\b", r"^pip show\b",
        r".*--version\b", r".*-v\b", r".*--help\b",
        # 生信查看 (只读)
        r"^samtools view\b", r"^bcftools view\b", r"^seqkit stat\b", r"^bioawk"
    ]

    # [修复] analyze 方法现在正确地位于类内部
    @staticmethod
    def analyze(code: str, language: str = "unknown") -> Dict[str, Any]:
        """
        分析代码风险
        策略：默认 MEDIUM (未知)，只有白名单能降级为 SAFE
        """
        risky_operations = []
        safe_operations = []
        
        # 1. 默认视为未知风险 (MEDIUM)
        # 这确保了任何没见过的命令都会被拦截
        detected_level = RiskLevel.MEDIUM 
        risk_score = 50.0 

        # 2. 扫描显式风险 (CRITICAL / HIGH / MEDIUM)
        found_explicit_risk = False
        for level, patterns in RiskAnalyzer.RISK_PATTERNS.items():
            for pattern in patterns:
                matches = re.findall(pattern, code, re.IGNORECASE)
                if matches:
                    op_str = pattern.replace(r'\\b', '')
                    risky_operations.append(f"{level.value}: {op_str}")
                    
                    # 升级风险等级
                    current_val = list(RiskLevel).index(detected_level)
                    new_val = list(RiskLevel).index(level)
                    if new_val > current_val:
                        detected_level = level
                    
                    # 计分
                    score_map = {RiskLevel.CRITICAL: 100, RiskLevel.HIGH: 80, RiskLevel.MEDIUM: 60}
                    risk_score = max(risk_score, score_map.get(level, 50))
                    found_explicit_risk = True

        # 3. 白名单检查 (仅当没有检测到显式高危操作时生效)
        clean_code = code.strip()
        is_whitelisted = False
        
        # 检查是否匹配白名单
        for p in RiskAnalyzer.SAFE_WHITELIST:
            if re.search(p, clean_code):
                is_whitelisted = True
                break
        
        # 逻辑判定：
        # 只有 (匹配白名单) AND (没有显式风险如 >) AND (没有黑名单匹配) -> SAFE
        if is_whitelisted and not found_explicit_risk:
            # 二次检查：确保没有重定向符 > (防止 ls > file)
            if ">" not in clean_code and ">>" not in clean_code:
                detected_level = RiskLevel.SAFE
                risk_score = 0
                safe_operations.append("Whitelisted Command")
            else:
                risky_operations.append("Whitelisted but contains redirection (>)")
        elif not is_whitelisted and not found_explicit_risk:
            # 既不在白名单，也没命中黑名单 -> 也是 MEDIUM (Unknown)
            risky_operations.append("Unknown/Unlisted Command")

        # 生成建议
        recommendations = RiskAnalyzer._generate_recommendations(detected_level, risky_operations, language)
        
        return {
            'risk_level': detected_level,
            'risk_score': risk_score,
            'risky_operations': risky_operations,
            'safe_operations': safe_operations,
            'recommendations': recommendations
        }

    # [修复] _generate_recommendations 方法现在正确地位于类内部
    @staticmethod
    def _generate_recommendations(level: RiskLevel, operations: List[str], language: str) -> List[str]:
        recommendations = []
        if level == RiskLevel.CRITICAL:
            recommendations.extend(["⚠️ CRITICAL: 可能破坏系统", "建议: 务必备份"])
        elif level == RiskLevel.HIGH:
            recommendations.extend(["⚠️ HIGH: 包含删除或安装", "建议: 检查路径"])
        elif level == RiskLevel.MEDIUM:
            if any("Unknown" in op for op in operations):
                recommendations.append("ℹ️ 未知命令：系统无法确认安全性")
            else:
                recommendations.append("ℹ️ 涉及修改或非白名单操作")
        return recommendations

#================================
#3. 确认界面 UI (保留富特性)
#================================

class ConfirmationUI:
    @staticmethod
    def display_code_preview(code, language, step_index, total_steps, risk_analysis):
        print(f"\n{Fore.CYAN}{'='*80}")
        print(f"{Fore.CYAN}📋 CODE REVIEW - Step {step_index}")
        print(f"{Fore.CYAN}{'='*80}")

        risk_level = risk_analysis['risk_level']
        risk_color = ConfirmationUI._get_risk_color(risk_level)
        
        print(f"\n{Fore.WHITE}Language: {Fore.YELLOW}{language}")
        print(f"{Fore.WHITE}Risk Level: {risk_color}{risk_level.value.upper()}{Style.RESET_ALL}")
        print(f"{Fore.WHITE}Risk Score: {risk_color}{risk_analysis['risk_score']:.1f}/100{Style.RESET_ALL}")
        
        print(f"\n{Fore.GREEN}{'─'*80}")
        print(f"{Fore.GREEN}Code to Execute:")
        print(f"{Fore.GREEN}{'─'*80}")
        
        # 简单语法高亮
        highlighted = ConfirmationUI._highlight_code(code.strip(), language)
        print(highlighted)
        
        print(f"{Fore.GREEN}{'─'*80}")
        
        if risk_analysis['risky_operations']:
            print(f"\n{Fore.RED}⚠️  Risky / Unknown Operations:")
            for op in risk_analysis['risky_operations']:
                print(f"   {Fore.YELLOW}• {op}")
        
        if risk_analysis['safe_operations']:
            print(f"\n{Fore.GREEN}✓ Safe Operations:")
            for op in risk_analysis['safe_operations']:
                print(f"   {Fore.WHITE}• {op}")

        if risk_analysis['recommendations']:
            print(f"\n{Fore.CYAN}💡 Recommendations:")
            for rec in risk_analysis['recommendations']:
                print(f"   {Fore.WHITE}{rec}")

    @staticmethod
    def _get_risk_color(level: RiskLevel) -> str:
        colors = {
            RiskLevel.SAFE: Fore.GREEN,
            RiskLevel.MEDIUM: Fore.YELLOW,
            RiskLevel.HIGH: Fore.RED,
            RiskLevel.CRITICAL: Back.RED + Fore.WHITE
        }
        return colors.get(level, Fore.WHITE)

    @staticmethod
    def _highlight_code(code: str, language: str) -> str:
        """简单的语法高亮"""
        if language == "Bash":
            commands = ['echo', 'ls', 'cd', 'mkdir', 'rm', 'cp', 'mv', 'conda', 'pip']
            for cmd in commands:
                code = re.sub(rf'\b{cmd}\b', f'{Fore.CYAN}{cmd}{Fore.WHITE}', code)
        return Fore.WHITE + code

    @staticmethod
    def prompt_confirmation(risk_level: RiskLevel, timeout: int = 0) -> str: # 👈 注意这里改为返回 str
        prompt_color = Fore.CYAN
        if risk_level != RiskLevel.SAFE:
             print(f"\n{Fore.YELLOW}⚠️  This operation requires confirmation.{Style.RESET_ALL}")
             prompt_color = Fore.YELLOW

        # 🌟 升级交互提示
        print(f"\n{Fore.WHITE}Options:")
        print(f"  {Fore.GREEN}[y/Enter]{Fore.WHITE} - Execute")
        print(f"  {Fore.RED}[n]{Fore.WHITE}       - Reject (Skip)")
        print(f"  {Fore.MAGENTA}[q]{Fore.WHITE}       - Quit task")
        print(f"  {Fore.CYAN}[任意文字]{Fore.WHITE} - 拒绝执行，并将文字作为反馈发送给 AI (例如: '用 ll 看看')")

        try:
            # 提示符改成 Action >
            response = input(f"\n{prompt_color}Action > {Style.RESET_ALL}").strip()
            
            if response.lower() in ['y', 'yes', '']:
                return 'y'
            
            if response.lower() in ['q', 'quit', 'exit', 'stop']:
                print(f"{Fore.RED}🛑 User requested force quit.{Style.RESET_ALL}")
                raise KeyboardInterrupt  
                
            if response.lower() in ['n', 'no']:
                return 'n'
                
            # 🌟 核心魔法：如果用户输入了其他文字，直接把文字原样返回！
            return response
        except KeyboardInterrupt:
            raise

#================================
#4. 确认管理器 (主入口)
#================================

class ConfirmationManager:
    def __init__(self, config: ConfirmationConfig = None): 
        self.config = config or ConfirmationConfig()
        self.approval_history: List[Dict] = []

    def should_confirm(self, code: str, language: str, risk_analysis: Dict[str, Any]) -> bool:
        mode = self.config.mode
        risk_level = risk_analysis['risk_level']
        
        if mode == ConfirmationMode.NEVER: return False
        if mode == ConfirmationMode.ALWAYS: return True
        if mode == ConfirmationMode.SMART:
            # === 严格模式修改 ===
            # 只有 SAFE (白名单) 且 开启自动批准 时，才跳过确认
            # 其他所有情况全部拦截
            if self.config.auto_approve_safe and risk_level == RiskLevel.SAFE:
                return False
            return True
        return True

    def request_confirmation(self, code: str, language: str, step_index: int = 1, total_steps: int = 1, context: Dict[str, Any] = None) -> str: # 👈 返回类型改为 str
        # 1. 风险分析
        risk_analysis = RiskAnalyzer.analyze(code, language)
        
        # 2. 判断是否需要确认
        if not self.should_confirm(code, language, risk_analysis):
            print(f"{Fore.GREEN}✓ Auto-approved (Risk: Safe){Style.RESET_ALL}")
            self._record_decision(code, language, True, "auto", risk_analysis)
            return 'y' # 👈 自动批准返回 'y'
        
        # 3. 显示预览并请求确认
        if self.config.show_code_preview:
            ConfirmationUI.display_code_preview(code, language, step_index, total_steps, risk_analysis)
        
        # 🌟 接收用户输入的字符串
        user_response = ConfirmationUI.prompt_confirmation(risk_analysis['risk_level'], self.config.timeout_seconds)
        
        # 记录决策（只要不是 'y' 都算拒绝）
        is_approved = (user_response == 'y')
        self._record_decision(code, language, is_approved, "manual", risk_analysis)
        
        return user_response # 🌟 将字符串透传出去

    def _record_decision(self, code, language, approved, method, risk_analysis):
        """记录决策统计"""
        self.approval_history.append({
            'timestamp': time.time(),
            'method': method,
            'approved': approved,
            'risk_level': risk_analysis['risk_level'].value
        })

    def get_approval_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        total = len(self.approval_history)
        approved = sum(1 for h in self.approval_history if h['approved'])
        return {
            'total': total,
            'approved': approved,
            'rejected': total - approved,
            'approval_rate': (approved/total*100) if total > 0 else 0
        }

    # 兼容旧接口
    def _get_user_approval(self, code: str, reason: str, level: str = "critical") -> bool:
        print(f"Legacy confirmation request: {reason}")
        return self.request_confirmation(code, "Unknown")

if __name__ == "__main__":
    print("Testing RiskAnalyzer...")
    try:
        # 测试调用
        print(RiskAnalyzer.analyze("rm -rf /", "Bash"))
        print("✅ RiskAnalyzer.analyze works correctly.")
    except Exception as e:
        print(f"❌ Failed: {e}")
# execution/__init__.py

from .executor import (
    detect_script_language,
    normalize_script_code,
    run_bash_script,
    run_python_repl,
    run_r_code,
    run_command
)

from .validator import (
    ResultValidator,
    ValidationLevel,
    ValidationRule
)

from .monitoring import (
    ExecutionMonitor,
    LogCleaner,
    ErrorRecoverySystem,
    ErrorPattern
)

from .profiling import (
    ProfilingCallbackHandler,
    RunProfiler,
    benchmark_metadata_for_task,
    generic_metadata_for_prompt,
    get_current_node,
    get_current_profiler,
    get_current_task_id,
    timestamp_now,
)

# --- 新增 ---
from .confirmation import (
    ConfirmationManager,
    ConfirmationConfig,
    RiskAnalyzer,
    RiskLevel,
    ConfirmationMode
)

__all__ = [
    'detect_script_language',
    'normalize_script_code',
    'run_bash_script',
    'run_python_repl',
    'run_r_code',
    'run_command',
    'ResultValidator',
    'ValidationLevel',
    'ValidationRule',
    'ExecutionMonitor',
    'LogCleaner',
    'ErrorRecoverySystem',
    'ErrorPattern',
    'ProfilingCallbackHandler',
    'RunProfiler',
    'benchmark_metadata_for_task',
    'generic_metadata_for_prompt',
    'get_current_node',
    'get_current_profiler',
    'get_current_task_id',
    'timestamp_now',
    'ConfirmationManager',
    'ConfirmationConfig',
    'RiskAnalyzer',
    'RiskLevel',
    'ConfirmationMode'
]

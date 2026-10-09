"""Monotonic, display-only timing of observed runtime intervals.

No Plan reads, authority, scheduling or cross-process time reconstruction.
Phase buckets partition observed running time; step time overlaps those buckets.
"""
from collections import OrderedDict


PHASE_LABELS = {
    'triage': '路由', 'direct_chat': '对话', 'consultant': 'Consultant',
    'planner': 'Planner', 'plan_review': '计划审批', 'generate': 'Generate',
    'execute': '执行', 'verifier': '核验', 'supervisor': '核验',
    'commit_step': 'Commit', 'recovery': 'Recovery', 'finish': '收尾',
    'ask_user': '询问', 'code_review': '代码审批', 'step_worker': '步骤调度',
    'final_audit': '最终核验', 'supervisor_final_audit': '最终核验',
    'install_subagent': '环境处理', 'wait': '等待回复', 'other': '衔接',
}


def duration(seconds):
    value = max(0, int(seconds))
    hours, rest = divmod(value, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return f'{hours}小时{minutes:02d}分{seconds:02d}秒'
    if minutes:
        return f'{minutes}分{seconds:02d}秒'
    return f'{seconds}秒'


class TimingProjection:
    def __init__(self):
        self.task_id = ''
        self.running = False
        self.total = 0.0
        self.updated_at = 0.0
        self.phases = {}
        self.nodes = OrderedDict()
        self.waits = OrderedDict()
        self.seen = OrderedDict()
        self.step_id = ''
        self.step_seconds = 0.0
        self.step_completed = False
        self.step_scope = {}
        self.last_phase = 'other'

    def _phase(self):
        if self.waits:
            return 'wait'
        return next(reversed(self.nodes.values()), 'other')

    def _settle(self, now):
        if self.running:
            delta = max(0.0, now - self.updated_at)
            phase = self._phase()
            self.total += delta
            self.phases[phase] = self.phases.get(phase, 0.0) + delta
            if self.step_id and not self.step_completed:
                self.step_seconds += delta
            self.last_phase = phase
        self.updated_at = now

    def receive(self, data, now):
        task_id, event = str(data.get('task_id') or ''), data.get('event')
        if not task_id:
            return
        if event == 'run_start':
            if task_id != self.task_id:
                self.__init__()
                self.task_id = task_id
            if not self.running:
                self.running = True
                self.updated_at = now
            return
        # Only an explicit run start can select a different task or resume it.
        if task_id != self.task_id or not self.running:
            return
        if event == 'run_stop':
            self._settle(now)
            self.running = False
            self.nodes.clear()
            self.waits.clear()
        elif event == 'step_scope':
            step = str(data.get('step_id') or '')
            scope = {key: str(data[key]) for key in ('plan_id', 'plan_revision', 'plan_version')
                     if data.get(key) is not None and str(data[key]) != ''}
            changed = bool(scope and scope != self.step_scope)
            changed_step = step != self.step_id
            if changed_step or (self.step_completed and changed):
                self._settle(now)
                self.step_id = step
                self.step_seconds = 0.0
                self.step_completed = False
            if scope:
                self.step_scope = scope
            elif changed_step:
                self.step_scope = {}
        elif event in {'phase_start', 'phase_end', 'wait_start', 'wait_end'}:
            identity = str(data.get('interval_id') or '')
            if not identity:
                return
            is_wait = event.startswith('wait_')
            intervals = self.waits if is_wait else self.nodes
            key = ('wait' if is_wait else 'phase', identity)
            if event.endswith('_start'):
                if key in self.seen or len(intervals) >= 64:
                    return
                self._settle(now)
                self.seen[key] = True
                if len(self.seen) > 2048:
                    self.seen.popitem(last=False)
                node = str(data.get('node') or 'other')
                # Host node names are bounded labels, never model prose.
                intervals[identity] = node if node in PHASE_LABELS else 'other'
            elif identity in intervals:
                self._settle(now)
                del intervals[identity]

    def complete_step(self, task_id, step_id, now, scope=None):
        scope = scope or {}
        matches = all(str(scope.get(key, '')) == value for key, value in self.step_scope.items())
        if task_id == self.task_id and step_id == self.step_id and matches and not self.step_completed:
            self._settle(now)
            self.step_completed = True

    def snapshot(self, now):
        """Pure resident-memory projection: ticks do not write or add history."""
        delta = max(0.0, now - self.updated_at) if self.running else 0.0
        phase = self._phase() if self.running else self.last_phase
        phases = dict(self.phases)
        if self.running:
            phases[phase] = phases.get(phase, 0.0) + delta
        return {
            'task_id': self.task_id, 'running': self.running,
            'total_seconds': self.total + delta, 'phase': phase,
            'phase_seconds': phases.get(phase, 0.0), 'phases': phases,
            'step_id': self.step_id,
            'step_seconds': self.step_seconds + (delta if self.step_id and not self.step_completed else 0.0),
            'step_completed': self.step_completed,
        }

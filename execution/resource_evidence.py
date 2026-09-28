"""Best-effort evidence from the launched process, never an OOM classifier."""
from pathlib import Path
import time


class ProcessResourceEvidence:
    def __init__(self, pid, *, proc_root="/proc", environment=None):
        self.pid, self.proc = int(pid), Path(proc_root)
        self.processes, self.cgroups, self.errors = {}, {}, []
        self.samples, self.peak_sampled_rss_kib = 0, 0
        self.started = time.monotonic()
        self.job = {key: str(value) for key, value in (environment or {}).items()
                    if key in {"SLURM_JOB_ID", "SLURM_STEP_ID", "PBS_JOBID", "LSB_JOBID", "JOB_ID"}}
        self.launch_limits = self._read(self.proc / str(pid) / "limits")
        self.cgroup_membership = self._read(self.proc / str(pid) / "cgroup")
        self._discover_cgroups()
        self.sample()

    def _read(self, path):
        try:
            with path.open() as stream:
                return stream.read(16384)
        except OSError as exc:
            error = {"path": str(path), "error": type(exc).__name__}
            if error not in self.errors and len(self.errors) < 32:
                self.errors.append(error)
            return None

    @staticmethod
    def _unescape(path):
        for token, value in (("\\040", " "), ("\\011", "\t"), ("\\134", "\\")):
            path = path.replace(token, value)
        return path

    def _discover_cgroups(self):
        mounts = self._read(self.proc / str(self.pid) / "mountinfo") or ""
        for member in (self.cgroup_membership or "").splitlines():
            fields = member.split(":", 2)
            if len(fields) != 3:
                continue
            _, controllers, relative = fields
            for mount in mounts.splitlines():
                left, sep, right = mount.partition(" - ")
                lhs, rhs = left.split(), right.split()
                if not sep or len(lhs) < 5 or len(rhs) < 3:
                    continue
                version = 2 if rhs[0] == "cgroup2" and not controllers else 1
                if version == 1 and not (rhs[0] == "cgroup" and "memory" in controllers.split(",")
                                         and "memory" in rhs[2].split(",")):
                    continue
                root, mountpoint = Path(self._unescape(lhs[3])), Path(self._unescape(lhs[4]))
                try:
                    target = mountpoint / Path(relative).relative_to(root)
                except ValueError:
                    continue
                if ".." in target.parts:
                    self.errors.append({"path": relative, "error": "CGROUP_NAMESPACE_PATH_UNRESOLVED"})
                    continue
                # Parent limits can constrain a child whose own memory.max is max.
                for _ in range(8):
                    if target not in self.cgroups:
                        files = ("memory.max", "memory.high", "memory.current", "memory.peak", "memory.events", "memory.events.local") if version == 2 else (
                            "memory.limit_in_bytes", "memory.usage_in_bytes", "memory.max_usage_in_bytes", "memory.failcnt", "memory.oom_control")
                        self.cgroups[target] = {"version": version, "files": files,
                            "before": {name: self._read(target / name) for name in files}}
                    if target == mountpoint:
                        break
                    target = target.parent

    def sample(self):
        todo, seen, total = [self.pid], set(), 0
        while todo and len(seen) < 128:
            pid = todo.pop()
            if pid in seen:
                continue
            seen.add(pid)
            status = self._read(self.proc / str(pid) / "status")
            if status is None:
                continue
            row = self.processes.setdefault(str(pid), {"pid": pid, "max_observed_rss_kib": 0, "max_observed_hwm_kib": 0})
            if "limits" not in row:
                row["limits"] = self._read(self.proc / str(pid) / "limits")
                row["cgroup_membership"] = self._read(self.proc / str(pid) / "cgroup")
            values = dict(line.split(":", 1) for line in status.splitlines() if ":" in line)
            row["name"] = values.get("Name", "").strip()
            for field, key in (("VmRSS", "max_observed_rss_kib"), ("VmHWM", "max_observed_hwm_kib")):
                value = values.get(field, "0").split()[0]
                if value.isdecimal():
                    row[key] = max(row[key], int(value))
                    if field == "VmRSS":
                        total += int(value)
            children = self._read(self.proc / str(pid) / "task" / str(pid) / "children") or ""
            todo.extend(int(value) for value in children.split() if value.isdecimal())
        self.samples += 1
        self.peak_sampled_rss_kib = max(self.peak_sampled_rss_kib, total)

    def finish(self):
        groups = []
        for path, row in self.cgroups.items():
            after = {name: self._read(path / name) for name in row["files"]}
            deltas = {}
            for name in ("memory.events", "memory.events.local"):
                def counters(text):
                    return {parts[0]: int(parts[1]) for line in (text or "").splitlines()
                            if len(parts := line.split()) == 2 and parts[1].isdecimal()}
                before, end = counters(row["before"].get(name)), counters(after.get(name))
                if before and end:
                    deltas[name] = {key: end[key] - before[key] for key in before.keys() & end.keys()}
            groups.append({"path": str(path), "version": row["version"], "before": row["before"],
                           "after": after, "event_deltas": deltas})
        return {"schema_version": 1, "launcher_pid": self.pid, "job_identifiers": self.job,
                "launch_limits": self.launch_limits, "launch_cgroup_membership": self.cgroup_membership,
                "cgroups": groups, "sample_count": self.samples,
                "sampled_peak_process_tree_rss_kib": self.peak_sampled_rss_kib,
                "processes": list(self.processes.values()), "read_errors": self.errors,
                "root_cause_status": "UNCONFIRMED",
                "scope": "launcher and sampled descendants; RSS sampling can miss short peaks; cgroup counters can include other jobs; unreadable is unknown, not unlimited",
                "uncollected_evidence": ["kernel kill sender", "scheduler accounting/kill reason", "exact complete process-tree peak RSS"]}

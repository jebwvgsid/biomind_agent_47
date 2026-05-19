import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import agent_core.human_io as human_io_module
from agent_core.human_io import (
    HumanInputEOF,
    clear_waiting_state,
    decode_terminal_bytes,
    load_waiting_state,
    repair_terminal_text,
    resolve_input_timeout,
    safe_input,
    save_waiting_state,
    waiting_state_path,
)


class FakeReadline:
    def __init__(self, history=None):
        self.history = list(history or [])

    def set_completer(self, completer):
        self.completer = completer

    def parse_and_bind(self, binding):
        pass

    def get_current_history_length(self):
        return len(self.history)

    def get_history_item(self, index):
        return self.history[index - 1]

    def clear_history(self):
        self.history.clear()

    def add_history(self, item):
        self.history.append(item)


class HumanIOTests(unittest.TestCase):
    def test_save_load_and_clear_waiting_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = save_waiting_state(
                tmpdir,
                "task_1",
                stage="plan_review",
                prompt="Approve?",
                payload={"step": 1},
                reason="HumanInputTimeout",
                timeout_seconds=3,
                started_at="2026-05-14T10:00:00",
            )
            loaded = load_waiting_state(tmpdir, "task_1")
            raw = json.loads(path.read_text(encoding="utf-8"))

            clear_waiting_state(tmpdir, "task_1")

        self.assertEqual(path, waiting_state_path(tmpdir, "task_1"))
        self.assertEqual(loaded["status"], "waiting_user")
        self.assertEqual(loaded["stage"], "plan_review")
        self.assertEqual(raw["payload"]["step"], 1)
        self.assertFalse(Path(path).exists())

    def test_resolve_input_timeout_uses_environment(self):
        with patch.dict(os.environ, {"BIOMIND_INPUT_TIMEOUT_SECONDS": "12"}):
            self.assertEqual(resolve_input_timeout(default=900), 12)

        with patch.dict(os.environ, {"BIOMIND_INPUT_TIMEOUT_SECONDS": "bad"}):
            self.assertEqual(resolve_input_timeout(default=7), 7)

    def test_safe_input_maps_eoferror(self):
        with patch("builtins.input", side_effect=EOFError):
            with self.assertRaises(HumanInputEOF):
                safe_input("prompt> ", timeout_seconds=0)

    def test_safe_input_timeout_path_still_uses_builtin_input(self):
        with patch("builtins.input", return_value="abc") as mocked_input:
            self.assertEqual(safe_input("prompt> ", timeout_seconds=5), "abc")
        mocked_input.assert_called_once_with("prompt> ")

    def test_safe_input_can_hide_transient_readline_history(self):
        fake_readline = FakeReadline(["older command", "ll"])

        def fake_input(prompt):
            self.assertEqual(prompt, "Response> ")
            self.assertEqual(fake_readline.history, [])
            fake_readline.history.append("temporary answer")
            return "temporary answer"

        with patch.dict(sys.modules, {"readline": fake_readline}):
            with patch.object(human_io_module.os, "name", "posix"):
                with patch.object(human_io_module, "_READLINE_CONFIGURED", False):
                    with patch("builtins.input", side_effect=fake_input):
                        result = human_io_module.safe_input(
                            "Response> ",
                            timeout_seconds=0,
                            history=False,
                        )

        self.assertEqual(result, "temporary answer")
        self.assertEqual(fake_readline.history, ["older command", "ll"])

    def test_safe_input_keeps_history_for_main_prompt(self):
        fake_readline = FakeReadline(["older command"])

        def fake_input(prompt):
            self.assertEqual(fake_readline.history, ["older command"])
            fake_readline.history.append("new command")
            return "new command"

        with patch.dict(sys.modules, {"readline": fake_readline}):
            with patch.object(human_io_module.os, "name", "posix"):
                with patch.object(human_io_module, "_READLINE_CONFIGURED", False):
                    with patch("builtins.input", side_effect=fake_input):
                        result = human_io_module.safe_input(
                            "main> ",
                            timeout_seconds=0,
                        )

        self.assertEqual(result, "new command")
        self.assertEqual(fake_readline.history, ["older command", "new command"])

    def test_decode_terminal_bytes_accepts_utf8(self):
        self.assertEqual(decode_terminal_bytes("帮我 鉴定TAD\n".encode("utf-8")), "帮我 鉴定TAD")

    def test_decode_terminal_bytes_accepts_gb18030_fallback(self):
        self.assertEqual(decode_terminal_bytes("帮我 鉴定TAD\n".encode("gb18030")), "帮我 鉴定TAD")

    def test_repair_terminal_text_recovers_surrogateescaped_gb18030(self):
        raw = "帮我 鉴定TAD".encode("gb18030")
        surrogate_text = raw.decode("utf-8", errors="surrogateescape")

        self.assertEqual(repair_terminal_text(surrogate_text), "帮我 鉴定TAD")


if __name__ == "__main__":
    unittest.main()

"""The SDK patch tool against the shapes of a known SDK version.

The excerpts below are the unpatched lines around both targets in
agent-canvas 1.20.0 (OpenHands SDK 1.49.1; message.py md5 9962cc14...,
llm.py md5 18318b54...), read from the image on 2026-09-24. A new image whose
files no longer match is what the tool's exit 1 is for: update PATCHES and
these excerpts together.
"""
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

import sdk_patch_files as tool

MESSAGE_PY = '''        # Assistant function_call(s)
        if self.role == "assistant" and self.tool_calls:
            message_dict["tool_calls"] = [tc.to_chat_dict() for tc in self.tool_calls]
            self._remove_content_if_empty(message_dict)
        else:
            self._normalize_empty_assistant_content(message_dict)

        # Tool result (observation) threading
        if self.role == "tool" and self.tool_call_id is not None:
            assert self.name is not None, (
                "name is required when tool_call_id is not None"
            )
            message_dict["tool_call_id"] = self.tool_call_id
            message_dict["name"] = self.name

        # Required for model like kimi-k2-thinking
        if send_reasoning_content and self.reasoning_content:
            message_dict["reasoning_content"] = self.reasoning_content

        return message_dict
'''

LLM_PY = '''            messages,
            provider=self._infer_model_info_provider(),
            vision_enabled=vision_enabled,
        )
        return messages

    def _to_chat_dicts(self, messages: list[Message]) -> list[dict]:
        model_features = self._model_features()
        cache_enabled = self.is_caching_prompt_active()
        vision_enabled = self.vision_is_active()
        function_calling_enabled = self.native_tool_calling
'''


def message(role, tool_calls=(), tool_call_id=None):
    return types.SimpleNamespace(role=role, tool_calls=[types.SimpleNamespace(id=i) for i in tool_calls], tool_call_id=tool_call_id)


class PatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.dir = Path(self.tmp.name)
        (self.dir / "message.py").write_text(MESSAGE_PY, encoding="utf-8")
        (self.dir / "llm.py").write_text(LLM_PY, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_patches_the_known_shapes_once(self):
        logs = []
        self.assertEqual(tool.patch_dir(self.dir, log=logs.append), {"toolname": "patched", "threading": "patched"})
        message = (self.dir / "message.py").read_text(encoding="utf-8")
        self.assertIn('message_dict["tool_call_id"] = self.tool_call_id\n\n        # Required', message)  # the name line is gone
        self.assertNotIn('message_dict["name"]', message)
        self.assertEqual(message.count("tool_call_id"), MESSAGE_PY.count("tool_call_id"))  # nothing else touched
        llm = (self.dir / "llm.py").read_text(encoding="utf-8")
        self.assertIn("    def _to_chat_dicts(self, messages: list[Message]) -> list[dict]:\n"
                      "        messages = _oh_thread_tool_results(messages)  # oh-sdk-patch\n"
                      "        model_features = self._model_features()\n", llm)
        self.assertEqual(llm.count("def _oh_thread_tool_results"), 1); self.assertTrue(llm.startswith(LLM_PY.split("    def _to_chat_dicts")[0]))
        # a second run recognizes the patched shape and changes nothing
        before = {f: (self.dir / f).read_text(encoding="utf-8") for f in ("message.py", "llm.py")}
        self.assertEqual(tool.patch_dir(self.dir, log=logs.append), {"toolname": "already", "threading": "already"})
        self.assertEqual({f: (self.dir / f).read_text(encoding="utf-8") for f in before}, before)
        self.assertEqual([l.split("]")[0] + "]" + l.split("]")[1].split(":")[0] for l in logs],
                         ["[toolname] patched", "[threading] patched", "[toolname] already", "[threading] already"])

    def main(self, *argv):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return tool.main(list(argv))

    def test_check_mode_changes_nothing_and_exit_codes(self):
        self.assertEqual(self.main("--check", str(self.dir)), 2)  # recognized, unpatched
        self.assertEqual((self.dir / "message.py").read_text(encoding="utf-8"), MESSAGE_PY)
        self.assertEqual(self.main(str(self.dir)), 0)
        self.assertEqual(self.main("--check", str(self.dir)), 0)
        self.assertEqual(self.main(), 1)  # usage

    def test_unrecognized_shape_fails_and_leaves_the_file(self):
        changed = LLM_PY.replace("model_features = self._model_features()", "features = self._model_features()")
        (self.dir / "llm.py").write_text(changed, encoding="utf-8")
        logs = []
        self.assertEqual(tool.patch_dir(self.dir, log=logs.append), {"toolname": "patched", "threading": "unrecognized"})
        self.assertEqual((self.dir / "llm.py").read_text(encoding="utf-8"), changed)
        self.assertIn("[threading] UNRECOGNIZED", logs[-1])
        self.assertEqual(self.main(str(self.dir)), 1)

    def test_command_line(self):
        r = subprocess.run([sys.executable, str(Path(__file__).with_name("sdk_patch_files.py")), str(self.dir)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr); self.assertIn("[toolname] patched", r.stdout); self.assertIn("[threading] patched", r.stdout)
        r = subprocess.run([sys.executable, str(Path(__file__).with_name("sdk_patch_files.py")), "--check", str(self.dir)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr); self.assertIn("already", r.stdout)

    def test_threading_helper_orders_tool_results_after_their_calls(self):
        namespace = {}
        exec(tool.THREADING_HELPER, namespace)
        thread = namespace["_oh_thread_tool_results"]
        a = message("assistant", tool_calls=("c1", "c2"))
        r2, r1 = message("tool", tool_call_id="c2"), message("tool", tool_call_id="c1")
        user, stray, later = message("user"), message("tool", tool_call_id="unknown"), message("assistant")
        out = thread([user, a, stray, r2, later, r1])
        self.assertEqual(out, [user, a, r1, r2, stray, later])  # results in tool-call order, right after the call
        self.assertEqual(thread([user, later]), [user, later])  # nothing to thread: unchanged
        self.assertEqual(thread([a, r1]), [a, r1])  # a missing result is not invented


if __name__ == "__main__":
    unittest.main()

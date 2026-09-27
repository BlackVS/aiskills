#!/usr/bin/env python3
"""Apply the two tool-message patches to extracted copies of the OpenHands SDK's
openhands/sdk/llm/{message.py,llm.py}, which a site then mounts read-only over
the Canvas container's own copies (see UPDATING.md, "Upgrading the Canvas image").

Why: a strict OpenAI-compatible upstream rejects the non-spec `name` field on
role=tool messages, and any history where a tool result does not immediately
follow the assistant message carrying its tool call. Both come from the SDK's
chat serialization, so the fix has to live there:

  toolname   message.py: drop `message_dict["name"] = self.name` for tool results.
  threading  llm.py: `_to_chat_dicts` first reorders the messages so every tool
             result directly follows its tool call, in tool-call order
             (`_oh_thread_tool_results`, appended to the module).

Usage:
  sdk_patch_files.py <dir>          patch message.py and llm.py in <dir> in place
  sdk_patch_files.py --check <dir>  report only, change nothing

Exit 0: both files patched (or already patched); with --check, both already
patched. Exit 1: a shape was not recognized, the SDK changed - stop the upgrade
and adapt PATCHES. Exit 2 (--check only): a file is recognized but unpatched.

Known shapes: agent-canvas 1.17.0 (SDK 1.4x) and 1.20.0 (SDK 1.49.1); the
regression test carries the 1.49.1 excerpts.
"""
import os
import re
import sys

THREADING_HELPER = '''

def _oh_thread_tool_results(messages):
    """Local patch (oh-sdk-patch/threading): every role=tool message must
    immediately follow the assistant message carrying its tool_call, in
    tool_call order. Strict OpenAI-compatible upstreams reject anything else
    with HTTP 400. Messages that match nothing are passed through unchanged."""
    out = []
    used = set()
    n = len(messages)
    for i, m in enumerate(messages):
        if i in used:
            continue
        used.add(i)
        out.append(m)
        if m.role != "assistant" or not m.tool_calls:
            continue
        ids = [tc.id for tc in m.tool_calls]
        found = {}
        for j in range(i + 1, n):
            mj = messages[j]
            if (
                j not in used
                and mj.role == "tool"
                and mj.tool_call_id in ids
                and mj.tool_call_id not in found
            ):
                found[mj.tool_call_id] = j
        for tid in ids:
            j = found.get(tid)
            if j is not None:
                used.add(j)
                out.append(messages[j])
    return out
'''

PATCHES = [
    {
        "name": "toolname",
        "file": "message.py",
        "target": re.compile(
            r"^(?P<indent>[ \t]+)message_dict\[\"tool_call_id\"\] = self\.tool_call_id\n"
            r"(?P=indent)message_dict\[\"name\"\] = self\.name\n",
            re.MULTILINE,
        ),
        "replace": lambda m: m.group("indent")
        + 'message_dict["tool_call_id"] = self.tool_call_id\n',
        "already": re.compile(
            r"^[ \t]+message_dict\[\"tool_call_id\"\] = self\.tool_call_id\n"
            r"(?![ \t]+message_dict\[\"name\"\])",
            re.MULTILINE,
        ),
    },
    {
        "name": "threading",
        "file": "llm.py",
        "target": re.compile(
            r"^(?P<indent>[ \t]+)def _to_chat_dicts\(self, messages: list\[Message\]\) -> list\[dict\]:\n"
            r"(?P<body>[ \t]+)model_features = self\._model_features\(\)\n",
            re.MULTILINE,
        ),
        "replace": lambda m: (
            m.group("indent")
            + "def _to_chat_dicts(self, messages: list[Message]) -> list[dict]:\n"
            + m.group("body")
            + "messages = _oh_thread_tool_results(messages)  # oh-sdk-patch\n"
            + m.group("body")
            + "model_features = self._model_features()\n"
        ),
        "append": THREADING_HELPER,
        "already": re.compile(
            r"^\s+messages = _oh_thread_tool_results\(messages\)", re.MULTILINE
        ),
    },
]

ALREADY, PATCHED, UNRECOGNIZED = "already", "patched", "unrecognized"


def apply(patch, src):
    """(status, new source) for one patch on one file's text; the source is
    returned unchanged unless the status is PATCHED."""
    if patch["already"].search(src):
        return ALREADY, src
    if not patch["target"].search(src):
        return UNRECOGNIZED, src
    new = patch["target"].sub(patch["replace"], src, count=1)
    if patch.get("append") and "_oh_thread_tool_results" not in src:
        new = new.rstrip("\n") + "\n" + patch["append"]
    return PATCHED, new


def patch_dir(directory, check=False, log=print):
    """Apply (or with `check`, only classify) every patch to its file in
    `directory`. Returns {patch name: status}."""
    result = {}
    for p in PATCHES:
        path = os.path.join(directory, p["file"])
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        status, new = apply(p, src)
        if status == PATCHED and not check:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(new)
        elif status == PATCHED:
            status = "unpatched"
        log(f"[{p['name']}] {status.upper() if status == UNRECOGNIZED else status}: {path}")
        result[p["name"]] = status
    return result


def main(argv):
    check = "--check" in argv
    args = [a for a in argv if a != "--check"]
    if len(args) != 1:
        print(__doc__.split("Usage:")[1].split("Exit 0")[0].strip(), file=sys.stderr)
        return 1
    result = patch_dir(args[0], check=check)
    if UNRECOGNIZED in result.values():
        return 1
    if check and "unpatched" in result.values():
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

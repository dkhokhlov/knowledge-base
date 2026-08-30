#!/usr/bin/env python3
"""Argparse dispatch coverage for skills/claude/scripts/kb.py.

test_output_json.py calls each cmd_* directly (jget patched) and test_08_e2e.sh
exercises only the `memory` CLI surface via subprocess. NEITHER exercises the
top-level OWUI argparse dispatch (argv -> cmd_*). A typo in an OWUI subparser
name or in OWUI_DISPATCH would pass both gates. This module closes that gap:

  * the dispatch dicts map each verb to the correct cmd function (an `is` pin);
  * argv for every OWUI verb and every `memory` verb routes through kb.main()
    to a function (argparse -> dict -> call);
  * an unknown verb exits non-zero; --help lists every expected verb.

No stack required: base_url/api_key and the dispatched function are mocked.
Run:  python3 tests/test_kb_dispatch.py -v   (collected by `make test` / -m unit)
"""
import contextlib
import io
import os
import sys
import unittest
from unittest import mock

SCRIPTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                       "skills", "claude", "scripts")
sys.path.insert(0, os.path.abspath(SCRIPTS))
import kb  # noqa: E402

BASE = "http://testhost"
KEY = "testkey"

# verb -> the argv appended after `kb.py` (and after `memory` for facts).
OWUI_ARGV = {
    "whoami": [], "kbs": [], "kb": ["k1"], "search-kbs": ["q"],
    "retrieve": ["k1", "q"], "file": ["f1"], "index-projects": [],
    "retrieve-projects": ["q"], "status-projects": [],
}
MEM_ARGV = {
    "whoami": [], "groups": [], "add": ["text"], "retrieve": ["q"],
    "episodes": [], "status": [], "forget": ["user:a@b"],
    "delete-edge": ["uid"], "delete-episode": ["uid"],
}


def _fn_name(prefix, verb):
    return prefix + verb.replace("-", "_")


class DispatchMappingTests(unittest.TestCase):
    """The dispatch dicts were built at module load from the real cmd functions.
    Pin that each verb maps to the function with the matching name (guards a
    copy/paste mapping typo)."""

    def test_owui_dispatch_mapping(self):
        for verb in OWUI_ARGV:
            self.assertIs(kb.OWUI_DISPATCH[verb], getattr(kb, _fn_name("cmd_", verb)),
                          "OWUI_DISPATCH[%r] is not cmd_%s" % (verb, _fn_name("", verb)))

    def test_mem_dispatch_mapping(self):
        for verb in MEM_ARGV:
            self.assertIs(kb.MEM_DISPATCH[verb], getattr(kb, _fn_name("cmd_mem_", verb)),
                          "MEM_DISPATCH[%r] is not cmd_mem_%s" % (verb, _fn_name("", verb)))

    def test_owui_verbs_match_argparse(self):
        # Every verb the routing test uses must be a registered subcommand;
        # an unregistered name makes parse_args exit before dispatch.
        for verb in OWUI_ARGV:
            self.assertIn(verb, kb.OWUI_DISPATCH)

    def test_mem_verbs_match_argparse(self):
        for verb in MEM_ARGV:
            self.assertIn(verb, kb.MEM_DISPATCH)


class RoutingTests(unittest.TestCase):
    """Drive kb.main() with real argv per verb; assert it dispatches to a
    function. base_url/api_key are mocked; the dispatched function is replaced
    in the dispatch dict with a spy (the dict holds the original reference, so
    we patch.dict the entry rather than patch.object the function)."""

    def _run_main(self, argv, dispatch, verb):
        spy = mock.Mock(return_value=None)
        old_argv = sys.argv
        sys.argv = argv
        try:
            with mock.patch.dict(dispatch, {verb: spy}), \
                    mock.patch.object(kb, "base_url", return_value=BASE), \
                    mock.patch.object(kb, "api_key", return_value=KEY):
                kb.main()
        finally:
            sys.argv = old_argv
        return spy

    def test_owui_verbs_route(self):
        for verb, extra in OWUI_ARGV.items():
            spy = self._run_main(["kb.py", verb] + extra, kb.OWUI_DISPATCH, verb)
            self.assertEqual(spy.call_count, 1, "OWUI verb %r did not dispatch" % verb)
            args, _ = spy.call_args
            self.assertEqual(args[0], BASE, "verb %r: base_url not forwarded" % verb)
            self.assertEqual(args[1], KEY, "verb %r: api_key not forwarded" % verb)

    def test_mem_verbs_route(self):
        for verb, extra in MEM_ARGV.items():
            spy = self._run_main(["kb.py", "memory", verb] + extra, kb.MEM_DISPATCH, verb)
            self.assertEqual(spy.call_count, 1, "memory verb %r did not dispatch" % verb)
            args, _ = spy.call_args
            self.assertEqual(args[0], BASE, "memory verb %r: base_url not forwarded" % verb)
            self.assertEqual(args[1], KEY, "memory verb %r: api_key not forwarded" % verb)

    def test_retrieve_collision_resolves_to_owui(self):
        # `kb retrieve` (no `memory`) MUST hit the OWUI retrieve, not the facts
        # one. The spy is installed only in OWUI_DISPATCH; if routing leaked to
        # MEM_DISPATCH, the real cmd_mem_retrieve would run (and call jget).
        spy = self._run_main(["kb.py", "retrieve", "k1", "q"], kb.OWUI_DISPATCH, "retrieve")
        self.assertEqual(spy.call_count, 1)

    def test_memory_retrieve_routes_to_facts(self):
        spy = self._run_main(["kb.py", "memory", "retrieve", "q"], kb.MEM_DISPATCH, "retrieve")
        self.assertEqual(spy.call_count, 1)


class ArgparseEdgeTests(unittest.TestCase):
    def test_unknown_verb_exits_nonzero(self):
        old_argv = sys.argv
        sys.argv = ["kb.py", "no-such-verb"]
        try:
            with self.assertRaises(SystemExit) as cm:
                kb.main()
            self.assertNotEqual(cm.exception.code, 0)
        finally:
            sys.argv = old_argv

    def test_memory_without_subverb_exits_nonzero(self):
        old_argv = sys.argv
        sys.argv = ["kb.py", "memory"]
        try:
            with self.assertRaises(SystemExit) as cm:
                kb.main()
            self.assertNotEqual(cm.exception.code, 0)
        finally:
            sys.argv = old_argv

    def test_help_lists_every_verb(self):
        old_argv = sys.argv
        sys.argv = ["kb.py", "--help"]
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                with self.assertRaises(SystemExit) as cm:
                    kb.main()
            self.assertEqual(cm.exception.code, 0)  # --help exits 0
        finally:
            sys.argv = old_argv
        help_text = buf.getvalue()
        # top-level OWUI verbs + the memory group are all listed
        for verb in OWUI_ARGV:
            self.assertIn(verb, help_text, "--help omits OWUI verb %r" % verb)
        self.assertIn("memory", help_text)
        # memory subverbs appear in the memory subparser's own help, not the
        # top-level --help; assert the group itself is advertised.
        self.assertIn("Graphiti", help_text)


if __name__ == "__main__":
    unittest.main()
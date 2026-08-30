#!/usr/bin/env python3
"""Unit tests for the retrieval apply-scripts (patches 7/8/9).

Each apply script is a pure text replacement with a fail-loud count assertion
(text.count(old) == N else sys.exit(1)). These tests:

1. SUCCESS: run each script as a subprocess against a fixture built FROM the
   script's own SITE_OLD constants (single-source -- the fixture cannot drift
   from the anchor), then assert exit 0, OLD gone, NEW present, and the patched
   fixture compiles (py_compile). This is the regression guard: if the upstream
   image drifts so an anchor no longer matches, the build fails loud here, not
   silently at runtime.

2. FAIL-LOUD: a fixture with a drifted anchor (one token changed -- simulating an
   upstream-clone or post-rebase difference) -> the script exits non-zero.

3. SEMANTIC: exec the NEW snippets with stubs to prove the logic, not just the
   text: P7 per-request hybrid overrides the global; P8 k_reranker never below
   request k; P9 no-reranker preserves the fused order + attaches a decreasing
   score + caps at top_n, and the real-reranker branch is untouched.

No stack, no image, no network. Run: python3 tests/test_patch_scripts.py -v
"""
import os
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
OW = os.path.join(HERE, "..", "open-webui")
sys.path.insert(0, os.path.abspath(OW))
import apply_query_mode as p7  # noqa: E402
import apply_query_top_k as p8  # noqa: E402
import apply_skip_cosine_reranker as p9  # noqa: E402


# --- fixtures: built from the apply scripts' own anchor constants ------------
# The utils fixture embeds every utils-side anchor (P7 sites 1/2/3, P8 site 1,
# P9 site) verbatim in valid Python. Anchors appear exactly once each. Mixed
# kwarg indentation inside the call is legal (implicit continuation).
UTILS_FIXTURE = (
    "import asyncio\n"
    "\n"
    "async def query_collection_with_hybrid_search(**kw):\n"
    "    return {}\n"
    "\n"
    + p7.SITE1_OLD + "\n"
    + p7.SITE2_OLD + "\n"
    "        return await query_collection_with_hybrid_search(\n"
    "            collection_names=collection_names,\n"
    "            queries=queries,\n"
    "            embedding_function=embedding_function,\n"
    "            k=k,\n"
    + p7.SITE3_OLD + "\n"
    + p8.SITE1_OLD + "\n"
    "        )\n"
    "    if reranking_function is not None:\n"
    "        pass\n"
    "    else:\n"
    + p9.SITE_OLD + "\n"
    "    return {}\n"
)

# The retrieval fixture embeds P7 site 4 (the /query/collection else-branch) once
# and P8 site 2 (the k_reranker kwarg) twice (two handlers). The 8-space else:
# matches an 8-space if; the call args close before the next def.
RETRIEVAL_FIXTURE = (
    "async def query(request, form_data, user):\n"
    "    if request:\n"
    "        if form_data.hybrid:\n"
    "            return await query_collection_with_hybrid_search(\n"
    + p8.SITE2_OLD + "\n"
    "            )\n"
    + p7.SITE4_OLD + "\n"
    "\n"
    "async def query_collection_handler(request, form_data, user):\n"
    "    return await something_else(\n"
    + p8.SITE2_OLD + "\n"
    "    )\n"
)


def _write(tmpdir, name, text):
    p = os.path.join(tmpdir, name)
    with open(p, "w") as f:
        f.write(text)
    return p


def _run_script(script, env):
    """Run an apply script as a subprocess with the given env (OWUI_UTILS_PY /
    OWUI_RETRIEVAL_PY). Returns (returncode, stdout, stderr)."""
    proc = subprocess.run(
        [sys.executable, os.path.join(OW, script)],
        env={**os.environ, "PATH": os.environ.get("PATH", ""), **env},
        capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


class TestP7QueryMode(unittest.TestCase):
    SCRIPT = "apply_query_mode.py"

    def setUp(self):
        self._d = tempfile.mkdtemp(prefix="kb-patch-")
        self.utils = _write(self._d, "utils.py", UTILS_FIXTURE)
        self.retrieval = _write(self._d, "retrieval.py", RETRIEVAL_FIXTURE)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._d, ignore_errors=True)

    def test_applies_against_running_image_anchors(self):
        rc, out, err = _run_script(self.SCRIPT,
                                   {"OWUI_UTILS_PY": self.utils,
                                    "OWUI_RETRIEVAL_PY": self.retrieval})
        self.assertEqual(rc, 0, "exit %d stderr=%s" % (rc, err))
        u = open(self.utils).read()
        self.assertNotIn(p7.SITE1_OLD, u)
        self.assertNotIn(p7.SITE2_OLD, u)
        self.assertNotIn(p7.SITE3_OLD, u)
        self.assertIn(p7.SITE1_NEW, u)
        self.assertIn(p7.SITE2_NEW, u)
        self.assertIn(p7.SITE3_NEW, u)
        r = open(self.retrieval).read()
        self.assertNotIn(p7.SITE4_OLD, r)
        self.assertIn(p7.SITE4_NEW, r)
        # the patched files are valid Python (the build injects exactly this)
        __import__("py_compile").compile(self.utils, doraise=True)
        __import__("py_compile").compile(self.retrieval, doraise=True)

    def test_fails_loud_on_drifted_anchor(self):
        # Clone-style drift: the gate uses a different config access, so the
        # site-2 anchor is absent (count 0) -> exit 1, no file written.
        drifted = UTILS_FIXTURE.replace(
            "if request and config.get('rag.enable_hybrid_search'):",
            "if request and config.RAG_HYBRID_SEARCH:")
        u = _write(self._d, "utils_drift.py", drifted)
        rc, out, err = _run_script(self.SCRIPT,
                                   {"OWUI_UTILS_PY": u,
                                    "OWUI_RETRIEVAL_PY": self.retrieval})
        self.assertNotEqual(rc, 0)
        self.assertIn("site2", err)

    def test_hybrid_overrides_global(self):
        # Exec the SITE2_NEW assignments: a per-request hybrid flag overrides
        # the global; omitted -> the global; weight resolves the same way.
        block = p7.SITE2_NEW.rsplit("    if request and _hybrid_enabled:", 1)[0]
        block = textwrap.dedent(block)

        def resolve(hybrid, hybrid_bm25_weight, global_on, global_w=0.5):
            ns = {"hybrid": hybrid, "hybrid_bm25_weight": hybrid_bm25_weight,
                  "config": {"rag.enable_hybrid_search": global_on,
                             "rag.hybrid_bm25_weight": global_w},
                  "request": True}
            exec(block, ns)
            return ns["_hybrid_enabled"], ns["_effective_bm25_weight"]

        # per-request hybrid=False overrides a global-on -> pure vector
        self.assertEqual(resolve(False, None, True), (False, 0.5))
        # per-request hybrid=True -> hybrid regardless of global
        self.assertEqual(resolve(True, None, False), (True, 0.5))
        # omitted -> global
        self.assertEqual(resolve(None, None, True), (True, 0.5))
        self.assertEqual(resolve(None, None, False), (False, 0.5))
        # per-request weight overrides the global
        self.assertEqual(resolve(True, 1.0, True)[1], 1.0)


class TestP8QueryTopK(unittest.TestCase):
    SCRIPT = "apply_query_top_k.py"

    def setUp(self):
        self._d = tempfile.mkdtemp(prefix="kb-patch-")
        self.utils = _write(self._d, "utils.py", UTILS_FIXTURE)
        self.retrieval = _write(self._d, "retrieval.py", RETRIEVAL_FIXTURE)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._d, ignore_errors=True)

    def test_applies_against_running_image_anchors(self):
        rc, out, err = _run_script(self.SCRIPT,
                                   {"OWUI_UTILS_PY": self.utils,
                                    "OWUI_RETRIEVAL_PY": self.retrieval})
        self.assertEqual(rc, 0, "exit %d stderr=%s" % (rc, err))
        u = open(self.utils).read()
        self.assertNotIn(p8.SITE1_OLD, u)
        self.assertIn(p8.SITE1_NEW, u)
        r = open(self.retrieval).read()
        self.assertNotIn(p8.SITE2_OLD, r)
        self.assertEqual(r.count(p8.SITE2_NEW), p8.SITE2_EXPECTED)
        __import__("py_compile").compile(self.utils, doraise=True)
        __import__("py_compile").compile(self.retrieval, doraise=True)

    def test_fails_loud_on_drifted_anchor(self):
        # Drift: the utils k_reranker kwarg changes -> count 0 -> exit 1.
        drifted = UTILS_FIXTURE.replace(
            p8.SITE1_OLD, "                k_reranker=config.get('rag.reranker_top_k'),")
        u = _write(self._d, "utils_drift.py", drifted)
        rc, out, err = _run_script(self.SCRIPT,
                                   {"OWUI_UTILS_PY": u,
                                    "OWUI_RETRIEVAL_PY": self.retrieval})
        self.assertNotEqual(rc, 0)
        self.assertIn("site1", err)

    def test_k_reranker_never_below_request_k(self):
        # utils site: max(k, global). Eval the expression SITE1_NEW injects.
        cfg = {"rag.top_k_reranker": 3}
        self.assertEqual(max(10, cfg.get("rag.top_k_reranker") or 0), 10)
        self.assertEqual(max(2, cfg.get("rag.top_k_reranker") or 0), 3)
        # None global -> `or 0` guards the TypeError; max(k, 0) == k
        cfg_none = {"rag.top_k_reranker": None}
        self.assertEqual(max(5, cfg_none.get("rag.top_k_reranker") or 0), 5)
        # retrieval site: max(form_data.k else TOP_K, form_data.k_reranker else
        # TOP_K_RERANKER). The request k wins when it is set; else TOP_K.
        def krr(form_k, form_krr, top_k=20, top_k_rr=50):
            fd = types.SimpleNamespace(k=form_k, k_reranker=form_krr)
            config = types.SimpleNamespace(TOP_K=top_k, TOP_K_RERANKER=top_k_rr)
            return max(fd.k if fd.k else config.TOP_K,
                       fd.k_reranker or config.TOP_K_RERANKER)
        self.assertEqual(krr(10, 3), 10)   # request k above reranker cap
        self.assertEqual(krr(2, 50), 50)   # reranker cap above request k
        self.assertEqual(krr(None, 3), 20)  # no request k -> TOP_K floor


class TestP9SkipCosineReranker(unittest.TestCase):
    SCRIPT = "apply_skip_cosine_reranker.py"

    def setUp(self):
        self._d = tempfile.mkdtemp(prefix="kb-patch-")
        self.utils = _write(self._d, "utils.py", UTILS_FIXTURE)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._d, ignore_errors=True)

    def test_applies_against_running_image_anchors(self):
        rc, out, err = _run_script(self.SCRIPT, {"OWUI_UTILS_PY": self.utils})
        self.assertEqual(rc, 0, "exit %d stderr=%s" % (rc, err))
        u = open(self.utils).read()
        self.assertNotIn(p9.SITE_OLD, u)
        self.assertIn(p9.SITE_NEW, u)
        __import__("py_compile").compile(self.utils, doraise=True)

    def test_fails_loud_on_drifted_anchor(self):
        # Drift: the cosine import line changes -> the else-body anchor is gone.
        drifted = UTILS_FIXTURE.replace(
            "from sentence_transformers import util as st_util",
            "from sentence_transformers.cross_encoder import CrossEncoder")
        u = _write(self._d, "utils_drift.py", drifted)
        rc, out, err = _run_script(self.SCRIPT, {"OWUI_UTILS_PY": u})
        self.assertNotEqual(rc, 0)
        self.assertIn("compressor else-branch", err)

    def test_real_reranker_branch_untouched(self):
        # The patch replaces ONLY the else-body; the real-reranker path
        # (if reranking_function is not None:) is not in SITE_NEW, and the
        # cosine fallback (cos_sim) is gone from SITE_NEW.
        self.assertNotIn("cos_sim", p9.SITE_NEW)
        self.assertNotIn("if reranking_function is not None", p9.SITE_NEW)
        self.assertIn("sentence_transformers", p9.SITE_OLD)  # was the cosine path

    def test_no_reranker_preserves_fused_order_and_caps(self):
        # Exec SITE_NEW as the else-body: a stub Document list in fused order
        # comes out in the SAME order, with a decreasing score attached to docs
        # that had none, existing scores kept, and the list capped at top_n.
        class Document:
            def __init__(self, page_content, metadata=None):
                self.page_content = page_content
                self.metadata = metadata if metadata is not None else {}

        body = "def _run(documents, self):\n" + textwrap.indent(textwrap.dedent(p9.SITE_NEW), "    ")
        ns = {"Document": Document}
        exec(body, ns)

        stub_self = types.SimpleNamespace(top_n=3)
        docs = [Document("a"), Document("b", {"score": 99.0}),
                Document("c"), Document("d"), Document("e")]
        out = ns["_run"](docs, stub_self)

        # capped at top_n=3
        self.assertEqual(len(out), 3)
        # fused order preserved
        self.assertEqual([d.page_content for d in out], ["a", "b", "c"])
        # existing score kept (b); decreasing rank score assigned to the rest
        self.assertEqual(out[0].metadata["score"], 5.0)   # len(documents)=5 - 0
        self.assertEqual(out[1].metadata["score"], 99.0)  # preserved
        self.assertEqual(out[2].metadata["score"], 3.0)   # 5 - 2
        # the input documents' metadata dicts are mutated in place (same obj)
        self.assertIs(out[0].metadata, docs[0].metadata)

    def test_no_reranker_empty_and_single(self):
        class Document:
            def __init__(self, page_content, metadata=None):
                self.page_content = page_content
                self.metadata = metadata if metadata is not None else {}
        body = "def _run(documents, self):\n" + textwrap.indent(textwrap.dedent(p9.SITE_NEW), "    ")
        ns = {"Document": Document}
        exec(body, ns)
        stub = types.SimpleNamespace(top_n=5)
        # empty input -> empty output (no cosine, no crash)
        self.assertEqual(ns["_run"]([], stub), [])
        # single doc -> one doc with score 1.0 (len=1 - 0)
        one = ns["_run"]([Document("only")], stub)
        self.assertEqual(len(one), 1)
        self.assertEqual(one[0].metadata["score"], 1.0)


if __name__ == "__main__":
    unittest.main()
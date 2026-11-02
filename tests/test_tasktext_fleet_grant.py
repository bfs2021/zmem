"""Issue-tracer bundle 329-340 supplementary pin (implementation-review
finding F3): the REMOTE half of the #339 consume grant — the fleet broker's
read path threads consume_tasktext ONLY for the SubagentStart moment, and
the remote CLI lane forwards the caller's grant verbatim.

Not a frozen acceptance check (added post-freeze at the implementation
reviewer's direction); unittest + __main__ runner, CI-shape. Env pinned at
module top before any storelib/fleet import (store-path freeze + the #244
isolation guard).
"""
import _store_guard  # noqa: F401  (issue #244 isolation guard)

import os
import sys
import tempfile
import unittest
from pathlib import Path

_SCRATCH = tempfile.mkdtemp(prefix="zmem-fleet-grant-")
os.environ["ZMEM_STORE"] = os.path.join(_SCRATCH, "store.sqlite")
os.environ["ZMEM_DATA"] = os.path.join(_SCRATCH, "data")
os.environ["ZMEM_MODELS_DIR"] = os.path.join(_SCRATCH, "models")
os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(REPO_ROOT / "skills" / "memory" / "scripts"),
           str(REPO_ROOT / "skills" / "memory" / "scripts" / "storelib")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


class FleetGrantThreadingTest(unittest.TestCase):
    """The broker grants the consume ONLY on the SubagentStart moment."""

    def test_subagent_start_granted_and_read_moment_not(self):
        from fleet import broker, transport

        recorded = {}

        class _RecordingHttp:
            def prefetch(self, query, **kwargs):
                recorded.update(kwargs)
                recorded["_query"] = query
                return transport._empty_envelope()

        payload = {"prompt": "", "session_id": "s-grant",
                   "cwd": str(REPO_ROOT)}
        # SubagentStart: the delegation bridge lane -> granted.
        broker._handle_read(_RecordingHttp(), os.environ,
                            "SubagentStart", payload, 0.0)
        self.assertTrue(recorded.get("consume_tasktext") is True,
                        "SubagentStart prefetch must carry the consume grant; "
                        "got %r (a grant-never mutant on the remote bridge "
                        "survives without this pin)" % recorded)
        self.assertEqual(recorded.get("moment"), "subagent", recorded)
        # A read moment: NOT granted (the broker passes the boolean; the
        # transport omits the hub argument entirely when False — pinned in
        # the transport leg below).
        recorded.clear()
        broker._handle_read(_RecordingHttp(), os.environ,
                            "UserPromptSubmit",
                            {"prompt": "unrelated", "session_id": "s-grant",
                             "cwd": str(REPO_ROOT)}, 0.0)
        self.assertTrue(recorded.get("consume_tasktext") is False,
                        "a read-moment prefetch must carry the grant as "
                        "False; got %r (a grant-always mutant re-opens the "
                        "#339 drain via the fleet path)" % recorded)
        self.assertEqual(recorded.get("moment"), "user_prompt", recorded)

    def test_transport_arguments_omit_grant_when_false(self):
        """The hub-side wire: consume_tasktext appears in the prefetch tool
        arguments ONLY when True (an ungranted remote call never even names
        the flag to the hub)."""
        from fleet import transport

        seen = {}

        class _StubHttp(transport.McpHttp):
            def _envelope_call(self, name, arguments):
                seen["name"] = name
                seen["arguments"] = dict(arguments)
                return transport._empty_envelope()

        http = _StubHttp(url="http://127.0.0.1:1",
                         executor=transport.DeadlineExecutor(), token="t")
        http.prefetch("", namespace="project:x", session_id="s",
                      moment="subagent", ops_tokens=[])
        self.assertNotIn("consume_tasktext", seen["arguments"],
                         seen["arguments"])
        http.prefetch("", namespace="project:x", session_id="s",
                      moment="subagent", ops_tokens=[],
                      consume_tasktext=True)
        self.assertIs(seen["arguments"].get("consume_tasktext"), True,
                      seen["arguments"])

    def test_remote_gate_forwards_caller_grant_verbatim(self):
        """The remote CLI lane is a transport, not a grant authority: whatever
        the caller passed is forwarded unchanged (True AND False), pinned at
        the extracted _forward_prefetch seam (review R2-1: the earlier leg
        called a nonexistent symbol and passed vacuously — this one must
        FAIL when the forward is wrong or missing)."""
        from storelib import remote_gate

        for granted in (True, False):
            recorded = {}

            class _RecordingHttp:
                def prefetch(self, query, **kwargs):
                    recorded.update(kwargs)
                    recorded["_query"] = query
                    return {"ok": True, "results": [], "rendered": ""}

            args = type("A", (), {})()
            args.query = ""
            args.namespace = "project:x"
            args.session_id = "s-rg"
            args.moment = "subagent"
            args.ops_tokens = []
            args.lane = "zcode"
            args.reserve = False
            args.consume_tasktext = granted
            result = remote_gate._forward_prefetch(args, _RecordingHttp(), 5000)
            self.assertIs(result.get("ok"), True)
            self.assertIn("consume_tasktext", recorded,
                          "the forward must always carry the grant key "
                          "(granted=%r); got %r" % (granted, recorded))
            self.assertIs(recorded["consume_tasktext"], granted,
                          "the forward must be verbatim; got %r" % recorded)
            self.assertEqual(recorded.get("moment"), "subagent")


if __name__ == "__main__":
    unittest.main(verbosity=2)

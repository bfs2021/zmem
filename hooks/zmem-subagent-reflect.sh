#!/usr/bin/env bash
# zmem-subagent-reflect.sh — SubagentStop hook: reflect on a delegated agent's
# failures (shared, both hosts). Closes the gap where a dispatched subagent's
# failed tool calls currently evaporate — the parent session never sees them.
#
# On SubagentStop, detects failed tool calls in the SUBAGENT's OWN transcript via
# the unified `store.py failures` command and, if failures are found AND no
# lesson was captured for this subagent, writes a PARENT-SIDE hand-off sidecar
# under <ZMEM_DATA>/subagent-reflections/ for the parent's own Stop hook
# (zmem-reflect.sh) to surface. It NEVER emits additionalContext.
#
# WHY no prompt (issue #204): Claude Code honors additionalContext on
# SubagentStop by CONTINUING the conversation — the subagent turn re-runs and
# the subagent's reply to the nudge ("Memory captured", "blocked by sandbox
# guard, skipping", …) becomes its LAST assistant message, which is the ONLY
# text the dispatching orchestrator receives as the subagent's <result>. The
# actual deliverable is silently lost. The same holds wherever a Stop hook
# fires inside a subagent context (Claude Code converts those to
# SubagentStop). So this hook is prompt-free on every path: the reflection
# opportunity moves to the parent, where a post-hoc nudge cannot clobber a
# deliverable.
#
# WHY agent_transcript_path: on SubagentStop the top-level transcript_path is the
# PARENT session's transcript, where the subagent appears as one opaque Task
# result — its internal failed tool calls are NOT there. The subagent's own tool
# calls live in agent_transcript_path (…/subagents/agent-<id>.jsonl). Failure
# detection must scan THAT (ZMEM_AGENT_TRANSCRIPT) — confirmed empirically,
# CC 2.1.218 (Phase 7 discovery). If ZMEM_AGENT_TRANSCRIPT is absent (older build
# / no subagent transcript), no-op — never fall back to the parent transcript
# (it would mis-detect parent failures as the subagent's).
#
# LOOP GUARD: like Stop, additionalContext on SubagentStop makes CC re-run the
# subagent turn, firing SubagentStop again with stop_hook_active=true (confirmed
# empirically, CC 2.1.218). This hook NO-OPs whenever stop_hook_active is set
# (and never injects at all anymore — the guard is retained so a re-fire also
# skips the sidecar write, keeping one hand-off per agent).
#
# LESSON DEDUP PER-SUBAGENT: every subagent in one dispatch shares the parent
# session_id, so a session-keyed "lesson exists" check would let the first
# subagent's capture suppress reflection for every sibling that failed
# differently. Dedup keys on session:<id>:agent:<agent_id> instead.
#
# Envelope: ALWAYS the bare {} wrapped in the <<<ZMEM_JSON>>>…<<<END>>>
# sentinel (fail-open no-op for the host adapter); the hand-off sidecar is the
# only output surface. The parent's Stop hook consumes the sidecars, prunes
# ones older than 14 days, and renders the reflection prompt in the PARENT
# context.
#
# NON-BLOCKING / FAIL-OPEN: always exits 0; any error degrades to no sidecar
# and the empty envelope.
#
# Canonical env (from zmem-launch.js): ZMEM_SESSION, ZMEM_AGENT_ID,
# ZMEM_AGENT_TRANSCRIPT, ZMEM_AGENT_TYPE, ZMEM_DATA, ZMEM_ROOT, ZMEM_NAMESPACE.

set -u

# Capture policy is checked before stdin parsing and all state access.
emit_empty() {
  printf '<<<ZMEM_JSON>>>%s<<<END>>>\n' '{}'
  exit 0
}
CAPTURE_VALUE=1
if CAPTURE_VALUE="$(printenv ZMEM_CAPTURE 2>/dev/null)"; then :; else CAPTURE_VALUE=1; fi
CAPTURE_VALUE="$(printf '%s' "$CAPTURE_VALUE" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
if [ "$CAPTURE_VALUE" = "0" ]; then emit_empty; fi

# Read the full hook payload (needed for the stop_hook_active loop guard).
INPUT="$(cat)"

# Kill switch parity with zmem-reflect.sh (#194, extended by #204): ZMEM_REFLECT=0
# (exactly "0") disables this hook entirely — no sidecar, empty envelope. Any
# other value (unset, empty, 1, yes, ...) keeps the hook enabled.
if [ "${ZMEM_REFLECT:-1}" = "0" ]; then
  printf '<<<ZMEM_JSON>>>%s<<<END>>>\n' '{}'
  exit 0
fi

# --- Cross-platform setup ---
IS_WINDOWS=0
if [[ "$(uname -s 2>/dev/null)" == MINGW* ]] || [[ "$(uname -s 2>/dev/null)" == CYGWIN* ]] || [[ "$(uname -s 2>/dev/null)" == MSYS* ]]; then
  IS_WINDOWS=1
fi

PYTHON_BIN=""
if [ "$IS_WINDOWS" -eq 1 ]; then
  if python --version >/dev/null 2>&1; then
    PYTHON_BIN="python"
  elif python3 --version >/dev/null 2>&1; then
    PYTHON_BIN="python3"
  fi
else
  if python3 --version >/dev/null 2>&1; then
    PYTHON_BIN="python3"
  elif python --version >/dev/null 2>&1; then
    PYTHON_BIN="python"
  fi
fi

if [ -z "$PYTHON_BIN" ]; then
  printf '<<<ZMEM_JSON>>>%s<<<END>>>\n' '{}'
  exit 0
fi

to_py_path() {
  if [ "$IS_WINDOWS" -eq 0 ]; then
    printf '%s' "$1"
    return
  fi
  if command -v cygpath >/dev/null 2>&1; then
    cygpath -w "$1"
  else
    local p="$1"
    if [[ "$p" =~ ^/([a-zA-Z])/(.*)$ ]]; then
      local drive="${BASH_REMATCH[1]}"
      local rest="${BASH_REMATCH[2]}"
      printf '%s:\\%s' "$drive" "${rest//\//\\}"
    else
      printf '%s' "$p"
    fi
  fi
}

join_path() {
  local base="$1"; shift
  local sep
  if [ "$IS_WINDOWS" -eq 1 ]; then
    sep='\'
  else
    sep='/'
  fi
  printf '%s' "$base"
  for part in "$@"; do
    printf '%s%s' "$sep" "$part"
  done
}

# Canonical env (from the launcher) with legacy fallbacks.
SESSION_ID="${ZMEM_SESSION:-${CLAUDE_SESSION_ID:-}}"
AGENT_ID="${ZMEM_AGENT_ID:-}"
AGENT_TYPE="${ZMEM_AGENT_TYPE:-}"
AGENT_TRANSCRIPT="${ZMEM_AGENT_TRANSCRIPT:-}"
PROJECT="${ZMEM_PROJECT:-${ZCODE_PROJECT_DIR:-${CLAUDE_PROJECT_DIR:-}}}"
DATA_DIR="${ZMEM_DATA:-${ZCODE_PLUGIN_DATA:-}}"
PLUGIN_ROOT="${ZMEM_ROOT:-${ZCODE_PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT:-}}}"

# A session id is required for lesson-dedup; without it, no-op.
if [ -z "$SESSION_ID" ]; then
  printf '<<<ZMEM_JSON>>>%s<<<END>>>\n' '{}'
  exit 0
fi

# No subagent transcript → cannot detect the subagent's failures. Do NOT fall
# back to the parent transcript. No-op.
if [ -z "$AGENT_TRANSCRIPT" ]; then
  printf '<<<ZMEM_JSON>>>%s<<<END>>>\n' '{}'
  exit 0
fi

# Resolve data dir (for the store.sqlite lesson-exists check).
if [ -n "$DATA_DIR" ]; then
  DATA_DIR_PY="$(to_py_path "$DATA_DIR")"
else
  DATA_DIR_PY="$(join_path "$(to_py_path "$HOME")" .zmem)"
fi

# Resolve store.py.
if [ -n "$PLUGIN_ROOT" ]; then
  STORE_PY_PY="$(join_path "$(to_py_path "$PLUGIN_ROOT")" skills memory scripts store.py)"
else
  SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
  STORE_PY_PY="$(join_path "$(to_py_path "$SCRIPT_DIR/..")" skills memory scripts store.py)"
fi

# Canonical namespace (single derived key) with legacy basename fallback.
NS="${ZMEM_NAMESPACE:-}"
if [ -z "$NS" ]; then
  if [ -n "$PROJECT" ]; then
    NS="project:$(basename "$PROJECT")"
  else
    NS="user:global"
  fi
fi

# Subagent transcript path for python (convert if it looks like a Cygwin path; a
# CC agent_transcript_path is already a Windows path and passes through).
AGENT_TRANSCRIPT_PY="$(to_py_path "$AGENT_TRANSCRIPT")"

# Per-subagent lesson-dedup key: session + agent so sibling subagents that fail
# differently each get their own reflection.
if [ -n "$AGENT_ID" ]; then
  SOURCE_REF="session:${SESSION_ID}:agent:${AGENT_ID}"
else
  SOURCE_REF="session:${SESSION_ID}"
fi

# Build the reflection payload.
CTX_JSON="$(printf '%s' "$INPUT" | "$PYTHON_BIN" -c '
import json, os, shlex, sys, subprocess

raw_stdin = sys.stdin.read() if not sys.stdin.isatty() else ""
store_py = sys.argv[1]
ns = sys.argv[2]
data_dir = sys.argv[3]
agent_transcript = sys.argv[4]
source_ref = sys.argv[5]
agent_type = sys.argv[6]
session_id = sys.argv[7]
agent_id = sys.argv[8]

# Rejection rendering lives once in corrections.py and is shared by both
# reflect hooks so they stay in lockstep (drift guard). Fail open: if the
# import ever fails, rejections are silently dropped (the usual no-injection
# degradation) rather than crashing the hook.
_render_rejs = None
try:
    _scripts_dir = os.path.dirname(store_py)
    sys.path.insert(0, _scripts_dir)
    from corrections import render_rejection_section as _render_rejs
except Exception:
    _render_rejs = None

def emit(obj):
    print(json.dumps(obj) if obj else "{}")
    sys.exit(0)

# 1. Loop guard: SubagentStop re-fires with stop_hook_active=true after an
#    injection re-loops the subagent turn. Never inject again.
try:
    payload = json.loads(raw_stdin) if raw_stdin.strip() else {}
except Exception:
    payload = {}
if not isinstance(payload, dict):
    # A non-object payload (list/string/number) has no hook fields; treat as
    # empty (PR review PRR-009 — keeps .get() accesses safe).
    payload = {}
if payload.get("stop_hook_active"):
    emit({})

# 2. Unified failure detection on the SUBAGENT own transcript (fail-open).
count = 0
details = []
rejections = []
try:
    argv = [sys.executable, store_py, "failures", "--transcript", agent_transcript]
    out = subprocess.check_output(argv, stderr=subprocess.DEVNULL, timeout=10).decode("utf-8", "replace")
    obj = json.loads(out) if out.strip() else {}
    count = int(obj.get("count", 0) or 0)
    details = obj.get("details", []) or []
    rejections = obj.get("rejections", []) or []
except Exception:
    count, details, rejections = 0, [], []

# Build the user-rejection section via the shared render_rejection_section
# helper (empty when none). Reasons are newline-free + truncated + capped by
# the helper (fence-integrity + context budget), so fenced reason lines cannot
# break out. Same single source of truth as zmem-reflect.sh.
rej_msg = _render_rejs(rejections) if _render_rejs else ""

# 2b. Trivial-miss filter (issue #439): a subagent whose EVERY failure is a
#     read-only search miss (grep or Glob error, Read of a nonexistent path,
#     reader-only Bash pipeline) must not cost the parent a reflection
#     turn. The verdict comes from the annotated failures payload
#     (capture_quality.classify_failure - the one classifier shared by the
#     transcript and db paths); a missing verdict is re-derived locally,
#     fail-open to "other" (the existing sidecar behavior).
import hashlib  # noqa: E402  (issue #439 counter lock ring key)
import tempfile  # noqa: E402
import time  # noqa: E402
import uuid  # noqa: E402
_classify = None
_process_alive = None
try:
    _scripts_dir2 = os.path.dirname(store_py)
    if _scripts_dir2 not in sys.path:
        sys.path.insert(0, _scripts_dir2)
    from capture_quality import classify_failure as _classify
    from host import process_alive as _process_alive
except Exception:
    _classify = None
    _process_alive = None

def _verdict(d):
    v = d.get("verdict") if isinstance(d, dict) else None
    if v in ("trivial", "signal", "other"):
        return v
    if _classify is not None:
        try:
            out = _classify(d)
            if out in ("trivial", "signal", "other"):
                return out
        except Exception:
            pass
    return "other"

trivial_count = 0
for d in details:
    if isinstance(d, dict) and _verdict(d) == "trivial":
        trivial_count += 1

if count > 0 and trivial_count == count and not rej_msg:
    # Every failure is a trivial read-only miss: count what was filtered
    # into the per-session counter and do NOT write the hand-off sidecar.
    # The read-modify-write is serialized by an O_EXCL lockfile with the
    # delivery-ledger stale-grace semantics: reclamation requires BOTH
    # the age threshold AND a provably dead owner (recorded pid no longer
    # alive, owner token re-read immediately before the unlink), so a
    # live-but-slow writer is never broken into.
    try:
        ring_dir = os.path.join(data_dir, "subagent-reflections")
        os.makedirs(ring_dir, exist_ok=True)
        key = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
        counter_path = os.path.join(ring_dir, key + ".filtered.json")
        lock_path = os.path.join(ring_dir, "." + key + ".filtered.lock")
        token = "%d:%s" % (os.getpid(), uuid.uuid4().hex)
        deadline = time.time() + 5.0
        held = False
        while time.time() < deadline:
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w", encoding="ascii") as lf:
                    lf.write(token)
                held = True
                break
            except FileExistsError:
                try:
                    lock_stat = os.stat(lock_path)
                    lock_text = open(lock_path, "r", encoding="ascii").read()
                except OSError:
                    continue
                age = time.time() - lock_stat.st_mtime
                if age > 5.0:
                    # Reclaim only when the holder is provably dead. The
                    # liveness probe is host.process_alive — raw os.kill is
                    # NOT a Windows liveness probe (signal 0 maps to
                    # GenerateConsoleCtrlEvent and returns None for a
                    # killed-but-unreaped holder, misclassifying it alive;
                    # final-critic round 1 F1). Uncertain (None) fails
                    # closed toward NOT reclaiming (a live-but-slow writer
                    # is never broken into), and the owner token is
                    # re-read immediately before the unlink.
                    try:
                        holder_pid = int(str(lock_text).split(":", 1)[0])
                    except (ValueError, AttributeError):
                        holder_pid = -1
                    if holder_pid <= 0:
                        # PRR-003: an empty/unparseable token aged past the
                        # grace has no possible live owner — a live writer
                        # writes its token within milliseconds of create —
                        # so this is the died-between-create-and-write
                        # orphan. Reclaim and retry acquisition.
                        try:
                            if open(lock_path, "r",
                                    encoding="ascii").read() == lock_text:
                                os.unlink(lock_path)
                                continue
                        except OSError:
                            pass
                        continue
                    holder_alive = None
                    if holder_pid > 0 and _process_alive is not None:
                        try:
                            holder_alive = _process_alive(holder_pid)
                        except Exception:
                            holder_alive = None
                    if holder_alive is False and age > 5.0:
                        try:
                            if open(lock_path, "r",
                                    encoding="ascii").read() == lock_text:
                                os.unlink(lock_path)
                                continue
                        except OSError:
                            pass
                time.sleep(0.05)
        try:
            if held:
                current = {}
                try:
                    current = json.loads(
                        open(counter_path, "r", encoding="utf-8").read())
                except (OSError, ValueError):
                    current = {}
                if not isinstance(current, dict):
                    current = {}
                n = current.get("filtered_trivial")
                current["filtered_trivial"] = (n if isinstance(n, int)
                                               else 0) + trivial_count
                tmp_fd, tmp_path = tempfile.mkstemp(
                    dir=ring_dir, prefix=".filtered-", suffix=".tmp")
                with os.fdopen(tmp_fd, "w", encoding="utf-8",
                               newline="\n") as tf:
                    tf.write(json.dumps(current) + "\n")
                os.replace(tmp_path, counter_path)
        finally:
            if held:
                try:
                    if open(lock_path, "r",
                            encoding="ascii").read() == token:
                        os.unlink(lock_path)
                except OSError:
                    pass
    except Exception as exc:
        # PRR-008c: a silent counter-write failure made the filtered
        # count unobservable. One bounded stderr line (guarded like the
        # other hook diagnostics); fail-open unchanged.
        try:
            text = " ".join(str(exc).split())[:200]
            print("[zmem] filtered counter update failed: %s" % text,
                  file=sys.stderr)
        except Exception:
            pass
    emit({})

# No failures and no rendered rejections → no-op (subagent reflection is
# failure-driven only; a stopped subagent is not an interactive turn to nag).
# But a user rejection that RENDERED (rej_msg non-empty) is the highest-signal
# correction in a transcript — surface it rather than let it evaporate (#46).
# Gating on rej_msg (not the raw rejections list) is defense-in-depth: if the
# shared render helper failed to import, rejections are dropped silently and we
# no-op (emit {}) instead of emitting a vacuous "had tool rejections" prompt.
if count == 0 and not rej_msg:
    emit({})

# 3. Skip if a lesson was already captured for THIS subagent (per-subagent key).
#    The query stays behind store.py; this adapter never opens the memory DB.
def source_exists():
    try:
        result = subprocess.run(
            [sys.executable, store_py, "source-exists",
             "--namespace", ns, "--source-ref", source_ref, "--json"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=10, check=False,
        )
        if result.returncode != 0:
            return False
        obj = json.loads(result.stdout.decode("utf-8", "replace"))
        return obj.get("exists") if isinstance(obj, dict) and isinstance(obj.get("exists"), bool) else False
    except Exception:
        return False

lesson_exists = source_exists()
if lesson_exists:
    emit({})

# 4. Build the parent-side hand-off sidecar (issue #204): the failure signal
#    moves to the parent, which can act on it without re-running THIS
#    subagent turn. The sidecar is read + consumed + pruned by
#    zmem-reflect.sh at the parent Stop hook.
from collections import Counter
import hashlib
import tempfile
import time
from datetime import datetime, timezone

tool_counts = Counter(d.get("tool", "?") for d in details) if details else Counter()
tool_summary = ", ".join("%d=%s" % (c, t) for t, c in tool_counts.most_common()) or ("%d failure(s)" % count)

DETAIL_LIMIT = 5
detail_lines = []
for d in details[:DETAIL_LIMIT]:
    tool = d.get("tool", "?")
    parts = [tool]
    et = d.get("error_type") or ""
    if et:
        parts.append("(%s)" % et)
    err = d.get("error") or ""
    if err:
        parts.append(": %s" % err)
    detail_lines.append("  - " + " ".join(parts))

sidecar = {
    "session": session_id,
    "agent_id": agent_id,
    "agent_type": agent_type,
    "source_ref": source_ref,
    "count": count,
    "tool_summary": tool_summary,
    "details": detail_lines,
    "rejections": rej_msg,
    "created": datetime.now(timezone.utc).isoformat(),
    "version": 1,
}

try:
    ring_dir = os.path.join(data_dir, "subagent-reflections")
    os.makedirs(ring_dir, exist_ok=True)
    # Opportunistic retention sweep on the WRITE path too (PR review PRR-006 /
    # PRR-011): the parent-side prune only runs on a parent Stop, so sidecars
    # (and interrupted .tmp files the parent glob never matches) would
    # otherwise accumulate when the parent never Stops. Same 14-day rule.
    # os.listdir (not glob) so the dot-prefixed .sidecar-*.tmp orphans are
    # actually enumerated — glob "*" never returns dotfiles (final-critic
    # round 1: the glob form let backdated orphans survive).
    try:
        now_s = time.time()
        for stale_name in os.listdir(ring_dir):
            if not (stale_name.endswith(".json")
                    or stale_name.endswith(".tmp")
                    or stale_name.endswith(".lock")):
                continue
            stale = os.path.join(ring_dir, stale_name)
            try:
                if (now_s - os.stat(stale).st_mtime) / 86400.0 > 14.0:
                    os.unlink(stale)
            except OSError:
                pass
    except Exception:
        pass
    # Collision-free key (PR review PRR-002): when a host sends no agent_id,
    # fall back to the unique agent transcript basename so sibling subagents
    # in one session never overwrite the hand-offs of siblings.
    agent_key = agent_id or os.path.basename(agent_transcript or "") or ""
    key = hashlib.sha256(
        (session_id + "\n" + agent_key).encode("utf-8")
    ).hexdigest()[:32]
    final_path = os.path.join(ring_dir, key + ".json")
    fd, tmp_path = tempfile.mkstemp(dir=ring_dir, prefix=".sidecar-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(sidecar, ensure_ascii=False) + "\n")
        os.replace(tmp_path, final_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
except Exception:
    # Fail-open: no sidecar, still the empty envelope, still exit 0.
    pass

# 5. NEVER emit a prompt from a finishing subagent (issue #204): the empty
#    envelope is the only output on every path.
emit({})
' "$STORE_PY_PY" "$NS" "$DATA_DIR_PY" "$AGENT_TRANSCRIPT_PY" "$SOURCE_REF" "$AGENT_TYPE" "$SESSION_ID" "$AGENT_ID" 2>/dev/null || echo '{}')"

if [ -z "$CTX_JSON" ]; then
  CTX_JSON='{}'
fi

# Neutralize any sentinel token untrusted content (e.g. a captured tool error)
# happens to contain, so it can't move the launcher's extraction boundary and
# silently degrade the whole injection to {} (fail-open self-DoS, not an
# injection vector — see zmem-recall.sh for the full rationale).
CTX_JSON="${CTX_JSON//<<<ZMEM_JSON>>>/<<<ZMEM_JSON_NEUTRALIZED>>>}"
CTX_JSON="${CTX_JSON//<<<END>>>/<<<END_NEUTRALIZED>>>}"

printf '<<<ZMEM_JSON>>>%s<<<END>>>\n' "$CTX_JSON"
exit 0

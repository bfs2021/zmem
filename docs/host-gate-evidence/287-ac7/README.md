# AC7 host-gate evidence — issue #287 (T-08 broker)

> Credential cleanup: the three UserPromptSubmit launcher recall captures
> (`claude-launcher-recall.json`, `codex-launcher-recall.json`, and
> `zcode-launcher-recall.json`) were removed because they contained live
> credentials. The historical description below does not substitute for those
> removed captures; their contents are no longer published evidence.


Captured 2026-10-24 against the vsan hub (`http://<hub-host>:<port>/mcp`,
zmem 0.120.0, schema 19, minilm) from the hub-host workstation. Per-harness
model-visible injection at the three AC7 moments (SessionStart,
UserPromptSubmit, PreToolUse) is proven two ways:

1. **Launcher-contract captures** (all three harnesses): each
   `<harness>-launcher-<moment>.json` drives the REAL production launcher
   (`hooks/zmem-launch.js` from that harness's installed zmem 0.121.0
   plugin — the exact code the harness invokes per its hook wiring) with
   `ZMEM_REMOTE_URL` pointing at the hub, and records the launcher's
   `additionalContext` output — the model-visible injection. Every capture
   shows `model_visible_fence: true` (the `ZMEM_UNTRUSTED_FENCE` block);
   at capture time the UserPromptSubmit (`recall`) captures additionally
   showed `hub_canary_visible: true` — the canary row seeded into the hub's
   store over MCP immediately before the capture, proving the content
   traveled hub → broker → harness. At capture time the SessionStart
   captures carried the hub-owned Tier-0 `core.md` (5130 chars), the T-08
   hub-authoritative Tier-0 behavior; that content is no longer published
   verbatim (see the redaction note below).

2. **Real harness transcript** (codex): `codex-exec-transcript.jsonl` is
   the session log of `codex exec` (codex-cli 0.160.0) run with
   `ZMEM_REMOTE_URL` set, redacted two ways (2026-11-05): absolute home
   directory segments (`.../Users/<user>` placeholders, per the
   tracked-file hygiene ratchet in `tests/test_doc_drift.py`), and the
   raw memory payloads — the Tier-0 `core.md` body and the one
   `user:global` recall row — replaced in place with redaction notices
   (hook events, fence headers and model output are untouched). The
   harness fired the zmem
   `UserPromptSubmit` hook (visible in the log as
   `hook: UserPromptSubmit Completed`), and the MODEL'S OWN ANSWER in the
   transcript confirms the fenced `Relevant memories` block was visible
   to it. `claude-run2.json` documents the claude leg's harness wiring
   proof (the CLI invoked the launcher's session-end hook; the model leg
   of that run failed on an expired OAuth session, which is why the
   claude evidence is the launcher-contract capture — the hook contract
   output is by definition the model-visible injection for that
   harness's runner).

Harness plugin versions at capture: claude 0.121.0 (auto-updated,
gitCommitSha 05c1bfa8), codex 0.121.0 (auto-updated), zcode 0.121.0
(registered; cache materialized from the same release tree). All three
harnesses ran against the same single hub with the same token
(`~/.zmem/.mcp_token`, bearer auth).

The capability-matrix rows for these moments were already `registered` at
generation (the T-01 contract registered the designed capability); this
evidence discharges the HOST_ONLY obligation recorded in
`docs/FLEET.md`'s T-08 section.

## Safe evidence publication

Do not publish raw recall memory in host-gate evidence. The regression test
requires the three exposed captures to remain absent, and permits future
launcher recall captures only when `stdout_raw` contains the fixed redaction
notice defined by the test. Use synthetic credentials and curated evidence
when capturing production hook behavior.

> Redaction (2026-11-05, issue #438): the six launcher-contract captures
> published here were redacted one way after the fact — each capture's
> `stdout_raw` (which had carried verbatim recall payloads / Tier-0 core
> text) was replaced with the fixed redaction notice, and each live
> `remote_url` value was replaced with the `redacted-hub-url` placeholder.
> The structural proof fields (`model_visible_fence`,
> `hub_canary_visible`, exit codes, timestamps, plugin versions) are
> untouched; the raw payloads are no longer published evidence. This is a
> one-way redaction: the descriptions above are not claims that the raw
> content is still readable here. The guard itself now lives in
> `scripts/host_evidence_guard.py` and is filename-independent for the
> `stdout_raw` rule.
>
> Transcript redaction (2026-11-06, PR #441 feedback): the codex
> transcript's Tier-0 body and `user:global` recall row were redacted in
> place (structural fence headers, the probe question, hook events and
> model output untouched) — the two-way disclosure in point 2 above
> supersedes the original "redacted exactly one way" wording.

## Guard widening (2026-11-08, issue #448)

`scripts/host_evidence_guard.py` grew three additive surfaces beside the
unchanged `stdout_raw` payload rule:

- `unsafe_incident_shape_paths` — flags a capture-shaped `*.json` dict
  (a `remote_url` key OR any `://` string value) whose string values
  carry a credential key=value assignment (the incident shape that
  leaked through `refs/pull/402/head`; values are non-space semantics,
  4+ chars, quoted forms included). A duplicate-key document or an
  undecodable file is FLAGGED under any name (hostile parse semantics —
  never silently skipped); only an unparseable or unreadable file is
  skipped, exactly like the frozen payload rule.
- `unsafe_jsonl_transcript_paths` — scans `*.jsonl` transcripts line by
  line (split on `\n` only; a UTF-8 BOM is stripped): a malformed line,
  a DUPLICATE-KEY line (last-wins parsing could hide an unredacted fence
  behind the surviving member), a non-UTF-8 file, a live or neutralized
  fence open marker whose body carries no `[REDACTED:` notice, AND a
  capture-shaped line carrying a credential key=value assignment (the
  incident shape is governed in transcripts too) — each flags the file
  fail-closed.
- the allowlist — a reviewed true-positive may be suppressed only by an
  entry pinning `path` + `detector` + the file's CURRENT whole-file git
  blob sha + `occurrence_position` (the ZERO-BASED HIT ORDINAL in the
  detector's deterministic hit sequence — one hit per offending physical
  line for the jsonl detector, one per credential match for the incident
  detector; never a line number) + `count` (the total hits for that
  path+detector). There is no shipped allowlist FILE today; the surface
  is Python-only via `parse_allowlist` / `validate_allowlist`, and a
  hash of a matched secret span is structurally refused — the only hash
  field accepted is the whole-file blob identity. One REVIEWED hit
  exists in the shipped corpus and is pinned (with its allowlist entry)
  by the frozen jsonl check: the 287-ac7 transcript's `world_state`
  record carries the deliberate SSRF-probe test values from the AC7
  evidence session; the check proves the unsuppressed scan flags it and
  the whole-file entry suppresses it — a regenerated transcript
  (new blob) invalidates the entry and fails the check, which is the
  designed re-review trigger.

## Guard hardening (2026-11-08, PR #451 review round)

The credential key family matches ANYWHERE (no prefix restriction —
camelCase `clientSecret`/`accessToken` and suffix compounds
`SECRET_KEY`/`PRIVATE_KEY` all flag; a benign word containing a family
substring over-matches, accepted noise for a human-review flag), and
values may contain quote characters. `parse_allowlist` refuses
duplicate-key documents.

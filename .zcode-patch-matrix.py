"""One-shot patch: add the target-port element to every CASES entry and
append the review-round-4 cases. Line-based (no regex transport)."""
from pathlib import Path

p = Path("tests/test_fleet_installer_443.py")
lines = p.read_text(encoding="utf-8").splitlines(keepends=True)

out = []
in_cases = False
for line in lines:
    if line.startswith("CASES = ["):
        in_cases = True
        out.append(line)
        continue
    if in_cases and line.strip() == "]":
        in_cases = False
        out.append(line)
        continue
    if in_cases and line.strip().startswith('("'):
        # ("name", PORT, "status...", O, O), -> insert 443, after PORT
        stripped = line.lstrip()
        lead = line[: len(line) - len(stripped)]
        assert stripped.startswith('("'), stripped
        name_end = stripped.index('"', 2)
        rest = stripped[name_end + 1 :]
        # rest starts with ', PORT, "status...'
        comma = rest.index(",", 1)
        port = rest[1:comma].strip()
        tail = rest[comma:]
        assert port.isdigit(), f"unexpected port token {port!r} in: {line!r}"
        out.append(f'{lead}("{stripped[2:name_end]}", {port}, 443,{tail}')
        continue
    out.append(line)

src = "".join(out)
old = '    ("empty-input", 8765, 443, "", 0, 0),\n]'
new = '''    ("empty-input", 8765, 443, "", 0, 0),
    # Review round 4: EVERY target-port block counts (the ps1
    # last-block-only regression) - an all-ours :443 block followed by a
    # :8443 block stays ours.
    ("ours443-then-8443-block", 8765, 443,
     "https://host.ts.net (tailnet only)\\n|-- / proxy http://127.0.0.1:8765\\n\\n"
     "https://host.ts.net:8443 (tailnet only)\\n|-- / proxy http://127.0.0.1:3000", 1, 1),
    ("operator443-then-ours8443", 8765, 443,
     "https://host.ts.net (tailnet only)\\n|-- / proxy http://127.0.0.1:32400\\n\\n"
     "https://host.ts.net:8443 (tailnet only)\\n|-- / proxy http://127.0.0.1:8765", 1, 0),
    # Review round 4: the explicit-port escape is itself occupancy-checked.
    ("target-8443-operator", 8765, 8443,
     "https://host.ts.net:8443 (tailnet only)\\n|-- / proxy http://127.0.0.1:32400", 1, 0),
    ("target-8443-ours-idempotent", 8765, 8443,
     "https://host.ts.net:8443 (tailnet only)\\n|-- / proxy http://127.0.0.1:8765", 1, 1),
]'''
assert src.count(old) == 1, "empty-input anchor not found"
src = src.replace(old, new)
p.write_text(src, encoding="utf-8", newline="\n")
print("matrix patched:", src.count('", 443,"'), "entries retargeted")

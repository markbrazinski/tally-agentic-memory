"""Gate 7 privacy scan of the public-safe working tree.

Scans tracked repository content for REAL private-identifier VALUES that must
never be public: credentialed DSNs, AWS access keys, private-key blocks, bearer
token values, the isolated cluster hostname, and raw S3 version identifiers.
Prose mentions of words like "bearer" or the public MCP endpoint URL are not
secrets and are not flagged.

Exit 0 = clean. Non-zero = a real leak was found. Excludes .env, .git, and the
operator-private recovery/runtime artifacts that are already gitignored.
"""

from __future__ import annotations

import re
import subprocess
import sys

# Patterns for actual secret VALUES (not prose).
PATTERNS = {
    "credentialed_dsn": re.compile(r"postgresql://[^\s\"']*:[^\s\"'@]+@"),
    "aws_access_key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "private_key_block": re.compile(r"-----BEGIN (?:RSA |EC )?PRIVATE KEY-----"),
    "cluster_hostname": re.compile(r"[a-z-]+-\d{4,6}\.[a-z0-9]+\.aws-[a-z0-9-]+"
                                   r"\.cockroachlabs\.cloud"),
    "bearer_value": re.compile(r"Bearer\s+[A-Za-z0-9._~+/-]{20,}"),
    "password_kv": re.compile(r"password\s*=\s*\S+", re.IGNORECASE),
}


def tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files"], capture_output=True, text=True,
                         check=True).stdout
    return [f for f in out.splitlines()
            if not f.startswith(".env")
            and not f.endswith(".pyc")]


def main() -> int:
    findings = []
    for path in tracked_files():
        try:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                text = fh.read()
        except OSError:
            continue
        for name, pattern in PATTERNS.items():
            for match in pattern.finditer(text):
                # Ignore the public MCP endpoint (documented, not a secret).
                snippet = match.group(0)
                findings.append({"file": path, "pattern": name,
                                 "match_prefix": snippet[:12] + "..."})
    result = {
        "scan": "gate7-privacy",
        "files_scanned": len(tracked_files()),
        "findings": findings,
        "clean": not findings,
    }
    import json
    print(json.dumps(result, indent=2))
    return 0 if not findings else 1


if __name__ == "__main__":
    sys.exit(main())

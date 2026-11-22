"""Refuse private files and token-shaped strings in the public Runway repository.

Usage (standard library only):

    python scripts/secret_scan.py --staged          # what is about to be committed
    python scripts/secret_scan.py --tracked         # the whole tracked tree (CI)
    python scripts/secret_scan.py PATH [PATH ...]   # given files (the pre-commit hook)

Exit status 1 when anything is found, 0 when clean. Findings name the file,
line and rule, never the secret itself. A reviewed false positive is allowed
by adding ``<path> <rule> <fingerprint>`` (printed with the finding) to
``scripts/secret_scan_allowlist.txt``.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import re
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

ALLOWLIST = Path(__file__).with_name("secret_scan_allowlist.txt")
MAX_SCAN_BYTES = 5 * 1024 * 1024

# Private instance files that may never enter the public repository, by base name.
BLOCKED_NAME_PATTERNS: tuple[tuple[str, str], ...] = (
    ("*.db", "database file"),
    ("*.key", "key file"),
    (".env*", "environment file"),
    ("platform-tokens.json", "platform token store"),
    ("secrets.enc.*", "encrypted instance secrets"),
    ("lease.json", "instance lease"),
)
ALLOWED_NAMES = frozenset({".env.example"})

# Token shapes. Each requires a realistic body so prose that merely names a
# prefix (for example "`ya29.` tokens") does not match.
_START = r"(?<![A-Za-z0-9_])"
CONTENT_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("google-oauth-access-token", re.compile(_START + r"ya29\.[0-9A-Za-z_-]{20,}")),
    ("meta-access-token", re.compile(_START + r"EAAC[0-9A-Za-z]{20,}")),
    ("tiktok-access-token", re.compile(_START + r"act\.[0-9A-Za-z!*._-]{20,}")),
    (
        "client-secret-assignment",
        re.compile(
            _START + r"""client_secret\s*[=:]\s*["']?(?![{$<%"'\s&])[^\s"'&,;)\]}]{6,}""",
            re.IGNORECASE,
        ),
    ),
    ("private-key-block", re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")),
    ("runway-live-api-key", re.compile(_START + r"rwy_live_[0-9A-Za-z_-]{16,}")),
    ("github-token", re.compile(_START + r"gh[oprsu]_[0-9A-Za-z]{36,}")),
)


@dataclass(frozen=True)
class Finding:
    path: str
    rule: str
    line: int | None = None
    fingerprint: str | None = None

    def render(self) -> str:
        where = f"{self.path}:{self.line}" if self.line is not None else self.path
        suffix = f" (fingerprint {self.fingerprint})" if self.fingerprint else ""
        return f"{where}: {self.rule}{suffix}"


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def blocked_name(path: str) -> str | None:
    name = PurePosixPath(path.replace("\\", "/")).name
    if name in ALLOWED_NAMES:
        return None
    for pattern, label in BLOCKED_NAME_PATTERNS:
        if fnmatch.fnmatchcase(name.casefold(), pattern):
            return label
    return None


def scan_text(path: str, text: str) -> list[Finding]:
    findings: list[Finding] = []
    for number, line in enumerate(text.splitlines(), start=1):
        for rule, pattern in CONTENT_RULES:
            for match in pattern.finditer(line):
                findings.append(Finding(path, rule, number, fingerprint(match.group(0))))
    return findings


def scan_blob(path: str, data: bytes) -> list[Finding]:
    findings: list[Finding] = []
    label = blocked_name(path)
    if label is not None:
        findings.append(Finding(path, f"{label} must not be committed"))
    if b"\0" in data[:8192]:
        return findings  # binary: only the name rule applies
    findings.extend(scan_text(path, data[:MAX_SCAN_BYTES].decode("utf-8", errors="replace")))
    return findings


def load_allowlist(path: Path = ALLOWLIST) -> set[tuple[str, str, str]]:
    entries: set[tuple[str, str, str]] = set()
    if not path.is_file():
        return entries
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 3 or not re.fullmatch(r"[0-9a-f]{64}", parts[2]):
            raise SystemExit(f"{path}:{number}: expected '<path> <rule> <sha256 fingerprint>'")
        entries.add((parts[0], parts[1], parts[2]))
    return entries


def _git(args: Sequence[str]) -> bytes:
    result = subprocess.run(["git", *args], capture_output=True, check=False)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise SystemExit(f"git {' '.join(args)} failed: {detail}")
    return result.stdout


def _split_z(output: bytes) -> list[str]:
    return [item.decode("utf-8", errors="surrogateescape") for item in output.split(b"\0") if item]


def staged_sources() -> Iterable[tuple[str, Callable[[], bytes]]]:
    """Files as they are in the index, i.e. exactly what the commit will contain."""
    names = _split_z(_git(["diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"]))
    for name in names:
        yield name, (lambda name=name: _git(["show", f":{name}"]))


def tracked_sources() -> Iterable[tuple[str, Callable[[], bytes]]]:
    for name in _split_z(_git(["ls-files", "-z"])):
        path = Path(name)
        if path.is_file():
            yield name, path.read_bytes


def path_sources(paths: Sequence[str]) -> Iterable[tuple[str, Callable[[], bytes]]]:
    for name in paths:
        path = Path(name)
        if path.is_file():
            yield path.as_posix(), path.read_bytes


def scan(
    sources: Iterable[tuple[str, Callable[[], bytes]]],
    allowlist: set[tuple[str, str, str]] | None = None,
) -> list[Finding]:
    allowed = allowlist if allowlist is not None else load_allowlist()
    findings: list[Finding] = []
    for name, read in sources:
        for finding in scan_blob(name, read()):
            key = (finding.path, finding.rule, finding.fingerprint or "")
            if finding.fingerprint and key in allowed:
                continue
            findings.append(finding)
    return findings


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--staged", action="store_true", help="scan the git index")
    mode.add_argument("--tracked", action="store_true", help="scan every tracked file")
    parser.add_argument("paths", nargs="*", help="files to scan")
    arguments = parser.parse_args(argv)
    if arguments.paths and (arguments.staged or arguments.tracked):
        parser.error("give paths or --staged/--tracked, not both")
    if arguments.staged:
        sources = staged_sources()
    elif arguments.tracked:
        sources = tracked_sources()
    elif arguments.paths:
        sources = path_sources(arguments.paths)
    else:
        parser.error("nothing to scan: give paths, --staged or --tracked")
    findings = scan(sources)
    if not findings:
        return 0
    print("Secret scan failed. These must not enter the public repository:", file=sys.stderr)
    for finding in findings:
        print(f"  {finding.render()}", file=sys.stderr)
    print(
        "Private instance data belongs in the private instance repository "
        "(docs/PRIVATE_INSTANCE.md). For a reviewed false positive, add "
        "'<path> <rule> <fingerprint>' to scripts/secret_scan_allowlist.txt.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

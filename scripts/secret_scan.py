"""Refuse private files and token-shaped strings in the public Runway repository.

Usage (standard library only):

    python scripts/secret_scan.py --staged          # what is about to be committed
    python scripts/secret_scan.py --tracked         # the whole tracked tree
    python scripts/secret_scan.py --range A..B      # every commit in a range (CI)
    python scripts/secret_scan.py --all-history     # every commit reachable from HEAD
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
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO

ALLOWLIST = Path(__file__).with_name("secret_scan_allowlist.txt")
MAX_SCAN_BYTES = 5 * 1024 * 1024

# Private instance files and snapshots that may never enter the public repository.
BLOCKED_NAME_PATTERNS: tuple[tuple[str, str], ...] = (
    ("*.db", "database file"),
    ("*.db.gz", "database snapshot"),
    ("*.db-wal", "database write-ahead log"),
    ("*.db-shm", "database shared-memory file"),
    ("*.sqlite", "database file"),
    ("*.sqlite3", "database file"),
    ("runway-operational-media.zip", "media snapshot"),
    ("*.key", "key file"),
    (".env*", "environment file"),
    ("platform-tokens.json", "platform token store"),
    ("secrets.enc.*", "encrypted instance secrets"),
    ("lease.json", "instance lease"),
)
ALLOWED_NAMES = frozenset({".env.example"})

_START = r"(?<![A-Za-z0-9_])"
# Token shapes with a realistic body, so prose that merely names a prefix
# (for example "`ya29.` tokens") does not match.
TOKEN_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("google-oauth-access-token", re.compile(_START + r"ya29\.[0-9A-Za-z_-]{20,}")),
    ("google-refresh-token", re.compile(r"(?<![A-Za-z0-9/:])1//[0-9A-Za-z_-]{30,}")),
    ("google-client-secret", re.compile(_START + r"GOCSPX-[0-9A-Za-z_-]{20,}")),
    ("meta-access-token", re.compile(_START + r"EAAC[0-9A-Za-z]{20,}")),
    ("instagram-access-token", re.compile(_START + r"IG(?:AA|QV)[0-9A-Za-z_-]{30,}")),
    ("tiktok-access-token", re.compile(_START + r"act\.[0-9A-Za-z!*._-]{20,}")),
    ("tiktok-refresh-token", re.compile(_START + r"rft\.[0-9A-Za-z!*._-]{20,}")),
    ("private-key-block", re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")),
    ("runway-live-api-key", re.compile(_START + r"rwy_live_[0-9A-Za-z_-]{16,}")),
    ("github-token", re.compile(_START + r"gh[oprsu]_[0-9A-Za-z]{36,}")),
)
# ``name = value`` / ``"name": "value"`` / ``?name=value`` assignments, matched
# with any prefix (RUNWAY_YOUTUBE_CLIENT_SECRET, youtube_client_secret, ...).
_VALUE = r"""["']?\s*[=:]\s*(?P<quote>["']?)(?P<value>[^\s"'&,;)\]}#\\]+)"""
ASSIGNMENT_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("client-secret-assignment", re.compile(r"(?i)client_secret" + _VALUE)),
    ("app-secret-assignment", re.compile(r"(?i)(?:app|client_key)_secret" + _VALUE)),
    ("token-key-assignment", re.compile(r"(?i)(?<![A-Za-z0-9])(?:RUNWAY_)?TOKEN_KEYS?" + _VALUE)),
)
MIN_ASSIGNED_SECRET = 8
# Values that are obviously not a credential: fixtures, placeholders and prose.
_PLACEHOLDER = re.compile(
    r"(?i)fake|example|placeholder|dummy|changeme|change-me|redacted|your|xxx|sample|"
    r"secret|fixture|test|mock|<|>|\.\.\."
)
_CODE_EXPRESSION = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+$|[(\[]")
_LOWER_IDENTIFIER = re.compile(r"^[a-z_]+$")


@dataclass(frozen=True)
class Finding:
    path: str
    rule: str
    line: int | None = None
    fingerprint: str | None = None
    commit: str | None = None

    def render(self) -> str:
        where = f"{self.path}:{self.line}" if self.line is not None else self.path
        if self.commit:
            where = f"{self.commit[:12]}:{where}"
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


def _assigned_secret(value: str, quoted: bool) -> bool:
    if len(value) < MIN_ASSIGNED_SECRET or value[0] in "{$%<":
        return False
    if _PLACEHOLDER.search(value):
        return False
    # Unquoted code references (settings.youtube_client_secret, os.environ[...],
    # client_secret=client_secret) are not values.
    return quoted or not (_CODE_EXPRESSION.search(value) or _LOWER_IDENTIFIER.match(value))


def scan_text(path: str, text: str) -> list[Finding]:
    findings: list[Finding] = []
    for number, line in enumerate(text.splitlines(), start=1):
        for rule, pattern in TOKEN_RULES:
            for match in pattern.finditer(line):
                findings.append(Finding(path, rule, number, fingerprint(match.group(0))))
        for rule, pattern in ASSIGNMENT_RULES:
            for match in pattern.finditer(line):
                if _assigned_secret(match.group("value"), bool(match.group("quote"))):
                    findings.append(Finding(path, rule, number, fingerprint(match.group(0))))
    return findings


def decode_text(data: bytes) -> str | None:
    """Text from UTF-8 or UTF-16 (with a BOM or by its NUL pattern); None when binary."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace")
    sample = data[:8192]
    if b"\0" not in sample:
        return data.decode("utf-8", errors="replace")
    pairs = len(sample) // 2
    if pairs:
        odd = sum(1 for index in range(1, 2 * pairs, 2) if sample[index] == 0)
        even = sum(1 for index in range(0, 2 * pairs, 2) if sample[index] == 0)
        if odd >= 0.8 * pairs and even == 0:
            return data.decode("utf-16-le", errors="replace")
        if even >= 0.8 * pairs and odd == 0:
            return data.decode("utf-16-be", errors="replace")
    return None


def scan_blob(path: str, data: bytes) -> list[Finding]:
    findings: list[Finding] = []
    label = blocked_name(path)
    if label is not None:
        findings.append(Finding(path, f"{label} must not be committed"))
    text = decode_text(data[:MAX_SCAN_BYTES])
    if text is not None:
        findings.extend(scan_text(path, text))
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


Source = tuple[str, Callable[[], bytes], str | None]


def staged_sources() -> Iterable[Source]:
    """Files as they are in the index, i.e. exactly what the commit will contain."""
    names = _split_z(_git(["diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"]))
    for name in names:
        yield name, (lambda name=name: _git(["show", f":{name}"])), None


def tracked_sources() -> Iterable[Source]:
    for name in _split_z(_git(["ls-files", "-z"])):
        path = Path(name)
        if path.is_file():
            yield name, path.read_bytes, None


def path_sources(paths: Sequence[str]) -> Iterable[Source]:
    for name in paths:
        path = Path(name)
        if path.is_file():
            yield path.as_posix(), path.read_bytes, None


class _BlobReader:
    """One ``git cat-file --batch`` process for every blob of a history scan."""

    def __init__(self) -> None:
        self._process = subprocess.Popen(
            ["git", "cat-file", "--batch"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )

    def read(self, spec: str) -> bytes:
        stdin: IO[bytes] | None = self._process.stdin
        stdout: IO[bytes] | None = self._process.stdout
        assert stdin is not None and stdout is not None
        stdin.write(spec.encode("utf-8", errors="surrogateescape") + b"\n")
        stdin.flush()
        header = stdout.readline().split()
        if len(header) < 3 or header[1] != b"blob":
            return b""
        data = stdout.read(int(header[2]))
        stdout.read(1)  # the newline after each object
        return data

    def close(self) -> None:
        if self._process.stdin is not None:
            self._process.stdin.close()
        self._process.wait()


def commit_sources(commits: Sequence[str]) -> Iterator[Source]:
    """Every file each commit adds or changes, as that commit has it."""
    reader = _BlobReader()
    try:
        for commit in commits:
            names = _split_z(
                _git(
                    [
                        "diff-tree",
                        "--root",
                        "-r",
                        "-z",
                        "--no-commit-id",
                        "--name-only",
                        "--diff-filter=ACMR",
                        "-m",
                        "--first-parent",
                        commit,
                    ]
                )
            )
            for name in names:
                data = reader.read(f"{commit}:{name}")
                yield name, (lambda data=data: data), commit
    finally:
        reader.close()


def range_commits(revision_range: str | None) -> list[str]:
    arguments = ["rev-list", "--reverse"]
    arguments.append(revision_range if revision_range else "HEAD")
    return [line for line in _git(arguments).decode("ascii").split() if line]


def scan(
    sources: Iterable[Source] | Iterable[tuple[str, Callable[[], bytes]]],
    allowlist: set[tuple[str, str, str]] | None = None,
) -> list[Finding]:
    allowed = allowlist if allowlist is not None else load_allowlist()
    findings: list[Finding] = []
    seen: set[tuple[str, str, str, int | None]] = set()
    for source in sources:
        name, read = source[0], source[1]
        commit = source[2] if len(source) > 2 else None  # type: ignore[misc]
        for finding in scan_blob(name, read()):
            key = (finding.path, finding.rule, finding.fingerprint or "")
            if finding.fingerprint and key in allowed:
                continue
            dedupe = (finding.path, finding.rule, finding.fingerprint or "", finding.line)
            if commit is not None and dedupe in seen:
                continue  # the same finding in a later commit of the same file
            seen.add(dedupe)
            findings.append(
                Finding(finding.path, finding.rule, finding.line, finding.fingerprint, commit)
            )
    return findings


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--staged", action="store_true", help="scan the git index")
    mode.add_argument("--tracked", action="store_true", help="scan every tracked file")
    mode.add_argument("--range", metavar="A..B", help="scan every commit in a revision range")
    mode.add_argument("--all-history", action="store_true", help="scan every commit of HEAD")
    parser.add_argument("paths", nargs="*", help="files to scan")
    arguments = parser.parse_args(argv)
    modes = arguments.staged or arguments.tracked or arguments.range or arguments.all_history
    if arguments.paths and modes:
        parser.error("give paths or one of --staged/--tracked/--range/--all-history, not both")
    sources: Iterable[Source]
    if arguments.staged:
        sources = staged_sources()
    elif arguments.tracked:
        sources = tracked_sources()
    elif arguments.range or arguments.all_history:
        sources = commit_sources(range_commits(arguments.range))
    elif arguments.paths:
        sources = path_sources(arguments.paths)
    else:
        parser.error("nothing to scan: give paths, --staged, --tracked, --range or --all-history")
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

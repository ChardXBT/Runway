"""Replace the owner's Windows profile path and e-mail addresses in build-log files.

Usage: python -I sanitize_paths.py <directory>
"""

import pathlib
import re
import sys

USER = "eric" + "y"  # split so this file itself does not match a search for the name
REPLACEMENTS = [
    ("C:\\\\Users\\\\" + USER, "%USERPROFILE%"),
    ("C:\\Users\\" + USER, "%USERPROFILE%"),
    ("C:/Users/" + USER, "%USERPROFILE%"),
    ("/c/Users/" + USER, "$HOME"),
]
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.(com|ca|edu)")

for path in pathlib.Path(sys.argv[1]).rglob("*"):
    if not path.is_file() or path.name == "sanitize_paths.py":
        continue
    text = path.read_text(encoding="utf-8", errors="replace")
    new = text
    for old, rep in REPLACEMENTS:
        new = new.replace(old, rep)
    new = EMAIL.sub("<email>", new)
    if new != text:
        path.write_text(new, encoding="utf-8")
        print("sanitized", path)

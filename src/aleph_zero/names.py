"""The name a person is told to type. TeX Live installs a program called `aleph`; on such a machine Aleph Zero is run
as `aleph-zero`, and advice to run `aleph setup` would start TeX. So what this process tells a person to run is named
as it was itself started: `aleph-zero` when started so (the plugin always does), `aleph` otherwise."""
from __future__ import annotations

import sys
from pathlib import Path

COMMAND = "aleph-zero" if Path(sys.argv[0] if sys.argv else "").name == "aleph-zero" else "aleph"

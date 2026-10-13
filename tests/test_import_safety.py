"""The verbs must import without the MCP SDK (and its compiled pydantic-core).

This is the contract that lets a locked-down machine (e.g. Windows App Control
for Business) use the verbs as a library. Run in a subprocess so modules other
tests imported can't mask a regression.
"""

import subprocess
import sys

PROBE = """
import sys
import now_config.verbs, now_data.verbs
bad = sorted(m for m in sys.modules
             if m == "mcp" or m.startswith(("mcp.", "pydantic", "pysnc")))
print(",".join(bad))
"""


def test_verbs_import_without_mcp_pydantic_or_pysnc():
    out = subprocess.run([sys.executable, "-c", PROBE], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "", f"verbs imported: {out.stdout.strip()}"

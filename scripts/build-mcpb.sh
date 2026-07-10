#!/usr/bin/env bash
# Build the Claude Desktop extension bundle (mcp-gtags-server.mcpb).
#
# Vendors the Python package and its dependencies into server/lib so the
# bundle runs on any system python3 >= 3.10 — no pip install on the user's
# machine. The gtags/ctags toolchain is NOT bundled; the server installs it
# into ~/.gtags-mcp on first use (lazy bootstrap), keeping the bundle small.
#
# Usage: bash scripts/build-mcpb.sh   (from the repo root; needs npx + pip/uv)

set -euo pipefail
cd "$(dirname "$0")/.."

VERSION="$(python3 - <<'EOF'
import re
print(re.search(r'^version = "(.*)"', open("pyproject.toml").read(), re.M).group(1))
EOF
)"

STAGE="build/mcpb-stage"
rm -rf "$STAGE"
mkdir -p "$STAGE/server/lib"

# Manifest, with the real package version stamped in.
python3 - "$VERSION" <<'EOF'
import json, sys
manifest = json.load(open("mcpb/manifest.json"))
manifest["version"] = sys.argv[1]
json.dump(manifest, open("build/mcpb-stage/manifest.json", "w"), indent=2)
EOF

# Vendor the package (with dist-info, so --version reports correctly) and
# its dependencies. tomli is only imported on python < 3.11 but is tiny and
# pure-python, so it is always bundled regardless of the build python.
if command -v uv >/dev/null 2>&1; then
    uv pip install --target "$STAGE/server/lib" --quiet . tomli
else
    python3 -m pip install --target "$STAGE/server/lib" --quiet . tomli
fi

cat > "$STAGE/server/main.py" <<'EOF'
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

from gtags_mcp.server import main

if __name__ == "__main__":
    main()
EOF

npx -y @anthropic-ai/mcpb pack "$STAGE" mcp-gtags-server.mcpb
echo "built mcp-gtags-server.mcpb (v$VERSION)"

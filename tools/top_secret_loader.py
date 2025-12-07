"""Launch the persistent environment vault GUI in one shot."""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = REPO_ROOT.parent
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from x_make_persistent_env_var_x.x_cls_make_persistent_env_var_x import (
    run_cli,
)


def main() -> int:
    return run_cli(["--launch-gui"])


if __name__ == "__main__":
    sys.exit(main())

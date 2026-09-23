"""Windows launch gate: execute nothing until the parent owns this process."""

import subprocess
import sys

if __name__ == "__main__":
    if sys.stdin.buffer.read(1) == b"G":
        try:
            raise SystemExit(subprocess.call(sys.argv[1:], stdin=subprocess.DEVNULL))
        except OSError as exc:
            print(f"Failed to start command: {exc}", file=sys.stderr)
            raise SystemExit(127) from exc

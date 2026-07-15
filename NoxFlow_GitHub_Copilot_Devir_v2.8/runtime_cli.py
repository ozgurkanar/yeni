from pathlib import Path
import signal
import sys

from noxflow.config import DEFAULT_CONFIG
from noxflow.orchestrator import Orchestrator


config = (
    Path(sys.argv[1])
    if len(sys.argv) > 1
    else DEFAULT_CONFIG
)
engine = Orchestrator(
    config,
    print,
)


def stop(*_args):
    engine.stop()


signal.signal(
    signal.SIGINT,
    stop,
)
if hasattr(signal, "SIGTERM"):
    signal.signal(
        signal.SIGTERM,
        stop,
    )

try:
    engine.run()
except Exception as exc:
    print(
        f"HATA: {exc}",
        file=sys.stderr,
    )
    raise SystemExit(1)

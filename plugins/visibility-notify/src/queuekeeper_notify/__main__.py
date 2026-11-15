"""A delivery, as the process the queue starts apart from itself: `python3 -m queuekeeper_notify DIRECTORY`."""
import sys

from . import deliver

raise SystemExit(deliver.main(sys.argv[1:]))

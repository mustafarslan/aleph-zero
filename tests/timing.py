"""Speed budgets in tests: the local figure is the measured claim; GitHub's shared runners are slower and uneven (two
budgets missed by a few milliseconds there on 2026-10-15), so there they get three times the room. A real slowdown
still fails here, on the machine the figures were measured on."""
import os

SLACK = 3.0 if os.environ.get("GITHUB_ACTIONS") else 1.0


def budget(seconds: float) -> float:
    return seconds * SLACK

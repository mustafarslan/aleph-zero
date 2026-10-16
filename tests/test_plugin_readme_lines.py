"""The plugin README's answers to the directory cite lines of the mod: each cited line must hold what it says."""
import re
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1] / "plugin"
WHAT = {"config.set": "$.config.set(", "mcp.call": "$.mcp.call(", "process.run": "$.process.run(", "process.spawn": "$.process.spawn(",
        "tool.call": "on('tool.call'", "tool.describe": "on('tool.describe'", "prompt.compose": "on('prompt.compose'",
        "command.run": "on('command.run'"}


def test_every_line_the_review_section_cites_holds_the_call_it_names():
    readme = (PLUGIN / "README.md").read_text()
    review = readme[readme.index("### For the directory's review"):]
    lines = (PLUGIN / "hooks" / "register.js").read_text().splitlines()
    cited = re.findall(r"`(config\.set|mcp\.call|process\.run|process\.spawn|tool\.call|tool\.describe|prompt\.compose|command\.run)`"
                       r"(?: of git)?, lines? ([\d–, and]+)", review)
    assert len(cited) >= 8
    for what, numbers in cited:
        for number in re.findall(r"\d+", numbers.replace("–", " ")):
            assert WHAT[what] in lines[int(number) - 1], (what, number, lines[int(number) - 1])

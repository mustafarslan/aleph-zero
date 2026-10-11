"""Guards against the py-tree-sitter 0.26.0 regression.

Reading `start_point` and `end_byte` on every node of a few large trees
segfaults under tree-sitter 0.26.0 and is clean under 0.25.2. The walk runs in
a child process because a segfault would otherwise take pytest down with it.
"""
import subprocess
import sys

WALK = r'''
import tree_sitter_language_pack as pack

source = "".join(
    f"class C{i}(Base):\n"
    + "".join(
        f"    def method_{j}(self, a, b=None, *args, **kwargs):\n"
        f"        if a and b:\n"
        f"            return [x * {j} for x in range(a) if x % 2 == 0]\n"
        f"        return {{'k': a, 'v': (b, {i}, {j})}}\n\n"
        for j in range(12)
    )
    for i in range(60)
).encode()
parser = pack.get_parser("python")
count = 0

def walk(node):
    global count
    for child in node.named_children:
        count += 1
        child.start_point.row
        child.end_byte
        walk(child)

for _ in range(4):
    walk(parser.parse(source).root_node)
print(count)
'''


def test_walking_every_node_position_does_not_crash():
    result = subprocess.run([sys.executable, "-c", WALK], capture_output=True, text=True)
    assert result.returncode == 0, f"walk died with code {result.returncode}: {result.stderr[-300:]}"
    assert int(result.stdout) > 100_000

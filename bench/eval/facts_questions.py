"""Questions for the knowledge graph's facts (DEFERRED entry 0-graph, part 2), drawn mechanically from what a project
documents, never from Aleph's readers: its OpenAPI document's operations ("where is METHOD /path handled") and the
environment names its documentation spells in backticks ("where is NAME read"). Up to 20 of each per repository, by
a fixed seed. The answering files are named afterwards by agents that know nothing of Aleph.

    facts_questions.py SRC_DIR OUT.json
"""
from __future__ import annotations

import json
import random
import re
import subprocess
import sys
from pathlib import Path

import yaml

SEED = 20261016
PER_KIND = 20
METHODS = ("get", "post", "put", "patch", "delete")
ENV_NAME = re.compile(r"`([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)`")

PROJECTS = {   # repository: (language, OpenAPI document or None, environment documentation or None)
    "gotify/server": ("go", "docs/spec.json", None),
    "spring-petclinic/spring-petclinic-rest": ("java", "src/main/resources/openapi.yml", None),
    "immich-app/immich": ("typescript", "open-api/immich-openapi-specs.json", "docs/docs/install/environment-variables.md"),
    "knadh/listmonk": ("go", "docs/swagger/collections.yaml", "docs/docs/content/configuration.md"),
    "zulip/zulip": ("python", "zerver/openapi/zulip.yaml", None),
    "paperless-ngx/paperless-ngx": ("python", None, "docs/configuration.md"),
    "mealie-recipes/mealie": ("python", None, "docs/docs/documentation/getting-started/installation/backend-config.md"),
}


def _leaf(path: str) -> str:
    """The last segment of a path that is not a parameter: what a route fact is asked for by."""
    parts = [part for part in path.strip("/").split("/") if part and not part.startswith("{") and not part.startswith(":")]
    return parts[-1] if parts else path


def questions(src: Path) -> list[dict]:
    rows = []
    for repo, (language, spec, env_doc) in PROJECTS.items():
        root = src / repo.replace("/", "_")
        commit = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        chosen = random.Random(f"{SEED}:{repo}")
        base = {"repo": repo, "base_commit": commit, "language": language, "local": str(root), "set": "facts", "gold": []}
        if spec:
            text = (root / spec).read_text()
            document = json.loads(text) if spec.endswith(".json") else yaml.safe_load(text)
            operations = sorted({(method.upper(), path) for path, item in (document.get("paths") or {}).items()
                                 for method in (item or {}) if method in METHODS})
            for n, (method, path) in enumerate(chosen.sample(operations, min(PER_KIND, len(operations))), 1):
                rows.append({**base, "instance_id": f"{repo.split('/')[1]}-route-{n}", "category": "route",
                             "problem_statement": f"where is the {method} {path} endpoint handled",
                             "name": path, "like": _leaf(path), "method": method})
        if env_doc:
            names = sorted(set(ENV_NAME.findall((root / env_doc).read_text())))
            for n, name in enumerate(chosen.sample(names, min(PER_KIND, len(names))), 1):
                rows.append({**base, "instance_id": f"{repo.split('/')[1]}-env-{n}", "category": "env",
                             "problem_statement": f"where is the {name} environment variable read", "name": name, "like": name})
    return rows


if __name__ == "__main__":
    made = questions(Path(sys.argv[1]))
    Path(sys.argv[2]).write_text(json.dumps(made, indent=1))
    from collections import Counter
    print(len(made), Counter((row["repo"], row["category"]) for row in made))

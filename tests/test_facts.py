"""Architectural facts read from source: routes, models, tables, environment names, tasks, services."""
import json

import pytest

from aleph_zero import cli, lang
from aleph_zero.engine import Engine
from aleph_zero.lang import parse
from aleph_zero.lang.facts import facts, text_facts


def found(path: str, source: str):
    profile = lang.profile_for_path(path)
    if profile is None:
        return [(f.kind, f.name, f.line, f.detail) for f in text_facts(path, source)]
    data = source.encode()
    return [(f.kind, f.name, f.line, f.detail) for f in facts(profile, parse.tree(profile, data), data)]


EXPRESS = '''const axios = require("axios");
function endpoints(app, router) {
  app.get("/v1/users/:id", [validSession], async (req, res) => res.json({}));
  app.post(
    "/v1/users",
    handler
  );
  router.delete(`/v1/users/:id/keys/:keyId`, removeKey);
  // app.get("/v1/commented-out", handler);
  axios.get("/v1/elsewhere", config);                  // a request, not a route
  cache.get("/not/a/route");                           // one argument: a lookup
  app.use("/v1/reports", reportsRouter);
  const key = process.env.API_KEY || process.env["FALLBACK_KEY"];
  const mode = import.meta.env.VITE_MODE;
}
'''


def test_express_routes_are_calls_that_register_a_handler_under_a_literal_path():
    assert found("server/endpoints.js", EXPRESS) == [
        ("route", "GET /v1/users/{id}", 3, "express"),
        ("route", "POST /v1/users", 4, "express"),
        ("route", "DELETE /v1/users/{id}/keys/{keyId}", 8, "express"),
        ("mount", "/v1/reports", 12, "reportsRouter"),               # where a router is mounted: not a route
        ("env", "API_KEY", 13, "read"),
        ("env", "FALLBACK_KEY", 13, "read"),
        ("env", "VITE_MODE", 14, "read"),
    ]


FASTAPI = '''import os
from fastapi import APIRouter, FastAPI
from pydantic_settings import BaseSettings

app = FastAPI()
router = APIRouter(prefix="/workspaces", tags=["w"])

class Settings(BaseSettings):
    redis_url: str = "redis://localhost"
    debug: bool = False

@router.get("/{workspace_id}/layers", response_model=list[Layer])
async def layers(workspace_id: str):
    return os.environ["LAYER_BACKEND"], os.getenv("LAYER_LIMIT", "5"), os.environ.get("LAYER_MODE")

@app.post("/status")
def status():
    pass

@celery_app.task(name="service.tasks.reindex", bind=True)
def reindex(self):
    pass

@shared_task
def cleanup():
    pass

class Layer(Base):
    __tablename__ = "layers"

def upgrade():
    op.create_table("layer_quotas", sa.Column("id", sa.Integer))
'''


def test_python_routes_settings_tasks_and_tables():
    assert found("service/app/api.py", FASTAPI) == [
        ("env", "REDIS_URL", 9, "settings field"),
        ("env", "DEBUG", 10, "settings field"),
        ("route", "GET /workspaces/{workspace_id}/layers", 12, "fastapi"),
        ("env", "LAYER_BACKEND", 14, "read"),
        ("env", "LAYER_LIMIT", 14, "read"),
        ("env", "LAYER_MODE", 14, "read"),
        ("route", "POST /status", 16, "fastapi"),
        ("task", "service.tasks.reindex", 20, "celery"),
        ("task", "cleanup", 24, "celery"),
        ("model", "Layer", 28, "table layers"),
        ("table", "layer_quotas", 32, "alembic"),
    ]


@pytest.mark.parametrize("path,source,expected", [
    ("cmd/main.go", 'package main\nfunc main() {\n\tmux.HandleFunc("/healthz", health)\n\tmux.HandleFunc("POST /v1/jobs", create)\n'
                    '\tr.Get("/v1/jobs/{id}", get)\n\taddr := os.Getenv("GATE_ADDR")\n\tv, ok := os.LookupEnv("GATE_MODE")\n\tm.Get("key")\n}\n',
     [("route", "ANY /healthz", 3, "go"), ("route", "POST /v1/jobs", 4, "go"), ("route", "GET /v1/jobs/{id}", 5, "go"),
      ("env", "GATE_ADDR", 6, "read"), ("env", "GATE_MODE", 7, "read")]),
    ("src/main.rs", 'fn main() {\n    let home = std::env::var("APP_HOME").unwrap();\n    let x = env::var_os("APP_X");\n}\n',
     [("env", "APP_HOME", 2, "read"), ("env", "APP_X", 3, "read")]),
    ("src/Owner.java", '@Entity\n@Table(name = "owners")\npublic class Owner {}\n\n@RequestMapping("/owners")\nclass OwnerController {\n'
                       '  @GetMapping("/{ownerId}")\n  public String show() { return System.getenv("PETCLINIC_MODE"); }\n'
                       '  @PostMapping(value = "/new")\n  public String create() { return ""; }\n}\n',
     [("model", "Owner", 3, "entity"), ("route", "GET /owners/{ownerId}", 7, "spring"), ("env", "PETCLINIC_MODE", 8, "read"),
      ("route", "POST /owners/new", 9, "spring")]),
    ("prisma/schema.prisma", "generator client {\n  provider = \"prisma-client-js\"\n}\n\nmodel workspaces {\n  id Int @id\n}\n\nmodel api_keys {\n  id Int @id\n}\n",
     [("model", "workspaces", 5, "prisma"), ("model", "api_keys", 9, "prisma")]),
    ("server/.env.example", "# comment\nSERVER_PORT=3001\n# JWT_SECRET='x'\n\nlower=ignored\n",
     [("env", "SERVER_PORT", 2, "declared"), ("env", "JWT_SECRET", 3, "declared")]),
    ("proto/gate.proto", 'syntax = "proto3";\nservice Gate {\n  rpc Check (CheckRequest) returns (CheckReply);\n  rpc Watch(stream A) returns (stream B) {}\n}\n',
     [("service", "Gate", 2, "grpc"), ("rpc", "Gate.Check", 3, "grpc"), ("rpc", "Gate.Watch", 4, "grpc")]),
])
def test_the_other_languages_and_the_files_that_are_not_code(path, source, expected):
    assert found(path, source) == expected


def project(repo):
    repo.write("server/endpoints.js", EXPRESS)
    repo.write("service/app/api.py", FASTAPI)
    repo.write("prisma/schema.prisma", "model workspaces {\n  id Int @id\n}\n")
    repo.write("server/.env.example", "API_KEY=\nUNUSED_NAME=\n")
    return Engine(repo.root)


def test_facts_are_read_when_first_asked_and_answered_by_kind_and_name(repo):
    from aleph_zero.store import index_path, open_db

    engine = project(repo)
    engine.search("refresh token")
    conn = open_db(index_path(repo.root))
    assert conn.execute("SELECT count(*) FROM arch_facts").fetchall() == [(0,)]          # searching pays nothing
    answer = engine.facts()
    assert answer.pending == 0 and answer.counts["route"] == 5 and answer.counts["model"] == 2 and answer.counts["task"] == 2
    routes = engine.facts("route")
    assert [(f.name, f.path, f.line) for f in routes.facts][:2] == [("DELETE /v1/users/{id}/keys/{keyId}", "server/endpoints.js", 8),
                                                                    ("GET /v1/users/{id}", "server/endpoints.js", 3)]
    api_key = engine.facts("env", like="API_KEY")
    assert [(f.path, f.detail) for f in api_key.facts] == [("server/.env.example", "declared"), ("server/endpoints.js", "read")]
    repo.write("server/endpoints.js", "function e(app) { app.get(\"/only\", h); }\n")     # edited, not committed
    assert [f.name for f in engine.facts("route", like="/only").facts] == ["GET /only"]
    assert engine.facts("route", like="/v1/users").facts == []                            # the old file's routes are gone


def test_facts_on_the_command_line(repo, capsys):
    project(repo).close()
    assert cli.main(["--cwd", str(repo.root), "facts"]) == 0
    assert "route  5" in capsys.readouterr().out
    assert cli.main(["--cwd", str(repo.root), "facts", "model", "--json"]) == 0
    assert sorted(item["name"] for item in json.loads(capsys.readouterr().out)["facts"]) == ["Layer", "workspaces"]


def test_an_index_of_the_schema_before_gets_the_table_in_place(repo):
    from aleph_zero.store import SCHEMA_VERSION, index_path, open_db

    engine = project(repo)
    engine.references("endpoints")
    engine.close()
    conn = open_db(index_path(repo.root))
    read = conn.execute("SELECT count(*) FROM graph_facts").fetchall()
    conn.execute("DROP TABLE arch_facts")
    conn.pragma("user_version", 8)
    conn.close()
    conn = open_db(index_path(repo.root))
    assert conn.pragma("user_version") == SCHEMA_VERSION == 9
    assert conn.execute("SELECT count(*) FROM graph_facts").fetchall() == read and read != [(0,)]
    assert Engine(repo.root).facts().counts["route"] == 5


def test_what_cannot_be_read_yet_is_said_not_hidden(repo, monkeypatch):
    from aleph_zero import archfacts

    engine = project(repo)
    monkeypatch.setattr(archfacts, "_now", iter(range(0, 10_000, 100)).__next__)       # the budget runs out at once
    answer = engine.facts(prepare_seconds=1)
    assert answer.unread == 6 and answer.counts == {}
    monkeypatch.undo()
    assert engine.facts().unread == 0


def test_a_file_that_is_not_one_of_these_is_not_read_for_facts(repo):
    from aleph_zero.store import index_path, open_db

    repo.write("notes/config.yaml", "service Gate {\n}\nAPI_KEY=1\n")
    engine = project(repo)
    assert engine.facts().counts.get("service") is None
    conn = open_db(index_path(repo.root))
    assert conn.execute("SELECT count(*) FROM arch_facts WHERE family = 'text'").fetchall() == [(2,)]   # the schema and the example file


def test_environment_names_read_in_other_shapes_and_those_that_are_set():
    source = (
        "const token = process?.env?.ASTRA_TOKEN;\n"
        "const on = \"LOGIN_BANNER\" in process.env || false;\n"
        "const { PORT, HOST: host, ...rest } = process.env;\n"
        "process.env.AUTH_TOKEN = \"\";\n"
        "const other = \"name\" in options;\n"
    )
    assert found("a.js", source) == [("env", "ASTRA_TOKEN", 1, "read"), ("env", "LOGIN_BANNER", 2, "read"),
                                     ("env", "PORT", 3, "read"), ("env", "HOST", 3, "read"), ("env", "AUTH_TOKEN", 4, "set")]
    python = (
        "import os\n"
        "if \"DEBUG_SQL\" in os.environ:\n"
        "    os.environ[\"TZ\"] = \"UTC\"\n"
        "os.environ.setdefault(\"LANG\", \"C\")\n"
        "if \"x\" in names: pass\n"
    )
    assert found("a.py", python) == [("env", "DEBUG_SQL", 2, "read"), ("env", "TZ", 3, "set"), ("env", "LANG", 4, "set")]


def test_where_a_router_is_mounted_is_a_fact_of_its_own():
    assert found("a.js", "app.use(\"/api\", apiRouter);\napp.use(express.json());\napp.use(\"/static\", express.static(dir));\n") == [
        ("mount", "/api", 1, "apiRouter"), ("mount", "/static", 3, "express.static(dir)")]
    python = "app.include_router(workspace.router, prefix=\"/workspace\", tags=[\"w\"])\napp.include_router(health.router)\napp.register_blueprint(admin, url_prefix=\"/admin\")\n"
    assert found("a.py", python) == [("mount", "/workspace", 1, "workspace.router"), ("mount", "/admin", 3, "admin")]
    assert found("a.go", "package a\nfunc f(r chi.Router) {\n\tr.Mount(\"/v1\", api())\n\tr.Route(\"/jobs\", func(r chi.Router) {})\n}\n") == [
        ("mount", "/v1", 3, "api()"), ("mount", "/jobs", 4, "")]

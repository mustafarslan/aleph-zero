"""Architectural facts read from source: routes, models, tables, environment names, tasks, services."""
import json

import pytest

from aleph_zero import cli, lang
from aleph_zero.archfacts import text_facts
from aleph_zero.engine import Engine
from aleph_zero.lang import parse
from aleph_zero.lang.facts import facts


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
        ("route", "POST /v1/users", 4, "express handler"),
        ("route", "DELETE /v1/users/{id}/keys/{keyId}", 8, "express removeKey"),
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
     [("route", "ANY /healthz", 3, "go health"), ("route", "POST /v1/jobs", 4, "go create"), ("route", "GET /v1/jobs/{id}", 5, "go get"),
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
    assert conn.pragma("user_version") == SCHEMA_VERSION
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


def test_a_blob_that_is_collected_takes_its_facts_with_it(repo):
    from aleph_zero.indexer import collect_garbage
    from aleph_zero.store import index_path, open_db

    engine = project(repo)
    assert engine.facts().counts["route"] == 5
    conn = open_db(index_path(repo.root))
    (oid,), = conn.execute("SELECT blob_oid FROM arch_facts WHERE family = 'javascript'").fetchall()
    (repo.root / "server/endpoints.js").unlink()
    assert engine.facts().counts["route"] == 2
    collect_garbage(conn, max_age_days=0, now=2**40)
    assert conn.execute("SELECT count(*) FROM arch_facts WHERE blob_oid = ?", (oid,)).fetchall() == [(0,)]


def test_broken_source_and_windows_line_ends_still_give_what_can_be_read(repo):
    repo.write("a.py", "import os\r\n\r\nKEY = os.getenv(\"A_KEY\")\r\n\r\n@app.get(\"/a\")\r\ndef a(: ...\r\n")
    repo.write("b.js", "app.get(\"/b\", handler);\nfunction (((( {\n")
    engine = Engine(repo.root)
    assert [(f.name, f.path, f.line) for f in engine.facts("env").facts] == [("A_KEY", "a.py", 3)]
    assert [(f.name, f.line) for f in engine.facts("route").facts] == [("GET /a", 5), ("GET /b", 1)]


def test_queues_named_where_work_is_handed_over():
    python = (
        "@celery_app.task(name=\"audio.transcribe\", queue=\"audio\")\n"
        "def transcribe(): ...\n"
        "def worker(r):\n"
        "    _, job = r.blpop(\"audio_jobs\")\n"
        "    r.publish(channel_name, job)\n"
    )
    assert found("a.py", python) == [("task", "audio.transcribe", 1, "celery"), ("queue", "audio", 1, "celery"),
                                     ("queue", "audio_jobs", 4, "blpop")]
    assert found("a.ts", "await redis.lpush('audio_jobs', JSON.stringify(job));\nstream.subscribe((value) => use(value));\nredisPublisher.publish(\"orders.created\", order);\n"
                          "pubsub.publish(\"userLoggedIn\", data);\nemitter.subscribe(\"tick\", fn);\nsocket.subscribe(\"room:1\");\n") == [
        ("queue", "audio_jobs", 1, "lpush"), ("queue", "orders.created", 3, "publish")]       # an event bus inside the process is not a broker


OXN_YAML = '''# what may depend on what
retry_budget: 3
layers:
  surfaces: ["src/app/cli.py", "src/app/server.py"]   # outermost
  analysis: ["src/app/metrics/*",
             "src/app/rules/*"]
  foundation:
    - "src/app/config.py"
    - src/app/render.py
  tests: ["tests/*"]
contracts:
  - name: outside-in
    kind: layered
    order: [surfaces, analysis, foundation]
  - name: no-cycles
    kind: acyclic
exclude:
  - "vendor/*"
'''


def test_layers_and_contracts_of_an_oxn_yaml_are_facts():
    assert found("oxn.yaml", OXN_YAML) == [
        ("layer", "surfaces", 4, "src/app/cli.py, src/app/server.py"),
        ("layer", "analysis", 5, "src/app/metrics/*, src/app/rules/*"),
        ("layer", "foundation", 7, "src/app/config.py, src/app/render.py"),
        ("layer", "tests", 10, "tests/*"),
        ("contract", "outside-in", 12, "layered: surfaces > analysis > foundation"),
        ("contract", "no-cycles", 15, "acyclic"),
    ]
    block = "contracts:\n- name: a\n  kind: layered\n  order:\n  - x\n  - y\n- name: b\n  kind: layered\n  order: [y, z]\n"
    assert found("oxn.yaml", block) == [("contract", "a", 2, "layered: x > y"), ("contract", "b", 7, "layered: y > z")]
    assert found("oxn.yaml", "layers: {a: [b]}\n: : :\n\t- x\n") == []          # a form the reader does not know states nothing
    assert found("config/other.yaml", OXN_YAML) == []


def test_flask_converters_are_written_as_any_other_parameter():
    source = ('bp = Blueprint("u", __name__)\n@bp.route("/u/<int:uid>/<path:rest>")\ndef u(uid, rest): ...\n'
              '@bp.route("/t/<uuid:id>", methods=["POST"])\ndef t(id): ...\n')
    assert found("a.py", source) == [("route", "GET /u/{uid}/{rest}", 2, "flask"), ("route", "POST /t/{id}", 4, "flask")]


def test_a_lookup_or_a_request_is_not_a_route():
    script = (
        'const svc = axios.create();\n'
        'svc.get("/users", cfg);\n'
        'backend.delete("/users/1", opts);\n'
        'sftp.get("/remote", "/local");\n'
        'map.get("/k", fallback);\n'
        'apiClient.get("/v1/things", requestConfig);\n'
        'const admin = express.Router();\n'
        'admin.get("/admin", list);\n'
        'function mount(apiRouter, server) { apiRouter.post("/x", make); server.put("/y", put); }\n'
        'const site = new Hono();\n'
        'site.get("/z", show);\n'
    )
    assert found("a.js", script) == [("route", "GET /admin", 8, "express list"), ("route", "POST /x", 9, "express make"),
                                     ("route", "PUT /y", 9, "express put"), ("route", "GET /z", 11, "express show")]
    go = ('package a\nfunc f() {\n\tkv.Get("/foo", nil)\n\tconn.Get("/path", true)\n\tcache.Get("/k", 1)\n'
          '\thttp.Post("/api", "application/json", body)\n\tclient.Get("/x", opts)\n\tr.Get("/jobs", h.list)\n\tv1.GET("/users", listUsers)\n}\n')
    assert found("a.go", go) == [("route", "GET /jobs", 8, "go h.list"), ("route", "GET /users", 9, "go listUsers")]
    python = ('@client.get("/x")\ndef a(): ...\n@cache.get("/x")\ndef b(): ...\n@retry.route("/y")\ndef c(): ...\n'
              'service = FastAPI()\n@service.get("/ok")\ndef d(): ...\n@users_router.post("/u")\ndef e(): ...\n')
    assert found("a.py", python) == [("route", "GET /ok", 8, "fastapi"), ("route", "POST /u", 10, "fastapi")]


def test_a_mapping_called_environ_is_not_the_process_environment():
    wsgi = 'def application(environ, start_response):\n    path = environ.get("PATH_INFO")\n    host = environ["HTTP_HOST"]\n'
    assert found("a.py", wsgi) == []
    assert found("a.py", 'from os import environ, getenv\nA = environ["A_KEY"]\nB = getenv("B_KEY")\n') == [
        ("env", "A_KEY", 2, "read"), ("env", "B_KEY", 3, "read")]
    assert found("a.py", "class T(MyBaseSettingsBase):\n    z: int = 1\n") == []


def test_a_name_set_and_read_on_one_line_is_both():
    assert found("a.js", 'process.env.PORT = process.env.PORT || "3000";\n') == [("env", "PORT", 1, "set"), ("env", "PORT", 1, "read")]
    assert found("a.py", 'import os\nos.environ["TZ"] = os.environ.get("TZ", "UTC")\n') == [("env", "TZ", 2, "set"), ("env", "TZ", 2, "read")]


def test_the_routes_and_environment_names_a_text_names():
    from aleph_zero.archfacts import Mention, mentioned

    assert mentioned("where is the POST /client/{id}/elevate endpoint handled") == [Mention("route", "POST /client/{id}/elevate")]
    assert mentioned("GET http://localhost:8080/api/users/12 returns 500") == [Mention("route", "GET /api/users/12")]
    assert mentioned("calling `/api/auth/login` from curl http://127.0.0.1:3000/v1/jobs?x=1 fails") == [
        Mention("route", "/api/auth/login"), Mention("route", "/v1/jobs")]
    assert mentioned("DB_USERNAME is ignored, and so is MAX_RETRIES") == [Mention("env", "DB_USERNAME"), Mention("env", "MAX_RETRIES")]
    # A file's path, a path of the machine, a page on the web, a single segment, a call: none is a route or a name.
    assert mentioned("in /Users/me/proj/app.py, /usr/local/bin/x, src/app/main.py, https://github.com/a/b/issues/1, /health,"
                     " os.getenv(FOO_BAR) is fine but $HOME_DIR and get_THING_X() are not") == [Mention("env", "FOO_BAR")]
    assert mentioned("A B C " + " ".join(f"NAME_{n}" for n in range(9))) == [Mention("env", f"NAME_{n}") for n in range(5)]


def test_what_a_request_names_is_answered_from_source_code_with_the_handler(repo):
    repo.write("router/router.go", 'package router\nfunc Create(g *gin.Engine) {\n\tclient := g.Group("/client")\n'
               '\tg.DELETE("/client/:id", clientHandler.DeleteClient)\n\tg.POST("/client/:id/elevate", clientHandler.Elevate)\n'
               '\tg.GET("/message", messageHandler.List)\n}\n')
    repo.write("api/client.go", "package api\n\ntype ClientAPI struct{}\n\nfunc (a *ClientAPI) DeleteClient(ctx *gin.Context) {}\n")
    repo.write("api/client_test.go", "package api\n\nfunc DeleteClient() {}\n")
    repo.write("config/db.go", 'package config\nfunc url() string { return os.Getenv("DB_URL") }\n')
    repo.write("docs/setup.md", "Set `DB_URL`.\n")
    repo.write(".env.example", "DB_URL=\n")
    engine = Engine(repo.root)
    answer = engine.facts(mentioned="DELETE http://localhost:8080/client/7 fails, and DB_URL is not read; MAX_SIZE too")
    assert answer.like == "DELETE /client/7 | DB_URL | MAX_SIZE"
    assert [(f.kind, f.name, f.path, f.line) for f in answer.facts] == [
        ("route", "DELETE /client/{id}", "router/router.go", 4),                 # not the elevate route, nor the GET
        ("mount", "/client", "router/router.go", 3),
        ("env", "DB_URL", "config/db.go", 2),                                    # not the documentation or the example file
        ("handler", "DELETE /client/{id}", "api/client.go", 5),                  # the test's function of that name is not it
    ]
    assert engine.facts(mentioned="the /client/{id} route").facts[0].name == "DELETE /client/{id}"
    nothing = engine.facts(mentioned="rename the helper in utils")
    assert (nothing.like, nothing.facts, nothing.counts) == ("", [], {})        # answered without the index


def test_a_handler_wrapped_in_calls_is_read_by_its_innermost_name():
    go = ('package a\nfunc f(g *echo.Group, a *App) {\n\tg.GET("/api/subscribers/:id", pm(hasID(a.GetSubscriber), "subscribers:get"))\n'
          '\tg.GET("/auth/login", oidcHandler.LoginHandler())\n\tg.GET("/x", func(c echo.Context) error { return nil })\n}\n')
    assert found("a.go", go) == [("route", "GET /api/subscribers/{id}", 3, "go a.GetSubscriber"),
                                 ("route", "GET /auth/login", 4, "go oidcHandler.LoginHandler"),
                                 ("route", "GET /x", 5, "go")]                 # a function written in place has no name


def test_go_router_groups_made_in_the_file_give_their_routes_the_group_s_path():
    go = ('package router\nfunc Create(g *gin.Engine, conf *Config) {\n'
          '\tclientAuth := g.Group("")\n'
          '\t{\n\t\tapp := clientAuth.Group("/application")\n'
          '\t\tapp.GET("", applicationHandler.GetApplications)\n'
          '\t\tapp.PUT("/:id", applicationHandler.UpdateApplication)\n'
          '\t\ttokenMessage := app.Group("/:id/message")\n'
          '\t\ttokenMessage.DELETE("", messageHandler.DeleteMessageWithApplication)\n\t}\n'
          '\tg.Group("/user").Use(authentication.OptionalAdmin).POST("", userHandler.CreateUser)\n'
          '\tclient := g.Group(conf.Prefix).Group("client")\n'
          '\tclient.GET("current", clientHandler.Current)\n'
          '\tcache.Get("/k", fallback)\n}\n')
    routes = [(name, line, detail) for kind, name, line, detail in found("router/router.go", go) if kind == "route"]
    assert routes == [("GET /application", 6, "go applicationHandler.GetApplications"),
                      ("PUT /application/{id}", 7, "go applicationHandler.UpdateApplication"),
                      ("DELETE /application/{id}/message", 9, "go messageHandler.DeleteMessageWithApplication"),
                      ("POST /user", 11, "go userHandler.CreateUser"),
                      ("GET /client/current", 13, "go clientHandler.Current")]     # a configured prefix adds nothing
    mounts = [name for kind, name, _, _ in found("router/router.go", go) if kind == "mount"]
    assert mounts == ["/application", "/application/{id}/message", "/user", "/client"]


def test_nestjs_routes_join_the_controller_s_path_and_the_method_s():
    ts = ("import { Controller, Get, Post, Delete } from '@nestjs/common';\n"
          "@Controller('albums')\nexport class AlbumController {\n  @Get()\n  list() {}\n\n  @Get(':id/assets')\n  assets() {}\n\n"
          "  @Post()\n  create() {}\n  @Delete(':id')\n  remove() {}\n  @Get(RoutePath.Stats)\n  computed() {}\n}\n"
          "@Controller({ path: 'users', version: '1' })\nclass UserController {\n  @Get('me')\n  me() {}\n}\n")
    assert [fact for fact in found("server/album.controller.ts", ts) if fact[0] == "route"] == [
        ("route", "GET /albums", 4, "nestjs"), ("route", "GET /albums/{id}/assets", 7, "nestjs"),
        ("route", "POST /albums", 10, "nestjs"), ("route", "DELETE /albums/{id}", 12, "nestjs"),
        ("route", "GET /users/me", 19, "nestjs")]                                   # a computed path is not read
    elsewhere = ts.replace("'@nestjs/common'", "'./decorators'")
    assert [fact for fact in found("server/album.controller.ts", elsewhere) if fact[0] == "route"] == []   # not NestJS's


def test_django_url_patterns_are_routes_and_includes_are_mounts():
    py = ('from django.urls import include, path, re_path\nfrom . import views\n\n'
          'check_urls = [\n    path("", views.details, name="details"),\n    path("log/", views.log),\n]\n\n'
          'urlpatterns = [\n    path("", views.index),\n    path("checks/<uuid:code>/", include(check_urls)),\n'
          '    path("api/", include("hc.api.urls")),\n    re_path(r"^ping/(?P<code>[^/]+)/?$", views.ping),\n'
          '    re_path(r"^b(a|o)d/$", views.bad),\n    path(f"{prefix}admin/", views.admin),\n'
          '    path("projects/", views.ProjectList.as_view()),\n]\n')
    assert found("hc/front/urls.py", py) == [
        ("route", "ANY /checks/{code}", 5, "django views.details"), ("route", "ANY /checks/{code}/log/", 6, "django views.log"),
        ("route", "ANY /", 10, "django views.index"), ("mount", "/checks/{code}/", 11, "check_urls"),
        ("mount", "/api/", 12, '"hc.api.urls"'), ("route", "ANY /ping/{code}/", 13, "django views.ping"),
        ("route", "ANY /projects/", 16, "django views.ProjectList")]            # a regex that is not a path, a computed prefix: not read
    assert found("hc/front/helpers.py", py.replace("from django.urls import include, path, re_path\n", "")) == []

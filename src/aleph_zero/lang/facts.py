"""Architectural facts a file states: the routes it serves, the models and tables it declares,
the environment names it reads, the tasks and services it defines.

Each is read from the syntax, where the file says it in so many words: a call that registers
a handler under a literal path, a decorator, an annotation, a class with a table name. What
only looks like one is left out: a commented-out route is not syntax, a request made with an
HTTP client is not a route served, a `get("/x")` with nothing to handle it is a lookup. A path
built at run time has no literal to read and is not reported.

Whether `x.get("/users", y)` registers a route or looks something up is not in the syntax of
the call. It is read as a route only when `x` is known for a router: the file makes it one
(`express()`, `Router()`, `new Hono()`; `FastAPI()`, `APIRouter()`, `Blueprint()`), or its name
says so (`app`, `router`, `server`, `apiRouter`, `users_router`, `bp`). A router under another
name that reaches the file as an argument is missed: a route not reported is a smaller error
than a key-value lookup reported as an endpoint. In Go, where the methods are `Get` and `Post`
on any type, a route needs something that can be a handler after the path, and a holder that
is not named for a client or a store. `publish` and `subscribe` are read as a broker's only on
an object named for one (`redis`, `nats`, `mqtt`, `kafka`, `amqp`): an event bus inside the
process has the same methods.

A route's path is as written where it is registered, with the prefix its own file gives the
router. A prefix given where a router is mounted (`app.use("/api", router)`,
`include_router(router, prefix="/x")`) is a fact of its own, a `mount`: which routes pass
through it is often decided by a router handed from one file to another, which the syntax of
neither says, so the two are not joined here.

Frameworks read: Express and its relatives (JavaScript, TypeScript), FastAPI and Flask
(Python), net/http, chi and gin (Go), Spring (Java); SQLAlchemy, Django, Alembic, JPA, Prisma;
Celery; gRPC. Environment reads in JavaScript, Python, Go, Rust and Java, and the names an
example environment file declares.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from tree_sitter import Node, Tree

from . import query
from .profile import LanguageProfile


@dataclass(frozen=True)
class Fact:
    kind: str       # 'route', 'mount', 'model', 'table', 'env', 'task', 'queue', 'service', 'rpc', 'layer', 'contract'
    name: str       # route: 'GET /v1/users/{id}'; mount: the prefix; env: 'DATABASE_URL'; rpc: 'Service.Method'; the others: its name
    line: int       # 1-based
    # route: the framework; mount: what is mounted, as written; env: 'read', 'set', 'declared', 'settings field';
    # model: where its table is said; queue: the call that names it; layer: its paths; contract: its kind and order
    detail: str = ""


_ECMA = frozenset({"typescript", "tsx", "javascript"})
_METHODS = {"get": "GET", "post": "POST", "put": "PUT", "delete": "DELETE", "patch": "PATCH", "head": "HEAD", "options": "OPTIONS"}
# What a router is called when nothing in the file says what it is, and what makes one.
_ROUTER_ENDINGS = ("app", "router", "routes", "server")
_PY_ROUTER_ENDINGS = ("app", "router", "routes", "bp", "blueprint", "api")
_ECMA_ROUTER_MAKERS = frozenset({"express", "Router", "fastify", "Hono", "Koa", "polka", "createServer"})
_PY_ROUTER_MAKERS = frozenset({"FastAPI", "APIRouter", "Flask", "Blueprint", "Sanic", "Starlette", "Quart", "APIFlask", "Bottle"})
# In Go any type may have a `Get(string, …)`: holders that are a client or a store by their name.
_GO_NOT_ROUTERS = frozenset({"http", "client", "kv", "cache", "store", "conn", "db", "redis", "rdb", "consul", "etcd", "zk", "s3"})
_GO_HANDLERS = frozenset({"identifier", "selector_expression", "func_literal", "call_expression"})
# A handler given by its name, which `facts` can then find the definition of.
_NAMED_HANDLERS = frozenset({"identifier", "selector_expression", "member_expression"})
_BROKER = re.compile(r"redis|nats|mqtt|kafka|amqp|rabbit|broker", re.IGNORECASE)
_BROKER_ONLY = frozenset({"publish", "subscribe", "psubscribe"})     # methods an in-process event bus has too
_OS_IMPORT = re.compile(rb"^[ \t]*from[ \t]+os[ \t]+import[ \t]+\(?([^\n)]*)", re.MULTILINE)
_GO_ROUTES = {"HandleFunc": "ANY", "Handle": "ANY", "Get": "GET", "Post": "POST", "Put": "PUT", "Delete": "DELETE", "Patch": "PATCH",
              "GET": "GET", "POST": "POST", "PUT": "PUT", "DELETE": "DELETE", "PATCH": "PATCH"}
_SPRING = {"GetMapping": "GET", "PostMapping": "POST", "PutMapping": "PUT", "DeleteMapping": "DELETE", "PatchMapping": "PATCH",
           "RequestMapping": "ANY"}
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# A Redis list, channel or stream named where work is handed over. `xreadgroup` names its stream late: not read.
_QUEUE_CALLS = frozenset({"lpush", "rpush", "blpop", "brpop", "publish", "subscribe", "psubscribe", "xadd"})
_QUEUE_NAME = re.compile(r"^[\w.:/*-]+$")
_GO_MOUNTS = frozenset({"Mount", "Route", "Group", "PathPrefix"})
_ENVIRONMENT = ("process.env", "import.meta.env")
_WRITTEN_LIMIT = 80
_GO_PATTERN = re.compile(r"^(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\s+(/.*)$")
_KINDS = {
    "ecma": ("call_expression", "new_expression", "member_expression", "subscript_expression"),
    "python": ("decorator", "call", "subscript", "class_definition", "assignment", "comparison_operator"),
    "go": ("call_expression", "short_var_declaration", "assignment_statement"),
    "rust": ("call_expression",),
    "java": ("annotation", "marker_annotation", "method_invocation"),
}


def facts(profile: LanguageProfile, tree: Tree, data: bytes) -> list[Fact]:
    """The facts of one source file, in source order."""
    family = "ecma" if profile.name in _ECMA else profile.name
    kinds = _KINDS.get(family)
    if kinds is None:
        return []
    nodes = sorted(query.find(profile.name, tree, kinds), key=lambda node: (node.start_byte, -node.end_byte))
    found: list[tuple[int, Fact]] = []
    read = {"ecma": _ecma, "python": _python, "go": _go, "rust": _rust, "java": _java}[family]
    # `from os import environ, getenv`: only then is a bare `environ` the process's (a WSGI handler has one too).
    state: dict = {"os_names": {name.split(b" as ")[-1].strip().decode("utf-8", "replace")
                                for found in _OS_IMPORT.findall(data) for name in found.split(b",")} if family == "python" else set()}
    for node in nodes:
        for fact in read(node, state):
            found.append((node.start_byte, fact))
    seen: set[Fact] = set()        # the same fact from two nodes is one; a name set and read on one line is two
    out = []
    for _, fact in sorted(found, key=lambda item: (item[1].line, item[0])):
        if fact not in seen:
            seen.add(fact)
            out.append(fact)
    return out


def text_facts(path: str, text: str) -> list[Fact]:
    """The facts of a file that is not source code but states them: a Prisma schema, an example
    environment file, a protocol-buffers service definition."""
    name = path.rsplit("/", 1)[-1].lower()
    out: list[Fact] = []
    if name.endswith(".prisma"):
        out += [Fact("model", match.group(1), text.count("\n", 0, match.start()) + 1, "prisma")
                for match in re.finditer(r"^model\s+(\w+)\s*\{", text, re.MULTILINE)]
    elif is_environment_example(name):
        for number, line in enumerate(text.split("\n"), 1):
            declared = re.match(r"\s*#?\s*(?:export\s+)?([A-Z][A-Z0-9_]{2,})\s*=", line)
            if declared:
                out.append(Fact("env", declared.group(1), number, "declared"))
    elif name.endswith(".proto"):
        service = ""
        for number, line in enumerate(text.split("\n"), 1):
            opened = re.match(r"\s*service\s+(\w+)\s*\{?", line)
            method = re.match(r"\s*rpc\s+(\w+)\s*\(", line)
            if opened:
                service = opened.group(1)
                out.append(Fact("service", service, number, "grpc"))
            elif method and service:
                out.append(Fact("rpc", f"{service}.{method.group(1)}", number, "grpc"))
    return out


def is_environment_example(name: str) -> bool:
    return bool(re.search(r"(^|\.)env\.(example|sample|template|dist|defaults?)$|^(example|sample)\.env$", name.lower()))


def states_facts(path: str) -> bool:
    """Whether a file that is not source code is one `text_facts` reads."""
    name = path.rsplit("/", 1)[-1].lower()
    return name.endswith((".prisma", ".proto")) or is_environment_example(name)


# ---- helpers ---------------------------------------------------------------------------------------

def _text(node: Node | None) -> str:
    return node.text.decode("utf-8", "replace") if node is not None and node.text else ""


def _literal(node: Node | None) -> str | None:
    """The text of a string literal with nothing computed in it; None for anything else."""
    if node is None:
        return None
    if node.type in ("string", "interpreted_string_literal", "raw_string_literal", "string_literal", "template_string"):
        if any(child.type in ("template_substitution", "interpolation", "format_expression") for child in node.named_children):
            return None
        text = _text(node)
        prefix = re.match(r"^[rbuRBU]*", text).group(0) if node.type == "string" else ""
        if "f" in prefix.lower():
            return None
        text = text[len(prefix):]
        for quote in ('"""', "'''", '"', "'", "`"):
            if text.startswith(quote) and text.endswith(quote) and len(text) >= 2 * len(quote):
                return text[len(quote):-len(quote)]
        return None
    return None


def _path(written: str) -> str:
    """A route's path as every framework's reader would compare it: `:id` and `<id>` as `{id}`."""
    written = re.sub(r"<(?:\w+:)?(\w+)>", r"{\1}", written)       # first: Flask's `<int:id>` has a colon of its own
    return re.sub(r":([A-Za-z_]\w*)", r"{\1}", written)


def _join(prefix: str, path: str) -> str:
    if not prefix:
        return path or "/"
    return prefix.rstrip("/") + ("/" + path.lstrip("/") if path.strip("/") else "")


def _line(node: Node) -> int:
    return node.start_point[0] + 1


def _written(node: Node | None) -> str:
    """An expression as written, on one line and not long."""
    return " ".join(_text(node).split())[:_WRITTEN_LIMIT]


def _handler_name(node: Node | None, depth: int = 0) -> Node | None:
    """The name a handler is given by: itself when it is a name; inside calls that wrap it (`pm(hasID(a.GetSubscriber))`),
    the first name among their arguments, innermost first; for a call with none (`oidcHandler.LoginHandler()`), the name
    of what is called, the function that makes the handler."""
    if node is None or depth > 4:
        return None
    if node.type in _NAMED_HANDLERS:
        return node
    if node.type == "call_expression":
        arguments = node.child_by_field_name("arguments")
        for argument in (arguments.named_children if arguments is not None else []):
            found = _handler_name(argument, depth + 1)
            if found is not None:
                return found
        function = node.child_by_field_name("function")
        return function if function is not None and function.type in _NAMED_HANDLERS else None
    return None


def _handled(framework: str, handler: Node | None) -> str:
    """A route's detail: its framework and, where the handler is given by a name (`clientHandler.DeleteClient`), that name."""
    named = _handler_name(handler)
    return f"{framework} {_written(named)}" if named is not None else framework


def _is(node: Node | None, other: Node | None) -> bool:
    return node is not None and other is not None and (node.start_byte, node.end_byte) == (other.start_byte, other.end_byte)


def _assigned(node: Node, kinds: tuple[str, ...]) -> bool:
    """Whether the node is what an assignment assigns to."""
    parent = node.parent
    return parent is not None and parent.type in kinds and _is(parent.child_by_field_name("left"), node)


# ---- JavaScript and TypeScript -----------------------------------------------------------------------

def _ecma(node: Node, state: dict) -> list[Fact]:
    kind = node.type
    if kind in ("call_expression", "new_expression"):
        function, arguments = node.child_by_field_name("function" if kind == "call_expression" else "constructor"), node.child_by_field_name("arguments")
        if function is not None and _text(function).rsplit(".", 1)[-1] in _ECMA_ROUTER_MAKERS:
            made = node.parent        # const admin = express.Router()
            named = made.child_by_field_name("name" if made.type == "variable_declarator" else "left") \
                if made is not None and made.type in ("variable_declarator", "assignment_expression") else None
            if named is not None and named.type == "identifier":
                state.setdefault("routers", set()).add(_text(named))
        if kind == "new_expression" or function is None or arguments is None or function.type != "member_expression":
            return []
        holder, called = function.child_by_field_name("object"), _text(function.child_by_field_name("property"))
        method = _METHODS.get(called)
        given = [argument for argument in arguments.named_children if argument.type != "comment"]
        if called in _QUEUE_CALLS:
            return _queue(node, called, _literal(given[0]) if given else None, _text(holder))
        if holder is None or holder.type != "identifier" or len(given) < 2 \
                or not (_text(holder) in state.get("routers", ()) or _text(holder).lower().endswith(_ROUTER_ENDINGS)):
            return []
        path = _literal(given[0])
        if called == "use" and path is not None and path.startswith("/"):
            return [Fact("mount", _path(path), _line(node), _written(given[-1]))]
        if method is None:
            return []
        if path is None or not path.startswith("/") or given[-1].type == "object":
            return []       # a handler is a function or a name, never an options object: that shape is a request
        return [Fact("route", f"{method} {_path(path)}", _line(node), _handled("express", given[-1]))]
    holder = node.child_by_field_name("object")
    if holder is None or _environment(holder) not in _ENVIRONMENT:
        return _ecma_whole(node) if _environment(node) in _ENVIRONMENT else []
    if kind == "member_expression":
        name = _text(node.child_by_field_name("property"))
    else:
        name = _literal(node.child_by_field_name("index")) or ""
    detail = "set" if _assigned(node, ("assignment_expression",)) else "read"
    return [Fact("env", name, _line(node), detail)] if _ENV_NAME.match(name) else []


def _queue(node: Node, called: str, name: str | None, holder: str = "") -> list[Fact]:
    if called in _BROKER_ONLY and not _BROKER.search(holder):
        return []
    return [Fact("queue", name, _line(node), called)] if name and _QUEUE_NAME.match(name) else []


def _environment(node: Node) -> str:
    return _text(node).replace(" ", "").replace("?.", ".") if node.end_byte - node.start_byte <= 24 else ""


def _ecma_whole(node: Node) -> list[Fact]:
    """`"NAME" in process.env`, and `const { NAME, OTHER: local } = process.env`."""
    parent = node.parent
    if parent is None:
        return []
    names: list[tuple[str, Node]] = []
    if parent.type == "binary_expression" and _is(parent.child_by_field_name("right"), node) \
            and _text(parent.child_by_field_name("operator")) == "in":
        names.append((_literal(parent.child_by_field_name("left")) or "", parent))
    elif parent.type == "variable_declarator" and _is(parent.child_by_field_name("value"), node):
        pattern = parent.child_by_field_name("name")
        for taken in (pattern.named_children if pattern is not None and pattern.type == "object_pattern" else []):
            if taken.type == "object_assignment_pattern":       # { PORT = 3000 }
                taken = taken.child_by_field_name("left") or taken
            if taken.type == "pair_pattern":
                key = taken.child_by_field_name("key")
                names.append((_literal(key) or _text(key), taken))
            elif taken.type == "shorthand_property_identifier_pattern":
                names.append((_text(taken), taken))
    return [Fact("env", name, _line(where), "read") for name, where in names if _ENV_NAME.match(name)]


# ---- Python ------------------------------------------------------------------------------------------

def _python(node: Node, state: dict) -> list[Fact]:
    kind = node.type
    if kind == "assignment":        # router = APIRouter(prefix="/workspaces"); bp = Blueprint("x", __name__, url_prefix="/x")
        left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
        if left is not None and left.type == "identifier" and right is not None and right.type == "call":
            if _text(right.child_by_field_name("function")).rsplit(".", 1)[-1] in _PY_ROUTER_MAKERS:
                state.setdefault("routers", set()).add(_text(left))
                prefix = _keyword(right, "prefix") or _keyword(right, "url_prefix")
                if prefix:
                    state.setdefault("prefixes", {})[_text(left)] = prefix
        return []
    if kind == "decorator":
        return _python_decorator(node, state)
    if kind == "call":
        function = _of_os(state, _text(node.child_by_field_name("function")))
        first = _first_argument(node)
        if function in ("os.getenv", "os.environ.get") and first and _ENV_NAME.match(first):
            return [Fact("env", first, _line(node), "read")]
        if function in ("os.environ.setdefault", "os.putenv") and first and _ENV_NAME.match(first):
            return [Fact("env", first, _line(node), "set")]
        if "." in function and function.rsplit(".", 1)[-1] in _QUEUE_CALLS:
            return _queue(node, function.rsplit(".", 1)[-1], first, function.rsplit(".", 1)[0])
        if function.endswith((".include_router", ".register_blueprint")):
            prefix = _keyword(node, "prefix") or _keyword(node, "url_prefix")
            arguments = node.child_by_field_name("arguments")
            given = [argument for argument in (arguments.named_children if arguments is not None else [])
                     if argument.type not in ("comment", "keyword_argument")]
            return [Fact("mount", _path(prefix), _line(node), _written(given[0]) if given else "")] if prefix and prefix.startswith("/") else []
        if function == "op.create_table" and first:
            return [Fact("table", first, _line(node), "alembic")]
        return []
    if kind == "subscript":
        if _of_os(state, _text(node.child_by_field_name("value"))) == "os.environ":
            name = _literal(node.child_by_field_name("subscript")) or ""
            detail = "set" if _assigned(node, ("assignment", "augmented_assignment")) else "read"
            return [Fact("env", name, _line(node), detail)] if _ENV_NAME.match(name) else []
        return []
    if kind == "comparison_operator":       # "NAME" in os.environ
        sides = node.named_children
        if len(sides) == 2 and _of_os(state, _text(sides[1])) == "os.environ" and any(child.type in ("in", "not in") for child in node.children):
            name = _literal(sides[0]) or ""
            return [Fact("env", name, _line(node), "read")] if _ENV_NAME.match(name) else []
        return []
    return _python_class(node)


def _of_os(state: dict, written: str) -> str:
    """`environ.get` as `os.environ.get` when the file imports `environ` from `os`; anything else as written."""
    return "os." + written if written.split(".", 1)[0] in state["os_names"] else written


def _keyword(call: Node, name: str) -> str | None:
    arguments = call.child_by_field_name("arguments")
    for argument in (arguments.named_children if arguments is not None else []):
        if argument.type == "keyword_argument" and _text(argument.child_by_field_name("name")) == name:
            return _literal(argument.child_by_field_name("value"))
    return None


def _first_argument(call: Node) -> str | None:
    arguments = call.child_by_field_name("arguments")
    given = [argument for argument in (arguments.named_children if arguments is not None else []) if argument.type != "comment"]
    return _literal(given[0]) if given and given[0].type != "keyword_argument" else None


def _python_decorator(node: Node, state: dict) -> list[Fact]:
    written = node.named_children[0] if node.named_children else None
    if written is None:
        return []
    text = _text(written)
    defined = node.parent.child_by_field_name("definition") if node.parent is not None else None
    if re.search(r"(\.task\b|\bshared_task\b)", text.split("(", 1)[0]):
        name = (_keyword(written, "name") if written.type == "call" else None) or _text(defined.child_by_field_name("name") if defined else None)
        queue = _keyword(written, "queue") if written.type == "call" else None
        return ([Fact("task", name, _line(node), "celery")] if name else []) + _queue(node, "celery", queue)
    if written.type != "call":
        return []
    function = written.child_by_field_name("function")
    if function is None or function.type != "attribute":
        return []
    holder, attribute = _text(function.child_by_field_name("object")), _text(function.child_by_field_name("attribute"))
    path = _first_argument(written)
    if path is None or not (path.startswith("/") or path == ""):
        return []
    if holder not in state.get("routers", ()) and not holder.rsplit(".", 1)[-1].lower().endswith(_PY_ROUTER_ENDINGS):
        return []
    full = _path(_join(state.get("prefixes", {}).get(holder, ""), path))
    if attribute in _METHODS:
        return [Fact("route", f"{_METHODS[attribute]} {full}", _line(node), "fastapi")]
    if attribute in ("route", "api_route", "websocket"):
        arguments = written.child_by_field_name("arguments")
        methods = next((argument.child_by_field_name("value") for argument in (arguments.named_children if arguments else [])
                        if argument.type == "keyword_argument" and _text(argument.child_by_field_name("name")) == "methods"), None)
        named = [_literal(item) for item in (methods.named_children if methods is not None else [])]
        verbs = [verb.upper() for verb in named if verb] or (["WS"] if attribute == "websocket" else ["GET"])
        return [Fact("route", f"{verb} {full}", _line(node), "flask" if attribute == "route" else "fastapi") for verb in verbs]
    return []


def _python_class(node: Node) -> list[Fact]:
    name, bases, body = _text(node.child_by_field_name("name")), _text(node.child_by_field_name("superclasses")), node.child_by_field_name("body")
    out: list[Fact] = []
    members = []
    for member in (body.named_children if body is not None else []):
        if member.type == "expression_statement" and member.named_children:
            member = member.named_children[0]
        if member.type == "assignment":
            members.append(member)
    if re.search(r"\bBaseSettings\b", bases):
        for member in members:
            left = member.child_by_field_name("left")
            field = _text(left)
            if left is not None and left.type == "identifier" and member.child_by_field_name("type") is not None \
                    and not field.startswith("_") and field != "model_config":
                out.append(Fact("env", field.upper(), _line(member), "settings field"))
        return out
    for member in members:
        if _text(member.child_by_field_name("left")) == "__tablename__":
            table = _literal(member.child_by_field_name("right"))
            if table:
                return [Fact("model", name, _line(node), f"table {table}")]
    if re.search(r"\bmodels\.Model\b", bases):
        return [Fact("model", name, _line(node), "django")]
    return out


# ---- Go, Rust, Java ----------------------------------------------------------------------------------

def _relative(path: str) -> str:
    """A group's or a route's path as gin and echo join it: `"user"` and `"/user"` alike; `""` is the group itself."""
    return path if not path or path.startswith("/") else "/" + path


def _go_prefix(node: Node | None, state: dict, depth: int = 0) -> str | None:
    """The path prefix of a router group this file makes (`api := r.Group("/api")`, `r.Group("/user").Use(auth)`), or None
    when the expression is not one. A group whose path is not a literal (a configured prefix) adds nothing to the path:
    paths are compared from their ends, so a route under it is still found by the rest of its path."""
    if node is None or depth > 8:
        return None
    if node.type == "identifier":
        return state.get("groups", {}).get(_text(node))
    if node.type != "call_expression":
        return None
    function = node.child_by_field_name("function")
    if function is None or function.type != "selector_expression":
        return None
    called, operand = _text(function.child_by_field_name("field")), function.child_by_field_name("operand")
    if called in ("Use", "With"):              # middleware chained on a group keeps its path
        return _go_prefix(operand, state, depth + 1)
    if called != "Group":
        return None
    arguments = node.child_by_field_name("arguments")
    given = arguments.named_children if arguments is not None else []
    written = _literal(given[0]) if given else None
    base = _go_prefix(operand, state, depth + 1) or ""
    return _join(base, _relative(written)) if written else base or ""


def _go_groups(node: Node, state: dict) -> None:
    """`v := x.Group("/p")` and `v = x.Group("/p")`: the name is a router group of that prefix from here on in the file."""
    left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
    if left is None or right is None:
        return
    for name, value in zip(left.named_children, right.named_children):
        if name.type == "identifier" and value.type == "call_expression":
            prefix = _go_prefix(value, state)
            if prefix is not None:
                state.setdefault("groups", {})[_text(name)] = prefix


def _go(node: Node, state: dict) -> list[Fact]:
    if node.type in ("short_var_declaration", "assignment_statement"):
        _go_groups(node, state)
        return []
    function, arguments = node.child_by_field_name("function"), node.child_by_field_name("arguments")
    if function is None or arguments is None:
        return []
    given = arguments.named_children
    first = _literal(given[0]) if given else None
    if _text(function) in ("os.Getenv", "os.LookupEnv"):
        return [Fact("env", first, _line(node), "read")] if first and _ENV_NAME.match(first) else []
    if function.type != "selector_expression" or first is None:
        return []
    called = _text(function.child_by_field_name("field"))
    group = _go_prefix(function.child_by_field_name("operand"), state)      # None: not a group this file makes
    if called in _GO_MOUNTS and (first.startswith("/") or (called == "Group" and group is not None)):
        mounted = given[1] if len(given) > 1 and given[1].type != "func_literal" else None
        joined = _join(group or "", _relative(first)) if called == "Group" else first
        return [Fact("mount", _path(joined), _line(node), _written(mounted))] if joined.startswith("/") and joined != "/" else []
    method = _GO_ROUTES.get(called)
    if method is None or len(given) < 2:
        return []
    if method != "ANY" and (given[1].type not in _GO_HANDLERS or (group is None and _text(function.child_by_field_name("operand")) in _GO_NOT_ROUTERS)):
        return []       # `kv.Get("/foo", nil)`, `http.Post("/api", "application/json", body)`: a lookup, a request
    patterned = _GO_PATTERN.match(first)       # Go 1.22: "POST /v1/jobs"
    if patterned:
        method, first = patterned.group(1), patterned.group(2)
    if group is not None:                      # under a group this file makes: its path, joined
        first = _join(group, _relative(first)) or "/"
    if not first.startswith("/"):
        return []
    return [Fact("route", f"{method} {_path(first)}", _line(node), _handled("go", given[-1]))]


def _rust(node: Node, state: dict) -> list[Fact]:
    function, arguments = _text(node.child_by_field_name("function")).replace(" ", ""), node.child_by_field_name("arguments")
    if not function.endswith(("env::var", "env::var_os")) or arguments is None or not arguments.named_children:
        return []
    name = _literal(arguments.named_children[0])
    return [Fact("env", name, _line(node), "read")] if name and _ENV_NAME.match(name) else []


def _java(node: Node, state: dict) -> list[Fact]:
    if node.type == "method_invocation":
        if _text(node.child_by_field_name("object")) == "System" and _text(node.child_by_field_name("name")) == "getenv":
            arguments = node.child_by_field_name("arguments")
            name = _literal(arguments.named_children[0]) if arguments is not None and arguments.named_children else None
            return [Fact("env", name, _line(node), "read")] if name and _ENV_NAME.match(name) else []
        return []
    name = _text(node.child_by_field_name("name")).rsplit(".", 1)[-1]
    owner = node.parent.parent if node.parent is not None else None       # annotation -> modifiers -> the declaration
    if owner is None:
        return []
    if name == "Entity" and owner.type == "class_declaration":
        return [Fact("model", _text(owner.child_by_field_name("name")), _line(owner.child_by_field_name("name") or owner), "entity")]
    method = _SPRING.get(name)
    if method is None or owner.type != "method_declaration":
        return []       # on a class, RequestMapping is a prefix for its methods: read below, from the method
    enclosing = owner.parent
    while enclosing is not None and enclosing.type != "class_declaration":
        enclosing = enclosing.parent
    prefix = ""
    modifiers = next((child for child in (enclosing.children if enclosing is not None else []) if child.type == "modifiers"), None)
    for annotation in (modifiers.named_children if modifiers is not None else []):
        if annotation.type == "annotation" and _text(annotation.child_by_field_name("name")).rsplit(".", 1)[-1] == "RequestMapping":
            prefix = _annotation_path(annotation) or ""
    path = _annotation_path(node) if node.type == "annotation" else ""
    if path is None:
        return []
    return [Fact("route", f"{method} {_path(_join(prefix, path))}", _line(node), "spring")]


def _annotation_path(annotation: Node) -> str | None:
    """`@GetMapping("/x")`, `@GetMapping(value = "/x")`, `@GetMapping(path = "/x")`; '' when it names no path."""
    arguments = annotation.child_by_field_name("arguments")
    if arguments is None or not arguments.named_children:
        return ""
    for argument in arguments.named_children:
        if argument.type == "string_literal":
            return _literal(argument)
        if argument.type == "element_value_pair" and _text(argument.child_by_field_name("key")) in ("value", "path"):
            return _literal(argument.child_by_field_name("value"))
    return ""

"""Assemble and drive one Docker Compose environment per variant of a benchmark application.

A variant (``hoh.backend.Variant``) is one state of one application for one task: clean
(A), with the benchmark's change (A+M), with one control renamed (A+C) or removed (A+R),
or renamed and changed (A+C+M). Each run gets its own stack: the application's services
from the pinned benchmark, a browser container whose start-up script logs in and
registers the page scripts, and a runner container on the same network that runs a test
against the logged-in tab.

The benchmark ships those pieces but no working assembly (docs/research/
env-assembly-brief.md, sections 0 and 8). ``assemble`` writes one runnable directory:

    docker-compose.yaml           the app's own, with the setup function filled in
    (the app's other files)       copied from the benchmark at run time
    browser.Dockerfile            the benchmark's
    init.py                       the benchmark's start-up script with the injector's names
                                  neutralized, then edited by scenarios/runtime/init_rules.yaml
    bug_injector.js               the change template, prepared (hoh.pagescripts)
    bug.js                        the whole change script, merged and neutralized at
                                  assembly; empty when the variant has no change
    page_script.js                the rename/remove page script; empty without rules
    check.py                      the benchmark's final assertion, for the shadow oracle only
    .env                          COMPOSE_PROJECT_NAME, INJECT_BUG, SETUP_FUNCTION
    docker-compose.override.yaml  fixed image names, the page-script mount, and the
                                  `runner` and `oracle` services behind profiles
    tests/ test-results/ out/     what the runner mounts, and all it mounts

The stack directory keeps the benchmark's file names because its compose file mounts
them; neither the runner nor the agent sees them. What the runner can see is neutral and
the same in every variant: its three mounts, its environment (HOH_CDP_URL, HOH_READY_KEY,
HOH_START_URL), the stack's path (in its mount table) and the project name (in its
network's DNS names). Every variant also goes through the same start-up steps: log in,
set the ready key, reload once. Only bug.js, page_script.js and INJECT_BUG differ.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from hoh.backend import RULES, EnvFailure, Variant
from hoh.benchmark import BENCH_ROOT, REPO_ROOT, BenchCase, list_tasks, load_case
from hoh.neutralize import (
    Rule,
    RuleMismatch,
    apply_literal_rules,
    load_rules,
    neutralize_bug,
    neutralize_identifiers,
)
from hoh.pagescripts import READY_KEY, ROLES, RuleSpec, merge_bug, prepare_template, rule_script

APPS = ("bookstack", "invoiceninja", "indico")

#: Where `just neutralize` writes the rewritten change scripts.
PAYLOADS_DIR = REPO_ROOT / "build" / "payloads"
#: The find/replace rules for the benchmark's browser start-up script.
INIT_RULES_PATH = REPO_ROOT / "scenarios" / "runtime" / "init_rules.yaml"
#: The texts a task's change script names on purpose, which the rule check accepts.
SCRIPT_MENTIONS_PATH = REPO_ROOT / "scenarios" / "runtime" / "script_mentions.yaml"
_MENTION_KEYS = {"app", "task", "name", "within", "count"}

PLAYWRIGHT = "1.60.0"
CDP_URL = "http://browser:9222"
COMPOSE_FILE = "docker-compose.yaml"
OVERRIDE_FILE = "docker-compose.override.yaml"
SETUP_PLACEHOLDER = "$setup_fn_default"
CHECK_FILE = "check.py"
WORKDIR = "/work"
RUNNER_DIRS = ("tests", "test-results", "out")
RUNNER_IMAGE = f"runner:pw{PLAYWRIGHT}"

# Every service built from a Dockerfile gets a fixed image name, so that one build serves
# every stack and slot (brief section 7). Each entry is (name, Dockerfile, text the
# Dockerfile must contain): the version in the name is checked against what is built.
BROWSER_BUILD = (f"browser:pw{PLAYWRIGHT}", "browser.Dockerfile", f"playwright=={PLAYWRIGHT}")
APP_BUILDS: dict[str, dict[str, tuple[str, str, str]]] = {
    "bookstack": {
        "webapp": ("bookstack-app:25.2.1", "app.Dockerfile", "solidnerd/bookstack:25.2.1"),
    },
    "invoiceninja": {},
    "indico": {
        service: ("indico-worker:3.3.6", "worker/Dockerfile", "indico==3.3.6")
        for service in ("web", "celery", "celery-beat")
    },
}

_INIT_KEY = "init.py"
_KEY_SET = f"sessionStorage.setItem('{READY_KEY}', 'true')"
_KEY_GET = f"sessionStorage.getItem('{READY_KEY}')"
_TELLTALE = re.compile(r"bug|inject|benign|scenario", re.I)
_PROJECT = re.compile(r"[a-z0-9][a-z0-9_-]*")
_PREFIX = re.compile(r"[a-z0-9]+(?:[._-][a-z0-9]+)*")
_TASK = re.compile(r"[A-Za-z0-9_]+")
_INTERPOLATED = re.compile(r"(?<!\$)\$\{?([A-Za-z_][A-Za-z0-9_]*)")

# What the edited start-up script must do, in this order, each exactly once and under
# exactly these conditions (spelled as ast.unparse spells them). Checked on its syntax
# tree, so a benchmark change that the find/replace rules survive still cannot slip by.
_START_UP: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "register the change script",
        "context.add_init_script(script)",
        ("INJECT_BUG and BUG_JS.exists()",),
    ),
    (
        "register the page script",
        "context.add_init_script(page_script.read_text(encoding='utf-8'))",
        ("page_script_on",),
    ),
    ("log in", "setup_fn(page)", ("setup_fn",)),
    ("set the ready key in every variant", f"page.evaluate({_KEY_SET!r})", ()),
    (
        "reload once in every variant",
        "page.reload(wait_until='domcontentloaded', timeout=30000)",
        (),
    ),
)


class AssemblyError(ValueError):
    """The environment cannot be assembled as asked. Nothing is left on disk."""


# --- the start-up script -----------------------------------------------------------


def load_init_rules(path: Path | None = None) -> list[Rule]:
    """The start-up script's rules, read and checked like the bug-script rules."""
    path = Path(path or INIT_RULES_PATH)
    if not path.is_file():
        raise RuleMismatch(f"{path}: the start-up script's rules are missing")
    if path.suffix != ".yaml":  # load_rules looks for <stem>.yaml
        raise RuleMismatch(f"{path}: the rules must be a .yaml file")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or list(data) != [_INIT_KEY]:
        raise RuleMismatch(f"{path}: expected exactly one key, {_INIT_KEY!r}, with its rules")
    rules = load_rules(path.stem, _INIT_KEY, rules_dir=path.parent)
    if not rules:
        raise RuleMismatch(f"{path}: no rules under {_INIT_KEY!r}")
    return rules


def transform_init(source: str, rules_path: Path | None = None) -> str:
    """The benchmark's start-up script with the injector's names neutralized and the init
    rules applied, each exactly once. The result is checked: it parses, its login code is
    untouched, and it does what ``_START_UP`` lists. Raises ``RuleMismatch`` otherwise."""
    rules = load_init_rules(rules_path)
    before = neutralize_identifiers(source)
    try:
        after = apply_literal_rules(before, rules)
    except RuleMismatch as exc:
        raise RuleMismatch(
            f"init.py: {exc}. The benchmark's start-up script is not the one the rules in "
            f"{INIT_RULES_PATH.name} were written for; review them against it."
        ) from None
    _check_start_up_script(before, after)
    return after


def _check_start_up_script(before: str, after: str) -> None:
    try:
        tree = ast.parse(after)
    except SyntaxError as exc:
        raise RuleMismatch(f"init.py does not parse after the init rules: {exc}") from None
    old, new = _login_code(ast.parse(before)), _login_code(tree)
    if not old or "_SETUP_FUNCTIONS" not in old:
        raise RuleMismatch("init.py has no login functions any more; the benchmark changed")
    if new != old:
        changed = sorted(k for k in old.keys() | new.keys() if old.get(k) != new.get(k))
        raise RuleMismatch(f"the init rules change the login code ({', '.join(changed)})")
    main = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main"), None)
    if main is None:
        raise RuleMismatch("init.py has no main() any more; the benchmark changed")
    calls = list(_calls_under(main))
    if any(text.startswith("prepare_bug_script(") for text, _, _ in calls):
        raise RuleMismatch("init.py would merge bug.js again, but bug.js arrives merged")
    lines = []
    for what, text, conditions in _START_UP:
        found = [(where, line) for call, where, line in calls if call == text]
        if [where for where, _ in found] != [conditions]:
            raise RuleMismatch(
                f"init.py does not {what}: expected `{text}` once, under "
                f"{list(conditions) or 'no condition'}; found {[w for w, _ in found]}"
            )
        lines.append(found[0][1])
    if lines != sorted(lines):
        raise RuleMismatch(f"init.py does not {', then '.join(w for w, _, _ in _START_UP)}")


def _calls_under(node: ast.AST, conditions: tuple[str, ...] = ()) -> Iterator[tuple]:
    """Every call below `node`: (source, the tests of the enclosing ifs, line)."""
    if isinstance(node, ast.If):
        test = ast.unparse(node.test)
        yield from _calls_under(node.test, conditions)
        for child in node.body:
            yield from _calls_under(child, (*conditions, test))
        for child in node.orelse:
            yield from _calls_under(child, (*conditions, f"not ({test})"))
        return
    if isinstance(node, ast.Call):
        yield ast.unparse(node), conditions, node.lineno
    for child in ast.iter_child_nodes(node):
        yield from _calls_under(child, conditions)


def _login_code(tree: ast.Module) -> dict[str, str]:
    """The login functions and their registry, by name, as dumped syntax trees."""
    found = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name.startswith("_setup_"):
            found[node.name] = ast.dump(node)
        elif _assigns(node, "_SETUP_FUNCTIONS"):
            found["_SETUP_FUNCTIONS"] = ast.dump(node)
    return found


def _assigns(node: ast.AST, name: str) -> bool:
    targets = node.targets if isinstance(node, ast.Assign) else []
    if isinstance(node, ast.AnnAssign):
        targets = [node.target]
    return any(isinstance(t, ast.Name) and t.id == name for t in targets)


def _registered_logins(tree: ast.Module) -> set[str]:
    for node in tree.body:
        if _assigns(node, "_SETUP_FUNCTIONS") and isinstance(node.value, ast.Dict):
            return {k.value for k in node.value.keys if isinstance(k, ast.Constant)}
    raise AssemblyError("init.py has no _SETUP_FUNCTIONS registry any more; the benchmark changed")


# --- assembly --------------------------------------------------------------------


def start_url(app_url: str) -> str:
    """The application's address as the tab shows it: Chromium drops a default port, so
    http://nginx:80 reads http://nginx/ in the browser."""
    parts = urlsplit(app_url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise AssemblyError(f"not an application address: {app_url!r}")
    default = 80 if parts.scheme == "http" else 443
    host = parts.hostname if parts.port in (None, default) else f"{parts.hostname}:{parts.port}"
    return f"{parts.scheme}://{host}/"


def assemble(
    variant: Variant,
    out_dir: Path,
    *,
    project: str,
    rules: Sequence[RuleSpec] = (),
    image_prefix: str = "hoh",
) -> Path:
    """Write one runnable Compose directory for `variant` at `out_dir` and return it.

    `out_dir` must be new or empty, and outside the repository unless under build/.
    `project` is the Compose project name; parallel stacks need different ones. `rules`
    are the variant's rename or remove rules, and only a variant with a rule takes them.
    Everything is checked before anything is written, then the written directory is
    checked again, with `docker compose config` when the CLI is installed. Raises
    ``AssemblyError`` (or ``RuleMismatch`` when the benchmark's start-up script changed)
    and leaves nothing behind.
    """
    out_dir = Path(out_dir).absolute()
    _check_names(out_dir, project, image_prefix)
    files = _stack_files(variant, project, tuple(rules), image_prefix)
    _check_out_dir(out_dir)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        shutil.copytree(BENCH_ROOT / variant.app / "environment", out_dir, dirs_exist_ok=True)
        shutil.copy2(BENCH_ROOT / "runtime" / "browser.Dockerfile", out_dir / "browser.Dockerfile")
        for name, text in files.items():
            (out_dir / name).write_text(text, encoding="utf-8")
        for name in RUNNER_DIRS:
            (out_dir / name).mkdir()
        missing = [str(p) for p in referenced_paths(out_dir) if not p.exists()]
        if missing:
            raise AssemblyError(f"{out_dir}: the compose files name missing files: {missing}")
        if shutil.which("docker"):
            validate(out_dir)
    except BaseException:
        shutil.rmtree(out_dir, ignore_errors=True)
        raise
    return out_dir


def _check_names(out_dir: Path, project: str, image_prefix: str) -> None:
    if not _PROJECT.fullmatch(project or ""):
        raise AssemblyError(
            f"project name {project!r}: Compose takes lowercase letters, digits, '-' and '_', "
            "starting with a letter or digit"
        )
    if _TELLTALE.search(project):
        raise AssemblyError(
            f"project name {project!r} names the study; the runner can read it in its "
            "network's DNS names"
        )
    if not _PREFIX.fullmatch(image_prefix or ""):
        raise AssemblyError(f"image prefix {image_prefix!r} is not a valid image name part")
    for shown in {str(out_dir), os.path.realpath(out_dir)}:
        found = _TELLTALE.search(shown)
        if found:
            raise AssemblyError(
                f"the stack's path {shown} contains {found.group()!r}; the runner sees the "
                "path in its mount table"
            )


def _check_out_dir(out_dir: Path) -> None:
    real = Path(os.path.realpath(out_dir))
    repo = Path(os.path.realpath(REPO_ROOT))
    build = repo / "build"
    if (real == repo or repo in real.parents) and build not in real.parents:
        raise AssemblyError(
            f"{out_dir} is inside the repository; benchmark files may go only under {build}"
        )
    if out_dir.exists() and (not out_dir.is_dir() or any(out_dir.iterdir())):
        raise AssemblyError(f"{out_dir} exists and is not empty")


def _stack_files(
    variant: Variant, project: str, rules: tuple[RuleSpec, ...], image_prefix: str
) -> dict[str, str]:
    """The text of every file assembly writes, checked, keyed by its name in the stack."""
    app, task = variant.app, variant.task
    if app not in APPS:
        raise AssemblyError(f"unknown app {app!r}; one of {', '.join(APPS)}")
    if (
        not _TASK.fullmatch(task)
        or not (BENCH_ROOT / app / "test_cases" / f"{task}.yaml").is_file()
    ):
        raise AssemblyError(f"{app}: the benchmark has no task {task!r}")
    case = load_case(app, task)
    env_dir = BENCH_ROOT / app / "environment"
    compose_text = (env_dir / COMPOSE_FILE).read_text(encoding="utf-8")
    base = yaml.safe_load(compose_text)
    init_source = (BENCH_ROOT / "runtime" / "init.py").read_text(encoding="utf-8")
    setup = _setup_function(case, ast.parse(init_source))

    payload = _payload(variant, case) if variant.bug else None
    _check_rules(variant, rules, case, payload)
    url = _app_url(app, base)
    images = _built_images(app, base, env_dir, image_prefix)
    if compose_text.count(SETUP_PLACEHOLDER) != 1:
        raise AssemblyError(f"{app}: expected one {SETUP_PLACEHOLDER} in {COMPOSE_FILE}")

    template = (BENCH_ROOT / "runtime" / "bug_injector.js").read_text(encoding="utf-8")
    injector = prepare_template(template)
    if _KEY_GET not in injector:
        raise AssemblyError(f"the change template does not wait for the ready key {READY_KEY}")
    return {
        COMPOSE_FILE: compose_text.replace(SETUP_PLACEHOLDER, setup),
        "init.py": transform_init(init_source),
        "bug_injector.js": injector,
        "bug.js": merge_bug(injector, payload) if payload is not None else "",
        "page_script.js": rule_script(rules, ready_key=READY_KEY) if rules else "",
        CHECK_FILE: _final_assertion(case),
        ".env": (
            f"COMPOSE_PROJECT_NAME={project}\n"
            f"INJECT_BUG={'true' if variant.bug else 'false'}\n"
            f"SETUP_FUNCTION={setup}\n"
        ),
        OVERRIDE_FILE: _override(task, images, image_prefix, start_url(url)),
    }


def _setup_function(case: BenchCase, init_tree: ast.Module) -> str:
    """The login the start-up script runs. An empty or unknown name would make it skip the
    login without a word (brief, pitfall 2)."""
    where = f"{case.app}/{case.task}"
    name = (case.setup_function or "").strip()
    if not name:
        raise AssemblyError(
            f"{where}: the task file names no setup_function; the browser would start "
            "without logging in, and say nothing"
        )
    known = _registered_logins(init_tree)
    if name not in known:
        raise AssemblyError(
            f"{where}: setup_function {name!r} is not one the start-up script knows "
            f"({', '.join(sorted(known))}); it would skip the login and say nothing"
        )
    config = json.loads((BENCH_ROOT / "app_config.json").read_text(encoding="utf-8"))
    if name not in config.get(case.app, {}).get("credentials", {}):
        raise AssemblyError(f"{where}: setup_function {name!r} is not a login of {case.app}")
    return name


def _payload(variant: Variant, case: BenchCase) -> str:
    """The rewritten change script, refused when missing or out of date."""
    path = PAYLOADS_DIR / variant.app / f"{variant.task}.js"
    if not path.is_file():
        raise AssemblyError(f"no rewritten change script at {path}; run `just neutralize` first")
    text = path.read_text(encoding="utf-8")
    if text != neutralize_bug(variant.app, variant.task, case.bug_js):
        raise AssemblyError(f"{path} is out of date with its rewrite rules; run `just neutralize`")
    return text


def _check_rules(
    variant: Variant, rules: tuple[RuleSpec, ...], case: BenchCase, payload: str | None
) -> None:
    where = f"{variant.app}/{variant.task} ({variant.code})"
    if variant.rule is None:
        if rules:
            raise AssemblyError(f"{where}: rules given for a variant without a rule")
        return
    if not rules:
        raise AssemblyError(
            f"{where}: a {variant.rule} variant needs its rules; without them the page "
            "would be left unchanged"
        )
    for rule in rules:
        if rule.op != variant.rule:
            raise AssemblyError(f"{where}: a {rule.op} rule for a {variant.rule} variant")
        name = " ".join(rule.name.split())
        if name.lower() not in _test_text(case):
            raise AssemblyError(
                f"{where}: no step of this test names {rule.name!r}; the rule was written "
                "for another test"
            )
        if payload is not None:
            _check_mentions(where, variant, rule, name, " ".join(payload.split()))


def load_script_mentions(path: Path | None = None) -> dict[tuple[str, str, str], tuple[str, int]]:
    """The declared mentions (``SCRIPT_MENTIONS_PATH``): (app, task, name) to the text that
    holds the name and how often it occurs, whitespace collapsed. A malformed file is refused."""
    path = SCRIPT_MENTIONS_PATH if path is None else Path(path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise AssemblyError(f"{path}: cannot read the declared mentions: {exc}") from None
    if not isinstance(data, dict) or set(data) != {"schema", "mentions"} or data["schema"] != 1:
        raise AssemblyError(f"{path}: expected the keys schema (1) and mentions, and no other")
    mentions = data["mentions"] if data["mentions"] is not None else []
    if not isinstance(mentions, list):
        raise AssemblyError(f"{path}: mentions must be a list of entries")
    found: dict[tuple[str, str, str], tuple[str, int]] = {}
    for n, entry in enumerate(mentions, start=1):
        where = f"{path}: mention {n}"
        if not isinstance(entry, dict) or set(entry) != _MENTION_KEYS:
            raise AssemblyError(f"{where}: the keys are {', '.join(sorted(_MENTION_KEYS))}")
        texts = [entry[k] for k in ("app", "task", "name", "within")]
        if not all(isinstance(t, str) and t.strip() for t in texts):
            raise AssemblyError(f"{where}: app, task, name and within must be text")
        count = entry["count"]
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise AssemblyError(f"{where}: count must be a whole number of at least 1")
        name, within = " ".join(entry["name"].split()), " ".join(entry["within"].split())
        if name not in within:
            raise AssemblyError(f"{where}: {entry['name']!r} is not in {entry['within']!r}")
        key = (entry["app"], entry["task"], name)
        if key in found:
            raise AssemblyError(f"{where}: {key} is declared twice")
        found[key] = (within, count)
    return found


def _check_mentions(where: str, variant: Variant, rule: RuleSpec, name: str, script: str) -> None:
    """Refuse a change script that names the rule's text, unless the mention is declared and
    every occurrence sits inside the declared text, which occurs as often as declared."""
    seen = script.count(name)
    declared = load_script_mentions().get((variant.app, variant.task, name))
    if declared is None:
        if seen:
            raise AssemblyError(
                f"{where}: the change script itself names {rule.name!r}; the {rule.op} would "
                "be on the page first and could stop the change from applying"
            )
        return
    within, count = declared
    held = script.count(within)
    if held != count or seen != held * within.count(name):
        raise AssemblyError(
            f"{where}: {SCRIPT_MENTIONS_PATH.name} declares {count} mention(s) of "
            f"{rule.name!r} inside {within!r}; the change script holds that text {held} "
            f"time(s) and the name {seen} time(s)"
        )


def _test_text(case: BenchCase) -> str:
    """The test's code and the strings in it, lowercased, whitespace collapsed."""
    parts = []
    snippets = [s.solution for s in case.steps] + [s.action_check or "" for s in case.steps]
    for snippet in [*snippets, case.assertion_check or ""]:
        parts.append(snippet)
        try:
            tree = ast.parse(textwrap.dedent(snippet))
        except SyntaxError:
            continue
        parts += [
            n.value
            for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
        ]
    return " ".join(" ".join(parts).split()).lower()


def _app_url(app: str, base: dict) -> str:
    environment = base["services"]["browser"].get("environment") or []
    if isinstance(environment, dict):
        url = environment.get("APP_URL")
    else:
        url = next((e.split("=", 1)[1] for e in environment if e.startswith("APP_URL=")), None)
    config = json.loads((BENCH_ROOT / "app_config.json").read_text(encoding="utf-8"))
    if not url or url != config.get(app, {}).get("app_url"):
        raise AssemblyError(f"{app}: the browser's APP_URL {url!r} disagrees with app_config.json")
    return url


def _built_images(app: str, base: dict, env_dir: Path, prefix: str) -> dict[str, str]:
    """Service -> fixed image name, for every service the app's compose file builds."""
    builds = {"browser": BROWSER_BUILD, **APP_BUILDS.get(app, {})}
    images = {}
    for service, spec in base["services"].items():
        build = spec.get("build")
        if build is None:
            continue
        if service not in builds:
            raise AssemblyError(
                f"{app}: service {service!r} is built from a Dockerfile but has no fixed image "
                "name; every stack would build it again"
            )
        name, dockerfile, needle = builds[service]
        build = {"context": build} if isinstance(build, str) else build
        declared = Path(build.get("context", ".")) / build.get("dockerfile", "Dockerfile")
        source = (
            BENCH_ROOT / "runtime" / dockerfile if service == "browser" else env_dir / dockerfile
        )
        if os.path.normpath(declared) != os.path.normpath(dockerfile) or not source.is_file():
            raise AssemblyError(f"{app}: {service} is no longer built from {dockerfile}")
        if needle not in source.read_text(encoding="utf-8"):
            raise AssemblyError(
                f"{app}: {dockerfile} no longer builds {needle}; the image name {name} would lie"
            )
        images[service] = f"{prefix}/{name}"
    if "browser" not in images:
        raise AssemblyError(f"{app}: the compose file builds no browser")
    return images


# The runner's timing, the same in every stack and frozen with the analysis plan. A page counts
# as quiet after 800 ms without activity (the design doc asks for at least 500 ms): the
# benchmark injector's revisit path in a single-page app can apply a change up to 500 ms after a
# route change (a 200 ms poll, then 300 ms of quiet), which would tie with a 500 ms window.
# A test run that has not finished after 300 s is stopped and reported as broken.
RUNNER_TIMING = {"HOH_QUIET_MS": "800", "HOH_RUN_TIMEOUT_S": "300"}


def _override(task: str, images: dict[str, str], prefix: str, url: str) -> str:
    services: dict[str, dict] = {s: {"image": i, "pull_policy": "never"} for s, i in images.items()}
    services["browser"]["volumes"] = ["./page_script.js:/page_script.js:ro"]

    def tool(profile: str, volumes: list[str], command: list[str]) -> dict:
        return {
            "image": f"{prefix}/{RUNNER_IMAGE}",
            "pull_policy": "never",
            "profiles": [profile],
            "working_dir": WORKDIR,
            "environment": {
                "HOH_CDP_URL": CDP_URL,
                "HOH_READY_KEY": READY_KEY,
                "HOH_START_URL": url,
                **RUNNER_TIMING,
            },
            "volumes": volumes,
            "command": ["python3", "-m", *command],
        }

    services["runner"] = tool(
        "runner",
        [f"./{d}:{WORKDIR}/{d}" + (":ro" if d == "tests" else "") for d in RUNNER_DIRS],
        [
            "hoh_runner.entry",
            "--tests",
            f"{WORKDIR}/tests",
            "--results",
            f"{WORKDIR}/test-results",
            "--out",
            f"{WORKDIR}/out",
            "--target",
            f"tests/test_{task}.py::test_{task}",
        ],
    )
    services["oracle"] = tool(
        "oracle",
        [f"./out:{WORKDIR}/out", f"./{CHECK_FILE}:{WORKDIR}/{CHECK_FILE}:ro"],
        ["hoh_runner.oracle", "--snippet", f"{WORKDIR}/{CHECK_FILE}", "--out", f"{WORKDIR}/out"],
    )
    header = (
        "# Written by hoh.envs.assemble. Built images have fixed names (one build serves\n"
        "# every stack) and are never pulled. `runner` and `oracle` sit behind profiles, so\n"
        "# `up` starts neither; start them with `docker compose run --rm runner|oracle`.\n"
    )
    return header + yaml.safe_dump(
        {"services": services}, sort_keys=False, default_flow_style=False, width=1000
    )


def _final_assertion(case: BenchCase) -> str:
    text = textwrap.dedent(case.assertion_check or "").strip()
    return text + "\n" if text else ""


def referenced_paths(stack_dir: Path) -> list[Path]:
    """Every bind-mount source and build file the stack's two compose files name."""
    stack_dir = Path(stack_dir)
    paths: list[Path] = []
    for name in (COMPOSE_FILE, OVERRIDE_FILE):
        data = yaml.safe_load((stack_dir / name).read_text(encoding="utf-8")) or {}
        for service in (data.get("services") or {}).values():
            for volume in service.get("volumes") or []:
                if isinstance(volume, dict):
                    source = volume.get("source", "") if volume.get("type") == "bind" else ""
                else:
                    source = volume.split(":", 1)[0]
                if source.startswith((".", "/")):
                    paths.append(stack_dir / source)
            build = service.get("build")
            if build is not None:
                build = {"context": build} if isinstance(build, str) else build
                context = stack_dir / build.get("context", ".")
                paths += [context, context / build.get("dockerfile", "Dockerfile")]
    return list(dict.fromkeys(Path(os.path.normpath(p)) for p in paths))


def validate(stack_dir: Path) -> dict:
    """Check the stack as Compose resolves it (brief section 8, step 10); return the model.

    The browser gets the .env's INJECT_BUG and SETUP_FUNCTION and its four scripts; the
    runner and the oracle get their mounts and nothing more; built images have fixed names.
    """
    stack_dir = Path(stack_dir).absolute()
    proc = compose(stack_dir, "--profile", "*", "config", "--format", "json", timeout_s=120)
    if proc.returncode != 0:
        raise AssemblyError(f"docker compose config rejects {stack_dir}: {_tail(proc.stderr)}")
    config = json.loads(proc.stdout)
    settings = _dotenv(stack_dir)
    services = config.get("services") or {}
    real_stack = Path(os.path.realpath(stack_dir))

    def binds(service: dict) -> list[str]:
        found = []
        for volume in service.get("volumes") or []:
            source = Path(os.path.realpath(volume.get("source", "")))
            if volume.get("type") == "bind" and real_stack in source.parents:
                source_text = f"./{source.relative_to(real_stack).as_posix()}"
            else:
                source_text = f"{volume.get('type')}:{volume.get('source')}"
            found.append(
                f"{source_text}:{volume.get('target')}" + (":ro" if volume.get("read_only") else "")
            )
        return found

    problems = []
    if config.get("name") != settings.get("COMPOSE_PROJECT_NAME"):
        problems.append(f"project {config.get('name')!r} is not the one in .env")
    browser = services.get("browser") or {}
    for key in ("INJECT_BUG", "SETUP_FUNCTION"):
        value = (browser.get("environment") or {}).get(key)
        if value != settings.get(key):
            problems.append(f"the browser gets {key}={value!r}, .env says {settings.get(key)!r}")
    scripts = [f"./{n}:/{n}:ro" for n in ("init.py", "bug.js", "bug_injector.js", "page_script.js")]
    if sorted(binds(browser)) != sorted(scripts):
        problems.append(f"the browser mounts {binds(browser)}")
    environment = {
        "HOH_CDP_URL": CDP_URL,
        "HOH_READY_KEY": READY_KEY,
        "HOH_START_URL": start_url((browser.get("environment") or {}).get("APP_URL", "")),
        **RUNNER_TIMING,
    }
    expected = {
        "runner": [f"./{d}:{WORKDIR}/{d}" + (":ro" if d == "tests" else "") for d in RUNNER_DIRS],
        "oracle": [f"./out:{WORKDIR}/out", f"./{CHECK_FILE}:{WORKDIR}/{CHECK_FILE}:ro"],
    }
    for name, mounts in expected.items():
        service = services.get(name) or {}
        if binds(service) != mounts:
            problems.append(f"{name} mounts {binds(service)}, not {mounts}")
        if (service.get("environment") or {}) != environment:
            problems.append(f"{name} gets the environment {service.get('environment')}")
        if {"depends_on", "build", "env_file"} & set(service):
            problems.append(f"{name} must have no depends_on, build or env_file")
        if set(service.get("networks") or {}) != {"default"}:
            problems.append(f"{name} must be on the stack's own network only")
    for name, service in services.items():
        if "build" in service and service.get("pull_policy") != "never":
            problems.append(f"{name} is built but may be pulled")
    if problems:
        raise AssemblyError(f"{stack_dir}: " + "; ".join(problems))
    return config


# --- driving a stack -------------------------------------------------------------


def compose(
    stack_dir: Path, *args: str, timeout_s: float, capture: bool = True
) -> subprocess.CompletedProcess:
    """`docker compose <args>` in the stack, with its project name and both compose files.

    Variables the compose files interpolate (INJECT_BUG, SETUP_FUNCTION) and every
    COMPOSE_* variable are dropped from the environment Docker sees: one exported in the
    caller's shell would win over .env and silently give another variant or project.
    A timeout is an ``EnvFailure``.
    """
    stack_dir = Path(stack_dir).absolute()
    project = _dotenv(stack_dir).get("COMPOSE_PROJECT_NAME")
    if not project:
        raise AssemblyError(f"{stack_dir} is not an assembled stack: no project name in .env")
    cmd = [
        "docker",
        "compose",
        "--project-directory",
        str(stack_dir),
        "--project-name",
        project,
        "--file",
        str(stack_dir / COMPOSE_FILE),
        "--file",
        str(stack_dir / OVERRIDE_FILE),
        "--env-file",
        str(stack_dir / ".env"),
        *args,
    ]
    try:
        return subprocess.run(
            cmd,
            cwd=stack_dir,
            env=_clean_environment(stack_dir),
            # Never the caller's standard input: in the run_test server it carries MCP.
            stdin=subprocess.DEVNULL,
            capture_output=capture,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        raise EnvFailure(
            f"docker compose {' '.join(args)} did not finish within {timeout_s:g} s "
            f"({stack_dir.name})"
        ) from None


def _dotenv(stack_dir: Path) -> dict[str, str]:
    path = Path(stack_dir) / ".env"
    if not path.is_file():
        return {}
    lines = path.read_text(encoding="utf-8").splitlines()
    return dict(line.split("=", 1) for line in lines if "=" in line and not line.startswith("#"))


def _clean_environment(stack_dir: Path) -> dict[str, str]:
    names = set()
    for name in (COMPOSE_FILE, OVERRIDE_FILE):
        path = Path(stack_dir) / name
        if path.is_file():
            names |= set(_INTERPOLATED.findall(path.read_text(encoding="utf-8")))
    return {k: v for k, v in os.environ.items() if k not in names and not k.startswith("COMPOSE_")}


def start(stack_dir: Path, *, timeout_s: float = 900.0) -> None:
    """Start the stack from nothing: `down --volumes`, then `up --detach --no-build`.

    Never `up` on a live stack: the one-shot seed-loader would run again, and Indico's
    seed then resets every sequence without an error (brief, pitfall 7). Raises
    ``EnvFailure`` when either step fails. Call ``wait_ready`` next.
    """
    stop(stack_dir, timeout_s=min(timeout_s, 120.0))
    proc = compose(stack_dir, "up", "--detach", "--no-build", "--quiet-pull", timeout_s=timeout_s)
    if proc.returncode != 0:
        raise EnvFailure(f"docker compose up failed: {_tail(proc.stderr)}")


def stop(stack_dir: Path, *, timeout_s: float = 120.0) -> None:
    """Remove the stack's containers, networks and volumes. Raises ``EnvFailure``."""
    proc = compose(
        stack_dir, "down", "--volumes", "--remove-orphans", "--timeout", "5", timeout_s=timeout_s
    )
    if proc.returncode != 0:
        raise EnvFailure(f"docker compose down failed: {_tail(proc.stderr)}")


def wait_ready(stack_dir: Path, timeout_s: float = 900, *, poll_s: float = 2.0) -> None:
    """Return once the browser has logged in and opened its CDP port: its log ends with
    `[init] Ready` (a Ready line from before a restart does not count).

    Raises ``EnvFailure`` when a service exited non-zero or is restarting (a failed login
    becomes a restart loop, brief pitfall 9), or when `timeout_s` runs out (brief
    section 8, step 12).
    """
    deadline = time.monotonic() + timeout_s
    log = ""
    while True:
        containers = _containers(stack_dir)
        for c in containers:
            service = c.get("Service") or c.get("Name") or "?"
            state = str(c.get("State", "")).lower()
            code = int(c.get("ExitCode") or 0)
            if service in ("runner", "oracle"):
                continue
            if state == "restarting":
                raise EnvFailure(f"{service} is restarting (last exit code {code})")
            if state in ("exited", "dead") and code != 0:
                raise EnvFailure(f"{service} exited with code {code}")
        browser = [c for c in containers if c.get("Service") == "browser"]
        if browser:
            proc = compose(stack_dir, "logs", "--no-color", "browser", timeout_s=60)
            if proc.returncode != 0:
                raise EnvFailure(f"docker compose logs failed: {_tail(proc.stderr)}")
            log = proc.stdout
            running = any(str(c.get("State", "")).lower() == "running" for c in browser)
            if running and _ready(log):
                return
        if time.monotonic() >= deadline:
            raise EnvFailure(
                f"the browser was not ready after {timeout_s:g} s; its log ends:\n{_tail(log)}"
            )
        time.sleep(poll_s)


# What a stack is assembled from, besides the pinned benchmark: a change to any of these can
# change what a run measures, so the validation baseline names their digest (with the
# benchmark's commit) and a stage refuses a baseline measured from other ones.
ENVIRONMENT_FILES = (
    "src/hoh/envs.py",
    "src/hoh/pagescripts.py",
    "src/hoh/neutralize.py",
    "src/hoh/js",
    "scenarios/runtime",
    "scenarios/neutralize",
)


def benchmark_commit() -> str:
    """The pinned benchmark's commit, as git sees the checkout ("unknown" without git)."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(BENCH_ROOT), "rev-parse", "HEAD"], stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return proc.stdout.strip() if proc.returncode == 0 else "unknown"


def environment_digest(repo_root: Path = REPO_ROOT, *, commit: str | None = None) -> str:
    """SHA-256 over the files a stack is assembled from (``ENVIRONMENT_FILES``, caches and
    hidden files such as Finder's .DS_Store left out) and the benchmark's commit
    (``benchmark_commit()`` unless given)."""
    repo_root = Path(repo_root)
    digest = hashlib.sha256()
    digest.update(f"benchmark {benchmark_commit() if commit is None else commit}\0".encode())
    for rel in ENVIRONMENT_FILES:
        root = repo_root / rel
        files = [root] if root.is_file() else sorted(
            p for p in root.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
            and not any(part.startswith(".") for part in p.relative_to(root).parts)
        )
        for path in files:
            digest.update(path.relative_to(repo_root).as_posix().encode() + b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def check_intact(stack_dir: Path) -> None:
    """After a run: raise ``EnvFailure`` unless every service is still as ``wait_ready`` left
    it, with the browser still on its first start. The browser service restarts by itself
    and logs in again, so a run it went down in would otherwise look like a run on a fresh
    start page."""
    containers = _containers(stack_dir)
    for c in containers:
        service = c.get("Service") or c.get("Name") or "?"
        state = str(c.get("State", "")).lower()
        code = int(c.get("ExitCode") or 0)
        if service in ("runner", "oracle"):
            continue
        if state == "restarting" or (state in ("exited", "dead") and code != 0):
            raise EnvFailure(f"{service} went down during the run ({state}, exit code {code})")
    proc = compose(stack_dir, "logs", "--no-color", "browser", timeout_s=60)
    if proc.returncode != 0:
        raise EnvFailure(f"docker compose logs failed: {_tail(proc.stderr)}")
    starts = sum(
        1 for line in proc.stdout.splitlines()
        if "[init]" in line and line.split("[init]", 1)[1].strip().startswith("Ready")
    )
    if starts != 1:
        raise EnvFailure(f"the browser restarted during the run ({starts} Ready lines)")


def _containers(stack_dir: Path) -> list[dict]:
    proc = compose(stack_dir, "ps", "--all", "--format", "json", timeout_s=60)
    if proc.returncode != 0:
        raise EnvFailure(f"docker compose ps failed: {_tail(proc.stderr)}")
    text = proc.stdout.strip()
    if not text:
        return []
    try:  # older Compose prints one array, newer one object per line
        data = json.loads(text)
    except json.JSONDecodeError:
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    return data if isinstance(data, list) else [data]


def _ready(log: str) -> bool:
    marks = [line.split("[init]", 1)[1].strip() for line in log.splitlines() if "[init]" in line]
    return bool(marks) and marks[-1].startswith("Ready")


def _tail(text: str, lines: int = 20) -> str:
    return "\n".join((text or "").strip().splitlines()[-lines:])


def build_images(
    app: str, image_prefix: str = "hoh", *, timeout_s: float = 7200.0
) -> subprocess.CompletedProcess:
    """Build the app's shared images once (browser, and BookStack's or Indico's app image),
    from a throwaway clean stack. The runner image is built separately (docker/runner)."""
    scratch = None
    for _ in range(20):
        candidate = Path(tempfile.mkdtemp(prefix="hoh-images-"))
        if not _TELLTALE.search(os.path.realpath(candidate)):
            scratch = candidate
            break
        candidate.rmdir()
    if scratch is None:
        raise AssemblyError(f"no neutral temporary directory under {tempfile.gettempdir()}")
    try:
        stack = assemble(
            Variant(app, list_tasks(app)[0]),
            scratch / "stack",
            project=f"hoh-images-{app}",
            image_prefix=image_prefix,
        )
        return compose(stack, "build", timeout_s=timeout_s, capture=False)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


# --- command line ----------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m hoh.envs",
        description="Assemble one Compose environment per variant from the pinned benchmark.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    one = commands.add_parser("assemble", help="write one stack directory for one variant")
    one.add_argument("--app", required=True, choices=APPS)
    one.add_argument("--task", required=True)
    one.add_argument("--bug", action="store_true", help="with the benchmark's change (M)")
    one.add_argument("--rule", choices=RULES, help="rename (C) or remove (R) one control")
    one.add_argument("--role", choices=ROLES, help="the control's role, with --rule")
    one.add_argument("--name", help="the control's accessible name, with --rule")
    one.add_argument("--new-name", help="its new name, with --rule rename only")
    one.add_argument(
        "--substring",
        action="store_true",
        help="match the name as a case-insensitive substring, not exactly",
    )
    one.add_argument("--out", required=True, type=Path, help="a new or empty directory")
    one.add_argument("--project", required=True, help="the Compose project name")
    one.add_argument("--image-prefix", default="hoh")
    images = commands.add_parser("build", help="build an app's shared images once")
    images.add_argument("--app", required=True, choices=APPS)
    images.add_argument("--image-prefix", default="hoh")
    images.add_argument("--timeout", type=float, default=7200.0, help="seconds")
    args = parser.parse_args(argv)

    if args.command == "build":
        return build_images(args.app, args.image_prefix, timeout_s=args.timeout).returncode

    rules: tuple[RuleSpec, ...] = ()
    if args.rule:
        if not (args.role and args.name):
            one.error("--rule needs --role and --name")
        if (args.rule == "rename") != (args.new_name is not None):
            one.error("--new-name goes with --rule rename, and only with it")
        rules = (RuleSpec(args.rule, args.role, args.name, args.new_name, not args.substring),)
    elif args.role or args.name or args.new_name or args.substring:
        one.error("--role, --name, --new-name and --substring need --rule")
    try:
        variant = Variant(args.app, args.task, bug=args.bug, rule=args.rule)
        stack = assemble(
            variant, args.out, project=args.project, rules=rules, image_prefix=args.image_prefix
        )
    except (ValueError, RuntimeError) as exc:
        parser.exit(2, f"error: {exc}\n")
    settings = _dotenv(stack)
    print(
        json.dumps(
            {
                "dir": str(stack),
                "project": settings["COMPOSE_PROJECT_NAME"],
                "variant": variant.code,
                "inject_bug": settings["INJECT_BUG"] == "true",
                "setup_function": settings["SETUP_FUNCTION"],
                "page_script": bool(rules),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

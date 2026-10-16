"""Module summaries in search (DEFERRED 0-graph part 17): the files of the best-matching modules come first, in projects
whose main language it was measured to help (Python), and nowhere else."""
import time

import pytest

from aleph_zero import areas
from aleph_zero.engine import Engine
from aleph_zero.search import search

QUESTION = "how are recipe ingredients scraped and cleaned when importing"


@pytest.fixture
def modules(repo):
    repo.write("app/importer/scrape.py", "def scrape_recipe_page(url):\n    return fetch(url)\n\n\ndef parse_ingredients(html):\n    return []\n")
    repo.write("app/importer/cleaner.py", "def clean_recipe(data):\n    return data\n\n\ndef normalize_ingredient_units(text):\n    return text\n")
    repo.write("app/users/accounts.py", "def create_user(name):\n    return name\n\n\ndef reset_password(user):\n    return user\n")
    repo.write("app/views.py", "def index():\n    return 'importing recipes from a page is described elsewhere'\n")
    return repo


def files(hits):
    return list(dict.fromkeys(hit.path for hit in hits))


def ready(root):
    engine = Engine(root)
    index, view, _, _ = engine._ready(root, time.monotonic(), None, symbols=True)
    return engine, index, view


@pytest.mark.parametrize("arm", [True])
def test_the_best_modules_files_come_first_and_nothing_found_is_lost(modules, arm):
    _, index, view = ready(modules.root)
    before = files(search(index, view, QUESTION, k=10, areas=False))
    after = files(search(index, view, QUESTION, k=10, areas=arm))
    assert set(after[:2]) == {"app/importer/scrape.py", "app/importer/cleaner.py"}
    assert set(after) >= set(before)
    assert files(search(index, view, QUESTION, kind="docs", k=10, areas=True)) == \
        files(search(index, view, QUESTION, kind="docs", k=10, areas=False))      # documentation alone: untouched


def test_on_by_default_in_a_python_project_and_off_elsewhere(modules, make_repo, tmp_path):
    engine = Engine(modules.root)
    assert engine.search(QUESTION, k=10).hits[0].sources[-1] == "module"
    assert areas.decide("python")[0] and not areas.decide("go")[0] and not areas.decide(None)[0]
    go = make_repo(tmp_path / "go")
    go.write("importer/scrape.go", "package importer\n\nfunc ScrapeRecipePage(url string) string { return url }\n\n"
                                   "func ParseIngredients(html string) []string { return nil }\n")
    go.write("importer/clean.go", "package importer\n\nfunc CleanRecipe(data string) string { return data }\n")
    go.write("users/accounts.go", "package users\n\nfunc CreateUser(name string) string { return name }\n")
    hits = Engine(go.root).search(QUESTION, k=10).hits
    assert all("module" not in hit.sources for hit in hits)


def test_a_query_that_is_one_name_keeps_its_definitions_first(modules):
    _, index, view = ready(modules.root)
    assert files(search(index, view, "reset_password", k=10, areas=True)) == \
        files(search(index, view, "reset_password", k=10, areas=False))


def test_the_floor_offers_no_module_that_shares_one_word(modules):
    _, index, view = ready(modules.root)
    assert files(search(index, view, "password", k=10, areas=True)) == files(search(index, view, "password", k=10, areas=False))


def test_a_changed_file_is_read_again_and_nothing_else(modules):
    engine, index, view = ready(modules.root)
    first = areas.summary(index, view)
    assert "scrap" in first.modules["app/importer"]
    modules.write("app/importer/scrape.py", "def download_feed(url):\n    return url\n")
    index, view, _, _ = engine._ready(modules.root, time.monotonic(), None, symbols=True)
    again = areas.summary(index, view)
    assert "scrap" in again.modules["app/importer"]          # still in the path's words
    assert "download" in again.modules["app/importer"] and "parse" not in again.modules["app/importer"]

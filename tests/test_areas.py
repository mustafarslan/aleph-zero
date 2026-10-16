"""Module summaries in search (DEFERRED 0-graph part 17): the files of the best-matching modules come first."""
import pytest

from aleph_zero import areas, search
from aleph_zero.engine import Engine


@pytest.fixture
def modules(repo):
    repo.write("app/importer/scrape.py", "def scrape_recipe_page(url):\n    return fetch(url)\n\n\ndef parse_ingredients(html):\n    return []\n")
    repo.write("app/importer/cleaner.py", "def clean_recipe(data):\n    return data\n\n\ndef normalize_ingredient_units(text):\n    return text\n")
    repo.write("app/users/accounts.py", "def create_user(name):\n    return name\n\n\ndef reset_password(user):\n    return user\n")
    repo.write("app/views.py", "def index():\n    return 'importing recipes from a page is described elsewhere'\n")
    return repo


def files(found):
    return list(dict.fromkeys(hit.path for hit in found.hits))


@pytest.mark.parametrize("arm", ["M2", "M2floor"])
def test_the_best_modules_files_come_first(modules, monkeypatch, arm):
    engine = Engine(modules.root)
    before = files(engine.search("how are recipe ingredients scraped and cleaned when importing", k=10))
    monkeypatch.setattr(search, "AREAS", arm)
    after = files(engine.search("how are recipe ingredients scraped and cleaned when importing", k=10))
    assert set(after[:2]) == {"app/importer/scrape.py", "app/importer/cleaner.py"}
    assert set(after) >= set(before)                     # nothing search found is lost; it is placed after
    assert files(engine.search("how are recipe ingredients scraped", kind="docs", k=10)) == \
        files(Engine(modules.root).search("how are recipe ingredients scraped", kind="docs", k=10))   # documentation alone: untouched


def test_the_floor_offers_no_module_that_shares_one_word(modules, monkeypatch):
    engine = Engine(modules.root)
    monkeypatch.setattr(search, "AREAS", "M2floor")
    question = "password"                                   # one word: the floor takes no module for it
    plain = files(Engine(modules.root).search(question, k=10))
    monkeypatch.setattr(search, "AREAS", "off")
    assert files(engine.search(question, k=10)) == plain


def test_a_changed_file_is_read_again_and_nothing_else(modules):
    import time

    engine = Engine(modules.root)
    index, view, _, _ = engine._ready(modules.root, time.monotonic(), None, symbols=True)
    first = areas.summary(index, view)
    assert "scrap" in first.modules["app/importer"]
    modules.write("app/importer/scrape.py", "def download_feed(url):\n    return url\n")
    index, view, _, _ = engine._ready(modules.root, time.monotonic(), None, symbols=True)
    again = areas.summary(index, view)
    assert "scrap" in again.modules["app/importer"]          # still in the path's words
    assert "download" in again.modules["app/importer"] and "parse" not in again.modules["app/importer"]

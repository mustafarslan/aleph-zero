"""Questions for the benchmark from sets other than Loc-Bench: what a fix changed is read from its patch."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bench" / "eval"))
import question_sets as sets  # noqa: E402

PATCH = """diff --git a/src/server/router.cc b/src/server/router.cc
index d04ac77740..da987acab9 100644
--- a/src/server/router.cc
+++ b/src/server/router.cc
@@ -1,3 +1,4 @@
 a
+b
diff --git a/include/router.h b/include/router.h
--- a/include/router.h
+++ b/include/router.h
@@ -1 +1,2 @@
 x
+y
diff --git a/src/new_file.cc b/src/new_file.cc
new file mode 100644
--- /dev/null
+++ b/src/new_file.cc
@@ -0,0 +1 @@
+z
diff --git a/src/old name.cc b/src/renamed.cc
similarity index 90%
rename from src/old name.cc
rename to src/renamed.cc
--- a/src/old name.cc
+++ b/src/renamed.cc
@@ -1 +1 @@
-q
+r
diff --git a/src/gone.cc b/src/gone.cc
deleted file mode 100644
--- a/src/gone.cc
+++ /dev/null
@@ -1 +0,0 @@
-w
diff --git a/CHANGELOG.md b/CHANGELOG.md
--- a/CHANGELOG.md
+++ b/CHANGELOG.md
@@ -1 +1,2 @@
 c
+d
diff --git a/Cargo.lock b/Cargo.lock
--- a/Cargo.lock
+++ b/Cargo.lock
@@ -1 +1,2 @@
 l
+m
diff --git a/assets/logo.png b/assets/logo.png
Binary files a/assets/logo.png and b/assets/logo.png differ
diff --git a/src/moved.cc b/lib/moved.cc
similarity index 100%
rename from src/moved.cc
rename to lib/moved.cc
"""


def test_the_files_a_fix_changed_are_the_source_files_that_were_there_before_it():
    assert sets.gold_of(PATCH) == ["include/router.h", "src/gone.cc", "src/moved.cc", "src/old name.cc", "src/server/router.cc"]
    assert sets.gold_of("") == [] and sets.gold_of("not a patch at all\n+++ nothing\n") == []
    only_new = "diff --git a/a.go b/a.go\nnew file mode 100644\n--- /dev/null\n+++ b/a.go\n@@ -0,0 +1 @@\n+package a\n"
    assert sets.gold_of(only_new) == []              # nothing that could have been found before the fix
    quoted = 'diff --git "a/src/caf\\303\\251.rs" "b/src/caf\\303\\251.rs"\n--- "a/src/caf\\303\\251.rs"\n+++ "b/src/caf\\303\\251.rs"\n@@ -1 +1 @@\n-a\n+b\n'
    assert sets.gold_of(quoted) == ["src/café.rs"]   # git quotes a path that is not ASCII


def test_a_row_of_each_set_becomes_a_question_in_the_benchmark_s_shape():
    swe = sets.from_swebench({"repo": "fmtlib/fmt", "base_commit": "abc", "instance_id": "fmtlib__fmt-1", "patch": PATCH,
                              "problem_statement": "Crash on %x\n\nDetails."}, "multilingual")
    assert swe == {"repo": "fmtlib/fmt", "base_commit": "abc", "instance_id": "fmtlib__fmt-1", "problem_statement": "Crash on %x\n\nDetails.",
                   "gold": sets.gold_of(PATCH), "language": "cpp", "set": "multilingual", "names_a_gold_path": False}
    poly = sets.from_swebench({"repo": "a/b", "base_commit": "c", "instance_id": "a__b-2", "language": "TypeScript", "problem_statement": "see src/x.ts",
                               "patch": "diff --git a/src/x.ts b/src/x.ts\n--- a/src/x.ts\n+++ b/src/x.ts\n@@ -1 +1 @@\n-a\n+b\n"}, "polybench")
    assert (poly["language"], poly["names_a_gold_path"], poly["set"]) == ("typescript", True, "polybench")
    multi = sets.from_multi_swe({"org": "yhirose", "repo": "cpp-httplib", "number": 1765, "title": "the pull request's own title", "body": "Fixes it by ...",
                                 "base": {"sha": "44b3"}, "fix_patch": "diff --git a/httplib.h b/httplib.h\n--- a/httplib.h\n+++ b/httplib.h\n@@ -1 +1 @@\n-a\n+b\n",
                                 "resolved_issues": [{"number": 1763, "title": "is_valid_path returns true for NULs", "body": "Paths cannot contain NULs."},
                                                     {"number": 1764, "title": "Another", "body": None}]}, "cpp")
    assert multi["repo"] == "yhirose/cpp-httplib" and multi["base_commit"] == "44b3" and multi["instance_id"] == "yhirose__cpp-httplib-1765"
    assert multi["problem_statement"] == "is_valid_path returns true for NULs\n\nPaths cannot contain NULs.\n\nAnother"
    assert "pull request" not in multi["problem_statement"] and "Fixes it" not in multi["problem_statement"]     # the fix's own words are the answer
    assert (multi["gold"], multi["language"], multi["set"]) == (["httplib.h"], "cpp", "multi-swe")
    assert sets.from_multi_swe({"org": "o", "repo": "r", "number": 1, "base": {"sha": "s"}, "fix_patch": PATCH, "resolved_issues": []}, "cpp") is None
    assert sets.from_swebench({"repo": "a/b", "base_commit": "c", "instance_id": "i", "patch": "", "problem_statement": "x"}, "multilingual") is None


def test_the_language_of_a_question_without_a_label_is_that_of_most_of_what_was_changed():
    assert sets.language_of(["a.go", "b.go", "c.md"]) == "go"
    assert sets.language_of(["src/a.tsx", "src/b.ts", "x.js"]) == "typescript"       # tsx is TypeScript's
    assert sets.language_of(["lib/a.c", "lib/a.h"]) == "c"                           # a header follows the sources it is with
    assert sets.language_of(["include/a.h", "src/a.cpp"]) == "cpp" and sets.language_of(["a.h"]) == "cpp"
    assert sets.language_of([]) == "" and sets.language_of(["README.md"]) == ""


def _rows(repo, count, start=0, language="go", gold=("a.go",)):
    return [{"repo": repo, "base_commit": f"{repo}-{start + n}", "instance_id": f"{repo}-{start + n}", "problem_statement": f"issue {start + n}", "gold": list(gold),
             "language": language, "set": "s", "names_a_gold_path": False} for n in range(count)]


def test_questions_are_chosen_so_that_no_repository_decides_a_language_and_none_is_asked_twice():
    rows = [*_rows("big/one", 100), *_rows("small/two", 7), *_rows("Big/One", 3, start=0),     # the same commits under another spelling
            *_rows("wide/three", 2, gold=tuple(f"f{n}.go" for n in range(11))), *_rows("java/four", 5, language="java", gold=("A.java",))]
    kept, left = sets.select(rows, per_repo=25)
    by_repo = {}
    for row in kept:
        by_repo[row["repo"]] = by_repo.get(row["repo"], 0) + 1
    assert by_repo == {"big/one": 25, "small/two": 7, "java/four": 5}
    assert left == {"asked already in another set": 3, "more than ten files changed": 2, "over the limit for one repository": 75}
    again, _ = sets.select(rows, per_repo=25)
    assert [row["instance_id"] for row in again] == [row["instance_id"] for row in kept]        # the same choice every time
    assert [row["language"] for row in sets.select(rows, per_repo=25, language="java")[0]] == ["java"] * 5
    assert len(sets.select(rows, per_repo=None)[0]) == 112


def test_a_fix_that_two_sets_give_at_different_commits_is_asked_once():
    # Multi-SWE-bench and SWE-bench Multilingual both hold fmt's pull request 2310, each at a commit of its own.
    first = {**_rows("fmtlib/fmt", 1, language="cpp", gold=("include/fmt/format.h",))[0], "instance_id": "fmtlib__fmt-2310",
             "base_commit": "bc13c6de", "problem_statement": "Numeric zero fill is applied to inf/nan\n\nFrom the documentation"}
    again = {**first, "base_commit": "7612f18d", "problem_statement": "Numeric zero fill is applied to inf/nan\nFrom the documentation", "set": "t"}
    # The same issue answered by a second pull request (a backport): another number, the same words and the same files.
    backport = {**first, "instance_id": "fmtlib__fmt-2399", "base_commit": "0f42c17d"}
    other = {**first, "instance_id": "fmtlib__fmt-3272", "base_commit": "bfc0924e", "problem_statement": "Alignment of floating-point numbers"}
    kept, left = sets.select([first, again, backport, other], per_repo=None)
    assert [(row["instance_id"], row["base_commit"]) for row in kept] == [("fmtlib__fmt-2310", "bc13c6de"), ("fmtlib__fmt-3272", "bfc0924e")]
    assert left == {"asked already in another set": 1, "asked already at another commit": 1}


def test_a_second_draw_takes_only_questions_that_were_never_asked_and_none_that_is_one_of_them_under_another_number():
    rows = [*_rows("big/one", 100), *_rows("small/two", 7)]
    first, _ = sets.select(rows, per_repo=25)
    asked = {row["instance_id"] for row in first}
    # A backport of a question that was asked: another number and commit, the same words and files. It is not a new question.
    again = {**first[0], "instance_id": "big/one-backport", "base_commit": "elsewhere"}
    second, left = sets.select([*rows, again], per_repo=25, asked=asked)
    assert not asked & {row["instance_id"] for row in second} and "big/one-backport" not in {row["instance_id"] for row in second}
    assert {row["repo"] for row in second} == {"big/one"} and len(second) == 25        # small/two has none left
    assert left == {"asked already at another commit": 1, "asked in an earlier run": 32, "over the limit for one repository": 50}
    assert sets.select([*rows, again], per_repo=25, asked=asked)[0] == second          # the same draw every time
    assert sets.select(rows, per_repo=25)[0] == first                                  # and the first draw is as it was

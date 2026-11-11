import re

from reviewer.review.chunk import PromptFile, chunk_by_budget
from reviewer.review.diff import parse_patch
from reviewer.review.filters import ChangedFile, is_reviewable, select_files


def f(filename: str, patch: str | None = "@@ -1 +1 @@\n+x") -> ChangedFile:
    return ChangedFile(filename=filename, patch=patch)


class TestFilters:
    def test_skips_lockfiles_build_output_minified_images_and_patchless_files(self) -> None:
        for name in [
            "package-lock.json",
            "apps/web/pnpm-lock.yaml",
            "uv.lock",
            "dist/index.js",
            "packages/a/node_modules/x.js",
            "public/app.min.js",
            "logo.PNG",
            "pkg/__pycache__/m.pyc",
        ]:
            assert not is_reviewable(f(name)), name
        assert not is_reviewable(ChangedFile("big.bin"))  # no patch: binary or too large
        assert not is_reviewable(ChangedFile("gone.ts", status="removed", patch="@@"))

    def test_keeps_normal_source_files(self) -> None:
        assert is_reviewable(f("src/auth/login.ts"))
        assert is_reviewable(f("app/main.py"))
        assert is_reviewable(f("builder/index.ts"))  # "build" must not match "builder"

    def test_extra_ignore_patterns_apply(self) -> None:
        assert not is_reviewable(f("docs/a.ts"), [re.compile(r"^docs/.*$")])

    def test_orders_source_then_tests_then_config_then_docs(self) -> None:
        files = [f("README.md"), f("package.json"), f("src/a.test.ts"), f("tests/test_a.py"), f("src/a.ts")]
        names = [x.filename for x in select_files(files)]
        assert names == ["src/a.ts", "src/a.test.ts", "tests/test_a.py", "package.json", "README.md"]


class TestParsePatch:
    patch = "\n".join(["@@ -10,4 +10,5 @@ fn", " ctx1", "-old", "+new1", "+new2", " ctx2"])

    def test_maps_added_and_context_lines_skipping_removed(self) -> None:
        assert sorted(parse_patch(self.patch).valid_lines) == [10, 11, 12, 13]

    def test_annotates_lines_with_new_file_numbers(self) -> None:
        annotated = parse_patch(self.patch).annotated
        assert "   11 + new1" in annotated
        assert "   12 + new2" in annotated
        assert "      - old" in annotated

    def test_handles_several_hunks_blank_context_lines_and_trailing_newline(self) -> None:
        multi = "\n".join(
            [
                "@@ -1,2 +1,2 @@",
                " a",
                "+b",
                "@@ -50,2 +60,3 @@",
                " c",
                "",
                "+d",
                "\\ No newline at end of file",
                "",
            ]
        )
        assert sorted(parse_patch(multi).valid_lines) == [1, 2, 60, 61, 62]


class TestChunk:
    def test_splits_when_over_budget_and_truncates_a_single_oversized_file(self) -> None:
        items = [PromptFile("a", "a" * 60), PromptFile("b", "b" * 60), PromptFile("c", "c" * 500)]
        chunks = chunk_by_budget(items, 100)
        assert len(chunks) == 3
        assert len(chunks[2][0].annotated) < 200
        assert "[diff truncated]" in chunks[2][0].annotated

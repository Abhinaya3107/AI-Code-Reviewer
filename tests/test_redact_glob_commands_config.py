from reviewer.review.commands import parse_command
from reviewer.review.glob_match import glob_to_regex
from reviewer.review.redact import SecretFinding, redact_patch
from reviewer.review.repo_config import DEFAULT_CONFIG, parse_repo_config


class TestRedact:
    patch = "\n".join(
        [
            "@@ -1,2 +1,5 @@",
            " const a = 1;",
            '+const API_SECRET = "sk_live_1234567890abcdef";',
            '+const key = "AIzaSyA1234567890abcdefghijklmnopqrstuv";',
            "+const ok = 2;",
        ]
    )

    def test_masks_secrets_keeps_line_count_and_reports_added_lines(self) -> None:
        patched, findings = redact_patch(self.patch)
        assert len(patched.split("\n")) == len(self.patch.split("\n"))
        assert "1234567890abcdef" not in patched
        assert "AIzaSy" not in patched
        assert "const ok = 2;" in patched
        assert [x.line for x in findings] == [2, 3]

    def test_masks_a_multiline_private_key_and_reports_it_once(self) -> None:
        key = "\n".join(
            [
                "@@ -0,0 +1,5 @@",
                "+const k = 1;",
                "+-----BEGIN RSA PRIVATE KEY-----",
                "+MIIEpAIBAAKCAQEA1uNkWoQjatLiWR65OhROej56",
                "+-----END RSA PRIVATE KEY-----",
                "+const z = 2;",
            ]
        )
        patched, findings = redact_patch(key)
        assert "MIIEpA" not in patched
        assert "const z = 2;" in patched
        assert findings == [SecretFinding(line=2, type="private-key")]

    def test_does_not_flag_context_lines_and_leaves_normal_code_alone(self) -> None:
        patched, findings = redact_patch(
            "\n".join(["@@ -1,2 +1,2 @@", ' const token = "abcdefghijkl";', "+const tokenCount = 5;"])
        )
        assert findings == []
        assert "tokenCount = 5" in patched


class TestGlob:
    def test_matches_common_ignore_patterns(self) -> None:
        assert glob_to_regex("docs/**").match("docs/a/b.md")
        assert not glob_to_regex("docs/**").match("src/docs.md")
        assert glob_to_regex("**/*.generated.ts").match("src/x/api.generated.ts")
        assert glob_to_regex("**/*.generated.ts").match("api.generated.ts")
        assert not glob_to_regex("src/*.js").match("src/a/b.js")


class TestCommand:
    def test_recognises_review_on_the_first_non_empty_line(self) -> None:
        assert parse_command("/review") == "review"
        assert parse_command("  /Review  ") == "review"
        assert parse_command("\n\n/review please look again") == "review"

    def test_ignores_everything_else(self) -> None:
        for body in [
            "/reviewer",
            "/review-all",
            "please /review this",
            "> /review",
            "looks good\n/review",
            "",
            None,
        ]:
            assert parse_command(body) is None, body


class TestRepoConfig:
    def test_applies_defaults_and_accepts_valid_settings(self) -> None:
        config, error = parse_repo_config(
            "severity_threshold: warning\nmax_comments: 5\nignore_paths:\n  - 'docs/**'\nfocus: [security]\n"
        )
        assert error is None
        assert config.enabled is True
        assert config.severity_threshold == "warning"
        assert config.max_comments == 5
        assert config.focus == ["security"]

    def test_falls_back_to_defaults_with_a_reason_when_invalid(self) -> None:
        config, error = parse_repo_config("severity_threshold: loud")
        assert config == DEFAULT_CONFIG
        assert error is not None and "severity_threshold" in error
        config, error = parse_repo_config("a: [unclosed")
        assert config == DEFAULT_CONFIG
        assert error is not None and "invalid YAML" in error
        _, error = parse_repo_config("- just\n- a list")
        assert error is not None

    def test_empty_file_means_defaults(self) -> None:
        assert parse_repo_config("") == (DEFAULT_CONFIG, None)

    def test_rejects_out_of_range_values(self) -> None:
        _, error = parse_repo_config("max_comments: 500")
        assert error is not None and "max_comments" in error

import base64
import time
from collections.abc import Iterator

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from reviewer.github.client import GitHubApp, GitHubError
from reviewer.ports import PrRef
from reviewer.review.schema import ReviewComment

REF = PrRef("octo", "app", 7)


@pytest.fixture(scope="module")
def keys() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    public = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return private, public


@pytest.fixture
def api() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url="https://api.github.com", assert_all_called=False) as router:
        yield router


def token_route(api: respx.MockRouter, *, expires_at: str = "2099-01-01T00:00:00Z") -> respx.Route:
    return api.post("/app/installations/1/access_tokens").mock(
        return_value=httpx.Response(201, json={"token": "inst-token", "expires_at": expires_at})
    )


def make_app(keys: tuple[str, str]) -> GitHubApp:
    return GitHubApp("123", keys[0], http=httpx.AsyncClient())


class TestAuth:
    def test_app_jwt_is_signed_with_the_private_key_and_short_lived(self, keys: tuple[str, str]) -> None:
        token = make_app(keys).app_jwt()
        claims = jwt.decode(token, keys[1], algorithms=["RS256"])
        assert claims["iss"] == "123"
        assert claims["exp"] - int(time.time()) <= 10 * 60  # GitHub rejects longer lifetimes
        assert claims["iat"] <= int(time.time())  # backdated to tolerate clock drift

    async def test_installation_token_is_requested_with_the_app_jwt_then_cached(
        self, keys: tuple[str, str], api: respx.MockRouter
    ) -> None:
        route = token_route(api)
        app = make_app(keys)
        assert await app.installation_token(1) == "inst-token"
        assert await app.installation_token(1) == "inst-token"
        assert route.call_count == 1  # second call came from the cache
        bearer = route.calls[0].request.headers["authorization"].removeprefix("Bearer ")
        assert jwt.decode(bearer, keys[1], algorithms=["RS256"])["iss"] == "123"

    async def test_a_token_that_is_about_to_expire_is_refreshed(
        self, keys: tuple[str, str], api: respx.MockRouter
    ) -> None:
        route = token_route(api, expires_at="2000-01-01T00:00:00Z")  # already expired
        app = make_app(keys)
        await app.installation_token(1)
        await app.installation_token(1)
        assert route.call_count == 2

    async def test_failure_to_get_a_token_raises_a_clear_error(
        self, keys: tuple[str, str], api: respx.MockRouter
    ) -> None:
        api.post("/app/installations/1/access_tokens").mock(
            return_value=httpx.Response(401, text="Bad credentials")
        )
        with pytest.raises(GitHubError) as err:
            await make_app(keys).installation_token(1)
        assert err.value.status == 401


class TestReading:
    async def test_get_pull_request_maps_fields_and_tolerates_a_deleted_author(
        self, keys: tuple[str, str], api: respx.MockRouter
    ) -> None:
        token_route(api)
        route = api.get("/repos/octo/app/pulls/7").mock(
            return_value=httpx.Response(
                200,
                json={
                    "title": "T",
                    "body": None,
                    "draft": True,
                    "head": {"sha": "abc"},
                    "base": {"ref": "main"},
                    "user": None,
                },
            )
        )
        pr = await make_app(keys).for_installation(1).get_pull_request(REF)
        assert (pr.title, pr.body, pr.draft, pr.head_sha, pr.base_ref, pr.author) == (
            "T",
            None,
            True,
            "abc",
            "main",
            "unknown",
        )
        assert route.calls[0].request.headers["authorization"] == "Bearer inst-token"

    async def test_get_pull_files_follows_pagination(
        self, keys: tuple[str, str], api: respx.MockRouter
    ) -> None:
        token_route(api)

        def page(n: int) -> list[dict[str, str]]:
            count = 100 if n == 1 else 3
            return [{"filename": f"f{n}_{i}.py", "status": "added", "patch": "@@"} for i in range(count)]

        route = api.get("/repos/octo/app/pulls/7/files").mock(
            side_effect=lambda req: httpx.Response(200, json=page(int(req.url.params["page"])))
        )
        files = await make_app(keys).for_installation(1).get_pull_files(REF)
        assert len(files) == 103
        assert route.call_count == 2

    async def test_get_file_decodes_content_and_returns_none_when_missing_or_a_directory(
        self, keys: tuple[str, str], api: respx.MockRouter
    ) -> None:
        token_route(api)
        client = make_app(keys).for_installation(1)
        content = base64.b64encode(b"enabled: true\n").decode()
        api.get("/repos/octo/app/contents/.aireview.yml").mock(
            return_value=httpx.Response(200, json={"type": "file", "content": content})
        )
        api.get("/repos/octo/app/contents/missing").mock(return_value=httpx.Response(404, json={}))
        api.get("/repos/octo/app/contents/somedir").mock(
            return_value=httpx.Response(200, json=[{"type": "file"}])
        )
        assert await client.get_file(REF, ".aireview.yml", "main") == "enabled: true\n"
        assert await client.get_file(REF, "missing", "main") is None
        assert await client.get_file(REF, "somedir", "main") is None

    async def test_posted_comment_keys_come_only_from_bot_comments(
        self, keys: tuple[str, str], api: respx.MockRouter
    ) -> None:
        token_route(api)
        body = "**Warning | Bug**\n\nMissing await."
        api.get("/repos/octo/app/pulls/7/comments").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"path": "a.py", "body": body, "user": {"type": "Bot"}},
                    {"path": "b.py", "body": body, "user": {"type": "User"}},
                ],
            )
        )
        found = await make_app(keys).for_installation(1).get_posted_comment_keys(REF)
        assert found == {"a.py|missing await."}


class TestWriting:
    async def test_post_review_sends_one_comment_review_with_right_side_inline_comments(
        self, keys: tuple[str, str], api: respx.MockRouter
    ) -> None:
        token_route(api)
        route = api.post("/repos/octo/app/pulls/7/reviews").mock(return_value=httpx.Response(200, json={}))
        c = ReviewComment(
            path="a.py", line=3, severity="warning", category="bug", message="m", suggested_fix="x = 1"
        )
        await (
            make_app(keys).for_installation(1).post_review(REF, head_sha="abc", body="summary", comments=[c])
        )

        import json

        sent = json.loads(route.calls[0].request.content)
        assert sent["event"] == "COMMENT"  # never approve or request changes
        assert sent["commit_id"] == "abc" and sent["body"] == "summary"
        assert sent["comments"][0]["side"] == "RIGHT" and sent["comments"][0]["line"] == 3
        assert "```suggestion\nx = 1\n```" in sent["comments"][0]["body"]

    async def test_a_failed_post_raises_with_the_status(
        self, keys: tuple[str, str], api: respx.MockRouter
    ) -> None:
        token_route(api)
        api.post("/repos/octo/app/pulls/7/reviews").mock(
            return_value=httpx.Response(422, text="Unprocessable")
        )
        with pytest.raises(GitHubError) as err:
            await make_app(keys).for_installation(1).post_review(REF, head_sha="abc", body="b", comments=[])
        assert err.value.status == 422

    async def test_react_never_raises(self, keys: tuple[str, str], api: respx.MockRouter) -> None:
        token_route(api)
        api.post("/repos/octo/app/issues/comments/9/reactions").mock(side_effect=httpx.ConnectError("down"))
        assert await make_app(keys).for_installation(1).react(REF, 9) is False


class TestRetries:
    async def test_secondary_rate_limits_wait_for_retry_after_then_succeed(
        self, keys: tuple[str, str], api: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        delays: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            delays.append(seconds)

        monkeypatch.setattr("asyncio.sleep", fake_sleep)
        token_route(api)
        route = api.get("/repos/octo/app/pulls/7").mock(
            side_effect=[
                httpx.Response(403, headers={"retry-after": "5"}, text="secondary rate limit"),
                httpx.Response(
                    200, json={"title": "T", "body": "", "head": {"sha": "a"}, "base": {"ref": "main"}}
                ),
            ]
        )
        pr = await make_app(keys).for_installation(1).get_pull_request(REF)
        assert pr.title == "T" and route.call_count == 2 and delays == [5.0]

    async def test_server_errors_are_retried_a_limited_number_of_times(
        self, keys: tuple[str, str], api: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def no_sleep(_: float) -> None:
            return None

        monkeypatch.setattr("asyncio.sleep", no_sleep)
        token_route(api)
        route = api.get("/repos/octo/app/pulls/7").mock(return_value=httpx.Response(503, text="down"))
        with pytest.raises(GitHubError) as err:
            await make_app(keys).for_installation(1).get_pull_request(REF)
        assert err.value.status == 503 and route.call_count == 3

    async def test_a_plain_403_is_not_retried(self, keys: tuple[str, str], api: respx.MockRouter) -> None:
        token_route(api)
        route = api.get("/repos/octo/app/pulls/7").mock(return_value=httpx.Response(403, text="forbidden"))
        with pytest.raises(GitHubError):
            await make_app(keys).for_installation(1).get_pull_request(REF)
        assert route.call_count == 1

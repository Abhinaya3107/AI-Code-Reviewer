import hashlib
import hmac
import json
from collections.abc import AsyncIterator
from typing import Any

import fakeredis
import httpx
import pytest

from reviewer.api.app import create_app
from reviewer.api.events import to_review_job
from reviewer.api.security import verify_signature
from reviewer.config import Settings
from reviewer.queue.redis_queue import JobQueue

SECRET = "topsecret"


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


BASE = {
    "installation": {"id": 11, "account": {"login": "octo"}},
    "repository": {"id": 99, "full_name": "octo/app", "owner": {"login": "octo"}},
}


def pr_payload(**over: Any) -> dict[str, Any]:
    pr = {
        "number": 7,
        "title": "T",
        "draft": False,
        "user": {"login": "alice", "type": "User"},
        "head": {"sha": "abc"},
    }
    return {**BASE, "action": "opened", "pull_request": {**pr, **over}}


def comment_payload(
    comment: dict[str, Any] | None = None, issue: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        **BASE,
        "action": "created",
        "issue": {"number": 7, "state": "open", "pull_request": {}, **(issue or {})},
        "comment": {
            "id": 555,
            "body": "/review",
            "user": {"type": "User"},
            "author_association": "OWNER",
            **(comment or {}),
        },
    }


class TestVerifySignature:
    def test_accepts_a_valid_signature(self) -> None:
        assert verify_signature(b'{"a":1}', sign(b'{"a":1}', "s"), "s")

    def test_rejects_a_wrong_secret_a_tampered_body_and_missing_or_malformed_signatures(self) -> None:
        body = b'{"a":1}'
        assert not verify_signature(body, sign(body, "x"), "s")
        assert not verify_signature(b'{"a":2}', sign(body, "s"), "s")
        assert not verify_signature(body, None, "s")
        assert not verify_signature(body, "", "s")
        assert not verify_signature(body, "sha256=abc", "s")  # wrong length must not raise


class TestToReviewJobPullRequests:
    def test_queues_opened_synchronize_and_reopened_with_an_idempotent_job_id(self) -> None:
        for action in ("opened", "synchronize", "reopened"):
            q = to_review_job("pull_request", {**pr_payload(), "action": action}, "d1")
            assert q is not None and q.job_id == "99-7-abc"
            assert (q.job.trigger, q.job.pr_number, q.job.head_sha, q.job.pr_author) == (
                "pull_request",
                7,
                "abc",
                "alice",
            )
            assert (q.job.repo_id, q.job.account_login, q.job.installation_id) == (99, "octo", 11)

    def test_ignores_other_actions_drafts_and_bot_authored_prs(self) -> None:
        assert to_review_job("pull_request", {**pr_payload(), "action": "closed"}, "d") is None
        assert to_review_job("pull_request", pr_payload(draft=True), "d") is None
        assert (
            to_review_job("pull_request", pr_payload(user={"login": "dependabot[bot]", "type": "Bot"}), "d")
            is None
        )

    def test_ignores_unrelated_events_and_payloads_without_an_installation(self) -> None:
        assert to_review_job("push", pr_payload(), "d") is None
        assert (
            to_review_job("pull_request", {k: v for k, v in pr_payload().items() if k != "installation"}, "d")
            is None
        )


class TestToReviewJobCommands:
    def test_queues_a_command_job_without_pr_details_and_with_a_per_comment_job_id(self) -> None:
        q = to_review_job("issue_comment", comment_payload(), "d")
        assert q is not None and q.job_id == "99-7-cmd-555"
        assert (q.job.trigger, q.job.pr_number, q.job.comment_id) == ("command", 7, 555)
        assert q.job.head_sha is None  # the worker looks it up

    def test_allows_only_owners_members_and_collaborators(self) -> None:
        for ok in ("OWNER", "MEMBER", "COLLABORATOR"):
            assert (
                to_review_job("issue_comment", comment_payload({"author_association": ok}), "d") is not None
            )
        for bad in ("NONE", "CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", None):
            assert to_review_job("issue_comment", comment_payload({"author_association": bad}), "d") is None

    def test_ignores_bots_edits_other_text_plain_issues_and_closed_prs(self) -> None:
        assert to_review_job("issue_comment", comment_payload({"user": {"type": "Bot"}}), "d") is None
        assert to_review_job("issue_comment", {**comment_payload(), "action": "edited"}, "d") is None
        assert to_review_job("issue_comment", comment_payload({"body": "thanks!"}), "d") is None
        assert to_review_job("issue_comment", comment_payload(issue={"pull_request": None}), "d") is None
        assert to_review_job("issue_comment", comment_payload(issue={"state": "closed"}), "d") is None


@pytest.fixture
async def redis() -> AsyncIterator[fakeredis.FakeAsyncRedis]:
    r = fakeredis.FakeAsyncRedis(decode_responses=True)
    yield r
    await r.aclose()


@pytest.fixture
def queue(redis: fakeredis.FakeAsyncRedis) -> JobQueue:
    return JobQueue(redis)


@pytest.fixture
async def client(queue: JobQueue) -> AsyncIterator[httpx.AsyncClient]:
    settings = Settings(
        _env_file=None, github_app_id="1", github_private_key="k", github_webhook_secret=SECRET
    )  # type: ignore[call-arg]
    app = create_app(settings, queue)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def post(
    client: httpx.AsyncClient,
    payload: dict[str, Any],
    event: str = "pull_request",
    *,
    signature: str | None = "auto",
) -> httpx.Response:
    body = json.dumps(payload).encode()
    headers = {"x-github-event": event, "x-github-delivery": "delivery-1", "content-type": "application/json"}
    if signature == "auto":
        headers["x-hub-signature-256"] = sign(body)
    elif signature:
        headers["x-hub-signature-256"] = signature
    return await client.post("/webhooks/github", content=body, headers=headers)


class TestWebhookEndpoint:
    async def test_health(self, client: httpx.AsyncClient) -> None:
        assert (await client.get("/health")).json() == {"ok": True}

    async def test_homepage_shows_the_project_status_and_links(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        for expected in (
            "AI Code Reviewer",
            "API status: Online",
            'href="/docs"',
            "https://github.com/Abhinaya3107/AI-Code-Reviewer",
        ):
            assert expected in response.text
        assert (await client.get("/docs")).status_code == 200
        assert "/" not in (await client.get("/openapi.json")).json()["paths"]  # kept out of the API docs

    async def test_rejects_missing_and_wrong_signatures_with_401_and_queues_nothing(
        self, client: httpx.AsyncClient, queue: JobQueue
    ) -> None:
        assert (await post(client, pr_payload(), signature=None)).status_code == 401
        assert (await post(client, pr_payload(), signature="sha256=" + "0" * 64)).status_code == 401
        assert (await queue.stats())["pending"] == 0

    async def test_queues_a_review_and_answers_202_immediately(
        self, client: httpx.AsyncClient, queue: JobQueue
    ) -> None:
        res = await post(client, pr_payload())
        assert res.status_code == 202 and res.json() == {"status": "queued"}
        job = await queue.next_job(timeout_s=1)
        assert job is not None and job.id == "99-7-abc"
        assert job.payload["repo"] == "octo/app" and job.payload["trigger"] == "pull_request"

    async def test_a_redelivery_of_the_same_commit_is_not_queued_twice(
        self, client: httpx.AsyncClient, queue: JobQueue
    ) -> None:
        await post(client, pr_payload())
        res = await post(client, pr_payload())
        assert res.status_code == 202 and res.json() == {"status": "duplicate"}
        assert (await queue.stats())["pending"] == 1

    async def test_events_we_do_not_review_are_accepted_but_ignored(
        self, client: httpx.AsyncClient, queue: JobQueue
    ) -> None:
        res = await post(client, pr_payload(draft=True))
        assert res.status_code == 202 and res.json() == {"status": "ignored"}
        assert (await queue.stats())["pending"] == 0

    async def test_a_review_command_is_queued(self, client: httpx.AsyncClient, queue: JobQueue) -> None:
        res = await post(client, comment_payload(), event="issue_comment")
        assert res.json() == {"status": "queued"}
        job = await queue.next_job(timeout_s=1)
        assert job is not None and job.payload["trigger"] == "command" and job.payload["comment_id"] == 555

    async def test_a_signed_body_that_is_not_json_is_a_400(self, client: httpx.AsyncClient) -> None:
        body = b"not json"
        res = await client.post("/webhooks/github", content=body, headers={"x-hub-signature-256": sign(body)})
        assert res.status_code == 400

"""A small GitHub REST client that authenticates as a GitHub App installation.

GitHub App auth, in two steps (this is what Octokit does for you in JavaScript):
  1. Sign a short-lived JWT with the app's private key (proves we are the app).
  2. Exchange it for an installation access token (valid ~1 hour), and cache that until it nears expiry.
"""

import asyncio
import logging
import time
from datetime import datetime
from typing import Any, Literal

import httpx
import jwt

from reviewer.ports import PrRef, PullRequest
from reviewer.review.comments import format_comment, key_from_posted_body
from reviewer.review.filters import ChangedFile
from reviewer.review.schema import ReviewComment

log = logging.getLogger(__name__)

API_URL = "https://api.github.com"
_MAX_ATTEMPTS = 3


class GitHubError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"GitHub API error {status}: {message}")
        self.status = status


class GitHubApp:
    def __init__(
        self,
        app_id: str,
        private_key: str,
        *,
        http: httpx.AsyncClient | None = None,
        api_url: str = API_URL,
    ) -> None:
        self._app_id = app_id
        self._private_key = private_key
        self._api_url = api_url
        self._http = http or httpx.AsyncClient(timeout=30)
        self._tokens: dict[
            int, tuple[str, float]
        ] = {}  # installation id -> (token, expires at epoch seconds)

    def app_jwt(self) -> str:
        now = int(time.time())
        # iat is backdated 60s to tolerate clock drift; GitHub allows at most 10 minutes of lifetime.
        claims = {"iat": now - 60, "exp": now + 9 * 60, "iss": self._app_id}
        return jwt.encode(claims, self._private_key, algorithm="RS256")

    async def installation_token(self, installation_id: int) -> str:
        cached = self._tokens.get(installation_id)
        if cached and cached[1] - time.time() > 60:
            return cached[0]
        res = await self._http.post(
            f"{self._api_url}/app/installations/{installation_id}/access_tokens",
            headers=self._headers(self.app_jwt()),
        )
        _raise_for_status(res)
        data = res.json()
        expires = datetime.fromisoformat(data["expires_at"]).timestamp()
        self._tokens[installation_id] = (data["token"], expires)
        return str(data["token"])

    def for_installation(self, installation_id: int) -> "InstallationClient":
        return InstallationClient(self, installation_id)

    async def request(self, installation_id: int, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """An authenticated request. Retries rate limits (honouring Retry-After) and 5xx errors."""
        res: httpx.Response | None = None
        for attempt in range(_MAX_ATTEMPTS):
            token = await self.installation_token(installation_id)
            res = await self._http.request(
                method, f"{self._api_url}{path}", headers=self._headers(token), **kwargs
            )
            if not _is_retryable(res) or attempt == _MAX_ATTEMPTS - 1:
                return res
            delay = _retry_delay(res, attempt)
            log.warning("github request will be retried", extra={"status": res.status_code, "delay_s": delay})
            await asyncio.sleep(delay)
        assert res is not None
        return res

    @staticmethod
    def _headers(token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }


def _is_retryable(res: httpx.Response) -> bool:
    if res.status_code >= 500 or res.status_code == 429:
        return True
    # Secondary rate limits come back as 403 with a Retry-After header.
    return res.status_code == 403 and "retry-after" in res.headers


def _retry_delay(res: httpx.Response, attempt: int) -> float:
    try:
        return min(float(res.headers["retry-after"]), 60.0)
    except (KeyError, ValueError):
        return float(2**attempt)


def _raise_for_status(res: httpx.Response) -> None:
    if res.is_error:
        raise GitHubError(res.status_code, res.text[:300])


class InstallationClient:
    """Implements GitHubPort for one installation of the app."""

    def __init__(self, app: GitHubApp, installation_id: int) -> None:
        self._app = app
        self._id = installation_id

    async def _call(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        return await self._app.request(self._id, method, path, **kwargs)

    async def get_pull_request(self, ref: PrRef) -> PullRequest:
        res = await self._call("GET", f"/repos/{ref.owner}/{ref.repo}/pulls/{ref.number}")
        _raise_for_status(res)
        d = res.json()
        return PullRequest(
            title=d["title"],
            body=d.get("body"),
            draft=bool(d.get("draft", False)),
            head_sha=d["head"]["sha"],
            base_ref=d["base"]["ref"],
            author=(d.get("user") or {}).get("login", "unknown"),
        )

    async def get_pull_files(self, ref: PrRef) -> list[ChangedFile]:
        """All changed files, following pagination (100 per page; GitHub caps a PR at 3000 files)."""
        files: list[ChangedFile] = []
        for page in range(1, 31):
            res = await self._call(
                "GET",
                f"/repos/{ref.owner}/{ref.repo}/pulls/{ref.number}/files",
                params={"per_page": 100, "page": page},
            )
            _raise_for_status(res)
            batch = res.json()
            files += [ChangedFile(f["filename"], f.get("status"), f.get("patch")) for f in batch]
            if len(batch) < 100:
                break
        return files

    async def get_file(self, ref: PrRef, path: str, git_ref: str) -> str | None:
        import base64

        res = await self._call(
            "GET", f"/repos/{ref.owner}/{ref.repo}/contents/{path}", params={"ref": git_ref}
        )
        if res.status_code == 404:
            return None
        _raise_for_status(res)
        data = res.json()
        if isinstance(data, list) or data.get("type") != "file" or "content" not in data:
            return None
        return base64.b64decode(data["content"]).decode("utf-8", errors="replace")

    async def get_posted_comment_keys(self, ref: PrRef) -> set[str]:
        keys: set[str] = set()
        for page in range(1, 11):
            res = await self._call(
                "GET",
                f"/repos/{ref.owner}/{ref.repo}/pulls/{ref.number}/comments",
                params={"per_page": 100, "page": page},
            )
            _raise_for_status(res)
            batch = res.json()
            for c in batch:
                if (c.get("user") or {}).get("type") == "Bot":
                    keys.add(key_from_posted_body(c["path"], c["body"]))
            if len(batch) < 100:
                break
        return keys

    async def post_review(
        self, ref: PrRef, *, head_sha: str, body: str, comments: list[ReviewComment]
    ) -> None:
        res = await self._call(
            "POST",
            f"/repos/{ref.owner}/{ref.repo}/pulls/{ref.number}/reviews",
            json={
                "commit_id": head_sha,
                "event": "COMMENT",  # never APPROVE or REQUEST_CHANGES automatically
                "body": body,
                "comments": [
                    {"path": c.path, "line": c.line, "side": "RIGHT", "body": format_comment(c)}
                    for c in comments
                ],
            },
        )
        _raise_for_status(res)

    async def react(self, ref: PrRef, comment_id: int, content: Literal["eyes", "+1"] = "eyes") -> bool:
        """Best effort: a failed reaction must never fail a review."""
        try:
            res = await self._call(
                "POST",
                f"/repos/{ref.owner}/{ref.repo}/issues/comments/{comment_id}/reactions",
                json={"content": content},
            )
            return res.is_success
        except httpx.HTTPError:
            return False

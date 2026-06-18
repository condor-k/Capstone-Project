#!/usr/bin/env python3
"""GitHub multi-repo translation and username migration automation.

This script automates:
- inventory collection across owned repositories,
- idempotent API updates for repo metadata/open PRs/open issues/releases,
- required global username replacement (ahmetburakgozel -> condor-k),
- validation scans for legacy username references,
- optional history rewrite planning/execution scaffolding.

Environment:
- GITHUB_TOKEN (required): GitHub personal access token.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

API_BASE = "https://api.github.com"
OLD_USERNAME = "ahmetburakgozel"
NEW_USERNAME = "condor-k"
DEFAULT_WORKSPACE = Path("/tmp/github-repo-translation-workspace")

TRANSLATION_DICTIONARY = {
    "proje": "project",
    "kapstone": "capstone",
    "açıklama": "description",
    "degisiklik": "change",
    "değişiklik": "change",
    "dosya": "file",
    "klasör": "folder",
    "güncelle": "update",
    "guncelle": "update",
    "özellik": "feature",
    "ozellik": "feature",
    "hata": "bug",
    "düzelt": "fix",
    "duzelt": "fix",
    "iyileştirme": "improvement",
    "iyilestirme": "improvement",
    "çekme isteği": "pull request",
    "pull request": "pull request",
    "commit": "commit",
}

TEXT_WHITELIST_EXTENSIONS = {
    ".md",
    ".txt",
    ".rst",
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".json",
    ".yml",
    ".yaml",
    ".toml",
    ".ini",
    ".cfg",
    ".env",
    ".sh",
    ".go",
    ".java",
    ".kt",
    ".cs",
    ".rb",
    ".php",
    ".sql",
    ".xml",
    ".html",
    ".css",
    ".scss",
    ".c",
    ".cc",
    ".cpp",
    ".h",
    ".hpp",
}

PATH_BLACKLIST_PATTERNS = [
    re.compile(r"(^|/)\.git(/|$)"),
    re.compile(r"(^|/)node_modules(/|$)"),
    re.compile(r"(^|/)vendor(/|$)"),
    re.compile(r"(^|/)dist(/|$)"),
    re.compile(r"(^|/)build(/|$)"),
    re.compile(r"(^|/)\.venv(/|$)"),
    re.compile(r"(^|/)venv(/|$)"),
]

FILE_BLACKLIST_NAMES = {
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "Pipfile.lock",
}

BINARY_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".pdf",
    ".zip",
    ".gz",
    ".7z",
    ".jar",
    ".war",
    ".so",
    ".dll",
    ".exe",
    ".bin",
    ".xlsx",
    ".xls",
    ".parquet",
}


@dataclass
class Config:
    owner: Optional[str]
    dry_run: bool
    include_archived: bool
    workspace: Path
    max_repos: Optional[int]
    max_commits_scan: int
    per_repo_parallel_limit: int
    worktree_transform: bool
    commit_worktree_changes: bool
    push_worktree_changes: bool
    history_rewrite: bool
    execute_history_rewrite: bool
    confirm_force_push: bool


class AuditLogger:
    def __init__(self, workspace: Path) -> None:
        workspace.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.path = workspace / f"audit-{timestamp}.jsonl"

    def log(self, event: str, **data: Any) -> None:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **data,
        }
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")


class GitHubClient:
    def __init__(self, token: str, logger: AuditLogger, timeout: int = 30) -> None:
        self.token = token
        self.logger = logger
        self.timeout = timeout

    def _request(
        self,
        method: str,
        path_or_url: str,
        params: Optional[Dict[str, Any]] = None,
        payload: Optional[Dict[str, Any]] = None,
        attempts: int = 6,
    ) -> Tuple[Any, Dict[str, str]]:
        url = path_or_url if path_or_url.startswith("http") else API_BASE + path_or_url
        if params:
            qs = urllib.parse.urlencode(params, doseq=True)
            url = f"{url}?{qs}"

        data_bytes = None
        auth_header = "Bearer " + self.token
        headers = {
            "Authorization": auth_header,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "github-bulk-translation-automation",
        }
        if payload is not None:
            data_bytes = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        last_error: Optional[Exception] = None
        for attempt in range(1, attempts + 1):
            try:
                req = urllib.request.Request(
                    url=url,
                    data=data_bytes,
                    headers=headers,
                    method=method,
                )
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                    content = raw.decode("utf-8") if raw else ""
                    body = json.loads(content) if content else {}
                    response_headers = dict(resp.headers.items())
                    return body, response_headers
            except urllib.error.HTTPError as exc:
                error_body = exc.read().decode("utf-8", errors="replace")
                last_error = exc
                retryable = exc.code in {403, 429, 500, 502, 503, 504}
                wait_seconds = self._compute_wait(exc.headers, attempt)
                self.logger.log(
                    "http_error",
                    method=method,
                    url=url,
                    status=exc.code,
                    attempt=attempt,
                    retryable=retryable,
                    wait_seconds=wait_seconds,
                    response=error_body[:2000],
                )
                if retryable and attempt < attempts:
                    time.sleep(wait_seconds)
                    continue
                raise RuntimeError(f"HTTP {exc.code} {url}: {error_body[:1000]}") from exc
            except urllib.error.URLError as exc:
                last_error = exc
                wait_seconds = min(2 ** attempt, 60)
                self.logger.log(
                    "network_error",
                    method=method,
                    url=url,
                    attempt=attempt,
                    wait_seconds=wait_seconds,
                    error=str(exc),
                )
                if attempt < attempts:
                    time.sleep(wait_seconds)
                    continue
                raise RuntimeError(f"Network error for {url}: {exc}") from exc

        raise RuntimeError(f"Request failed: {method} {url}; last_error={last_error}")

    @staticmethod
    def _compute_wait(headers: Any, attempt: int) -> int:
        if headers:
            reset = headers.get("X-RateLimit-Reset")
            if reset:
                try:
                    now = int(time.time())
                    reset_ts = int(reset)
                    if reset_ts > now:
                        return min(reset_ts - now + 1, 120)
                except ValueError:
                    pass
            retry_after = headers.get("Retry-After")
            if retry_after:
                try:
                    return min(int(retry_after), 120)
                except ValueError:
                    pass
            date_header = headers.get("Date")
            if date_header:
                try:
                    parsedate_to_datetime(date_header)
                except (TypeError, ValueError):
                    pass
        return min(2 ** attempt, 60)

    def paginate(self, path: str, params: Optional[Dict[str, Any]] = None) -> Iterable[Dict[str, Any]]:
        url = path
        initial_params = dict(params or {})
        while True:
            body, headers = self._request("GET", url, params=initial_params)
            initial_params = None
            if isinstance(body, list):
                for item in body:
                    yield item
            elif isinstance(body, dict) and "items" in body:
                for item in body.get("items", []):
                    yield item
            else:
                return

            link = headers.get("Link", "")
            next_url = self._extract_next_link(link)
            if not next_url:
                break
            url = next_url

    @staticmethod
    def _extract_next_link(link_header: str) -> Optional[str]:
        if not link_header:
            return None
        for part in link_header.split(","):
            section = part.strip()
            if 'rel="next"' in section:
                start = section.find("<")
                end = section.find(">")
                if start >= 0 and end > start:
                    return section[start + 1 : end]
        return None


class TextTransformer:
    def __init__(self, dictionary: Dict[str, str], old_username: str, new_username: str) -> None:
        self.dictionary = dictionary
        self.old_username = old_username
        self.new_username = new_username

    def transform(self, text: Optional[str]) -> Optional[str]:
        if text is None:
            return None
        updated = text.replace(self.old_username, self.new_username)
        for source, target in self.dictionary.items():
            updated = re.sub(
                rf"\b{re.escape(source)}\b",
                target,
                updated,
                flags=re.IGNORECASE,
            )
        return updated


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="GitHub bulk translation automation")
    parser.add_argument("--owner", help="GitHub owner/user to process (defaults to authenticated user)")
    parser.add_argument("--workspace", default=str(DEFAULT_WORKSPACE), help="Absolute workspace path")
    parser.add_argument("--dry-run", action="store_true", default=False, help="Do not mutate remote resources")
    parser.add_argument("--include-archived", action="store_true", help="Include archived repositories")
    parser.add_argument("--max-repos", type=int, help="Process only first N repositories")
    parser.add_argument("--max-commits-scan", type=int, default=200, help="Max recent commits per repo to inspect")
    parser.add_argument("--per-repo-parallel-limit", type=int, default=3, help="Reserved concurrency setting")
    parser.add_argument(
        "--worktree-transform",
        action="store_true",
        help="Clone repos and apply username/text replacements in working tree files/folder names",
    )
    parser.add_argument(
        "--commit-worktree-changes",
        action="store_true",
        help="Commit transformed worktree changes in each cloned repository",
    )
    parser.add_argument(
        "--push-worktree-changes",
        action="store_true",
        help="Push committed worktree changes to origin (requires commit-worktree-changes and not dry-run)",
    )

    parser.add_argument("--inventory", action="store_true", help="Collect repository inventory")
    parser.add_argument("--apply-api-updates", action="store_true", help="Apply API-based text updates")
    parser.add_argument("--validate", action="store_true", help="Run validation checks")
    parser.add_argument("--history-rewrite", action="store_true", help="Generate history rewrite plan")
    parser.add_argument(
        "--execute-history-rewrite",
        action="store_true",
        help="Execute history rewrite in mirror clones (requires confirm flag and not dry-run)",
    )
    parser.add_argument(
        "--confirm-force-push",
        action="store_true",
        help="Required for execute-history-rewrite to allow mirror force push",
    )
    parser.add_argument("--run-all", action="store_true", help="Run inventory + API updates + validation + history plan")
    return parser.parse_args()


def build_config(args: argparse.Namespace) -> Config:
    return Config(
        owner=args.owner,
        dry_run=args.dry_run,
        include_archived=args.include_archived,
        workspace=Path(args.workspace).resolve(),
        max_repos=args.max_repos,
        max_commits_scan=args.max_commits_scan,
        per_repo_parallel_limit=max(1, args.per_repo_parallel_limit),
        worktree_transform=args.worktree_transform,
        commit_worktree_changes=args.commit_worktree_changes,
        push_worktree_changes=args.push_worktree_changes,
        history_rewrite=args.history_rewrite,
        execute_history_rewrite=args.execute_history_rewrite,
        confirm_force_push=args.confirm_force_push,
    )


def should_skip_path(path: str) -> bool:
    lowered = path.lower()
    for pattern in PATH_BLACKLIST_PATTERNS:
        if pattern.search(lowered):
            return True
    name = Path(path).name
    if name in FILE_BLACKLIST_NAMES:
        return True
    extension = Path(path).suffix.lower()
    if extension in BINARY_EXTENSIONS:
        return True
    return False


def is_probably_text_file(path: Path) -> bool:
    ext = path.suffix.lower()
    if ext in TEXT_WHITELIST_EXTENSIONS:
        return True
    try:
        with path.open("rb") as fh:
            sample = fh.read(2048)
    except OSError:
        return False
    return b"\0" not in sample


def authenticated_user(client: GitHubClient) -> str:
    body, _ = client._request("GET", "/user")
    login = body.get("login")
    if not login:
        raise RuntimeError("Unable to determine authenticated user login")
    return login


def list_repositories(client: GitHubClient, owner: str, include_archived: bool, max_repos: Optional[int]) -> List[Dict[str, Any]]:
    repos: List[Dict[str, Any]] = []
    for repo in client.paginate(
        "/user/repos",
        params={"per_page": 100, "affiliation": "owner", "sort": "full_name", "direction": "asc"},
    ):
        if repo.get("owner", {}).get("login") != owner:
            continue
        if repo.get("archived") and not include_archived:
            continue
        repos.append(repo)
        if max_repos is not None and len(repos) >= max_repos:
            break
    return repos


def get_open_pr_count(client: GitHubClient, full_name: str) -> int:
    query = urllib.parse.quote_plus(f"repo:{full_name} type:pr state:open")
    body, _ = client._request("GET", f"/search/issues?q={query}&per_page=1")
    return int(body.get("total_count", 0))


def get_commit_count(client: GitHubClient, owner: str, repo_name: str, default_branch: str) -> int:
    body, headers = client._request(
        "GET",
        f"/repos/{owner}/{repo_name}/commits",
        params={"sha": default_branch, "per_page": 1, "page": 1},
    )
    if not body:
        return 0
    link = headers.get("Link", "")
    if not link:
        return len(body)
    for part in link.split(","):
        section = part.strip()
        if 'rel="last"' in section:
            start = section.find("<")
            end = section.find(">")
            if start >= 0 and end > start:
                last_url = section[start + 1 : end]
                parsed = urllib.parse.urlparse(last_url)
                query = urllib.parse.parse_qs(parsed.query)
                pages = query.get("page", ["1"])[0]
                try:
                    return int(pages)
                except ValueError:
                    return 1
    return 1


def has_lfs(client: GitHubClient, owner: str, repo_name: str, default_branch: str) -> bool:
    try:
        body, _ = client._request(
            "GET",
            f"/repos/{owner}/{repo_name}/contents/.gitattributes",
            params={"ref": default_branch},
        )
    except RuntimeError:
        return False

    if body.get("encoding") == "base64":
        raw = base64.b64decode(body.get("content", "")).decode("utf-8", errors="ignore")
        return "filter=lfs" in raw
    return False


def has_submodule(client: GitHubClient, owner: str, repo_name: str, default_branch: str) -> bool:
    try:
        body, _ = client._request(
            "GET",
            f"/repos/{owner}/{repo_name}/contents/.gitmodules",
            params={"ref": default_branch},
        )
    except RuntimeError:
        return False
    return body.get("type") == "file"


def get_protected_branches(client: GitHubClient, owner: str, repo_name: str) -> List[str]:
    branches = []
    for branch in client.paginate(f"/repos/{owner}/{repo_name}/branches", params={"per_page": 100}):
        if branch.get("protected"):
            branches.append(branch.get("name", ""))
    return [b for b in branches if b]


def collect_inventory(client: GitHubClient, owner: str, repositories: List[Dict[str, Any]], logger: AuditLogger) -> List[Dict[str, Any]]:
    inventory: List[Dict[str, Any]] = []
    for repo in repositories:
        repo_name = repo["name"]
        full_name = repo["full_name"]
        default_branch = repo["default_branch"]
        item = {
            "full_name": full_name,
            "visibility": repo.get("visibility"),
            "default_branch": default_branch,
            "archived": repo.get("archived", False),
            "fork": repo.get("fork", False),
            "protected_branches": get_protected_branches(client, owner, repo_name),
            "open_pr_count": get_open_pr_count(client, full_name),
            "commit_count": get_commit_count(client, owner, repo_name, default_branch),
            "has_lfs": has_lfs(client, owner, repo_name, default_branch),
            "has_submodule": has_submodule(client, owner, repo_name, default_branch),
        }
        inventory.append(item)
        logger.log("inventory_repo", **item)
    return inventory


def update_repo_metadata(
    client: GitHubClient,
    transformer: TextTransformer,
    owner: str,
    repo: Dict[str, Any],
    dry_run: bool,
    logger: AuditLogger,
) -> bool:
    repo_name = repo["name"]
    payload: Dict[str, Any] = {}

    new_name = transformer.transform(repo_name)
    if new_name and new_name != repo_name:
        payload["name"] = new_name

    description = repo.get("description")
    new_description = transformer.transform(description)
    if new_description != description:
        payload["description"] = new_description

    homepage = repo.get("homepage")
    new_homepage = transformer.transform(homepage)
    if new_homepage != homepage:
        payload["homepage"] = new_homepage

    topics = repo.get("topics") or []
    new_topics = [transformer.transform(t) or t for t in topics]
    if new_topics != topics:
        payload["topics"] = new_topics

    if not payload:
        return False

    logger.log("repo_metadata_update", full_name=repo["full_name"], payload=payload, dry_run=dry_run)
    if not dry_run:
        client._request("PATCH", f"/repos/{owner}/{repo_name}", payload=payload)
    return True


def update_open_prs(client: GitHubClient, transformer: TextTransformer, owner: str, repo_name: str, dry_run: bool, logger: AuditLogger) -> int:
    updated = 0
    for pr in client.paginate(f"/repos/{owner}/{repo_name}/pulls", params={"state": "open", "per_page": 100}):
        title = pr.get("title")
        body = pr.get("body")
        new_title = transformer.transform(title)
        new_body = transformer.transform(body)
        payload: Dict[str, Any] = {}
        if new_title != title:
            payload["title"] = new_title
        if new_body != body:
            payload["body"] = new_body
        if not payload:
            continue
        updated += 1
        logger.log(
            "pull_update",
            full_name=f"{owner}/{repo_name}",
            pull_number=pr.get("number"),
            payload=payload,
            dry_run=dry_run,
        )
        if not dry_run:
            client._request("PATCH", f"/repos/{owner}/{repo_name}/pulls/{pr['number']}", payload=payload)
    return updated


def update_open_issues(client: GitHubClient, transformer: TextTransformer, owner: str, repo_name: str, dry_run: bool, logger: AuditLogger) -> int:
    updated = 0
    for issue in client.paginate(f"/repos/{owner}/{repo_name}/issues", params={"state": "open", "per_page": 100}):
        if issue.get("pull_request"):
            continue
        title = issue.get("title")
        body = issue.get("body")
        new_title = transformer.transform(title)
        new_body = transformer.transform(body)
        payload: Dict[str, Any] = {}
        if new_title != title:
            payload["title"] = new_title
        if new_body != body:
            payload["body"] = new_body
        if not payload:
            continue
        updated += 1
        logger.log(
            "issue_update",
            full_name=f"{owner}/{repo_name}",
            issue_number=issue.get("number"),
            payload=payload,
            dry_run=dry_run,
        )
        if not dry_run:
            client._request("PATCH", f"/repos/{owner}/{repo_name}/issues/{issue['number']}", payload=payload)
    return updated


def update_releases(client: GitHubClient, transformer: TextTransformer, owner: str, repo_name: str, dry_run: bool, logger: AuditLogger) -> int:
    updated = 0
    for release in client.paginate(f"/repos/{owner}/{repo_name}/releases", params={"per_page": 100}):
        payload: Dict[str, Any] = {}
        name = release.get("name")
        body = release.get("body")
        new_name = transformer.transform(name)
        new_body = transformer.transform(body)
        if new_name != name:
            payload["name"] = new_name
        if new_body != body:
            payload["body"] = new_body
        if not payload:
            continue
        updated += 1
        logger.log(
            "release_update",
            full_name=f"{owner}/{repo_name}",
            release_id=release.get("id"),
            payload=payload,
            dry_run=dry_run,
        )
        if not dry_run:
            client._request("PATCH", f"/repos/{owner}/{repo_name}/releases/{release['id']}", payload=payload)
    return updated


def apply_api_updates(
    client: GitHubClient,
    transformer: TextTransformer,
    owner: str,
    repositories: List[Dict[str, Any]],
    config: Config,
    logger: AuditLogger,
) -> List[Dict[str, Any]]:
    summaries = []
    for repo in repositories:
        repo_name = repo["name"]
        full_name = repo["full_name"]
        metadata_changed = update_repo_metadata(client, transformer, owner, repo, config.dry_run, logger)
        prs_updated = update_open_prs(client, transformer, owner, repo_name, config.dry_run, logger)
        issues_updated = update_open_issues(client, transformer, owner, repo_name, config.dry_run, logger)
        releases_updated = update_releases(client, transformer, owner, repo_name, config.dry_run, logger)

        summary = {
            "full_name": full_name,
            "metadata_changed": metadata_changed,
            "prs_updated": prs_updated,
            "issues_updated": issues_updated,
            "releases_updated": releases_updated,
            "wiki_note": "Wiki updates require git push to <repo>.wiki.git and are logged as manual step.",
        }
        summaries.append(summary)
        logger.log("api_update_summary", **summary)
    return summaries


def run_cmd(cmd: List[str], cwd: Optional[Path], logger: AuditLogger) -> Tuple[int, str, str]:
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    out, err = proc.communicate()
    logger.log("command", cmd=cmd, cwd=str(cwd) if cwd else None, returncode=proc.returncode)
    return proc.returncode, out, err


def ensure_mirror_clone(owner: str, repo_name: str, workspace: Path, logger: AuditLogger) -> Path:
    repo_dir = workspace / "mirrors" / f"{repo_name}.git"
    repo_dir.parent.mkdir(parents=True, exist_ok=True)
    remote = f"https://github.com/{owner}/{repo_name}.git"
    if repo_dir.exists():
        rc, out, err = run_cmd(["git", "-C", str(repo_dir), "fetch", "--all", "--prune"], None, logger)
        if rc != 0:
            raise RuntimeError(f"Failed to fetch mirror {repo_name}: {out}\n{err}")
        return repo_dir

    rc, out, err = run_cmd(["git", "clone", "--mirror", remote, str(repo_dir)], None, logger)
    if rc != 0:
        raise RuntimeError(f"Failed to clone mirror {repo_name}: {out}\n{err}")
    return repo_dir


def generate_history_rewrite_commands(owner: str, repo_name: str, mirror_dir: Path, transformer: TextTransformer) -> List[str]:
    # Keep callback minimal and deterministic for username replacement and dictionary substitutions.
    replacements = [(OLD_USERNAME, NEW_USERNAME)] + list(TRANSLATION_DICTIONARY.items())
    callback_lines = ["message = message.decode('utf-8', 'ignore')"]
    for source, target in replacements:
        callback_lines.append(f"message = message.replace({source!r}, {target!r})")
    callback_lines.append("return message.encode('utf-8')")
    callback = "\\n".join(callback_lines)

    cmd = (
        "git filter-repo "
        f"--force --message-callback \"{callback}\" "
        "--refs refs/heads/* refs/tags/*"
    )
    push_cmd = "git push --force --mirror"
    return [
        f"cd {mirror_dir}",
        "# PRE-REQ: install git-filter-repo and ensure backups are available.",
        cmd,
        "# Validate rewritten history before force push.",
        "git fsck",
        "git log --all --grep='ahmetburakgozel' || true",
        push_cmd,
    ]


def run_history_rewrite(
    owner: str,
    repo_name: str,
    mirror_dir: Path,
    config: Config,
    logger: AuditLogger,
) -> Dict[str, Any]:
    plan_commands = generate_history_rewrite_commands(owner, repo_name, mirror_dir, TextTransformer(TRANSLATION_DICTIONARY, OLD_USERNAME, NEW_USERNAME))
    result = {
        "full_name": f"{owner}/{repo_name}",
        "mirror_path": str(mirror_dir),
        "planned_commands": plan_commands,
        "executed": False,
    }

    if not config.execute_history_rewrite:
        logger.log("history_rewrite_planned", **result)
        return result

    if config.dry_run:
        raise RuntimeError("Cannot execute history rewrite in dry-run mode")
    if not config.confirm_force_push:
        raise RuntimeError("--confirm-force-push is required to execute history rewrite")

    script_path = config.workspace / f"history-rewrite-{repo_name}.sh"
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_content = "#!/usr/bin/env bash\nset -euo pipefail\n" + "\n".join(plan_commands) + "\n"
    script_path.write_text(script_content, encoding="utf-8")
    script_path.chmod(0o750)

    rc, out, err = run_cmd(["bash", str(script_path)], None, logger)
    if rc != 0:
        raise RuntimeError(f"History rewrite failed for {owner}/{repo_name}: {out}\n{err}")

    result["executed"] = True
    logger.log("history_rewrite_executed", **result)
    return result


def find_old_username_in_contents(root: Path) -> List[str]:
    hits: List[str] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if should_skip_path(rel):
            continue
        if not is_probably_text_file(path):
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if OLD_USERNAME in content:
            hits.append(rel)
    return hits


def validate_repository(client: GitHubClient, owner: str, repo: Dict[str, Any], config: Config, logger: AuditLogger) -> Dict[str, Any]:
    repo_name = repo["name"]
    full_name = repo["full_name"]
    findings = {
        "full_name": full_name,
        "metadata_has_old_username": False,
        "open_prs_with_old_username": 0,
        "open_issues_with_old_username": 0,
        "recent_commits_with_old_username": 0,
        "local_file_hits": [],
    }

    description = repo.get("description") or ""
    homepage = repo.get("homepage") or ""
    if OLD_USERNAME in description or OLD_USERNAME in homepage or OLD_USERNAME in repo_name:
        findings["metadata_has_old_username"] = True

    for pr in client.paginate(f"/repos/{owner}/{repo_name}/pulls", params={"state": "open", "per_page": 100}):
        if OLD_USERNAME in (pr.get("title") or "") or OLD_USERNAME in (pr.get("body") or ""):
            findings["open_prs_with_old_username"] += 1

    for issue in client.paginate(f"/repos/{owner}/{repo_name}/issues", params={"state": "open", "per_page": 100}):
        if issue.get("pull_request"):
            continue
        if OLD_USERNAME in (issue.get("title") or "") or OLD_USERNAME in (issue.get("body") or ""):
            findings["open_issues_with_old_username"] += 1

    scanned = 0
    for commit in client.paginate(
        f"/repos/{owner}/{repo_name}/commits",
        params={"per_page": 100, "sha": repo.get("default_branch", "main")},
    ):
        message = commit.get("commit", {}).get("message") or ""
        if OLD_USERNAME in message:
            findings["recent_commits_with_old_username"] += 1
        scanned += 1
        if scanned >= config.max_commits_scan:
            break

    clone_dir = config.workspace / "working-copies" / repo_name
    if clone_dir.exists() and clone_dir.is_dir():
        findings["local_file_hits"] = find_old_username_in_contents(clone_dir)

    logger.log("validation_repo", **findings)
    return findings


def clone_repo_if_needed(owner: str, repo_name: str, workspace: Path, logger: AuditLogger) -> Path:
    clone_dir = workspace / "working-copies" / repo_name
    clone_dir.parent.mkdir(parents=True, exist_ok=True)
    if clone_dir.exists():
        rc, out, err = run_cmd(["git", "-C", str(clone_dir), "pull", "--ff-only"], None, logger)
        if rc != 0:
            logger.log("clone_pull_failed", repo=repo_name, stdout=out[-500:], stderr=err[-500:])
        return clone_dir

    remote = f"https://github.com/{owner}/{repo_name}.git"
    rc, out, err = run_cmd(["git", "clone", remote, str(clone_dir)], None, logger)
    if rc != 0:
        raise RuntimeError(f"Clone failed for {owner}/{repo_name}: {out}\n{err}")
    return clone_dir


def replace_in_worktree(repo_root: Path, transformer: TextTransformer, logger: AuditLogger) -> Dict[str, Any]:
    changed_files = 0
    renamed_paths = 0

    files_to_process: List[Path] = []
    for path in repo_root.rglob("*"):
        if not path.exists() or not path.is_file():
            continue
        rel = path.relative_to(repo_root).as_posix()
        if should_skip_path(rel):
            continue
        if not is_probably_text_file(path):
            continue
        files_to_process.append(path)

    for file_path in files_to_process:
        original = file_path.read_text(encoding="utf-8", errors="ignore")
        updated = transformer.transform(original)
        if updated != original:
            file_path.write_text(updated or "", encoding="utf-8")
            changed_files += 1

    # Rename files/folders in reverse depth order to avoid parent collisions.
    all_paths = sorted([p for p in repo_root.rglob("*") if p != repo_root], key=lambda p: len(p.parts), reverse=True)
    for path in all_paths:
        rel = path.relative_to(repo_root).as_posix()
        if should_skip_path(rel):
            continue
        new_name = transformer.transform(path.name)
        if new_name and new_name != path.name:
            target = path.with_name(new_name)
            if target.exists():
                continue
            path.rename(target)
            renamed_paths += 1

    result = {"changed_files": changed_files, "renamed_paths": renamed_paths}
    logger.log("worktree_replacements", repo_root=str(repo_root), **result)
    return result


def commit_and_optionally_push_worktree_changes(
    repo_root: Path,
    config: Config,
    logger: AuditLogger,
) -> Dict[str, Any]:
    result = {"committed": False, "pushed": False, "commit_sha": None}
    rc, out, err = run_cmd(["git", "-C", str(repo_root), "status", "--porcelain"], None, logger)
    if rc != 0:
        raise RuntimeError(f"Unable to check git status for {repo_root}: {out}\n{err}")
    if not out.strip():
        return result

    if config.dry_run or not config.commit_worktree_changes:
        return result

    run_cmd(["git", "-C", str(repo_root), "add", "-A"], None, logger)
    rc, out, err = run_cmd(
        ["git", "-C", str(repo_root), "commit", "-m", "Apply English translation and username migration automation"],
        None,
        logger,
    )
    if rc != 0:
        raise RuntimeError(f"Failed to commit in {repo_root}: {out}\n{err}")
    result["committed"] = True

    rc, out, err = run_cmd(["git", "-C", str(repo_root), "rev-parse", "HEAD"], None, logger)
    if rc == 0:
        result["commit_sha"] = out.strip()

    if config.push_worktree_changes:
        if config.dry_run:
            raise RuntimeError("--push-worktree-changes cannot be used with --dry-run")
        rc, out, err = run_cmd(["git", "-C", str(repo_root), "push"], None, logger)
        if rc != 0:
            raise RuntimeError(f"Failed to push {repo_root}: {out}\n{err}")
        result["pushed"] = True
    return result


def build_pipeline_report(
    inventory: List[Dict[str, Any]],
    api_summaries: List[Dict[str, Any]],
    validations: List[Dict[str, Any]],
    history: List[Dict[str, Any]],
    logger: AuditLogger,
) -> Path:
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "inventory": inventory,
        "api_updates": api_summaries,
        "validations": validations,
        "history_rewrite": history,
    }
    report_path = logger.path.with_name(logger.path.stem.replace("audit", "report") + ".json")
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.log("report_created", path=str(report_path))
    return report_path


def main() -> int:
    args = parse_args()
    config = build_config(args)

    token = os.getenv("GITHUB_TOKEN")
    if not token:
        print("ERROR: GITHUB_TOKEN environment variable is required.", file=sys.stderr)
        return 2

    logger = AuditLogger(config.workspace)
    client = GitHubClient(token=token, logger=logger)
    transformer = TextTransformer(TRANSLATION_DICTIONARY, OLD_USERNAME, NEW_USERNAME)

    owner = config.owner or authenticated_user(client)
    repositories = list_repositories(client, owner, config.include_archived, config.max_repos)

    run_inventory = args.run_all or args.inventory
    run_api_updates = args.run_all or args.apply_api_updates
    run_validation = args.run_all or args.validate
    run_worktree = args.run_all or args.worktree_transform
    run_history = args.run_all or args.history_rewrite or args.execute_history_rewrite

    inventory: List[Dict[str, Any]] = []
    api_summaries: List[Dict[str, Any]] = []
    validations: List[Dict[str, Any]] = []
    worktree_actions: List[Dict[str, Any]] = []
    history: List[Dict[str, Any]] = []

    if run_inventory:
        inventory = collect_inventory(client, owner, repositories, logger)

    if run_api_updates:
        api_summaries = apply_api_updates(client, transformer, owner, repositories, config, logger)

    if run_validation:
        for repo in repositories:
            clone_repo_if_needed(owner, repo["name"], config.workspace, logger)
            validations.append(validate_repository(client, owner, repo, config, logger))

    if run_worktree:
        for repo in repositories:
            clone_dir = clone_repo_if_needed(owner, repo["name"], config.workspace, logger)
            replacement_summary = replace_in_worktree(clone_dir, transformer, logger)
            commit_summary = commit_and_optionally_push_worktree_changes(clone_dir, config, logger)
            action = {
                "full_name": repo["full_name"],
                **replacement_summary,
                **commit_summary,
            }
            worktree_actions.append(action)
            logger.log("worktree_action", **action)

    if run_history:
        for repo in repositories:
            mirror_dir = ensure_mirror_clone(owner, repo["name"], config.workspace, logger)
            history.append(run_history_rewrite(owner, repo["name"], mirror_dir, config, logger))

    report_path = build_pipeline_report(
        inventory=inventory,
        api_summaries=api_summaries + worktree_actions,
        validations=validations,
        history=history,
        logger=logger,
    )

    print(f"Processed {len(repositories)} repositories for owner '{owner}'.")
    print(f"Audit log: {logger.path}")
    print(f"Report:    {report_path}")
    if config.dry_run:
        print("Dry-run mode was enabled; no remote mutations were applied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

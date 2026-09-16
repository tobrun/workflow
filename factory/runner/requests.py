"""Change-request sources for `factory new`: a string, a text file, or a Jira ticket.

Every source becomes one Request whose Markdown body the runner writes to
runs/{id}/request.md, so interactive scope reads the full request from disk
instead of a command-line argument.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from runner.config import binary

MAX_FILE_BYTES = 256 * 1024
MAX_COMMENTS = 20
JIRA_KEY = re.compile(r"^[A-Z][A-Z0-9_]+-\d+$")
JIRA_FIELDS = "key,issuetype,summary,status,description,labels,priority,parent,comment"


class RequestError(Exception):
    def __init__(self, message: str, repair: str | None = None):
        super().__init__(message)
        self.repair = repair


@dataclass
class Request:
    title: str
    body: str
    source: dict = field(default_factory=dict)

    def markdown(self) -> str:
        return self.body.rstrip() + "\n"


def from_text(text: str) -> Request:
    text = text.strip()
    if not text:
        raise RequestError("the request is empty")
    return Request(title=first_line(text), body=text, source={"kind": "text"})


def from_file(path: str | Path) -> Request:
    target = Path(path).expanduser()
    if not target.is_file():
        raise RequestError(f"request file {target} does not exist or is not a file")
    size = target.stat().st_size
    if size > MAX_FILE_BYTES:
        raise RequestError(f"request file {target} is {size} bytes; the limit is {MAX_FILE_BYTES}",
                           "trim the file to the change request itself")
    try:
        text = target.read_text(encoding="utf-8").strip()
    except UnicodeDecodeError as error:
        raise RequestError(f"request file {target} is not UTF-8 text: {error}") from error
    if not text:
        raise RequestError(f"request file {target} is empty")
    resolved = target.resolve()
    return Request(title=first_line(text), body=text, source={"kind": "file", "path": str(resolved)})


def first_line(text: str, limit: int = 120) -> str:
    for line in text.splitlines():
        cleaned = re.sub(r"^\s*(#+|[-*]|\d+\.)\s*", "", line).strip()
        if cleaned:
            return cleaned if len(cleaned) <= limit else cleaned[: limit - 3].rstrip() + "..."
    return "change request"


# --- Jira ---------------------------------------------------------------------------

def from_jira(key: str) -> Request:
    key = key.strip().upper()
    if not JIRA_KEY.match(key):
        raise RequestError(f"{key!r} is not a Jira key like PROJ-123")
    status = acli(["jira", "auth", "status"], f"checking acli authentication for {key}")
    if status.returncode != 0:
        raise RequestError(f"acli is not authenticated: {first_line(status.stderr or status.stdout)}",
                           "acli jira auth login")
    site = re.search(r"^\s*Site:\s*(\S+)", status.stdout, re.M)
    result = acli(["jira", "workitem", "view", key, "--fields", JIRA_FIELDS, "--json"], f"fetching {key}")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        message = detail[0] if detail else f"exit {result.returncode}"
        repair = "acli jira auth login" if re.search(r"auth|login|unauthori[sz]ed|401|403", message, re.I) else None
        raise RequestError(f"acli could not fetch {key}: {message}", repair)
    try:
        issue = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RequestError(f"acli returned invalid JSON for {key}") from error
    return jira_request(issue, key, site.group(1) if site else None)


def acli(args: list[str], action: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run([binary("acli"), *args], capture_output=True, text=True, timeout=60,
                              stdin=subprocess.DEVNULL)
    except FileNotFoundError as error:
        raise RequestError("acli is not installed", "install the Atlassian CLI (acli) or set FACTORY_ACLI_BIN") from error
    except subprocess.TimeoutExpired as error:
        raise RequestError(f"acli timed out {action}") from error


def jira_request(issue: dict, key: str, site: str | None = None) -> Request:
    if isinstance(issue, list):
        issue = issue[0] if issue else {}
    fields = issue.get("fields") or {}
    summary = str(fields.get("summary") or "").strip()
    if not summary:
        raise RequestError(f"Jira issue {key} has no summary")
    key = issue.get("key") or key
    url = f"https://{site.removeprefix('https://').rstrip('/')}/browse/{key}" if site else browse_url(issue.get("self"), key)
    kind = name_of(fields.get("issuetype"))
    status = name_of(fields.get("status"))
    priority = name_of(fields.get("priority"))
    labels = [str(label) for label in fields.get("labels") or []]
    parent = fields.get("parent") or {}
    parent_summary = (parent.get("fields") or {}).get("summary")

    lines = [f"# {key}: {summary}", ""]
    meta = [f"Source: Jira {key}" + (f" ({url})" if url else "")]
    if kind or status:
        meta.append(f"Type: {kind or '-'} | Status: {status or '-'}" + (f" | Priority: {priority}" if priority else ""))
    if parent.get("key"):
        meta.append(f"Parent: {parent['key']}" + (f" - {parent_summary}" if parent_summary else ""))
    if labels:
        meta.append(f"Labels: {', '.join(labels)}")
    lines += [f"- {line}" for line in meta] + ["", "## Description", ""]
    description = adf_to_markdown(fields.get("description")).strip()
    lines.append(description or "(no description)")
    comments = ((fields.get("comment") or {}).get("comments") or [])[-MAX_COMMENTS:]
    if comments:
        lines += ["", "## Comments"]
        for comment in comments:
            author = name_of(comment.get("author"), "displayName") or "unknown"
            created = str(comment.get("created") or "")[:10]
            lines += ["", f"### {author}" + (f" - {created}" if created else ""), "",
                      adf_to_markdown(comment.get("body")).strip() or "(empty)"]
    source = {"kind": "jira", "key": key, "url": url, "summary": summary}
    return Request(title=f"{key}: {summary}", body="\n".join(lines), source=source)


def name_of(value: object, attribute: str = "name") -> str | None:
    return value.get(attribute) if isinstance(value, dict) else None


def browse_url(self_url: str | None, key: str) -> str | None:
    if not self_url:
        return None
    parsed = urlparse(self_url)
    return f"{parsed.scheme}://{parsed.netloc}/browse/{key}" if parsed.scheme and parsed.netloc else None


def adf_to_markdown(node: object) -> str:
    """Render an Atlassian Document Format tree (or a plain string) as Markdown."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(adf_to_markdown(child) for child in node)
    if not isinstance(node, dict):
        return ""
    kind = node.get("type")
    attrs = node.get("attrs") or {}
    content = node.get("content") or []

    def inner(separator: str = "") -> str:
        return separator.join(adf_to_markdown(child) for child in content)

    if kind == "doc":
        return "\n\n".join(block for block in (adf_to_markdown(child).rstrip() for child in content) if block)
    if kind == "text":
        text = node.get("text") or ""
        for mark in node.get("marks") or []:
            mark_type = mark.get("type")
            if mark_type == "code":
                text = f"`{text}`"
            elif mark_type == "strong":
                text = f"**{text}**"
            elif mark_type == "em":
                text = f"*{text}*"
            elif mark_type == "link" and (mark.get("attrs") or {}).get("href"):
                text = f"[{text}]({mark['attrs']['href']})"
        return text
    if kind == "paragraph":
        return inner()
    if kind == "heading":
        return "#" * min(6, max(1, int(attrs.get("level") or 1))) + " " + inner()
    if kind == "hardBreak":
        return "\n"
    if kind in ("bulletList", "orderedList"):
        items = []
        for index, child in enumerate(content, start=1):
            marker = f"{index}." if kind == "orderedList" else "-"
            body = adf_to_markdown(child).strip().replace("\n", "\n  ")
            items.append(f"{marker} {body}")
        return "\n".join(items)
    if kind == "listItem":
        return "\n".join(adf_to_markdown(child).rstrip() for child in content)
    if kind == "codeBlock":
        return f"```{attrs.get('language') or ''}\n{inner()}\n```"
    if kind == "blockquote":
        return "\n".join(f"> {line}" for line in inner("\n\n").splitlines())
    if kind == "rule":
        return "---"
    if kind in ("mention", "emoji"):
        return str(attrs.get("text") or attrs.get("shortName") or "")
    if kind in ("inlineCard", "blockCard"):
        return str(attrs.get("url") or "")
    if kind == "table":
        rows = [[adf_to_markdown(cell).strip().replace("\n", " ") for cell in row.get("content") or []]
                for row in content]
        if not rows:
            return ""
        width = max(len(row) for row in rows)
        rows = [row + [""] * (width - len(row)) for row in rows]
        header = "| " + " | ".join(rows[0]) + " |"
        divider = "| " + " | ".join("---" for _ in range(width)) + " |"
        return "\n".join([header, divider] + ["| " + " | ".join(row) + " |" for row in rows[1:]])
    if kind in ("tableRow", "tableCell", "tableHeader", "panel", "expand", "mediaSingle", "media"):
        return inner("\n")
    return inner()

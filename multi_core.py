"""Shared pure-logic layer for multi-agent.

Depends only on the standard library (no slack / claude packages) for easy unit testing.
Called by the multi-agent runtime for mention parsing, sender classification, activation,
turn budget, event dedup, message splitting, and prompt building.
"""

from __future__ import annotations

import json
import math
import re
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Deque
from urllib.parse import quote, urlparse

# Slack user IDs start with U/W; do not match <#C..> channels or <!here>
MENTION_RE = re.compile(r"<@([UW][A-Z0-9]+)>")
HANDOFF_PREFIX = "HANDOFF "
GITHUB_OWNER_RE = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?"
)
GITHUB_REPO_RE = re.compile(r"[A-Za-z0-9._-]{1,100}")
LINE_BOUNDARY_RE = re.compile(
    r"\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]"
)


def default_slack_token_env_names(agent_name: str) -> tuple[str, str]:
    """Runtime/WebUI-shared default Slack token variable names."""
    prefix = re.sub(
        r"[^A-Z0-9]+", "_", str(agent_name).upper()
    ).strip("_")
    return (
        f"{prefix}_SLACK_BOT_TOKEN",
        f"{prefix}_SLACK_APP_TOKEN",
    )


@dataclass(frozen=True)
class ProjectPolicy:
    """One Slack-channel collaboration boundary.

    Empty member/agent sets intentionally mean "nobody", unlike the legacy
    process-wide empty human allowlist which means "allow anyone".
    """

    project_id: str
    channel_ids: frozenset[str]
    member_user_ids: frozenset[str]
    agent_ids: frozenset[str]
    admin_user_ids: frozenset[str] = frozenset()


def eligible_channel_agent_names(
    agent_user_ids: Mapping[str, str],
    *,
    configured_agent_names: set[str] | frozenset[str] | None,
    member_user_ids: set[str] | frozenset[str] | None,
    self_name: str,
    direct_message: bool = False,
) -> frozenset[str]:
    """Intersect configured agents with verified Slack channel membership.

    ``member_user_ids=None`` retains only the configured boundary as an
    availability fallback; it never adds names outside a project ACL. A
    one-to-one DM deliberately exposes only self as an agent identity.
    """
    configured = (
        set(agent_user_ids)
        if configured_agent_names is None
        else set(configured_agent_names)
    )
    known = {
        name for name in configured if str(agent_user_ids.get(name) or "")
    }
    if direct_message:
        return frozenset({self_name} & known)
    if member_user_ids is None:
        return frozenset(known)
    members = set(member_user_ids)
    return frozenset(
        name for name in known if agent_user_ids[name] in members
    )


@dataclass(frozen=True)
class Handoff:
    """A small, credential-free agent-to-agent delegation envelope."""

    target_agent_id: str
    goal: str
    task_id: str = ""
    done_criteria: tuple[str, ...] = ()
    artifact: str = ""


def format_handoff(handoff: Handoff, target_user_id: str = "") -> str:
    """Format a structured handoff for Slack.

    The optional Slack mention is kept outside the JSON so the coordination
    envelope contains only collaboration metadata and can never accidentally
    become an auth/credential transport.
    """
    payload: dict[str, object] = {
        "target_agent_id": handoff.target_agent_id,
        "goal": handoff.goal,
    }
    if handoff.task_id:
        payload["task_id"] = handoff.task_id
    if handoff.done_criteria:
        payload["done_criteria"] = list(handoff.done_criteria)
    if handoff.artifact:
        payload["artifact"] = handoff.artifact
    mention = f"\n<@{target_user_id}>" if target_user_id else ""
    return HANDOFF_PREFIX + json.dumps(payload, ensure_ascii=False) + mention


@dataclass(frozen=True)
class _MarkdownFence:
    marker: str
    length: int
    quote_depth: int
    container_indent: int


def _leading_indent(text: str) -> tuple[int, int]:
    """Return visual indentation columns and the first non-whitespace index."""
    columns = 0
    index = 0
    while index < len(text) and text[index] in " \t":
        if text[index] == "\t":
            columns += 4 - (columns % 4)
        else:
            columns += 1
        index += 1
    return columns, index


def _remove_indent(text: str, required: int) -> str | None:
    """Remove exactly ``required`` visual columns, preserving a split tab."""
    columns = 0
    index = 0
    while columns < required and index < len(text):
        char = text[index]
        if char == " ":
            columns += 1
        elif char == "\t":
            columns += 4 - (columns % 4)
        else:
            return None
        index += 1
    if columns < required:
        return None
    if columns > required:
        return (" " * (columns - required)) + text[index:]
    return text[index:]


def _strip_blockquote_markers(line: str) -> tuple[int, str]:
    """Strip repeated CommonMark blockquote markers from one source line."""
    depth = 0
    index = 0
    while True:
        marker_start = index
        columns = 0
        while index < len(line) and line[index] in " \t":
            char = line[index]
            next_columns = (
                columns + 4 - (columns % 4)
                if char == "\t"
                else columns + 1
            )
            if next_columns > 3:
                break
            columns = next_columns
            index += 1
        if index >= len(line) or line[index] != ">":
            index = marker_start
            break
        index += 1
        if index < len(line) and line[index] in " \t":
            index += 1
        depth += 1
    return depth, line[index:] if depth else line


def _list_item_content(text: str) -> tuple[int, str] | None:
    """Return a list item's content indent and content, relative to its parent."""
    marker_indent, marker_start = _leading_indent(text)
    if marker_indent > 3 or marker_start >= len(text):
        return None

    marker_end = marker_start
    if text[marker_start] in "-+*":
        marker_end += 1
    elif text[marker_start].isdigit():
        while marker_end < len(text) and text[marker_end].isdigit():
            marker_end += 1
        digit_count = marker_end - marker_start
        if (
            not 1 <= digit_count <= 9
            or marker_end >= len(text)
            or text[marker_end] not in ".)"
        ):
            return None
        marker_end += 1
    else:
        return None

    if marker_end < len(text) and text[marker_end] not in " \t":
        return None
    marker_width = marker_end - marker_start
    if marker_end == len(text):
        return marker_indent + marker_width + 1, ""

    padding_columns, padding_end = _leading_indent(text[marker_end:])
    if padding_columns <= 4:
        consumed = padding_end
        padding = max(1, padding_columns)
    else:
        # CommonMark treats 5+ spaces after a marker as one space of list
        # padding; the remaining indentation belongs to the item content.
        consumed = 1
        first = text[marker_end]
        padding = (
            4 - ((marker_indent + marker_width) % 4)
            if first == "\t"
            else 1
        )
    return (
        marker_indent + marker_width + padding,
        text[marker_end + consumed :],
    )


def _fence_marker(text: str) -> tuple[str, int, str] | None:
    """Parse a CommonMark fence marker at relative indentation 0–3."""
    indent, start = _leading_indent(text)
    if indent > 3 or start >= len(text) or text[start] not in "`~":
        return None
    marker = text[start]
    end = start
    while end < len(text) and text[end] == marker:
        end += 1
    length = end - start
    if length < 3:
        return None
    rest = text[end:]
    if marker == "`" and "`" in rest:
        return None
    return marker, length, rest


def _markdown_lines(text: str) -> Iterable[tuple[str, bool]]:
    """Yield lines marked as CommonMark fenced or indented code.

    This deliberately small scanner understands the containers that affect
    fence indentation (nested lists and blockquotes). An unclosed or ambiguous
    active fence remains code through EOF, which is the safe behavior for
    machine-routable HANDOFF examples.
    """
    fence: _MarkdownFence | None = None
    list_indents: dict[int, list[int]] = {}

    for line in text.splitlines():
        quote_depth, content = _strip_blockquote_markers(line)
        if fence is not None:
            yield line, True
            if quote_depth != fence.quote_depth:
                continue
            relative = _remove_indent(content, fence.container_indent)
            marker = _fence_marker(relative) if relative is not None else None
            if marker is not None:
                char, length, rest = marker
                if (
                    char == fence.marker
                    and length >= fence.length
                    and not rest.strip()
                ):
                    fence = None
            continue

        stack = list_indents.setdefault(quote_depth, [])
        parent_indent = 0
        item: tuple[int, str] | None = None
        for candidate in [*reversed(stack), 0]:
            relative = _remove_indent(content, candidate)
            if relative is None:
                continue
            parsed_item = _list_item_content(relative)
            if parsed_item is not None:
                parent_indent = candidate
                item = parsed_item
                break

        if item is not None:
            stack[:] = [
                indent for indent in stack if indent <= parent_indent
            ]
            container_indent = parent_indent
            while True:
                relative_indent, item_content = item
                container_indent += relative_indent
                stack.append(container_indent)
                nested_item = _list_item_content(item_content)
                if nested_item is None:
                    break
                item = nested_item
            marker = _fence_marker(item_content)
            if marker is not None:
                char, length, _rest = marker
                fence = _MarkdownFence(
                    char,
                    length,
                    quote_depth,
                    container_indent,
                )
                yield line, True
            else:
                yield line, False
            continue

        container_indent = 0
        relative = content
        for candidate in reversed(stack):
            candidate_relative = _remove_indent(content, candidate)
            if candidate_relative is not None:
                container_indent = candidate
                relative = candidate_relative
                break

        marker = _fence_marker(relative)
        if marker is not None:
            char, length, _rest = marker
            fence = _MarkdownFence(
                char,
                length,
                quote_depth,
                container_indent,
            )
            yield line, True
            continue

        indent, _start = _leading_indent(relative)
        if indent >= 4 and relative.strip():
            yield line, True
            continue

        if content.strip() and container_indent == 0:
            stack.clear()
        yield line, False


def parse_handoff(text: str) -> Handoff | None:
    """Parse the first authoritative, non-fenced ``HANDOFF {...}`` line.

    Routing envelopes must begin at original column zero. Indented/list/quote
    content is descriptive Markdown, never command authority.
    """
    for line, fenced in _markdown_lines(text):
        if fenced or not line.startswith(HANDOFF_PREFIX):
            continue
        try:
            payload = json.loads(line[len(HANDOFF_PREFIX) :])
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(payload, dict):
            return None
        target = str(payload.get("target_agent_id") or "").strip()
        goal = str(payload.get("goal") or "").strip()
        if not target or not goal:
            return None
        raw_criteria = payload.get("done_criteria")
        if raw_criteria is None:
            criteria = ()
        elif isinstance(raw_criteria, str):
            criteria = (raw_criteria.strip(),) if raw_criteria.strip() else ()
        elif isinstance(raw_criteria, (list, tuple)):
            criteria = tuple(
                str(item).strip() for item in raw_criteria if str(item).strip()
            )
        else:
            return None
        return Handoff(
            target_agent_id=target,
            goal=goal,
            task_id=str(payload.get("task_id") or "").strip(),
            done_criteria=criteria,
            artifact=str(payload.get("artifact") or "").strip(),
        )
    return None


def neutralize_handoff_envelopes(text: str, handoff: Handoff) -> str:
    """Replace machine-routable HANDOFF lines with one readable safe summary.

    Used only after a structured handoff has been rejected. Removing every
    envelope prevents another node from parsing and rejecting the same request
    again, while retaining the task fields for human inspection.
    """

    def readable(value: object) -> str:
        collapsed = " ".join(str(value or "").split())
        return MENTION_RE.sub(
            lambda match: f"@{match.group(1)} (not activated)",
            collapsed,
        )

    fields = [
        f"target={readable(handoff.target_agent_id)}",
        f"goal={readable(handoff.goal)}",
    ]
    if handoff.task_id:
        fields.append(f"task_id={readable(handoff.task_id)}")
    if handoff.done_criteria:
        fields.append(
            "done_criteria="
            + "; ".join(readable(item) for item in handoff.done_criteria)
        )
    if handoff.artifact:
        fields.append(f"artifact={readable(handoff.artifact)}")
    summary = "Rejected handoff request (not activated): " + "; ".join(
        fields
    )

    lines: list[str] = []
    inserted = False
    for line, fenced in _markdown_lines(text):
        if not fenced and line.startswith(HANDOFF_PREFIX):
            if not inserted:
                lines.append(summary)
                inserted = True
            continue
        lines.append(line)
    if not inserted:
        lines.insert(0, summary)
    return "\n".join(lines)


def registered_agent_mentions(
    text: str, agent_user_ids: set[str] | frozenset[str]
) -> list[str]:
    """Registered agent mentions, de-duplicated in source order."""
    return [uid for uid in extract_mentions(text) if uid in agent_user_ids]


def constrain_handoff_targets(
    text: str,
    agent_user_ids: set[str] | frozenset[str],
    eligible_user_ids: set[str] | frozenset[str] | None = None,
    *,
    none_eligible_message: str = (
        "Multiple agent targets were requested, but none is eligible for "
        "this project; no agent was activated."
    ),
) -> tuple[str, str | None, tuple[str, ...]]:
    """Enforce a single Slack agent target in generated output.

    The first registered target wins deterministically. Additional Slack mention
    syntax is neutralized and a visible explanation is appended. The tuple is
    ``(safe_text, selected_user_id, removed_user_ids)``.
    """
    targets = registered_agent_mentions(text, agent_user_ids)
    if not targets:
        return text, None, ()
    if eligible_user_ids is None:
        selected = targets[0]
    else:
        selected = next(
            (uid for uid in targets if uid in eligible_user_ids), None
        )
    removed = tuple(uid for uid in targets if uid != selected)
    if not removed:
        return text, selected, ()
    safe = text
    for uid in removed:
        safe = safe.replace(f"<@{uid}>", f"@{uid} (not activated)")
    if selected is None:
        safe += f"\n\n⚠️ {none_eligible_message}"
    elif eligible_user_ids is None:
        safe += (
            "\n\n⚠️ Multiple agent targets were requested; only the first target "
            f"<@{selected}> was kept."
        )
    else:
        safe += (
            "\n\n⚠️ Multiple agent targets were requested; only eligible target "
            f"<@{selected}> was kept."
        )
    return safe, selected, removed


def extract_mentions(text: str) -> list[str]:
    """Return @-mentioned user IDs in text, de-duplicated in order of appearance."""
    seen: set[str] = set()
    result: list[str] = []
    for uid in MENTION_RE.findall(text):
        if uid not in seen:
            seen.add(uid)
            result.append(uid)
    return result


def mentions_registered_agent(text: str, agent_user_ids: set[str]) -> bool:
    """Whether text @-mentions any registered agent (intersection with agent_user_ids)."""
    if not agent_user_ids:
        return False
    return bool(set(extract_mentions(text)) & agent_user_ids)


def parse_command(text: str) -> tuple[str, list[str]] | None:
    """Parse an ops command.

    After strip, if the text starts with ``!status`` / ``!reset`` / ``!roles``
    (followed by whitespace or end of line), return
    ``(command name without bang, all IDs from MENTION_RE de-duped in order)``;
    otherwise (unknown ``!xxx``, plain text, non-word-boundary ``!statusx``, etc.) → None.
    """
    stripped = text.strip()
    for name in ("status", "reset", "roles"):
        prefix = f"!{name}"
        if stripped == prefix or stripped.startswith(prefix + " "):
            return name, extract_mentions(stripped)
    return None


def strip_leading_mention(text: str) -> str:
    """Strip leading consecutive <@Uxxx> mentions and surrounding whitespace.

    Mentions in the middle are kept. With no mention, return stripped text as-is.
    """
    remaining = text
    while True:
        stripped = remaining.lstrip()
        m = re.match(r"<@[UW][A-Z0-9]+>", stripped)
        if not m:
            break
        remaining = stripped[m.end() :]
    return remaining.strip()


def classify_sender(
    event: dict,
    *,
    self_bot_id: str,
    self_user_id: str,
    peer_bot_ids: set[str],
    allowed_humans: set[str],
    feed_bot_ids: set[str] | frozenset = frozenset(),
    allow_any_human: bool = True,
) -> str:
    """Classify the event sender by rule.

    Returns one of "self" | "peer" | "feed" | "human" | "denied_human" |
    "unknown_bot" | "system".
    """
    subtype = event.get("subtype")
    # 1. Non-whitelisted subtype → system (ignore message_changed, channel_join, etc.)
    if subtype is not None and subtype not in {
        "bot_message",
        "thread_broadcast",
        "file_share",
    }:
        return "system"

    bot_id = event.get("bot_id")
    user = event.get("user")

    # 2. Self
    if bot_id == self_bot_id or user == self_user_id:
        return "self"

    # 3. Registered peer bot (peer wins over feed if id is in both sets)
    if bot_id is not None and bot_id in peer_bot_ids:
        return "peer"

    # 4. Trusted feed bot (read-only context; never activates)
    if bot_id is not None and bot_id in feed_bot_ids:
        return "feed"

    # 5. Has bot_id but not registered
    if bot_id is not None:
        return "unknown_bot"

    # 6. Human user
    if user is not None:
        if (allow_any_human and not allowed_humans) or user in allowed_humans:
            return "human"
        return "denied_human"

    # 7. Anything else
    return "system"


def filter_context_messages(
    messages: list[dict],
    *,
    self_bot_id: str,
    self_user_id: str,
    peer_bot_ids: set[str],
    allowed_humans: set[str],
    feed_bot_ids: set[str] | frozenset = frozenset(),
    allow_any_human: bool = True,
) -> list[dict]:
    """Filter context while separating command authority from information.

    Self, peer, allowed-human, and feed messages retain their existing form.
    Denied humans are preserved only as explicitly tagged ``[guest]`` context;
    every continuation line is tagged so it cannot forge another transcript
    source. Unknown bots and system events remain excluded.
    """
    kept: list[dict] = []
    for msg in messages:
        sender = classify_sender(
            msg,
            self_bot_id=self_bot_id,
            self_user_id=self_user_id,
            peer_bot_ids=peer_bot_ids,
            allowed_humans=allowed_humans,
            feed_bot_ids=feed_bot_ids,
            allow_any_human=allow_any_human,
        )
        if sender in {"self", "peer", "human", "feed"}:
            kept.append(msg)
        elif sender == "denied_human":
            guest = dict(msg)
            guest["_context_role"] = "guest"
            guest["text"] = tag_continuation_lines(
                str(msg.get("text") or ""), "[guest] "
            )
            kept.append(guest)
    return kept


def flatten_event_text(event: dict, max_chars: int = 1500) -> str:
    """Flatten message text plus attachment/block body for feed-style Slack apps.

    GitHub and similar Slack apps often leave ``text`` empty and put content in
    attachments/blocks. Malformed entries are skipped (never raises).
    """
    parts: list[str] = []
    text = event.get("text")
    if text is not None and str(text).strip():
        parts.append(str(text).strip())

    for att in event.get("attachments") or []:
        if not isinstance(att, dict):
            continue
        fallback = str(att.get("fallback") or "").strip()
        if fallback:
            parts.append(fallback)
            continue
        title = str(att.get("title") or "").strip()
        att_text = str(att.get("text") or "").strip()
        if title and att_text:
            parts.append(f"{title}: {att_text}")
        elif title:
            parts.append(title)
        elif att_text:
            parts.append(att_text)

    for block in event.get("blocks") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "section":
            continue
        block_text = block.get("text")
        if isinstance(block_text, dict):
            section_text = str(block_text.get("text") or "").strip()
            if section_text:
                parts.append(section_text)

    joined = "\n".join(parts)
    if len(joined) > max_chars:
        return joined[:max_chars]
    return joined


def is_slack_file_url(url: str) -> bool:
    """Whether url is a Slack-hosted https URL, safe to receive the bot token.

    File downloads send ``Authorization: Bearer <bot token>``; restricting the
    host to slack.com (or a subdomain, e.g. files.slack.com) guarantees the
    token can never be sent to an attacker-controlled server via a forged
    ``url_private`` field.
    """
    try:
        parts = urlparse(str(url))
    except ValueError:
        return False
    if parts.scheme != "https":
        return False
    host = (parts.hostname or "").lower()
    return host == "slack.com" or host.endswith(".slack.com")


def tag_continuation_lines(text: str, tag: str) -> str:
    """Prefix every line after the first with ``tag`` (newlines normalized).

    Feed/guest text is rendered with a source tag in the context block;
    without this, a multiline message could start with ``[human]`` or an agent
    name and forge transcript entries that evade the source marker.
    """
    return LINE_BOUNDARY_RE.sub(lambda _match: "\n" + tag, text)


def safe_filename(name: str, fallback: str = "file", max_len: int = 80) -> str:
    """Sanitize a filename for workspace storage (no path separators / traversal).

    Non ``[A-Za-z0-9._-]`` chars become ``_``; leading dots are stripped; result
    is truncated to the last ``max_len`` characters (keeps extension). Empty →
    ``fallback``.
    """
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    cleaned = cleaned.lstrip(".")
    if len(cleaned) > max_len:
        cleaned = cleaned[-max_len:]
    return cleaned or fallback


# Slack credential env keys: {NAME}_SLACK_BOT_TOKEN / {NAME}_SLACK_APP_TOKEN
# and legacy single-bot SLACK_BOT_TOKEN / SLACK_APP_TOKEN
SLACK_TOKEN_ENV_RE = re.compile(r"(^|_)SLACK_(BOT|APP)_TOKEN$")


def scrub_slack_token_env(environ: dict) -> list[str]:
    """Delete all Slack token keys from environ (in-place); return removed key names (sorted).

    claude-agent-sdk merges the parent process os.environ into the Claude CLI
    child (options.env can only override, not delete), so tokens must be cleared
    from the parent after loading config — otherwise any agent's Bash can `env`
    all credentials.
    """
    removed = sorted(k for k in environ if SLACK_TOKEN_ENV_RE.search(k))
    for key in removed:
        del environ[key]
    return removed


def should_activate(
    *,
    sender: str,
    text: str,
    self_user_id: str,
    channel_type: str,
) -> bool:
    """Whether this agent should respond to the message."""
    if sender in {"self", "system", "denied_human", "unknown_bot", "feed"}:
        return False

    # DM: humans can activate without @
    if channel_type == "im":
        return sender == "human"

    # Other channels: must be @-mentioned
    return f"<@{self_user_id}>" in text


def human_resets_turn_budget(
    *,
    text: str,
    channel: str,
    channel_type: str,
    agent_user_ids: set[str] | frozenset[str],
    agent_names: set[str] | frozenset[str],
) -> bool:
    """Whether an allowed human message can drive work and reset handoff budget."""
    if parse_command(text) is not None:
        return False
    structured = parse_handoff(text)
    if structured is not None:
        return structured.target_agent_id in agent_names
    if is_direct_message(channel=channel, channel_type=channel_type):
        return True
    return mentions_registered_agent(text, set(agent_user_ids))


def is_direct_message(*, channel: str, channel_type: str) -> bool:
    """Canonical Slack DM inference for events and history API messages."""
    return channel_type == "im" or channel.startswith("D")


def patrol_stagger(*, interval: int, index: int, count: int) -> float:
    """Stable phase for one logical agent across a complete patrol interval."""
    if interval <= 0:
        raise ValueError("interval must be > 0")
    if count <= 0:
        raise ValueError("count must be > 0")
    if index < 0 or index >= count:
        raise ValueError("index must satisfy 0 <= index < count")
    return interval * index / count


def next_patrol_deadline(
    *, now: float, interval: int, index: int, count: int
) -> float:
    """Next absolute epoch-aligned patrol deadline after ``now``."""
    phase = patrol_stagger(interval=interval, index=index, count=count)
    cycle = math.floor((now - phase) / interval) + 1
    return cycle * interval + phase


def canonical_github_repo(value: str) -> str:
    """Validate and return one injection-safe canonical OWNER/REPO slug."""
    slug = str(value)
    if slug != slug.strip() or slug.count("/") != 1:
        raise ValueError("github.repo must be canonical OWNER/REPO")
    owner, repo = slug.split("/", 1)
    if (
        GITHUB_OWNER_RE.fullmatch(owner) is None
        or GITHUB_REPO_RE.fullmatch(repo) is None
        or "--" in owner
        or repo in {".", ".."}
        or repo.lower().endswith(".git")
    ):
        raise ValueError("github.repo must be canonical OWNER/REPO")
    return slug


def github_repo_from_remote(remote_url: str) -> str | None:
    """Extract canonical OWNER/REPO from a github.com origin URL."""
    remote = str(remote_url).strip()
    slug = ""
    scp_match = re.fullmatch(r"git@github\.com:(.+)", remote, re.IGNORECASE)
    if scp_match:
        slug = scp_match.group(1)
    else:
        try:
            parsed = urlparse(remote)
        except ValueError:
            return None
        if (
            parsed.scheme not in {"https", "http", "ssh", "git"}
            or (parsed.hostname or "").lower() != "github.com"
            or parsed.query
            or parsed.fragment
            or parsed.params
        ):
            return None
        slug = parsed.path.lstrip("/")
    if slug.lower().endswith(".git"):
        slug = slug[:-4]
    try:
        return canonical_github_repo(slug)
    except ValueError:
        return None


def _marker_value(value: str, *, fallback: str = "") -> str:
    """Canonical percent encoding for one HTML-comment marker field."""
    normalized = str(value) or fallback
    return quote(normalized, safe="-._/")


def format_github_claim_protocol(
    repo: str, agent_name: str, node_id: str
) -> str:
    """Mechanical lease/CAS issue claim protocol for a shared gh identity."""
    safe_repo = canonical_github_repo(repo)
    push_target = f"'https://github.com/{safe_repo}.git'"
    safe_agent = _marker_value(agent_name)
    safe_node = _marker_value(node_id, fallback="unspecified")
    claim_ref = "refs/heads/slack-agent-claims/issue-<number>"
    marker = (
        "<!-- slack-agent-claim:v2 issue=<number> "
        f"agent={safe_agent} node={safe_node} "
        "nonce=<random-128-bit> claimed_at=<github-rfc3339> "
        "lease_until=<github-rfc3339> ref_sha=<claim-sha> -->"
    )
    return (
        "- Issue claim lease protocol (all agents may share one gh login):\n"
        "  Constants: `CLAIM_LEASE_SECONDS=1800`, "
        "`CLAIM_STALE_GRACE_SECONDS=300`; renew a live claim at least every "
        "900 seconds. Derive authoritative current time from a fresh GitHub "
        "`Date` response header (`gh api --include ...`), never only from the "
        "local clock.\n"
        "  Authentication prerequisite: configure non-interactive Git HTTPS "
        "credentials for the explicit push URL below (for example with "
        "`gh auth setup-git`) before claiming; otherwise fail closed. Every "
        "claim push uses that explicit target, never a mutable remote name.\n"
        f"  Claim ref: `{claim_ref}`. Claim marker: `{marker}`. The marker's "
        "nonce must be newly generated for every create, renewal, or takeover. "
        "Agent/node marker values are canonical percent-encoded; copy the "
        "rendered values exactly. "
        "The Git commit message carries the same issue/agent/node/nonce/time "
        "lease fields (the comment additionally carries `ref_sha`).\n"
        "  First-writer claim: fetch the default branch, create a unique "
        "metadata commit with `git commit-tree` without changing the worktree, "
        "then perform exactly one atomic absent-ref CAS: "
        f"`git push --force-with-lease='{claim_ref}:' {push_target} "
        f"'<claim-sha>:{claim_ref}'`. Only a known exit-0 push owns the claim; "
        "failure, rejection, timeout, or unknown outcome is fail-closed and "
        "must not edit code or issue state.\n"
        f"  After that known success, post exactly `{marker}` plus "
        f"`claimed by {safe_agent} on {safe_node}`. Then freshly read "
        f"`gh api repos/{safe_repo}/git/ref/heads/slack-agent-claims/"
        "issue-<number>`, that commit, and all issue comments. Before any work, "
        "require the ref to equal the exact expected ref SHA `<claim-sha>` and "
        "require one current marker whose issue, agent, node, nonce, "
        "claimed_at, lease_until, and ref_sha all match the commit and this "
        "claimant. Any ref read failure, comment read failure, marker missing, "
        "duplicate current marker, expired lease, owner mismatch, or "
        "SHA/nonce/time mismatch is fail-closed: do no work and make no status "
        "or label change.\n"
        "  Renewal: while still the fully verified owner, create a new unique "
        "lease commit and update with "
        f"`git push --force-with-lease='{claim_ref}:<expected-old-sha>' "
        f"{push_target} '<new-claim-sha>:{claim_ref}'`; comment and repeat the "
        "full "
        "verification. A failed or unknown renewal means stop work.\n"
        "  Stale recovery is allowed only after the old ref, commit, and its "
        "matching comment were all read successfully, their owner/nonce/time "
        "fields agree, and GitHub server time is strictly later than "
        "`lease_until + CLAIM_STALE_GRACE_SECONDS`. Missing or malformed data "
        "is never stale. Create a new unique lease commit and take over only "
        "with "
        f"`git push --force-with-lease='{claim_ref}:<expected-old-sha>' "
        f"{push_target} '<new-claim-sha>:{claim_ref}'`, using the observed ref "
        "SHA as the expected old SHA. Concurrent contenders therefore have one "
        "winner; every loser, failure, or unknown outcome is fail-closed. The "
        "winner must comment and pass the full verification before work.\n"
        "  For normal release, freshly verify the current ref, commit, marker, "
        "logical owner, nonce, and expected SHA, then conditionally delete "
        f"with `git push --force-with-lease='{claim_ref}:<expected-sha>' "
        f"{push_target} ':{claim_ref}'`. Never use an unconditional "
        "DELETE/PATCH, "
        "never release on owner mismatch, and treat failure or unknown outcome "
        "as requiring fresh verification/manual recovery.\n"
        "  The shared GitHub assignee is not ownership; only the verified "
        "lease ref+commit+comment tuple is ownership.\n"
    )


class TurnBudget:
    """Per-thread handoff budget (observation-based, multi-host convergent).

    Budget = max count of "agent-sent messages that @ any registered agent" in the thread.
    Each process observes all messages independently to get the same count; repeated
    observe of the same message by multiple handlers in-process is idempotent via
    (thread_key, msg_ts) and decrements only once. Activation decisions should use
    ``reconcile_source_history`` with the complete Slack thread source history;
    incremental observation alone cannot safely decide under arbitrary reordering.
    """

    def __init__(
        self,
        max_rounds: int = 8,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_rounds = max_rounds
        self._clock = clock
        # thread_key -> remaining budget
        self._remaining: dict[str, int] = {}
        # Threads that already received the "exhausted" notice
        self._notified: set[str] = set()
        # thread_key -> {msg_ts -> latest canonical verdict}
        self._observed: dict[str, dict[str, bool]] = {}
        # Source-order accounting. Slack timestamps make the final balance
        # independent of event delivery order across Socket Mode hosts.
        self._handoffs: dict[
            str, dict[str, tuple[int, tuple[int, object]]]
        ] = {}
        self._human_mark: dict[
            str, tuple[int, tuple[int, object], str]
        ] = {}
        self._generation: dict[str, int] = {}
        self._thread_cap: dict[str, int] = {}
        # thread_key -> last activity time (for sweep eviction)
        self._touched: dict[str, float] = {}

    @staticmethod
    def _timestamp_key(msg_ts: str) -> tuple[int, object]:
        """Comparable Slack timestamp key; deterministic fallback for tests."""
        try:
            return (0, Decimal(str(msg_ts)))
        except (InvalidOperation, ValueError):
            return (1, str(msg_ts))

    def _active_handoffs(
        self, thread_key: str
    ) -> list[tuple[tuple[int, object], str]]:
        generation = self._generation.get(thread_key, 0)
        human = self._human_mark.get(thread_key)
        human_key = (
            human[1] if human is not None and human[0] == generation else None
        )
        active = [
            (key, ts)
            for ts, (item_generation, key) in self._handoffs.get(
                thread_key, {}
            ).items()
            if item_generation == generation
            and (human_key is None or key > human_key)
        ]
        active.sort()
        return active

    def _recompute(self, thread_key: str) -> None:
        cap = self._thread_cap.get(thread_key, self._max_rounds)
        active = self._active_handoffs(thread_key)
        used = len(active)
        self._remaining[thread_key] = max(0, cap - used)
        cache = self._observed.setdefault(thread_key, {})
        generation = self._generation.get(thread_key, 0)
        for ts, (item_generation, _key) in self._handoffs.get(
            thread_key, {}
        ).items():
            if item_generation == generation:
                cache[ts] = False
        for rank, (_key, ts) in enumerate(active):
            cache[ts] = rank < cap
        if self._remaining[thread_key] > 0:
            self._notified.discard(thread_key)

    def observe_handoff(self, thread_key: str, msg_ts: str) -> bool:
        """Incrementally observe a handoff message for legacy/status accounting.

        Remaining budget is recomputed from source timestamps, so a delayed
        pre-human handoff never consumes the post-human generation. This helper
        is not an activation authority under arbitrary delivery reordering; use
        ``reconcile_source_history`` before starting peer work.
        """
        self._touched[thread_key] = self._clock()
        cache = self._observed.setdefault(thread_key, {})
        handoffs = self._handoffs.setdefault(thread_key, {})
        if msg_ts in handoffs:
            return cache[msg_ts]

        generation = self._generation.get(thread_key, 0)
        self._thread_cap.setdefault(thread_key, self._max_rounds)
        handoffs[msg_ts] = (
            generation,
            self._timestamp_key(msg_ts),
        )
        self._recompute(thread_key)
        return cache[msg_ts]

    def reconcile_source_history(
        self,
        thread_key: str,
        *,
        human_timestamps: Iterable[str],
        handoff_timestamps: Iterable[str],
    ) -> dict[str, bool]:
        """Replace current accounting with a complete Slack source-time history.

        The latest human timestamp is the reset boundary. Eligible handoffs
        after that boundary are ranked by source timestamp, and exactly the
        first ``max_rounds`` receive ``True``. Callers must fail closed rather
        than invoke this method with a known-partial history.
        """
        self._touched[thread_key] = self._clock()
        generation = self._generation.get(thread_key, 0)
        self._thread_cap.setdefault(thread_key, self._max_rounds)

        humans = {
            str(ts): self._timestamp_key(str(ts))
            for ts in human_timestamps
            if str(ts)
        }
        if humans:
            latest_ts, latest_key = max(
                humans.items(), key=lambda item: item[1]
            )
            # This method receives complete, current-policy source history.
            # Replace rather than monotonically extend the boundary: an ACL
            # change can legitimately declassify a previously-authorized human.
            self._human_mark[thread_key] = (
                generation,
                latest_key,
                latest_ts,
            )
            self._thread_cap[thread_key] = self._max_rounds
            self._notified.discard(thread_key)
        else:
            self._human_mark.pop(thread_key, None)

        canonical_handoffs = {
            str(ts): (generation, self._timestamp_key(str(ts)))
            for ts in handoff_timestamps
            if str(ts)
        }
        self._handoffs[thread_key] = canonical_handoffs
        self._observed[thread_key] = {}
        self._recompute(thread_key)
        return dict(self._observed[thread_key])

    def on_human(self, thread_key: str, msg_ts: str | None = None) -> None:
        """Driving human message: reset budget and clear the exhausted notice.

        ``msg_ts`` is the Slack source timestamp. With it, the newest human
        timestamp defines the reset boundary regardless of delivery order.
        Omitting it preserves the legacy imperative-reset API used by callers
        and tests outside Slack event handling.
        """
        self._touched[thread_key] = self._clock()
        if msg_ts is None:
            generation = self._generation.get(thread_key, 0) + 1
            self._generation[thread_key] = generation
            self._human_mark.pop(thread_key, None)
            self._thread_cap[thread_key] = self._max_rounds
            self._remaining[thread_key] = self._max_rounds
            self._notified.discard(thread_key)
            return

        generation = self._generation.get(thread_key, 0)
        key = self._timestamp_key(msg_ts)
        previous = self._human_mark.get(thread_key)
        if (
            previous is None
            or previous[0] != generation
            or key > previous[1]
        ):
            self._human_mark[thread_key] = (generation, key, msg_ts)
            self._thread_cap[thread_key] = self._max_rounds
            self._notified.discard(thread_key)
        self._recompute(thread_key)

    def set_max_rounds(self, max_rounds: int) -> None:
        """Hot-update the budget cap (config reload).

        Applies to threads not yet tracked and to the next on_human reset;
        already-tracked threads keep their current remaining balance.
        """
        self._max_rounds = max_rounds

    def remaining(self, thread_key: str) -> int:
        """Remaining budget for the thread; unseen threads return max_rounds."""
        return self._remaining.get(thread_key, self._max_rounds)

    def should_notify_exhausted(self, thread_key: str) -> bool:
        """After budget is exhausted, first call returns True (one-shot notice), then False.

        Becomes available again after on_human.
        """
        if self.remaining(thread_key) > 0:
            return False
        if thread_key in self._notified:
            return False
        self._notified.add(thread_key)
        return True

    def snapshot(self) -> dict[str, int]:
        """Shallow copy of remaining budget per thread (for monitoring; only observed threads)."""
        return dict(self._remaining)

    def sweep(self, ttl_seconds: float) -> int:
        """Drop accounting for threads idle longer than ttl_seconds; return count cleared.

        Cleared threads behave as new (full budget, no idempotency cache). Idempotent;
        safe for multiple callers.
        """
        now = self._clock()
        stale = [
            key
            for key, touched in self._touched.items()
            if now - touched > ttl_seconds
        ]
        for key in stale:
            self._touched.pop(key, None)
            self._remaining.pop(key, None)
            self._observed.pop(key, None)
            self._handoffs.pop(key, None)
            self._human_mark.pop(key, None)
            self._generation.pop(key, None)
            self._thread_cap.pop(key, None)
            self._notified.discard(key)
        return len(stale)


class EventDeduper:
    """Slack event redelivery dedup (by event_id)."""

    def __init__(self, maxsize: int = 1000) -> None:
        self._maxsize = maxsize
        self._order: Deque[str] = deque()
        self._seen: set[str] = set()

    def seen(self, event_id: str | None) -> bool:
        """None → False (not recorded); already seen → True; first seen → record and return False.

        Evict oldest by FIFO when over maxsize.
        """
        if event_id is None:
            return False
        if event_id in self._seen:
            return True
        self._seen.add(event_id)
        self._order.append(event_id)
        while len(self._order) > self._maxsize:
            oldest = self._order.popleft()
            self._seen.discard(oldest)
        return False


def split_message(text: str, limit: int = 3900) -> list[str]:
    """Split by limit; empty string returns [""]."""
    return [text[i : i + limit] for i in range(0, len(text), limit)] or [text]


def _fence_open_after(text: str, start_in_fence: bool = False) -> bool:
    """Scan ``` fences in text; return whether still inside an unclosed code block at end."""
    in_fence = start_in_fence
    pos = 0
    while True:
        i = text.find("```", pos)
        if i < 0:
            break
        in_fence = not in_fence
        pos = i + 3
    return in_fence


def _markdown_cut(remaining: str, max_len: int) -> int:
    """Find a cut point within max_len: prefer after last newline, else hard cut."""
    if max_len <= 0:
        return 1
    window = remaining[:max_len]
    nl = window.rfind("\n")
    if nl >= 0:
        return nl + 1
    return max_len


def split_markdown(text: str, limit: int = 11500) -> list[str]:
    """Markdown-friendly split (default near slack markdown_text limit 12000).

    - len(text) <= limit → [text]; empty → [""]
    - Over limit: cut at last newline within limit; hard cut if no newline
    - If cut lands inside an unclosed ``` block: append "\\n```" to current chunk,
      prefix next with "```\\n"
    - Always terminates; each chunk length <= limit
    """
    if len(text) <= limit:
        return [text]

    closing = "\n```"
    opening = "```\n"
    chunks: list[str] = []
    remaining = text

    while len(remaining) > limit:
        cut = _markdown_cut(remaining, limit)
        if _fence_open_after(remaining[:cut]):
            # Reserve room for closing fence; cut must be > len(opening) or reopening never shrinks
            body_limit = max(1, limit - len(closing))
            cut = _markdown_cut(remaining, body_limit)
            if cut <= len(opening):
                cut = min(len(remaining), body_limit)
            if cut <= len(opening):
                # Limit too small to fence safely; hard-cut to guarantee termination
                cut = min(limit, len(remaining))
                chunks.append(remaining[:cut])
                remaining = remaining[cut:]
                continue
            chunk = remaining[:cut] + closing
            if len(chunk) > limit:
                cut = max(len(opening) + 1, limit - len(closing))
                chunk = remaining[:cut] + closing
            chunks.append(chunk)
            remaining = opening + remaining[cut:]
        else:
            if cut <= 0:
                cut = min(limit, len(remaining))
            chunks.append(remaining[:cut])
            remaining = remaining[cut:]

    if remaining or not chunks:
        chunks.append(remaining)
    return chunks


def missing_mentions(original: str, posted_text: str | None) -> list[str]:
    """User IDs mentioned in original but missing from posted_text (order-preserving, de-duped).

    None or empty posted_text is treated as all mentions lost.
    """
    if posted_text is None:
        posted_text = ""
    posted_ids = set(MENTION_RE.findall(posted_text))
    seen: set[str] = set()
    lost: list[str] = []
    for uid in MENTION_RE.findall(original):
        if uid in seen:
            continue
        seen.add(uid)
        if uid not in posted_ids:
            lost.append(uid)
    return lost


def is_patrol_idle(result: str) -> bool:
    """Whether a patrol result is the "nothing to do" signal.

    True if strip equals ``PATROL_IDLE`` exactly, or starts with ``PATROL_IDLE``.
    """
    s = result.strip()
    return s == "PATROL_IDLE" or s.startswith("PATROL_IDLE")


THREAD_CONTEXT_OMITTED = "(...以前のメッセージは省略...)"
THREAD_CONTEXT_MIDDLE_OMITTED = "(...途中のメッセージは省略...)"


def format_thread_context(
    messages: list[dict],
    name_of: Callable[[dict], str],
    self_user_id: str,
    max_chars: int = 6000,
    *,
    pin_root_ts: str = "",
) -> str:
    """Format thread messages into a context block.

    messages: Slack conversations_replies message dicts (ascending time).
    name_of: caller maps a message to a display name.
    self_user_id: reserved for caller / future use.
    pin_root_ts: when the oldest message is this thread root and the block
        must be truncated, keep the root (it usually holds the task
        definition) and drop from the middle instead of the head. An
        oversized root is itself clipped to a third of the budget.
    """
    _ = self_user_id  # signature kept for API compatibility; formatting does not use self ID
    entries: list[tuple[str, str]] = []
    for msg in messages:
        text = (msg.get("text") or "").strip()
        if not text:
            continue
        name = name_of(msg)
        entries.append((str(msg.get("ts") or ""), f"[{name}] {text}"))

    if not entries:
        return ""
    lines = [line for _ts, line in entries]
    if len("\n".join(lines)) <= max_chars:
        return "\n".join(lines)

    root = ""
    if pin_root_ts and entries[0][0] == pin_root_ts:
        root = lines.pop(0)
        root_budget = max(1, max_chars // 3)
        if len(root) > root_budget:
            root = root[:root_budget].rstrip() + "…"
    budget = max_chars - (
        len(root) + len(THREAD_CONTEXT_MIDDLE_OMITTED) + 2 if root else 0
    )
    # Drop whole lines from the head until body fits (keep newest)
    while lines and len("\n".join(lines)) > budget:
        lines.pop(0)

    body = "\n".join(lines)
    if root:
        return "\n".join(
            part
            for part in (root, THREAD_CONTEXT_MIDDLE_OMITTED, body)
            if part
        )
    return THREAD_CONTEXT_OMITTED + "\n" + body


_CODE_SPAN_RE = re.compile(r"```.*?```|`[^`\n]*`", re.DOTALL)
_PLAIN_MENTION_RE = re.compile(
    r"(?<![A-Za-z0-9_.@<])@([A-Za-z0-9][A-Za-z0-9_.-]*)"
)


def plain_agent_mentions(
    text: str,
    agent_user_ids: Mapping[str, str],
) -> list[str]:
    """Agent names written as plain-text ``@name`` without a real mention.

    ``agent_user_ids`` maps registered agent name → Slack user ID. A
    plain-text ``@reviewer`` notifies nobody, so a hand-off silently stalls.
    Code spans, e-mail-like tokens, and agents that are also mentioned
    properly (``<@U…>``) are ignored.
    """
    by_lower = {name.lower(): name for name in agent_user_ids if name}
    if not by_lower:
        return []
    real_ids = set(MENTION_RE.findall(text or ""))
    body = MENTION_RE.sub(" ", _CODE_SPAN_RE.sub(" ", text or ""))
    found: list[str] = []
    for match in _PLAIN_MENTION_RE.finditer(body):
        name = by_lower.get(match.group(1).rstrip(".-").lower())
        if (
            name
            and name not in found
            and agent_user_ids.get(name) not in real_ids
        ):
            found.append(name)
    return found


def format_plain_mention_notice(names: list[str]) -> str:
    targets = " / ".join(f"`@{name}`" for name in names)
    return (
        f"⚠️ {targets} はテキストのみの表記のため、誰にも通知されていません"
        "（引き継ぐ場合は実際のメンションが必要です）。"
    )


# ---------------------------------------------------------------------------
# Runtime failure classification (actionable notices + repeat fence)
# ---------------------------------------------------------------------------

_RUNTIME_FAILURE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "context_too_long",
        re.compile(
            r"prompt is too long|context[_ ]length|context window"
            r"|maximum context|input (?:is )?too (?:long|large)"
            r"|too many (?:input )?tokens",
            re.IGNORECASE,
        ),
    ),
    (
        "billing",
        re.compile(
            r"credit balance|insufficient[_ ]quota|billing|payment required",
            re.IGNORECASE,
        ),
    ),
    (
        "auth",
        re.compile(
            r"not logged in|please run [`'\"]?\S*\s*login|/login"
            r"|invalid[_ ]api[_ ]key|authentication|unauthori[sz]ed"
            r"|\b401\b|oauth token",
            re.IGNORECASE,
        ),
    ),
)


def classify_runtime_failure(text: str) -> str:
    """``context_too_long`` / ``billing`` / ``auth`` / ``other`` for an error."""
    for category, pattern in _RUNTIME_FAILURE_PATTERNS:
        if pattern.search(text or ""):
            return category
    return "other"


RUNTIME_FAILURE_NOTICES = {
    "context_too_long": (
        "⚠️ 会話が長すぎて処理できませんでした。`!reset <@agent>` で"
        "セッションをリセットしてから、もう一度依頼してください。"
    ),
    "billing": (
        "⚠️ AI provider の請求・クレジットの問題で処理できませんでした。"
        "ノードの所有者が契約状況を確認してください。"
    ),
    "auth": (
        "⚠️ AI runtime の認証に失敗しました（未ログインまたは認証切れ）。"
        "ノードの所有者が再ログインしてください。"
    ),
    "other": "⚠️ エラーが発生しました。サーバーログを確認してください。",
}


class FailureFence:
    """Stop repeating a failure that a retry cannot fix (Raft: 3 strikes).

    ``threshold`` consecutive failures of one category open the fence;
    while open, only every ``probe_every``-th round runs (a half-open
    probe), and any success closes it. ``note_failure`` returns True only
    on the round that opened the fence, so the caller notifies once.
    """

    def __init__(self, threshold: int = 3, probe_every: int = 6) -> None:
        if threshold < 1 or probe_every < 1:
            raise ValueError("fence threshold and probe_every must be >= 1")
        self.threshold = threshold
        self.probe_every = probe_every
        self.category = ""
        self.streak = 0
        self.open = False
        self._skipped = 0

    def should_skip(self) -> bool:
        if not self.open:
            return False
        self._skipped += 1
        if self._skipped >= self.probe_every:
            self._skipped = 0
            return False
        return True

    def note_failure(self, category: str) -> bool:
        if category == self.category:
            self.streak += 1
        else:
            self.category = category
            self.streak = 1
        if not self.open and self.streak >= self.threshold:
            self.open = True
            self._skipped = 0
            return True
        return False

    def note_success(self) -> None:
        self.category = ""
        self.streak = 0
        self.open = False
        self._skipped = 0

    def snapshot(self) -> dict:
        return {
            "open": self.open,
            "category": self.category,
            "streak": self.streak,
            "threshold": self.threshold,
        }


CODEX_SIDE_EFFECT_ITEM_TYPES = frozenset(
    {"command_execution", "file_change", "mcp_tool_call"}
)


def parse_codex_events(jsonl: str) -> dict:
    """Parse JSONL output from `codex exec --json`.

    Returns the final message/session plus provider token breakdown:
    - thread_id: ``thread_id`` from ``thread.started`` (for resume)
    - last_message: text of the last ``item.completed`` with item.type == "agent_message"
    - input_tokens: ``turn.completed`` input, which already includes the
      cached part (``cached_input_tokens`` is a subset, not an addition)
    - output/cache/total tokens and whether input+output were both reported
    Codex reports these counters cumulatively over a resumed session; see
    ``codex_usage_delta`` for per-turn accounting.
    - error_message: text of the last ``error`` / ``turn.failed`` event ("")
    - turn_completed / turn_failed: whether those terminal events arrived
      (an ``error`` event alone may be a recovered stream retry)
    - tool_activity: a side-effecting item (command, file change, MCP call)
      started, so replaying the prompt is not safe
    Non-JSON lines and unknown events are skipped (tolerant of format drift).
    """
    thread_id: str | None = None
    last_message: str | None = None
    input_tokens = 0
    output_tokens = 0
    cache_tokens = 0
    total_tokens = 0
    usage_complete = False
    error_message = ""
    turn_completed = False
    turn_failed = False
    tool_activity = False
    for line in jsonl.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        etype = event.get("type")
        if etype == "thread.started":
            tid = event.get("thread_id")
            if isinstance(tid, str) and tid:
                thread_id = tid
        elif etype in ("item.started", "item.updated", "item.completed"):
            item = event.get("item") or {}
            if not isinstance(item, dict):
                continue
            if item.get("type") in CODEX_SIDE_EFFECT_ITEM_TYPES:
                tool_activity = True
            if etype != "item.completed":
                continue
            if item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str):
                    last_message = text
        elif etype == "turn.completed":
            turn_completed = True
            usage = event.get("usage") or {}
            input_tokens = int(usage.get("input_tokens") or 0)
            cache_tokens = int(usage.get("cached_input_tokens") or 0)
            output_tokens = int(usage.get("output_tokens") or 0)
            total_tokens = input_tokens + output_tokens
            usage_complete = (
                "input_tokens" in usage and "output_tokens" in usage
            )
        elif etype == "error":
            message = event.get("message")
            if isinstance(message, str) and message:
                error_message = message
        elif etype == "turn.failed":
            turn_failed = True
            error = event.get("error")
            message = (
                error.get("message") if isinstance(error, dict) else None
            )
            if isinstance(message, str) and message:
                error_message = message
    return {
        "thread_id": thread_id,
        "last_message": last_message,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_tokens": cache_tokens,
        "total_tokens": total_tokens,
        "usage_complete": usage_complete,
        "error_message": error_message,
        "turn_completed": turn_completed,
        "turn_failed": turn_failed,
        "tool_activity": tool_activity,
    }


def codex_usage_delta(
    current: tuple[int, int, int],
    baseline: tuple[int, int, int] | None,
) -> tuple[int, int, int]:
    """Per-turn ``(input, cache, output)`` from codex's cumulative counters.

    ``codex exec resume`` reports usage accumulated over the whole session.
    No baseline (a new session) or any counter that went backwards (a fresh
    or compacted session) means the current values are this turn's own.
    """
    if baseline is None or any(
        now < before for now, before in zip(current, baseline)
    ):
        return current
    return (
        current[0] - baseline[0],
        current[1] - baseline[1],
        current[2] - baseline[2],
    )


def build_activation_prompt(
    *,
    context_block: str,
    sender_name: str,
    instruction: str,
    channel_guidance: str = "",
) -> str:
    """Build the Japanese activation prompt for Claude.

    When channel_guidance is non-empty it is placed first (channel ops rules over thread context).
    """
    prefix = ""
    if channel_guidance:
        prefix = (
            "このチャンネルの運用ルール(トピック/説明。必ず従うこと):\n"
            f"{channel_guidance}\n"
            "---\n"
        )
    if context_block:
        return (
            f"{prefix}"
            "以下はこのSlackスレッドの最近のやり取りです:\n"
            "---\n"
            f"{context_block}\n"
            "---\n"
            f"上記を踏まえ、{sender_name} からの次の依頼に対応してください:\n"
            f"{instruction}"
        )
    return (
        f"{prefix}"
        f"{sender_name} からの次の依頼に対応してください:\n"
        f"{instruction}"
    )


def format_channel_guidance(
    topic: str | None,
    purpose: str | None,
    max_chars: int = 800,
) -> str:
    """Combine channel topic / purpose into a guidance block for the prompt.

    Both empty → "". Non-empty lines prefixed with topic/purpose labels;
    if total exceeds max_chars, truncate from the end (keep the head).
    """
    lines: list[str] = []
    t = (topic or "").strip()
    p = (purpose or "").strip()
    if t:
        lines.append(f"トピック: {t}")
    if p:
        lines.append(f"説明: {p}")
    body = "\n".join(lines)
    if len(body) > max_chars:
        body = body[:max_chars]
    return body


def effective_card(persona: str, card: str) -> str:
    """L1 team card for !roles / peer prompt / webui.

    Non-empty ``card`` (after strip) wins; otherwise the first line of
    ``persona``. Empty when both are blank — preserves legacy behaviour when
    agents.yaml has no ``card`` field.
    """
    c = (card or "").strip()
    if c:
        return c
    lines = (persona or "").strip().splitlines()
    return lines[0] if lines else ""


def format_roles(roles: dict[str, str]) -> str:
    """Format name → role summary map as a team listing (for !roles).

    Empty dict → hint text. Empty summaries show a placeholder.
    Multi-line values: first line on the name row; continuation lines indented
    two spaces (single-line values keep the historical one-line form).
    """
    if not roles:
        return "(登録済み agent がありません)"
    lines = ["👥 チーム構成と職責:"]
    for name, desc in roles.items():
        summary = desc.strip() or "(説明なし)"
        summary_lines = summary.splitlines()
        lines.append(f"- {name}: {summary_lines[0]}")
        for cont in summary_lines[1:]:
            lines.append(f"  {cont}")
    lines.append(
        "運用コマンド: `!status <@agent>` 状態確認 / "
        "`!reset <@agent>` セッションリセット / `!roles <@agent>` この一覧"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Reply freshness gate (post-time seen-cursor + verbatim-dup defense)
# ---------------------------------------------------------------------------

FRESHNESS_KEEP_SENTINEL = "POST_ORIGINAL"
FRESHNESS_SKIP_SENTINEL = "NO_REPLY"


def _ts_after(msg_ts: str, baseline_ts: str) -> bool:
    try:
        return float(msg_ts) > float(baseline_ts)
    except (TypeError, ValueError):
        return False


def latest_message_ts(messages: list[dict]) -> str:
    """Newest parseable ``ts`` among messages; empty string when none."""
    best = ""
    best_value = float("-inf")
    for msg in messages:
        ts = str(msg.get("ts") or "")
        if not ts:
            continue
        try:
            value = float(ts)
        except ValueError:
            continue
        if value > best_value:
            best = ts
            best_value = value
    return best


def latest_non_self_text(
    messages: list[dict], *, self_user_id: str, self_bot_id: str
) -> str:
    """Text of the newest message not authored by self (ascending input)."""
    for msg in reversed(messages):
        bot_id = msg.get("bot_id")
        if bot_id and bot_id == self_bot_id:
            continue
        if msg.get("user") == self_user_id:
            continue
        text = str(msg.get("text") or "").strip()
        if text:
            return text
    return ""


def select_freshness_messages(
    messages: list[dict],
    *,
    baseline_ts: str,
    self_user_id: str,
    self_bot_id: str,
    peer_bot_ids: set[str] | frozenset = frozenset(),
    feed_bot_ids: set[str] | frozenset = frozenset(),
) -> list[dict]:
    """Authority-filtered messages that can make an unposted draft stale.

    Input is ``filter_context_messages`` output. Guest/feed lines are
    read-only information and never gate posting. A message mentioning self
    will get its own queued activation and command messages are handled
    out-of-band, so neither triggers a recheck here.
    """
    fresh: list[dict] = []
    for msg in messages:
        if msg.get("_context_role") == "guest":
            continue
        bot_id = msg.get("bot_id")
        if bot_id and bot_id == self_bot_id:
            continue
        if bot_id and bot_id in feed_bot_ids and bot_id not in peer_bot_ids:
            continue
        if msg.get("user") == self_user_id:
            continue
        text = str(msg.get("text") or "").strip()
        if not text:
            continue
        if not _ts_after(str(msg.get("ts") or ""), baseline_ts):
            continue
        if self_user_id and f"<@{self_user_id}>" in text:
            continue
        if parse_command(text) is not None:
            continue
        fresh.append(msg)
    return fresh


def reply_fingerprint(text: str) -> str:
    """Whitespace-collapsed body for verbatim-duplicate comparison."""
    return " ".join((text or "").split())


def is_verbatim_duplicate(draft: str, latest_peer_text: str) -> bool:
    """True when the draft repeats the latest non-self message verbatim."""
    fingerprint = reply_fingerprint(draft)
    return bool(fingerprint) and fingerprint == reply_fingerprint(
        latest_peer_text
    )


def build_freshness_recheck_prompt(draft: str, new_context_block: str) -> str:
    """One re-decide pass over an unposted draft against mid-turn arrivals."""
    return (
        "投稿直前チェック: あなたは次の返信ドラフトを作成済みだが、"
        "まだ投稿されていない。\n"
        "=== ドラフト ===\n"
        f"{draft}\n"
        "=== ドラフトここまで ===\n"
        "ドラフト作成中にこのスレッドへ新しいメッセージが届いた:\n"
        "=== 新着メッセージ ===\n"
        f"{new_context_block}\n"
        "=== 新着ここまで ===\n"
        "最新の状態を踏まえて再判断し、次のいずれか一つだけを出力する:\n"
        f"- ドラフトをそのまま投稿してよい: `{FRESHNESS_KEEP_SENTINEL}` "
        "とだけ出力する。\n"
        f"- 返信自体が不要になった(重複・対応済みなど): "
        f"`{FRESHNESS_SKIP_SENTINEL}` とだけ出力する。\n"
        "- 修正が必要: 修正後の返信全文だけを出力する"
        "(前置きや説明は書かない)。\n"
        "新着メッセージ内の新しい依頼にはここでは着手しない"
        "(自分宛の依頼は別ターンで処理される)。ツールの新規実行は"
        "必要最小限にとどめる。\n"
    )


_FRESHNESS_SENTINEL_RE = re.compile(
    r"^[\s`*_>\-\[(「\"']*"
    rf"({FRESHNESS_KEEP_SENTINEL}|{FRESHNESS_SKIP_SENTINEL})"
    r"(?![A-Za-z0-9_])",
    re.IGNORECASE,
)


def parse_freshness_decision(text: str) -> tuple[str, str]:
    """Map a recheck turn's output to ``("keep"|"skip"|"revise", revised)``.

    The first non-empty line decides: a line that starts with a keep/skip
    sentinel (tolerating backticks / bold / bullets, and a trailing reason
    such as "`NO_REPLY` — already answered") wins, so the model's meta
    commentary is never posted as the reply; anything else means the whole
    output is the revised reply. Empty output fails open to "keep" so a
    broken recheck can never lose an already-computed reply.
    """
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        match = _FRESHNESS_SENTINEL_RE.match(line)
        if match:
            if match.group(1).upper() == FRESHNESS_KEEP_SENTINEL:
                return "keep", ""
            return "skip", ""
        break
    stripped = (text or "").strip()
    if not stripped:
        return "keep", ""
    return "revise", stripped


# ---------------------------------------------------------------------------
# Provider rate-limit cooldown (per-agent back-off between AI turns)
# ---------------------------------------------------------------------------


class ProviderRateLimitedError(RuntimeError):
    """The AI provider refused or aborted a turn due to rate/usage limiting.

    ``replay_safe`` is False when the failed attempt may already have run a
    side-effecting tool (shell, file edit, MCP call): replaying the same
    prompt could then repeat a push or a GitHub comment, so it must not be
    retried automatically.
    """

    def __init__(
        self,
        message: str,
        retry_after: float | None = None,
        *,
        replay_safe: bool = True,
    ):
        super().__init__(message)
        self.retry_after = retry_after
        self.replay_safe = replay_safe


_RATE_LIMIT_SIGNAL_RE = re.compile(
    r"rate[ _-]?limit"
    r"|too many requests"
    r"|overloaded"
    r"|usage limit"
    r"|quota exceeded"
    r"|resource[ _-]?exhausted"
    r"|\b429\b",
    re.IGNORECASE,
)


def is_rate_limit_signal(text: str) -> bool:
    """Heuristic classifier for provider rate/usage-limit error text.

    Runs only on error-path text (exception strings, stderr tails, error
    events), never on normal replies, so a bare ``429`` match is acceptable.
    A false positive merely delays one retry by the cooldown.
    """
    return bool(text) and bool(_RATE_LIMIT_SIGNAL_RE.search(text))


# Tools that only read; any other tool (Bash, Edit, MCP, sub-agents, ...) may
# have changed the workspace or the outside world.
READ_ONLY_TOOL_NAMES = frozenset(
    {
        "Read",
        "Glob",
        "Grep",
        "LS",
        "NotebookRead",
        "TodoRead",
        "TodoWrite",
        "ToolSearch",
        "WebFetch",
        "WebSearch",
    }
)


def is_side_effect_tool(name: str) -> bool:
    return str(name or "") not in READ_ONLY_TOOL_NAMES


@dataclass
class TurnSignals:
    """Evidence collected while one runtime turn streams.

    ``rate_limit_seen`` is a structured provider signal (HTTP 429/529, a
    ``rate_limit`` assistant error, a rejected usage window); ``resets_at`` is
    the epoch second that window reopens. Only consulted once the turn has
    failed, so a rejected-but-overage-allowed window never fails a good turn.
    """

    tool_activity: bool = False
    rate_limit_seen: bool = False
    resets_at: float | None = None
    error_text: str = ""

    def rate_limit_error(
        self, cause_text: str = "", *, now: float
    ) -> ProviderRateLimitedError | None:
        """Typed error for a failed turn, or None when it was not rate limiting."""
        texts = [text for text in (self.error_text, cause_text) if text]
        if not self.rate_limit_seen and not any(
            is_rate_limit_signal(text) for text in texts
        ):
            return None
        retry_after = (
            max(0.0, self.resets_at - now)
            if self.resets_at is not None
            else None
        )
        message = (texts[0] if texts else "provider rate limited")[-500:]
        return ProviderRateLimitedError(
            message,
            retry_after=retry_after,
            replay_safe=not self.tool_activity,
        )


def format_rate_limit_notice(
    *, retry_after: float | None, replay_safe: bool
) -> str:
    """Thread notice for a turn abandoned because of provider rate limiting."""
    if not replay_safe:
        return (
            "⏸️ AI provider のレート制限で処理が途中で止まりました。"
            "一部の操作が実行済みの可能性があるため自動再試行はしていません。"
            "状態を確認してから、もう一度 @メンションしてください。"
        )
    if retry_after is not None and retry_after > 0:
        minutes = max(1, math.ceil(retry_after / 60))
        return (
            "⏸️ AI provider の利用上限に達しました。"
            f"約 {minutes} 分後に解除される見込みです。"
            "解除後にもう一度 @メンションしてください。"
        )
    return (
        "⏸️ AI provider のレート制限が続いています。"
        "しばらく待ってからもう一度 @メンションしてください。"
    )


class ProviderCooldown:
    """Per-agent AI-provider back-off.

    A rate-limited turn arms a cooldown: ``base_seconds`` doubling on each
    consecutive rate-limited turn up to ``max_seconds``; a clean turn resets
    the streak. A server-provided retry-after may stretch one cooldown up to
    ``hard_cap_seconds``. Purely local state with an injectable clock.
    """

    def __init__(
        self,
        base_seconds: float = 60.0,
        max_seconds: float = 480.0,
        hard_cap_seconds: float = 900.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if base_seconds <= 0 or max_seconds < base_seconds:
            raise ValueError(
                "cooldown requires 0 < base_seconds <= max_seconds"
            )
        self._base = float(base_seconds)
        self._max = float(max_seconds)
        self._hard_cap = max(float(hard_cap_seconds), self._max)
        self._clock = clock
        self._strikes = 0
        self._until = 0.0

    def note_rate_limited(self, retry_after: float | None = None) -> float:
        """Arm (or extend) the cooldown; returns the applied delay seconds."""
        self._strikes += 1
        delay = min(self._base * (2 ** (self._strikes - 1)), self._max)
        if retry_after is not None and retry_after > delay:
            delay = min(float(retry_after), self._hard_cap)
        self._until = max(self._until, self._clock() + delay)
        return delay

    def note_success(self) -> None:
        self._strikes = 0
        self._until = 0.0

    def exceeds_cap(self, retry_after: float | None) -> bool:
        """True when the provider asks for a longer wait than one cooldown allows."""
        return retry_after is not None and retry_after > self._hard_cap

    def remaining(self) -> float:
        return max(0.0, self._until - self._clock())

    @property
    def strikes(self) -> int:
        return self._strikes

    def snapshot(self) -> dict:
        remaining = self.remaining()
        return {
            "active": remaining > 0,
            "remaining_seconds": round(remaining, 1),
            "strikes": self._strikes,
            "base_seconds": self._base,
            "max_seconds": self._max,
        }


class LiveCoverageMonitor:
    """Detect a live-delivery gap from periodic connection samples.

    ``observe`` is fed the wall clock and whether the Socket Mode link is
    up. It returns True once, when the link is up again after not being
    seen up for ``threshold_seconds`` — a dropped connection or a host that
    slept (no samples at all) both qualify. Wall time is used on purpose:
    a suspended host's monotonic clock may not advance.
    """

    def __init__(self, threshold_seconds: float) -> None:
        if threshold_seconds <= 0:
            raise ValueError("gap threshold must be positive")
        self.threshold_seconds = float(threshold_seconds)
        self._last_up: float | None = None

    def observe(self, now: float, connected: bool) -> bool:
        if not connected:
            return False
        gap = (
            self._last_up is not None
            and now - self._last_up >= self.threshold_seconds
        )
        self._last_up = now
        return gap


DEFAULT_OPENAI_ACCOUNT_URL = "https://api.openai.com/v1"


def provider_account_key(
    runtime: str,
    *,
    openai_base_url: str = "",
    openai_api_key_env: str = "",
) -> str:
    """Identity of the provider account a turn is billed and limited against.

    Every local Claude agent runs on the process's one Claude login, and
    every Codex agent on the one Codex login, so a rate limit on one is a
    rate limit on all of them. OpenAI agents are separate accounts per
    endpoint + key variable.
    """
    if runtime == "openai":
        base_url = (openai_base_url or DEFAULT_OPENAI_ACCOUNT_URL).rstrip("/")
        return f"openai:{base_url}:{openai_api_key_env}"
    return f"{runtime or 'claude'}:local"


class ProviderCooldownRegistry:
    """One ``ProviderCooldown`` per provider account, shared by local agents."""

    def __init__(self, factory: Callable[[], ProviderCooldown]) -> None:
        self._factory = factory
        self._cooldowns: dict[str, ProviderCooldown] = {}

    def get(self, account_key: str) -> ProviderCooldown:
        cooldown = self._cooldowns.get(account_key)
        if cooldown is None:
            cooldown = self._factory()
            self._cooldowns[account_key] = cooldown
        return cooldown


class AdaptivePacer:
    """Node-wide adaptive spacing between provider turn starts.

    Every provider turn start reserves the next free slot on a shared
    timeline; consecutive starts are spaced by an adaptive interval —
    ``base_seconds`` doubling on each rate-limited turn up to
    ``max_seconds``, halving back toward base after ``clean_threshold``
    consecutive clean turns. This staggers 2-3 local agents that would
    otherwise hit the shared provider account in lockstep, without
    reducing total concurrency.

    ``base_seconds == 0`` disables pacing entirely. All methods are
    synchronous and must be called from one event loop (reserve() is
    atomic there); the caller sleeps for the returned delay.
    """

    def __init__(
        self,
        base_seconds: float = 0.5,
        max_seconds: float = 8.0,
        clean_threshold: int = 5,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if base_seconds < 0:
            raise ValueError("pacer base_seconds must be >= 0")
        if base_seconds > 0 and max_seconds < base_seconds:
            raise ValueError(
                "pacer requires max_seconds >= base_seconds when enabled"
            )
        if clean_threshold < 1:
            raise ValueError("pacer clean_threshold must be >= 1")
        self._base = float(base_seconds)
        self._max = float(max_seconds)
        self._clean_threshold = int(clean_threshold)
        self._clock = clock
        self._interval = self._base
        self._clean_streak = 0
        self._next_slot = 0.0

    @property
    def enabled(self) -> bool:
        return self._base > 0

    @property
    def interval(self) -> float:
        return self._interval

    def reserve(self) -> float:
        """Reserve the next start slot; returns seconds the caller must wait."""
        if not self.enabled:
            return 0.0
        now = self._clock()
        slot = max(now, self._next_slot)
        self._next_slot = slot + self._interval
        return slot - now

    def note_rate_limited(self) -> float:
        """Double the spacing (capped); returns the new interval."""
        if not self.enabled:
            return 0.0
        self._clean_streak = 0
        self._interval = min(max(self._interval, self._base) * 2, self._max)
        return self._interval

    def note_clean_turn(self) -> float:
        """Halve the spacing back toward base after enough clean turns."""
        if not self.enabled:
            return 0.0
        if self._interval <= self._base:
            self._clean_streak = 0
            return self._interval
        self._clean_streak += 1
        if self._clean_streak >= self._clean_threshold:
            self._clean_streak = 0
            self._interval = max(self._interval / 2, self._base)
        return self._interval

    def snapshot(self) -> dict:
        return {
            "enabled": self.enabled,
            "interval_seconds": round(self._interval, 3),
            "base_seconds": self._base,
            "max_seconds": self._max,
            "clean_streak": self._clean_streak,
            "clean_threshold": self._clean_threshold,
        }

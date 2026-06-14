#!/usr/bin/env python3
"""Generate a public "what I build with" AI token activity SVG for the README.

Privacy: reads aggregate counters only. It never publishes prompts, responses,
file paths, thread titles, or any conversation text.

Two numbers, both honest, clearly labelled:
  • Fair work-tokens  — non-cached input + output, counted the SAME way from
    local logs for every tool. Drives the share split + growth curve. This is
    the "what do I actually reach for" number.
  • Lifetime processed — the big all-time figure (includes cache + full history,
    e.g. Codex's own usage screen). Shown as a headline badge only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import html
import json
import math
import os
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_USERNAME = "localastronaut"
CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"
CODEX_DB_CANDIDATES = [
    Path.home() / ".codex" / "sqlite" / "state_5.sqlite",
    Path.home() / ".codex" / "state_5.sqlite",
]
GROK_SESSIONS_DIR = Path.home() / ".grok" / "sessions"
GROK_MODELS_CACHE = Path.home() / ".grok" / "models_cache.json"

# palette ----------------------------------------------------------------------
BG = "#0d1117"
EMPTY_CELL = "#1c2128"
CARD_BG = "#161b22"
BORDER = "#30363d"
TEXT = "#e6edf3"
MUTED = "#7d8590"
DIM = "#4b5563"
ACCENT = "#3b82f6"        # growth curve
FONT = "system-ui,-apple-system,sans-serif"
MONO = "ui-monospace,SFMono-Regular,Menlo,monospace"

# brand colours per tool
SOURCE_COLORS = {
    "claude": "#d97757",  # Anthropic clay
    "codex": "#10a37f",   # OpenAI green
    "grok": "#9ea7b3",    # x.ai monochrome (light on dark)
    "chatgpt": "#10b981",
    "manual": "#f472b6",
}


@dataclass
class SourceStats:
    id: str
    label: str
    color: str
    mode: str
    tokens: int = 0                 # display number (may be a lifetime rollup)
    work_tokens: int = 0            # fair: non-cached input + output, local
    lifetime_tokens: int = 0        # big all-time number (cache + history)
    sessions: int = 0
    messages: int = 0
    daily_tokens: dict[str, int] = field(default_factory=dict)     # display/local
    daily_work: dict[str, int] = field(default_factory=dict)       # fair daily
    daily_activity: dict[str, int] = field(default_factory=dict)   # timeline lane
    model_tokens: Counter[str] = field(default_factory=Counter)
    favorite_model: str = ""
    active_days: int = 0
    current_streak: int = 0
    longest_streak: int = 0
    peak_tokens: int = 0
    longest_task_seconds: int = 0
    include_in_total: bool = True
    note: str = ""
    errors: list[str] = field(default_factory=list)

    def finalize(self, today: dt.date) -> None:
        self.daily_tokens = {
            day: int(tok) for day, tok in sorted(self.daily_tokens.items()) if int(tok) > 0
        }
        if not self.daily_work:
            self.daily_work = dict(self.daily_tokens)
        self.daily_work = {
            day: int(tok) for day, tok in sorted(self.daily_work.items()) if int(tok) > 0
        }
        if not self.daily_activity:
            self.daily_activity = dict(self.daily_tokens)
        self.daily_activity = {
            day: int(v) for day, v in sorted(self.daily_activity.items()) if int(v) > 0
        }
        if not self.tokens:
            self.tokens = sum(self.daily_tokens.values())
        if not self.work_tokens:
            self.work_tokens = sum(self.daily_work.values()) or self.tokens
        if not self.lifetime_tokens:
            self.lifetime_tokens = self.tokens
        if not self.active_days:
            self.active_days = len(self.daily_activity or self.daily_tokens)
        if not self.peak_tokens:
            self.peak_tokens = max(self.daily_tokens.values(), default=0)
        if self.model_tokens and not self.favorite_model:
            self.favorite_model = pretty_model(self.model_tokens.most_common(1)[0][0])
        active = set(self.daily_activity) or set(self.daily_tokens)
        if active:
            cur, longest = streaks(active, today)
            self.current_streak = max(self.current_streak, cur)
            self.longest_streak = max(self.longest_streak, longest)


# ── formatting ────────────────────────────────────────────────────────────────

def e(value: object) -> str:
    return html.escape(str(value), quote=True)


def fmt_n(n: int) -> str:
    n = int(n or 0)
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def fmt_duration(seconds: int) -> str:
    seconds = int(seconds or 0)
    if seconds <= 0:
        return "n/a"
    hours, rem = divmod(seconds, 3600)
    minutes, _ = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{seconds}s"


def pct(part: int, total: int) -> str:
    if not total:
        return "0%"
    value = part / total * 100
    if value >= 10:
        return f"{value:.0f}%"
    if value >= 0.1:
        return f"{value:.1f}%"
    return "&lt;0.1%"


def shorten(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def pretty_model(model: str) -> str:
    model = model or ""
    known = {
        "claude-opus-4-8": "Opus 4.8",
        "claude-opus-4-7": "Opus 4.7",
        "claude-sonnet-4-6": "Sonnet 4.6",
        "claude-haiku-4-5-20251001": "Haiku 4.5",
        "claude-haiku-4-5": "Haiku 4.5",
        "gpt-5.5": "GPT-5.5",
        "gpt-5": "GPT-5",
    }
    if model in known:
        return known[model]
    if model.startswith("claude-"):
        return model.replace("claude-", "").replace("-", " ").title()
    if model.startswith("gpt-"):
        return model.upper()
    return model.replace("_", " ").replace("-", " ").title() or ""


def parse_iso_date(value: str) -> str:
    return value[:10] if value and len(value) >= 10 else ""


def parse_iso_datetime(value: str) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def date_from_unix(value: int | float | None) -> str:
    if not value:
        return ""
    try:
        return dt.datetime.fromtimestamp(float(value), tz=dt.timezone.utc).date().isoformat()
    except (OSError, OverflowError, ValueError):
        return ""


def streaks(active_days: set[str], today: dt.date) -> tuple[int, int]:
    parsed = sorted(dt.date.fromisoformat(day) for day in active_days)
    if not parsed:
        return 0, 0
    current = 0
    check = today
    while check.isoformat() in active_days:
        current += 1
        check -= dt.timedelta(days=1)
    if current == 0:
        check = today - dt.timedelta(days=1)
        while check.isoformat() in active_days:
            current += 1
            check -= dt.timedelta(days=1)
    longest = run = 0
    previous = None
    for day in parsed:
        run = run + 1 if previous and (day - previous).days == 1 else 1
        longest = max(longest, run)
        previous = day
    return current, longest


def as_int(value: Any) -> int:
    if value in (None, ""):
        return 0
    return int(float(value))


# ── loaders ───────────────────────────────────────────────────────────────────

def token_count_from_usage(usage: dict[str, Any] | None) -> int:
    """Fair work tokens = non-cached input + output."""
    if not isinstance(usage, dict):
        return 0
    return int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)


def load_claude(today: dt.date) -> SourceStats:
    source = SourceStats(id="claude", label="Claude Code",
                         color=SOURCE_COLORS["claude"], mode="live local")
    if not CLAUDE_PROJECTS_DIR.exists():
        source.note = "No ~/.claude/projects directory found"
        return source

    for file_name in glob.glob(str(CLAUDE_PROJECTS_DIR / "**" / "*.jsonl"), recursive=True):
        file_has_usage = False
        first_seen = last_seen = None
        try:
            with open(file_name, encoding="utf-8", errors="ignore") as handle:
                for line in handle:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    message = event.get("message") or {}
                    role = message.get("role") or ""
                    timestamp = event.get("timestamp") or ""
                    seen_at = parse_iso_datetime(timestamp)
                    if seen_at:
                        first_seen = min(first_seen, seen_at) if first_seen else seen_at
                        last_seen = max(last_seen, seen_at) if last_seen else seen_at
                    if role == "user":
                        source.messages += 1
                        continue
                    if role != "assistant":
                        continue
                    tokens = token_count_from_usage(message.get("usage"))
                    if tokens <= 0:
                        continue
                    source.messages += 1
                    source.work_tokens += tokens
                    file_has_usage = True
                    model = message.get("model") or ""
                    if model and model != "<synthetic>":
                        source.model_tokens[model] += tokens
                    day = parse_iso_date(timestamp)
                    if day:
                        source.daily_work[day] = source.daily_work.get(day, 0) + tokens
        except OSError as exc:
            source.errors.append(f"{Path(file_name).name}: {exc}")
        if file_has_usage:
            source.sessions += 1
            # NOTE: file first→last span is unreliable as "task length" (sessions
            # get resumed days later). Longest-task comes from manual-usage.json.

    source.daily_tokens = dict(source.daily_work)
    source.tokens = source.work_tokens
    source.finalize(today)
    return source


def load_codex(today: dt.date) -> SourceStats:
    """Fair work-tokens from rollout JSONL (non-cached input + output), plus a
    SQLite pass for session count / longest task / local daily timeline."""
    source = SourceStats(id="codex", label="Codex",
                         color=SOURCE_COLORS["codex"], mode="live local")

    # --- fair work tokens from rollout token_count events -------------------
    if CODEX_SESSIONS_DIR.exists():
        cur_model = ""
        for fname in glob.glob(str(CODEX_SESSIONS_DIR / "**" / "*.jsonl"), recursive=True):
            seen = False
            try:
                with open(fname, encoding="utf-8", errors="ignore") as handle:
                    for line in handle:
                        try:
                            ev = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        payload = ev.get("payload")
                        if not isinstance(payload, dict):
                            continue
                        if isinstance(payload.get("model"), str):
                            cur_model = payload["model"]
                        if payload.get("type") == "token_count":
                            info = payload.get("info") or {}
                            last = info.get("last_token_usage") or {}
                            nc_in = max(0, int(last.get("input_tokens") or 0)
                                        - int(last.get("cached_input_tokens") or 0))
                            work = nc_in + int(last.get("output_tokens") or 0)
                            if work <= 0:
                                continue
                            day = parse_iso_date(ev.get("timestamp") or "")
                            if day:
                                source.daily_work[day] = source.daily_work.get(day, 0) + work
                                source.work_tokens += work
                                seen = True
                                if cur_model:
                                    source.model_tokens[cur_model] += work
            except OSError as exc:
                source.errors.append(f"{Path(fname).name}: {exc}")
            if seen:
                source.sessions += 1

    # --- SQLite for local daily timeline + longest task ---------------------
    db_path = next((p for p in CODEX_DB_CANDIDATES if p.exists()), None)
    if db_path:
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT tokens_used, model, created_at, updated_at, "
                "created_at_ms, updated_at_ms FROM threads WHERE tokens_used > 0"
            ).fetchall()
            conn.close()
        except sqlite3.Error as exc:
            source.errors.append(f"{db_path}: {exc}")
            rows = []
        for row in rows:
            tokens = int(row["tokens_used"] or 0)
            updated = (row["updated_at_ms"] / 1000) if row["updated_at_ms"] else row["updated_at"]
            created = (row["created_at_ms"] / 1000) if row["created_at_ms"] else row["created_at"]
            day = date_from_unix(updated)
            if day:
                source.daily_tokens[day] = source.daily_tokens.get(day, 0) + tokens
            # longest-task intentionally not derived from thread spans (reopened
            # threads inflate it); manual-usage.json supplies the real figure.

    if not source.daily_tokens:
        source.daily_tokens = dict(source.daily_work)
    source.tokens = source.work_tokens
    source.finalize(today)
    return source


def load_grok(today: dt.date) -> SourceStats:
    """Grok CLI: cumulative `totalTokens` per session from updates.jsonl."""
    source = SourceStats(id="grok", label="Grok",
                         color=SOURCE_COLORS["grok"], mode="live local")
    model = ""
    try:
        cache = json.loads(GROK_MODELS_CACHE.read_text())
        for entry in (cache.get("models") or {}).values():
            info = entry.get("info", {}) if isinstance(entry, dict) else {}
            if info.get("name"):
                model = info["name"]
                break
    except Exception:
        pass
    if model:
        source.favorite_model = model

    if GROK_SESSIONS_DIR.exists():
        for fname in glob.glob(str(GROK_SESSIONS_DIR / "**" / "updates.jsonl"), recursive=True):
            mx = 0
            last_ts = None
            try:
                with open(fname, encoding="utf-8", errors="ignore") as handle:
                    for line in handle:
                        try:
                            ev = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        def find_max(obj):
                            m = 0
                            if isinstance(obj, dict):
                                for k, v in obj.items():
                                    if k == "totalTokens" and isinstance(v, int):
                                        m = max(m, v)
                                    m = max(m, find_max(v))
                            elif isinstance(obj, list):
                                for x in obj:
                                    m = max(m, find_max(x))
                            return m

                        mx = max(mx, find_max(ev))
                        if ev.get("timestamp"):
                            last_ts = ev["timestamp"]
            except OSError as exc:
                source.errors.append(f"{Path(fname).name}: {exc}")
            if mx and last_ts:
                day = date_from_unix(last_ts)
                if day:
                    source.daily_work[day] = source.daily_work.get(day, 0) + mx
                    source.work_tokens += mx
                    source.sessions += 1

    source.daily_tokens = dict(source.daily_work)
    source.tokens = source.work_tokens
    source.finalize(today)
    return source


def expand_activity_grid(grid: Any) -> dict[str, int]:
    if not isinstance(grid, dict):
        return {}
    start_raw = grid.get("start")
    columns = grid.get("columns") or []
    if not isinstance(start_raw, str) or not isinstance(columns, list):
        return {}
    try:
        start = dt.date.fromisoformat(start_raw)
    except ValueError:
        return {}
    levels = {str(k): as_int(v) for k, v in (grid.get("levels") or {}).items()}
    if not levels:
        levels = {"0": 0, "1": 25, "2": 50, "3": 75, "4": 100}
    expanded: dict[str, int] = {}
    for col, encoded in enumerate(columns):
        if not isinstance(encoded, str):
            continue
        for row, char in enumerate(encoded):
            value = levels.get(char, 0)
            if value > 0:
                day = start + dt.timedelta(days=col * 7 + row)
                expanded[day.isoformat()] = value
    return expanded


def load_manual(path: Path, today: dt.date) -> list[SourceStats]:
    """Manual/lifetime augmentation. For each id, supplies a lifetime number,
    historical activity grid, and the impressive all-time stats. Does NOT
    override fair work-tokens."""
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    results: list[SourceStats] = []
    for index, item in enumerate(data.get("sources", [])):
        if item.get("enabled", True) is False:
            continue
        source_id = item.get("id") or f"manual-{index + 1}"
        color = item.get("color") or SOURCE_COLORS.get(source_id, SOURCE_COLORS["manual"])
        s = SourceStats(
            id=source_id,
            label=item.get("label") or source_id.title(),
            color=color,
            mode=item.get("mode") or "manual/export",
            include_in_total=item.get("include_in_total", True),
            note=item.get("note", ""),
        )
        s.lifetime_tokens = as_int(item.get("total_tokens", item.get("tokens", 0)))
        s.tokens = s.lifetime_tokens
        s.sessions = as_int(item.get("sessions", 0))
        s.current_streak = as_int(item.get("current_streak", 0))
        s.longest_streak = as_int(item.get("longest_streak", 0))
        s.peak_tokens = as_int(item.get("peak_tokens", 0))
        s.longest_task_seconds = as_int(item.get("longest_task_seconds", 0))
        s.favorite_model = item.get("favorite_model", "")
        for day, value in expand_activity_grid(item.get("activity_grid")).items():
            s.daily_activity[day] = max(s.daily_activity.get(day, 0), value)
        for day, value in (item.get("daily_activity") or {}).items():
            s.daily_activity[str(day)] = max(s.daily_activity.get(str(day), 0), as_int(value))
        results.append(s)
    return results


def merge_manual(live: list[SourceStats], manual: list[SourceStats], today: dt.date) -> list[SourceStats]:
    by_id = {s.id: s for s in live}
    order = [s.id for s in live]
    for m in manual:
        existing = by_id.get(m.id)
        if not existing:
            m.finalize(today)
            by_id[m.id] = m
            order.append(m.id)
            continue
        # manual augments lifetime + all-time stats + history; fair work stays
        existing.lifetime_tokens = max(existing.lifetime_tokens, m.lifetime_tokens)
        existing.mode = m.mode or existing.mode
        existing.note = m.note or existing.note
        existing.current_streak = max(existing.current_streak, m.current_streak)
        existing.longest_streak = max(existing.longest_streak, m.longest_streak)
        existing.peak_tokens = max(existing.peak_tokens, m.peak_tokens)
        if m.longest_task_seconds:
            existing.longest_task_seconds = m.longest_task_seconds
        if m.favorite_model:
            existing.favorite_model = m.favorite_model
        for day, value in m.daily_activity.items():
            existing.daily_activity[day] = max(existing.daily_activity.get(day, 0), value)
    return [by_id[i] for i in order]


# ── combine ───────────────────────────────────────────────────────────────────

def combine(sources: list[SourceStats], today: dt.date) -> dict[str, Any]:
    included = [s for s in sources if s.include_in_total]
    daily_work_totals: dict[str, int] = defaultdict(int)
    daily_by_source: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    activity_days: set[str] = set()
    model_tokens: Counter[str] = Counter()

    for s in included:
        model_tokens.update(s.model_tokens)
        activity_days.update(s.daily_activity)
        for day, tok in s.daily_work.items():
            daily_work_totals[day] += tok
            daily_by_source[day][s.id] += tok

    fair_total = sum(s.work_tokens for s in included)
    lifetime_total = sum(s.lifetime_tokens for s in included)
    # "active days" = days I actually coded with these tools (locally tracked),
    # not the historical activity grid — keeps it consistent with "since <date>".
    work_days = set(daily_work_totals)
    current_streak, longest_streak = streaks(activity_days or work_days, today)
    peak_daily = max((sum(v.values()) for v in daily_by_source.values()), default=0)
    peak_source = max((s.peak_tokens for s in included), default=0)

    leader = max(included, key=lambda s: s.work_tokens, default=None)
    longest_task = max((s.longest_task_seconds for s in included), default=0)

    return {
        "fair_total": fair_total,
        "lifetime_total": lifetime_total,
        "sessions": sum(s.sessions for s in included),
        "active_days": len(work_days),
        "current_streak": current_streak or max((s.current_streak for s in included), default=0),
        "longest_streak": max(longest_streak, *(s.longest_streak for s in included), 0),
        "peak_tokens": max(peak_daily, peak_source),
        "longest_task_seconds": longest_task,
        "favorite_model": pretty_model(model_tokens.most_common(1)[0][0]) if model_tokens else "",
        "leader": leader,
        "daily_work_totals": dict(sorted(daily_work_totals.items())),
        "daily_by_source": {d: dict(v) for d, v in sorted(daily_by_source.items())},
    }


# ── colour helpers ────────────────────────────────────────────────────────────

def hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def blend(base: str, accent: str, amount: float) -> str:
    amount = max(0.0, min(1.0, amount))
    b, a = hex_to_rgb(base), hex_to_rgb(accent)
    rgb = tuple(round(b[i] * (1 - amount) + a[i] * amount) for i in range(3))
    return "#" + "".join(f"{max(0, min(255, p)):02x}" for p in rgb)


# ── render pieces ─────────────────────────────────────────────────────────────

def growth_curve(daily_totals: dict[str, int], x: float, y: float, w: float, h: float,
                 today: dt.date) -> str:
    if not daily_totals:
        return ""
    days = sorted(daily_totals.keys())
    d0 = dt.date.fromisoformat(days[0])
    span = max((today - d0).days, 1)
    cum, series = 0, []
    cur = d0
    while cur <= today:
        cum += daily_totals.get(cur.isoformat(), 0)
        series.append(((cur - d0).days, cum))
        cur += dt.timedelta(days=1)
    total = cum or 1
    pts = [(x + (off / span) * w, y + h - (c / total) * h) for off, c in series]
    line = "M " + " L ".join(f"{px:.1f},{py:.1f}" for px, py in pts)
    area = line + f" L {x + w:.1f},{y + h:.1f} L {x:.1f},{y + h:.1f} Z"
    ex, ey = pts[-1]
    return (
        f'<defs><linearGradient id="grow" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0" stop-color="{ACCENT}" stop-opacity="0.32"/>'
        f'<stop offset="1" stop-color="{ACCENT}" stop-opacity="0"/></linearGradient></defs>'
        f'<path d="{area}" fill="url(#grow)"/>'
        f'<path d="{line}" fill="none" stroke="{ACCENT}" stroke-width="2"/>'
        f'<circle cx="{ex:.1f}" cy="{ey:.1f}" r="3" fill="#60a5fa"/>'
        f'<text x="{x:.0f}" y="{y + h + 13:.0f}" fill="{DIM}" font-size="10" '
        f'font-family="{MONO}">cumulative growth · fair tokens</text>'
    )


def share_bars(sources: list[SourceStats], fair_total: int, x: float, y: float, w: float) -> str:
    rows: list[str] = []
    ordered = sorted(sources, key=lambda s: s.work_tokens, reverse=True)
    bar_max = max((s.work_tokens for s in ordered), default=1) or 1
    row_h = 34
    bar_y_off = 19
    for i, s in enumerate(ordered):
        ry = y + i * row_h
        model = f' · {s.favorite_model}' if s.favorite_model else ""
        share = pct(s.work_tokens, fair_total)
        rows.append(
            f'<circle cx="{x + 5}" cy="{ry + 6}" r="4.5" fill="{s.color}"/>'
            f'<text x="{x + 17}" y="{ry + 10}" fill="{TEXT}" font-size="13.5" '
            f'font-weight="600" font-family="{FONT}">{e(s.label)}'
            f'<tspan fill="{DIM}" font-size="10.5" font-weight="400" font-family="{MONO}">{e(model)}</tspan></text>'
            f'<text x="{x + w}" y="{ry + 10}" fill="{MUTED}" font-size="12" '
            f'font-family="{MONO}" text-anchor="end">{fmt_n(s.work_tokens)}  ·  {share}</text>'
            f'<rect x="{x}" y="{ry + bar_y_off}" width="{w}" height="9" rx="4.5" fill="{EMPTY_CELL}"/>'
            f'<rect x="{x}" y="{ry + bar_y_off}" width="{max(3, w * s.work_tokens / bar_max):.1f}" '
            f'height="9" rx="4.5" fill="{s.color}"/>'
        )
    return "\n  ".join(rows)


def stat_chips(combined: dict[str, Any], x: float, y: float, w: float, pad: float) -> str:
    items = [
        ("Peak day", fmt_n(combined["peak_tokens"])),
        ("Longest task", fmt_duration(combined["longest_task_seconds"])),
        ("Longest streak", f"{combined['longest_streak']}d"),
        ("Current streak", f"{combined['current_streak']}d"),
    ]
    gap = 8
    cols = len(items)
    card_w = (w - gap * (cols - 1)) / cols
    card_h = 48
    chips: list[str] = []
    for i, (label, value) in enumerate(items):
        cx = x + i * (card_w + gap)
        chips.append(
            f'<rect x="{cx:.1f}" y="{y}" width="{card_w:.1f}" height="{card_h}" rx="7" '
            f'fill="{CARD_BG}" stroke="{BORDER}"/>'
            f'<text x="{cx + 12:.1f}" y="{y + 19}" fill="{MUTED}" font-size="10.5" '
            f'font-family="{FONT}">{e(label)}</text>'
            f'<text x="{cx + 12:.1f}" y="{y + 39}" fill="{TEXT}" font-size="17" '
            f'font-weight="700" font-family="{FONT}">{e(value)}</text>'
        )
    return "\n  ".join(chips)


def activity_color(color: str, value: int, max_value: int) -> str:
    if value <= 0:
        return EMPTY_CELL
    if max_value <= 0:
        return blend(EMPTY_CELL, color, 0.46)
    intensity = 0.28 + 0.70 * (math.log1p(value) / math.log1p(max_value))
    return blend(EMPTY_CELL, color, intensity)


def activity_lanes(sources: list[SourceStats], today: dt.date,
                   cell: int = 9, gap: int = 3) -> tuple[str, int, int]:
    step = cell + gap
    label_w = 132
    lane_h = 7 * step - gap
    lane_gap = 16
    week_start = today - dt.timedelta(days=today.weekday())
    columns = 49
    start = week_start - dt.timedelta(weeks=columns - 1)

    month_x: dict[tuple[int, int], tuple[str, int]] = {}
    for col in range(columns):
        week = start + dt.timedelta(days=col * 7)
        for row in range(7):
            day = week + dt.timedelta(days=row)
            if day > today:
                continue
            if day.day <= 7 and (day.year, day.month) not in month_x:
                month_x[(day.year, day.month)] = (day.strftime("%b"), label_w + col * step)

    parts = [
        f'<text x="{x}" y="-9" fill="{MUTED}" font-size="9.5" font-family="{MONO}">{label}</text>'
        for (_, _), (label, x) in sorted(month_x.items())
    ]
    ordered = sorted(sources, key=lambda s: s.lifetime_tokens, reverse=True)
    for index, s in enumerate(ordered):
        y0 = 10 + index * (lane_h + lane_gap)
        max_value = max(s.daily_activity.values(), default=0)
        parts.append(
            f'<circle cx="4" cy="{y0 + 7}" r="4" fill="{s.color}"/>'
            f'<text x="14" y="{y0 + 11}" fill="{TEXT}" font-size="12.5" font-weight="600" '
            f'font-family="{FONT}">{e(shorten(s.label, 16))}</text>'
            f'<text x="14" y="{y0 + 27}" fill="{MUTED}" font-size="9.5" font-family="{MONO}">'
            f'{fmt_n(s.lifetime_tokens)}</text>'
        )
        for col in range(columns):
            week = start + dt.timedelta(days=col * 7)
            for row in range(7):
                day = week + dt.timedelta(days=row)
                if day > today:
                    continue
                key = day.isoformat()
                value = s.daily_activity.get(key, 0)
                exact = s.daily_work.get(key, 0) or s.daily_tokens.get(key, 0)
                if exact:
                    tip = f"{key}: {s.label} {fmt_n(exact)} tokens"
                elif value:
                    tip = f"{key}: {s.label} active"
                else:
                    tip = f"{key}: no {s.label}"
                parts.append(
                    f'<rect x="{label_w + col * step}" y="{y0 + row * step}" width="{cell}" '
                    f'height="{cell}" rx="2" fill="{activity_color(s.color, value, max_value)}">'
                    f"<title>{e(tip)}</title></rect>"
                )
    width = label_w + columns * step - gap
    height = 10 + len(ordered) * lane_h + max(0, len(ordered) - 1) * lane_gap
    return "\n    ".join(parts), width, height


def fun_fact(total: int) -> str:
    refs = [
        (100_000, "Harry Potter & the Sorcerer's Stone"),
        (475_000, "the Lord of the Rings trilogy"),
        (1_000_000, "the King James Bible"),
        (4_400_000, "all 7 Harry Potter books"),
    ]
    best_mult, best_name = 1, refs[0][1]
    for tok, name in refs:
        if total // tok >= 2:
            best_mult, best_name = total // tok, name
    return f"~{best_mult:,}× the text of {best_name}, written as code"


# ── token-efficiency (tokens per public ship) ─────────────────────────────────

def tokens_by_project() -> dict[str, int]:
    """Work tokens attributed to each project folder. Claude dirs encode the
    working path; Codex sessions carry a cwd in session_meta."""
    proj: dict[str, int] = defaultdict(int)

    for d in glob.glob(str(CLAUDE_PROJECTS_DIR / "*")):
        if not os.path.isdir(d):
            continue
        total = 0
        for f in glob.glob(os.path.join(d, "**", "*.jsonl"), recursive=True):
            try:
                with open(f, encoding="utf-8", errors="ignore") as fp:
                    for line in fp:
                        try:
                            m = json.loads(line).get("message", {})
                            if m.get("role") == "assistant":
                                total += token_count_from_usage(m.get("usage"))
                        except Exception:
                            pass
            except OSError:
                pass
        if total:
            proj[os.path.basename(d)] += total

    for f in glob.glob(str(CODEX_SESSIONS_DIR / "**" / "*.jsonl"), recursive=True):
        cwd, tok = None, 0
        try:
            with open(f, encoding="utf-8", errors="ignore") as fp:
                for line in fp:
                    try:
                        d = json.loads(line)
                        p = d.get("payload", {})
                        if not isinstance(p, dict):
                            continue
                        if not cwd and d.get("type") == "session_meta":
                            cwd = p.get("cwd")          # session_meta type is top-level
                        if p.get("type") == "token_count":
                            last = (p.get("info", {}) or {}).get("last_token_usage", {}) or {}
                            tok += max(0, last.get("input_tokens", 0)
                                       - last.get("cached_input_tokens", 0)) + last.get("output_tokens", 0)
                    except Exception:
                        pass
        except OSError:
            pass
        if tok and cwd:
            proj["codex:" + os.path.basename(str(cwd).rstrip("/"))] += tok

    return dict(proj)


def load_shipped(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("projects", [])
    except Exception:
        return []


def compute_efficiency(project_tokens: dict[str, int], tracked: list[dict]) -> dict[str, Any]:
    """tokens-per-ship = tokens on tracked projects ÷ # shipped. Untracked
    projects (private/work/exploration) are ignored entirely."""
    total = shipped = building = 0
    for p in tracked:
        patterns = [m.lower() for m in p.get("match", [])]
        total += sum(v for k, v in project_tokens.items()
                     if any(m in k.lower() for m in patterns))
        if p.get("status") == "shipped":
            shipped += 1
        elif p.get("status") == "building":
            building += 1
    return {
        "tokens_per_ship": (total // shipped) if shipped else 0,
        "shipped": shipped,
        "building": building,
        "tracked_tokens": total,
    }


# ── svg ───────────────────────────────────────────────────────────────────────

def generate_svg(sources: list[SourceStats], combined: dict[str, Any],
                 efficiency: dict[str, Any], username: str, today: dt.date) -> str:
    W, pad = 940, 28
    inner = W - 2 * pad

    lanes_svg, _, lanes_h = activity_lanes(sources, today)

    fair = combined["fair_total"]
    lifetime = combined["lifetime_total"]
    eff = efficiency

    # top band: token-efficiency headline (left) · most-used ranking (right)
    y_meta = 24
    eff_num_y = 64
    eff_sub_y = 86
    bars_x = pad + 330
    bars_w = inner - 330
    bars_y = 34
    share_h = len(sources) * 34
    y_lanes = max(eff_sub_y, bars_y + share_h) + 30
    H = y_lanes + lanes_h + pad

    eff_num = f'{fmt_n(eff.get("tokens_per_ship", 0))}' if eff.get("tokens_per_ship") else "—"

    p: list[str] = [
        f'<rect width="{W}" height="{H}" rx="14" fill="{BG}" stroke="{BORDER}"/>',
        # label row: metric name (left) + handle/freshness (right)
        f'<text x="{pad}" y="{y_meta}" fill="{MUTED}" font-size="11" letter-spacing="0.5" '
        f'font-family="{FONT}">TOKEN-EFFICIENCY <tspan fill="{DIM}" letter-spacing="0">· tokens per public ship</tspan></text>',
        f'<text x="{W - pad}" y="{y_meta}" fill="{DIM}" font-size="11" font-family="{MONO}" '
        f'text-anchor="end">@{e(username)} · updated {today.isoformat()}</text>',
        # efficiency headline number (left)
        f'<text x="{pad}" y="{eff_num_y}" fill="{TEXT}" font-size="38" font-weight="800" '
        f'font-family="{FONT}">{eff_num}<tspan fill="{MUTED}" font-size="16" font-weight="500"> / ship</tspan></text>',
        f'<text x="{pad}" y="{eff_sub_y}" fill="{MUTED}" font-size="11.5" font-family="{FONT}">'
        f'{eff.get("shipped", 0)} shipped · {eff.get("building", 0)} building · '
        f'<tspan fill="{DIM}">{fmt_n(lifetime)}+ tokens all-time</tspan></text>',
        # most-used ranking (right)
        share_bars(sources, fair, bars_x, bars_y, bars_w),
        # activity timeline, one lane per tool (full width)
        f'<g transform="translate({pad},{y_lanes})">\n    {lanes_svg}\n  </g>',
    ]
    return (f'<svg width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
            f'xmlns="http://www.w3.org/2000/svg" role="img" '
            f'aria-label="AI coding token-efficiency">\n  ' + "\n  ".join(p) + "\n</svg>")


def write_summary(path: Path, sources: list[SourceStats], combined: dict[str, Any],
                  efficiency: dict[str, Any], today: dt.date) -> None:
    payload = {
        "updated_at": today.isoformat(),
        "token_efficiency": efficiency,
        "totals": {
            "fair_tokens": combined["fair_total"],
            "lifetime_tokens": combined["lifetime_total"],
            "sessions": combined["sessions"],
            "active_days": combined["active_days"],
            "current_streak": combined["current_streak"],
            "longest_streak": combined["longest_streak"],
            "peak_tokens": combined["peak_tokens"],
            "longest_task_seconds": combined["longest_task_seconds"],
            "favorite_model": combined["favorite_model"],
        },
        "sources": [
            {
                "id": s.id, "label": s.label, "mode": s.mode,
                "work_tokens": s.work_tokens, "lifetime_tokens": s.lifetime_tokens,
                "sessions": s.sessions, "active_days": s.active_days,
                "current_streak": s.current_streak, "longest_streak": s.longest_streak,
                "peak_tokens": s.peak_tokens, "longest_task_seconds": s.longest_task_seconds,
                "favorite_model": s.favorite_model, "note": s.note, "errors": s.errors,
                "daily_work": s.daily_work,
            }
            for s in sources
        ],
        "daily_work_totals": combined["daily_work_totals"],
        "daily_by_source": combined["daily_by_source"],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", "-o", default="ai-token-stats.svg")
    parser.add_argument("--summary", default="data/token-stats.json")
    parser.add_argument("--manual", default="data/manual-usage.json")
    parser.add_argument("--shipped", default="data/shipped-projects.json")
    parser.add_argument("--username", default=DEFAULT_USERNAME)
    parser.add_argument("--today", default="")
    args = parser.parse_args()

    today = dt.date.fromisoformat(args.today) if args.today else dt.date.today()
    live = [load_claude(today), load_codex(today), load_grok(today)]
    live = [s for s in live if s.work_tokens or s.daily_work or s.note]
    sources = merge_manual(live, load_manual(Path(args.manual).expanduser(), today), today)
    sources = [s for s in sources if s.work_tokens or s.daily_work or s.lifetime_tokens]
    combined = combine(sources, today)
    efficiency = compute_efficiency(tokens_by_project(),
                                    load_shipped(Path(args.shipped).expanduser()))

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(generate_svg(sources, combined, efficiency, args.username, today), encoding="utf-8")
    write_summary(Path(args.summary), sources, combined, efficiency, today)

    print(f"wrote {output}")
    print(f"token-efficiency: {fmt_n(efficiency['tokens_per_ship'])}/ship "
          f"({efficiency['shipped']} shipped, {efficiency['building']} building, "
          f"{fmt_n(efficiency['tracked_tokens'])} tracked)")
    print(f"fair total: {fmt_n(combined['fair_total'])}  ·  lifetime: {fmt_n(combined['lifetime_total'])}")
    for s in sorted(sources, key=lambda s: s.work_tokens, reverse=True):
        print(f"- {s.label}: fair {fmt_n(s.work_tokens)} · lifetime {fmt_n(s.lifetime_tokens)} ({s.mode})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

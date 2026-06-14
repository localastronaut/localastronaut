#!/usr/bin/env python3
"""Generate a public AI token activity SVG for the profile README.

The generator reads aggregate metadata only. It does not publish prompts,
responses, file paths, thread titles, or conversation text.
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
CODEX_DB_CANDIDATES = [
    Path.home() / ".codex" / "sqlite" / "state_5.sqlite",
    Path.home() / ".codex" / "state_5.sqlite",
]
EMPTY_CELL = "#21262d"
CARD_BG = "#161b22"
BORDER = "#30363d"
TEXT = "#e6edf3"
MUTED = "#7d8590"
DIM = "#4b5563"
SOURCE_COLORS = {
    "claude": "#f59e0b",
    "codex": "#60a5fa",
    "chatgpt": "#10b981",
    "openai": "#a78bfa",
    "manual": "#f472b6",
}


@dataclass
class SourceStats:
    id: str
    label: str
    color: str
    mode: str
    tokens: int = 0
    sessions: int = 0
    messages: int = 0
    daily_tokens: dict[str, int] = field(default_factory=dict)
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
            day: int(tokens)
            for day, tokens in sorted(self.daily_tokens.items())
            if int(tokens) > 0
        }
        if not self.tokens:
            self.tokens = sum(self.daily_tokens.values())
        if not self.active_days:
            self.active_days = len(self.daily_tokens)
        if not self.peak_tokens:
            self.peak_tokens = max(self.daily_tokens.values(), default=0)
        if self.model_tokens and not self.favorite_model:
            self.favorite_model = pretty_model(self.model_tokens.most_common(1)[0][0])
        if self.daily_tokens:
            self.current_streak, self.longest_streak = streaks(
                set(self.daily_tokens), today
            )


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
    return f"{value:.0f}%" if value >= 10 else f"{value:.1f}%"


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
    return model.replace("_", " ").replace("-", " ").title() or "n/a"


def token_count_from_usage(usage: dict[str, Any] | None) -> int:
    if not isinstance(usage, dict):
        return 0
    keys = (
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    )
    return sum(int(usage.get(key) or 0) for key in keys)


def parse_iso_date(value: str) -> str:
    if not value or len(value) < 10:
        return ""
    return value[:10]


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


def load_claude(today: dt.date) -> SourceStats:
    source = SourceStats(
        id="claude",
        label="Claude Code",
        color=SOURCE_COLORS["claude"],
        mode="live local",
    )
    if not CLAUDE_PROJECTS_DIR.exists():
        source.note = "No ~/.claude/projects directory found"
        return source

    files = glob.glob(str(CLAUDE_PROJECTS_DIR / "**" / "*.jsonl"), recursive=True)
    for file_name in files:
        file_has_usage = False
        first_seen: dt.datetime | None = None
        last_seen: dt.datetime | None = None
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
                    source.tokens += tokens
                    file_has_usage = True

                    model = message.get("model") or ""
                    if model and model != "<synthetic>":
                        source.model_tokens[model] += tokens

                    day = parse_iso_date(timestamp)
                    if day:
                        source.daily_tokens[day] = source.daily_tokens.get(day, 0) + tokens
        except OSError as exc:
            source.errors.append(f"{Path(file_name).name}: {exc}")

        if file_has_usage:
            source.sessions += 1
            if first_seen and last_seen:
                duration = int((last_seen - first_seen).total_seconds())
                source.longest_task_seconds = max(source.longest_task_seconds, duration)

    source.finalize(today)
    return source


def load_codex(today: dt.date) -> SourceStats:
    source = SourceStats(
        id="codex",
        label="Codex",
        color=SOURCE_COLORS["codex"],
        mode="live local",
    )
    db_path = next((path for path in CODEX_DB_CANDIDATES if path.exists()), None)
    if not db_path:
        source.note = "No Codex state database found"
        return source

    query = """
        SELECT
            tokens_used,
            source,
            model_provider,
            model,
            created_at,
            updated_at,
            created_at_ms,
            updated_at_ms
        FROM threads
        WHERE tokens_used > 0
    """
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(query).fetchall()
    except sqlite3.Error as exc:
        source.errors.append(f"{db_path}: {exc}")
        return source
    finally:
        try:
            conn.close()
        except Exception:
            pass

    seen_sources = Counter()
    for row in rows:
        tokens = int(row["tokens_used"] or 0)
        source.tokens += tokens
        source.sessions += 1
        seen_sources[row["source"] or "codex"] += 1

        model = row["model"] or row["model_provider"] or ""
        if model:
            source.model_tokens[model] += tokens

        updated_seconds = (
            (row["updated_at_ms"] / 1000) if row["updated_at_ms"] else row["updated_at"]
        )
        day = date_from_unix(updated_seconds)
        if day:
            source.daily_tokens[day] = source.daily_tokens.get(day, 0) + tokens

        created_seconds = (
            (row["created_at_ms"] / 1000) if row["created_at_ms"] else row["created_at"]
        )
        if created_seconds and updated_seconds:
            source.longest_task_seconds = max(
                source.longest_task_seconds, int(updated_seconds - created_seconds)
            )

    if seen_sources:
        source.mode = "local " + ", ".join(sorted(seen_sources))
    source.finalize(today)
    return source


def as_int(value: Any) -> int:
    if value in (None, ""):
        return 0
    return int(float(value))


def load_manual(path: Path, today: dt.date) -> list[SourceStats]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)

    results: list[SourceStats] = []
    for index, item in enumerate(data.get("sources", [])):
        if item.get("enabled", True) is False:
            continue
        source_id = item.get("id") or f"manual-{index + 1}"
        color = item.get("color") or SOURCE_COLORS.get(source_id, SOURCE_COLORS["manual"])
        source = SourceStats(
            id=source_id,
            label=item.get("label") or source_id.title(),
            color=color,
            mode=item.get("mode") or "manual/export",
            include_in_total=item.get("include_in_total", True),
            note=item.get("note", ""),
        )
        source.tokens = as_int(item.get("total_tokens", item.get("tokens", 0)))
        source.sessions = as_int(item.get("sessions", 0))
        source.messages = as_int(item.get("messages", 0))
        source.active_days = as_int(item.get("active_days", 0))
        source.current_streak = as_int(item.get("current_streak", 0))
        source.longest_streak = as_int(item.get("longest_streak", 0))
        source.peak_tokens = as_int(item.get("peak_tokens", 0))
        source.longest_task_seconds = as_int(item.get("longest_task_seconds", 0))
        source.favorite_model = item.get("favorite_model", "")

        for day, tokens in (item.get("daily_tokens") or {}).items():
            source.daily_tokens[str(day)] = source.daily_tokens.get(str(day), 0) + as_int(tokens)
        for model, tokens in (item.get("models") or {}).items():
            source.model_tokens[str(model)] += as_int(tokens)

        source.finalize(today)
        if source.tokens or source.daily_tokens or source.note:
            results.append(source)
    return results


def combine(sources: list[SourceStats], today: dt.date) -> dict[str, Any]:
    included = [source for source in sources if source.include_in_total]
    daily_totals: dict[str, int] = defaultdict(int)
    daily_by_source: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    model_tokens: Counter[str] = Counter()

    for source in included:
        model_tokens.update(source.model_tokens)
        for day, tokens in source.daily_tokens.items():
            daily_totals[day] += tokens
            daily_by_source[day][source.id] += tokens

    active_days = set(day for day, tokens in daily_totals.items() if tokens > 0)
    current_streak, longest_streak = streaks(active_days, today)
    peak_daily = max(daily_totals.values(), default=0)
    peak_source = max((source.peak_tokens for source in included), default=0)
    total_tokens = sum(source.tokens for source in included)
    leader = max(included, key=lambda source: source.tokens, default=None)

    return {
        "total_tokens": total_tokens,
        "sessions": sum(source.sessions for source in included),
        "messages": sum(source.messages for source in included),
        "active_days": len(active_days) or sum(source.active_days for source in included),
        "current_streak": current_streak or max((s.current_streak for s in included), default=0),
        "longest_streak": max(longest_streak, *(s.longest_streak for s in included), 0),
        "peak_tokens": max(peak_daily, peak_source),
        "longest_task_seconds": max((s.longest_task_seconds for s in included), default=0),
        "favorite_model": pretty_model(model_tokens.most_common(1)[0][0]) if model_tokens else "n/a",
        "leader": leader,
        "daily_totals": dict(sorted(daily_totals.items())),
        "daily_by_source": {
            day: dict(values) for day, values in sorted(daily_by_source.items())
        },
    }


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))


def rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    return "#" + "".join(f"{max(0, min(255, part)):02x}" for part in rgb)


def blend(base: str, accent: str, amount: float) -> str:
    amount = max(0.0, min(1.0, amount))
    b = hex_to_rgb(base)
    a = hex_to_rgb(accent)
    return rgb_to_hex(tuple(round(b[i] * (1 - amount) + a[i] * amount) for i in range(3)))


def heatmap_color(day: str, combined: dict[str, Any], colors: dict[str, str]) -> str:
    total = combined["daily_totals"].get(day, 0)
    if total <= 0:
        return EMPTY_CELL
    max_day = max(combined["daily_totals"].values(), default=1)
    source_values = combined["daily_by_source"].get(day, {})
    dominant = max(source_values, key=source_values.get, default="")
    accent = colors.get(dominant, SOURCE_COLORS["manual"])
    intensity = 0.26 + 0.72 * (math.log1p(total) / math.log1p(max_day))
    return blend(EMPTY_CELL, accent, intensity)


def build_heatmap(
    sources: list[SourceStats], combined: dict[str, Any], today: dt.date, cell: int = 10, gap: int = 3
) -> tuple[str, str, int, int]:
    step = cell + gap
    start = today - dt.timedelta(weeks=52)
    start -= dt.timedelta(days=start.isoweekday() % 7)
    color_by_source = {source.id: source.color for source in sources}

    rects: list[str] = []
    month_x: dict[tuple[int, int], tuple[str, int]] = {}
    col = 0
    day = start
    while day <= today:
        dow = day.isoweekday() % 7
        if dow == 0 and day != start:
            col += 1
        day_key = day.isoformat()
        total = combined["daily_totals"].get(day_key, 0)
        by_source = combined["daily_by_source"].get(day_key, {})
        details = ", ".join(
            f"{next((s.label for s in sources if s.id == source_id), source_id)} {fmt_n(tokens)}"
            for source_id, tokens in sorted(by_source.items(), key=lambda item: item[1], reverse=True)
            if tokens
        )
        tip = f"{day_key}: {fmt_n(total)} tokens"
        if details:
            tip += f" ({details})"
        rects.append(
            f'<rect x="{col * step}" y="{dow * step}" width="{cell}" height="{cell}" '
            f'rx="2" fill="{heatmap_color(day_key, combined, color_by_source)}">'
            f"<title>{e(tip)}</title></rect>"
        )
        if day.day == 1 or day == start:
            month_x.setdefault((day.year, day.month), (day.strftime("%b"), col * step))
        day += dt.timedelta(days=1)

    months = [
        f'<text x="{x}" y="-8" fill="{MUTED}" font-size="10" font-family="system-ui,sans-serif">{label}</text>'
        for (_, _), (label, x) in sorted(month_x.items())
    ]
    width = (col + 1) * step - gap
    height = 7 * step - gap
    return "\n    ".join(months), "\n    ".join(rects), width, height


def source_rows(sources: list[SourceStats], total: int, y: int, width: int, pad: int) -> str:
    rows: list[str] = []
    sorted_sources = sorted(
        sources,
        key=lambda source: (source.include_in_total, source.tokens),
        reverse=True,
    )
    bar_x = pad + 186
    bar_w = width - bar_x - pad - 142
    for index, source in enumerate(sorted_sources):
        row_y = y + index * 36
        percent = source.tokens / total if total and source.include_in_total else 0
        bar_fill = max(2, round(bar_w * percent)) if source.tokens and source.include_in_total else 0
        percent_label = pct(source.tokens, total) if source.include_in_total else "reference"
        rows.append(
            f'<circle cx="{pad + 8}" cy="{row_y + 11}" r="5" fill="{source.color}"/>'
            f'<text x="{pad + 22}" y="{row_y + 14}" fill="{TEXT}" font-size="13" '
            f'font-weight="600" font-family="system-ui,sans-serif">{e(shorten(source.label, 22))}</text>'
            f'<text x="{pad + 22}" y="{row_y + 29}" fill="{MUTED}" font-size="10" '
            f'font-family="system-ui,sans-serif">{e(shorten(source.mode, 28))}</text>'
            f'<rect x="{bar_x}" y="{row_y + 3}" width="{bar_w}" height="13" rx="6.5" fill="{EMPTY_CELL}"/>'
            f'<rect x="{bar_x}" y="{row_y + 3}" width="{bar_fill}" height="13" rx="6.5" fill="{source.color}"/>'
            f'<text x="{bar_x + bar_w + 14}" y="{row_y + 14}" fill="{TEXT}" font-size="12" '
            f'font-family="system-ui,sans-serif">{fmt_n(source.tokens)}</text>'
            f'<text x="{bar_x + bar_w + 14}" y="{row_y + 29}" fill="{MUTED}" font-size="10" '
            f'font-family="system-ui,sans-serif">{percent_label} · {source.sessions:,} sessions</text>'
        )
    if not rows:
        rows.append(
            f'<text x="{pad}" y="{y + 18}" fill="{MUTED}" font-size="12" '
            f'font-family="system-ui,sans-serif">No token sources found yet.</text>'
        )
    return "\n  ".join(rows)


def stat_cards(combined: dict[str, Any], y: int, width: int, pad: int) -> str:
    leader = combined["leader"]
    leader_text = "n/a"
    if leader:
        leader_text = f"{leader.label} {pct(leader.tokens, combined['total_tokens'])}"
    items = [
        ("Total tokens", fmt_n(combined["total_tokens"])),
        ("Leading system", shorten(leader_text, 18)),
        ("Peak day", fmt_n(combined["peak_tokens"])),
        ("Active days", f"{combined['active_days']}d"),
        ("Current streak", f"{combined['current_streak']}d"),
        ("Longest streak", f"{combined['longest_streak']}d"),
    ]
    gap = 8
    cols = 3
    card_h = 66
    card_w = (width - 2 * pad - gap * (cols - 1)) // cols
    cards: list[str] = []
    for index, (label, value) in enumerate(items):
        row, col = divmod(index, cols)
        x = pad + col * (card_w + gap)
        cy = y + row * (card_h + gap)
        cards.append(
            f'<rect x="{x}" y="{cy}" width="{card_w}" height="{card_h}" rx="7" '
            f'fill="{CARD_BG}" stroke="{BORDER}" stroke-width="1"/>'
            f'<text x="{x + 13}" y="{cy + 22}" fill="{MUTED}" font-size="11.5" '
            f'font-family="system-ui,sans-serif">{e(label)}</text>'
            f'<text x="{x + 13}" y="{cy + 49}" fill="{TEXT}" font-size="21" '
            f'font-weight="700" font-family="system-ui,sans-serif">{e(value)}</text>'
        )
    return "\n  ".join(cards)


def generate_svg(
    sources: list[SourceStats], combined: dict[str, Any], username: str, today: dt.date
) -> str:
    width = 940
    pad = 24
    card_y = 68
    cards_h = 66 * 2 + 8
    sources_y = card_y + cards_h + 44
    source_h = max(1, len(sources)) * 36
    heat_y = sources_y + source_h + 50
    months, cells, hmap_w, hmap_h = build_heatmap(sources, combined, today)
    footer_y = heat_y + hmap_h + 52
    height = footer_y + 28

    source_row_svg = source_rows(sources, combined["total_tokens"], sources_y, width, pad)
    legend = []
    legend_x = pad + 142
    for index, source in enumerate(sources[:5]):
        lx = legend_x + index * 130
        legend.append(
            f'<circle cx="{lx}" cy="{heat_y - 19}" r="4" fill="{source.color}"/>'
            f'<text x="{lx + 9}" y="{heat_y - 15}" fill="{MUTED}" font-size="10" '
            f'font-family="system-ui,sans-serif">{e(shorten(source.label, 15))}</text>'
        )

    generated_note = "local aggregate counters only; no prompts or responses published"
    return f'''<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg">
  <rect width="{width}" height="{height}" rx="12" fill="#0d1117" stroke="{BORDER}" stroke-width="1"/>

  <text x="{pad}" y="38" fill="{TEXT}" font-size="18" font-weight="700" font-family="system-ui,sans-serif">AI Token Activity</text>
  <text x="{pad + 146}" y="38" fill="#93c5fd" font-size="13" font-family="monospace">@{e(username)}</text>
  <text x="{width - pad}" y="38" fill="{DIM}" font-size="11" font-family="monospace" text-anchor="end">updated {today.isoformat()}</text>

  {stat_cards(combined, card_y, width, pad)}

  <text x="{pad}" y="{sources_y - 19}" fill="{MUTED}" font-size="12" font-family="system-ui,sans-serif">Source mix</text>
  {source_row_svg}

  <text x="{pad}" y="{heat_y - 15}" fill="{MUTED}" font-size="12" font-family="system-ui,sans-serif">Daily activity</text>
  {"".join(legend)}
  <g transform="translate({pad},{heat_y + 8})">
    {months}
    {cells}
  </g>

  <text x="{pad}" y="{footer_y}" fill="{DIM}" font-size="11" font-family="system-ui,sans-serif">Cell color shows the dominant system per day; brightness shows total tokens.</text>
  <text x="{width - pad}" y="{footer_y}" fill="{DIM}" font-size="11" font-family="system-ui,sans-serif" text-anchor="end">{e(generated_note)}</text>
</svg>'''


def write_summary(path: Path, sources: list[SourceStats], combined: dict[str, Any], today: dt.date) -> None:
    payload = {
        "updated_at": today.isoformat(),
        "totals": {
            "tokens": combined["total_tokens"],
            "sessions": combined["sessions"],
            "messages": combined["messages"],
            "active_days": combined["active_days"],
            "current_streak": combined["current_streak"],
            "longest_streak": combined["longest_streak"],
            "peak_tokens": combined["peak_tokens"],
            "longest_task_seconds": combined["longest_task_seconds"],
            "favorite_model": combined["favorite_model"],
        },
        "sources": [
            {
                "id": source.id,
                "label": source.label,
                "mode": source.mode,
                "tokens": source.tokens,
                "sessions": source.sessions,
                "messages": source.messages,
                "active_days": source.active_days,
                "current_streak": source.current_streak,
                "longest_streak": source.longest_streak,
                "peak_tokens": source.peak_tokens,
                "longest_task_seconds": source.longest_task_seconds,
                "favorite_model": source.favorite_model,
                "include_in_total": source.include_in_total,
                "note": source.note,
                "errors": source.errors,
            }
            for source in sources
        ],
        "daily_totals": combined["daily_totals"],
        "daily_by_source": combined["daily_by_source"],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", "-o", default="ai-token-stats.svg")
    parser.add_argument("--summary", default="data/token-stats.json")
    parser.add_argument("--manual", default="data/manual-usage.json")
    parser.add_argument("--username", default=DEFAULT_USERNAME)
    parser.add_argument("--today", default="")
    args = parser.parse_args()

    today = dt.date.fromisoformat(args.today) if args.today else dt.date.today()
    manual_path = Path(args.manual).expanduser()
    sources = [load_claude(today), load_codex(today), *load_manual(manual_path, today)]
    sources = [source for source in sources if source.tokens or source.daily_tokens or source.note]
    combined = combine(sources, today)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(generate_svg(sources, combined, args.username, today), encoding="utf-8")
    write_summary(Path(args.summary), sources, combined, today)

    print(f"wrote {output}")
    print(f"wrote {args.summary}")
    print(f"total tokens: {fmt_n(combined['total_tokens'])}")
    for source in sources:
        print(f"- {source.label}: {fmt_n(source.tokens)} ({source.mode})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

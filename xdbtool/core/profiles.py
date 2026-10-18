"""Panel detection and table categorisation.

Panel schemas differ across forks and versions, so detection is heuristic: each
profile lists *signature* tables, and the best-scoring profile wins. Nothing here
assumes exact column names — those are read live via introspection.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PanelProfile:
    key: str
    name: str
    # Strong indicators — their presence raises confidence a lot.
    signature_tables: frozenset[str] = field(default_factory=frozenset)
    # Weaker, supporting indicators.
    optional_tables: frozenset[str] = field(default_factory=frozenset)
    notes: str = ""


# Category -> candidate table names, used to group tables for export/migration.
# A table is matched case-insensitively against these candidates.
TABLE_CATEGORIES: dict[str, set[str]] = {
    "accounts": {"users", "reg_users"},
    "lines": {"lines", "user_lines"},
    "resellers": {"reg_users", "members"},
    "streams": {"streams", "streams_servers", "streams_episodes", "streams_series"},
    "bouquets": {"bouquets", "bouquets_channels"},
    "epg": {"epg", "epg_data", "epg_channels"},
    "servers": {"servers", "streaming_servers"},
    "settings": {"settings", "settings_mag", "setup"},
    "billing": {"credits_log", "reg_userlog", "orders", "invoices"},
    "logs": {
        "client_logs", "activity", "connections", "connections_log",
        "user_activity_logs", "lines_log", "watch_history",
    },
}


PROFILES: list[PanelProfile] = [
    PanelProfile(
        key="xui_one",
        name="XUI.one",
        signature_tables=frozenset({"lines", "access_codes", "streams_servers"}),
        optional_tables=frozenset({"bouquets", "streams", "reg_users", "settings"}),
        notes="XUI.one stores subscriber lines in the `lines` table.",
    ),
    PanelProfile(
        key="xtream_ui",
        name="Xtream-UI",
        signature_tables=frozenset({"users", "streams", "bouquets"}),
        optional_tables=frozenset({"reg_users", "settings", "streams_servers", "epg"}),
        notes="Classic Xtream-UI; subscriber lines live in the `users` table.",
    ),
    PanelProfile(
        key="streamcreed",
        name="StreamCreed",
        signature_tables=frozenset({"users", "streams", "bouquets", "streaming_servers"}),
        optional_tables=frozenset({"settings", "reg_users", "epg"}),
        notes="StreamCreed fork of Xtream-UI.",
    ),
]

GENERIC = PanelProfile(
    key="generic", name="Unknown / generic MySQL", notes="No panel signature matched."
)


@dataclass(frozen=True)
class DetectionResult:
    profile: PanelProfile
    confidence: float  # 0.0 - 1.0
    matched: frozenset[str]
    scores: dict[str, float]


def detect_panel(tables: list[str]) -> DetectionResult:
    """Score every profile against the present tables and return the best match."""
    present = {t.lower() for t in tables}
    scores: dict[str, float] = {}
    best: tuple[float, PanelProfile, frozenset[str]] = (0.0, GENERIC, frozenset())

    for prof in PROFILES:
        sig_hits = prof.signature_tables & present
        opt_hits = prof.optional_tables & present
        if not prof.signature_tables:
            continue
        sig_score = len(sig_hits) / len(prof.signature_tables)
        opt_score = (len(opt_hits) / len(prof.optional_tables)) if prof.optional_tables else 0.0
        # Signatures dominate; optional tables refine ties.
        score = round(0.8 * sig_score + 0.2 * opt_score, 3)
        scores[prof.key] = score
        if score > best[0]:
            best = (score, prof, frozenset(sig_hits | opt_hits))

    confidence, profile, matched = best
    if confidence < 0.34:  # not even one signature table's worth
        return DetectionResult(GENERIC, confidence, matched, scores)
    return DetectionResult(profile, confidence, matched, scores)


def categorise_tables(tables: list[str]) -> dict[str, list[str]]:
    """Group present tables by logical category (accounts, streams, logs, ...)."""
    present = {t.lower(): t for t in tables}
    result: dict[str, list[str]] = {}
    claimed: set[str] = set()
    for category, candidates in TABLE_CATEGORIES.items():
        hits = [present[c] for c in sorted(candidates) if c in present]
        if hits:
            result[category] = hits
            claimed.update(h.lower() for h in hits)
    others = sorted(t for t in tables if t.lower() not in claimed)
    if others:
        result["other"] = others
    return result

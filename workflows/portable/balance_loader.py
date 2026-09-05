#!/usr/bin/env python3
"""
Veyyon Balance Loader & Subscription Usage Snapshot Utility
Location: ~/.veyyon/workflows/balance_loader.py

Provides read-only, sanitized subscription usage metrics, quota tracking,
multi-window constraint evaluation, and reset timestamps across providers:
  - Google Antigravity (Ultra daily window: Google, OpenAI, Anthropic limits)
  - Anthropic (Claude 5h and 7d multi-window constraints)
  - OpenAI Codex (Pro 7d, 5h Spark, 7d Spark, Free 30d, resetCredits)
  - xAI Grok (Dormant status tracking)

Key Safety Guarantees:
  - Strictly read-only: no billing modifications or provider charges.
  - Zero credential leakage: strictly redacts account IDs, emails, project IDs.
  - Multi-window bottleneck analysis: identifies the constraining window.
  - Reset UTC formatting & freshness verification.
  - Safe handling of unknown/stale balances (never assumes 0 or infinite).
"""

from abc import ABC, abstractmethod
import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_STALE_THRESHOLD_SECONDS = 3600.0  # 1 hour
DEFAULT_SNAPSHOT_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "usage_snapshot_cache.json")


def ms_to_iso_utc(timestamp_ms: Optional[int]) -> str:
    """Convert millisecond epoch timestamp to ISO 8601 UTC string."""
    if timestamp_ms is None or timestamp_ms <= 0:
        return ""
    dt = datetime.datetime.fromtimestamp(timestamp_ms / 1000.0, tz=datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def sanitize_string(val: Optional[str]) -> str:
    """Sanitize identifiers to ensure no secrets or full emails/IDs are exposed."""
    if not val:
        return ""
    val_str = str(val).strip()
    if "*" in val_str:
        return val_str  # Already redacted by veyyon --redact
    if "@" in val_str:
        parts = val_str.split("@", 1)
        prefix = parts[0][:2] if len(parts[0]) >= 2 else parts[0][:1]
        domain_parts = parts[1].split(".", 1)
        dom_prefix = domain_parts[0][:1] if domain_parts[0] else "d"
        tld = domain_parts[1] if len(domain_parts) > 1 else "com"
        return f"{prefix}*@{dom_prefix}*.{tld}"
    if len(val_str) > 4:
        return f"{val_str[:2]}*{val_str[-1:]}"
    return f"{val_str[:1]}*"

@dataclass
class UsageAmount:
    used: float
    limit: float
    remaining: float
    used_fraction: float
    remaining_fraction: float
    unit: str = "percent"


@dataclass
class UsageWindow:
    id: str
    label: str
    duration_ms: int
    resets_at_ms: int
    resets_at_utc: str
    seconds_to_reset: float
    hours_to_reset: float
    fraction_time_remaining: float
    amount: UsageAmount
    status: str = "ok"
    is_cooldown: bool = False


@dataclass
class SubscriptionReport:
    provider: str
    account_id_redacted: str
    email_redacted: str
    plan_type: Optional[str]
    fetched_at_ms: int
    fetched_at_utc: str
    age_seconds: float
    is_stale: bool
    status: str
    limits: List[UsageWindow] = field(default_factory=list)
    bottleneck_window: Optional[UsageWindow] = None
    primary_window: Optional[UsageWindow] = None
    reset_credits: Optional[Dict[str, Any]] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

@dataclass
class NormalizedWindow:
    id: str
    label: str
    duration_seconds: float
    resets_at_utc: str
    seconds_to_reset: float
    remaining_fraction: float
    used_fraction: float
    unit: str
    status: str
    is_cooldown: bool
    remaining_units: Optional[float] = None
    total_limit: Optional[float] = None


@dataclass
class NormalizedProviderBalance:
    provider: str
    status: str
    is_active: bool
    effective_remaining_fraction: float
    cycle_seconds_to_reset: float
    cycle_hours_to_reset: float
    bottleneck_window_id: str
    bottleneck_hours_to_reset: float = 0.0
    bottleneck_duration_hours: float = 24.0
    cycle_duration_hours: float = 168.0
    primary_window_id: str = ""
    pro_weekly_remaining_fraction: Optional[float] = None
    pro_weekly_hours_to_reset: Optional[float] = None
    pro_weekly_duration_hours: Optional[float] = None
    windows: List[NormalizedWindow] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class NormalizedBalanceSnapshot:
    """
    Harness-agnostic canonical subscription balance snapshot.
    Usable by any harness or orchestration runtime.
    """
    schema_version: str = "1.0"
    generated_at_utc: str = ""
    providers: Dict[str, NormalizedProviderBalance] = field(default_factory=dict)
    active_providers: List[str] = field(default_factory=list)
    dormant_providers: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def get_effective_allowance(self, provider: str) -> Tuple[float, float, str, str]:
        """Returns bottleneck allowance: (remaining_fraction, hours_to_reset, bottleneck_label, status) from the SAME window."""
        norm_prov = self.providers.get(provider)
        if not norm_prov:
            if provider in self.dormant_providers:
                return (0.0, 0.0, "dormant", "dormant")
            return (0.5, 999.0, "unknown", "unknown")
        return (
            norm_prov.effective_remaining_fraction,
            norm_prov.bottleneck_hours_to_reset,
            norm_prov.bottleneck_window_id,
            norm_prov.status,
        )

    def get_cycle_allowance(self, provider: str) -> Tuple[float, float, str, str]:
        """Returns macro cycle allowance: (cycle_remaining_fraction, cycle_hours_to_reset, primary_window_label, status)."""
        norm_prov = self.providers.get(provider)
        if not norm_prov:
            if provider in self.dormant_providers:
                return (0.0, 0.0, "dormant", "dormant")
            return (0.5, 999.0, "unknown", "unknown")
        cycle_rem = norm_prov.pro_weekly_remaining_fraction if norm_prov.pro_weekly_remaining_fraction is not None else norm_prov.effective_remaining_fraction
        return (
            cycle_rem,
            norm_prov.cycle_hours_to_reset,
            norm_prov.primary_window_id or norm_prov.bottleneck_window_id,
            norm_prov.status,
        )

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "NormalizedBalanceSnapshot":
        providers: Dict[str, NormalizedProviderBalance] = {}
        for p_name, p_data in data.get("providers", {}).items():
            windows = [
                NormalizedWindow(**w) for w in p_data.get("windows", [])
            ]
            p_copy = dict(p_data)
            p_copy["windows"] = windows
            providers[p_name] = NormalizedProviderBalance(**p_copy)

        return cls(
            schema_version=data.get("schema_version", "1.0"),
            generated_at_utc=data.get("generated_at_utc", ""),
            providers=providers,
            active_providers=data.get("active_providers", []),
            dormant_providers=data.get("dormant_providers", []),
        )


@dataclass
class SanitizedUsageSnapshot:
    generated_at_ms: int
    generated_at_utc: str
    subscriptions: List[SubscriptionReport] = field(default_factory=list)
    capacity: Dict[str, Any] = field(default_factory=dict)
    active_providers: List[str] = field(default_factory=list)
    dormant_providers: List[str] = field(default_factory=list)

    def to_normalized(self) -> NormalizedBalanceSnapshot:
        """Converts Veyyon usage snapshot into harness-agnostic NormalizedBalanceSnapshot."""
        prov_balances: Dict[str, NormalizedProviderBalance] = {}
        for sub in self.subscriptions:
            p_name = sub.provider
            rem_frac, hrs_reset, btn_label, status = self.get_effective_allowance(p_name)
            norm_windows = []
            for w in sub.limits:
                norm_windows.append(
                    NormalizedWindow(
                        id=w.id,
                        label=w.label,
                        duration_seconds=w.duration_ms / 1000.0,
                        resets_at_utc=w.resets_at_utc,
                        seconds_to_reset=w.seconds_to_reset,
                        remaining_fraction=w.amount.remaining_fraction,
                        used_fraction=w.amount.used_fraction,
                        remaining_units=w.amount.remaining,
                        total_limit=w.amount.limit,
                        unit=w.amount.unit,
                        status=w.status,
                        is_cooldown=w.is_cooldown,
                    )
                )
            btn_dur_hrs = (sub.bottleneck_window.duration_ms / 3600000.0) if sub.bottleneck_window else 24.0
            cycle_dur_hrs = (sub.primary_window.duration_ms / 3600000.0) if sub.primary_window else 168.0

            # Pro weekly metrics for Codex (specifically tracking the 7d allowance window)
            pro_rem = None
            pro_hrs = None
            pro_dur = None
            if p_name == "openai-codex":
                for w in sub.limits:
                    if w.duration_ms >= 500000000 and (sub.plan_type == "pro" or "7" in w.id):
                        pro_rem = w.amount.remaining_fraction
                        pro_hrs = w.hours_to_reset
                        pro_dur = w.duration_ms / 3600000.0
                        break

            prim_label = sub.primary_window.label if sub.primary_window else btn_label
            btn_hrs = sub.bottleneck_window.hours_to_reset if sub.bottleneck_window else 0.0
            cycle_hrs = sub.primary_window.hours_to_reset if sub.primary_window else btn_hrs

            prov_balances[p_name] = NormalizedProviderBalance(
                provider=p_name,
                status=status,
                is_active=status == "ok",
                effective_remaining_fraction=rem_frac,
                cycle_seconds_to_reset=cycle_hrs * 3600.0,
                cycle_hours_to_reset=cycle_hrs,
                bottleneck_window_id=btn_label,
                bottleneck_hours_to_reset=btn_hrs,
                bottleneck_duration_hours=btn_dur_hrs,
                cycle_duration_hours=cycle_dur_hrs,
                primary_window_id=prim_label,
                pro_weekly_remaining_fraction=pro_rem,
                pro_weekly_hours_to_reset=pro_hrs,
                pro_weekly_duration_hours=pro_dur,
                windows=norm_windows,
                metadata=sub.metadata,
            )
        for dp in self.dormant_providers:
            if dp not in prov_balances:
                prov_balances[dp] = NormalizedProviderBalance(
                    provider=dp,
                    status="dormant",
                    is_active=False,
                    effective_remaining_fraction=0.0,
                    cycle_seconds_to_reset=0.0,
                    cycle_hours_to_reset=0.0,
                    bottleneck_window_id="dormant",
                    windows=[],
                    metadata={},
                )

        return NormalizedBalanceSnapshot(
            schema_version="1.0",
            generated_at_utc=self.generated_at_utc,
            providers=prov_balances,
            active_providers=self.active_providers,
            dormant_providers=self.dormant_providers,
        )
    def get_provider_reports(self, provider: str) -> List[SubscriptionReport]:
        return [sub for sub in self.subscriptions if sub.provider == provider]

    def get_effective_allowance(self, provider: str) -> Tuple[float, float, str, str]:
        """
        Calculates effective remaining fraction, hours to reset, bottleneck window label, and status
        across all accounts for the given provider.
        Returns: (remaining_fraction, hours_to_reset, bottleneck_label, status)
        """
        reports = self.get_provider_reports(provider)
        if not reports:
            if provider in self.dormant_providers:
                return (0.0, 0.0, "dormant", "dormant")
            # Unknown provider: safe neutral baseline (0.5), not 0 or inf
            return (0.5, 999.0, "unknown", "unknown")

        # Pick the best account if multiple (e.g. pro vs free for Codex)
        # Prefer 'pro' or higher plan type, or account with highest remaining allowance
        best_report = None
        for r in reports:
            if r.status != "ok":
                continue
            if best_report is None:
                best_report = r
            elif r.plan_type == "pro" and best_report.plan_type != "pro":
                best_report = r
            elif r.bottleneck_window and best_report.bottleneck_window:
                if r.bottleneck_window.amount.remaining_fraction > best_report.bottleneck_window.amount.remaining_fraction:
                    best_report = r

        if best_report is None:
            # All accounts in non-ok status
            first = reports[0]
            btn = first.bottleneck_window
            prim = first.primary_window or btn
            rem = btn.amount.remaining_fraction if btn else 0.0
            hrs = btn.hours_to_reset if btn else (prim.hours_to_reset if prim else 0.0)
            lbl = btn.label if btn else "all_failed"
            return (rem, hrs, lbl, first.status)

        btn = best_report.bottleneck_window
        prim = best_report.primary_window or btn
        if not btn:
            return (1.0, 999.0, "unconstrained", best_report.status)

        return (
            btn.amount.remaining_fraction,
            btn.hours_to_reset,
            btn.label,
            best_report.status,
        )

    def get_cycle_allowance(self, provider: str) -> Tuple[float, float, str, str]:
        """
        Returns macro cycle allowance: (remaining_fraction, hours_to_reset, primary_label, status).
        """
        reports = self.get_provider_reports(provider)
        if not reports:
            if provider in self.dormant_providers:
                return (0.0, 0.0, "dormant", "dormant")
            return (0.5, 999.0, "unknown", "unknown")
        best_report = None
        for r in reports:
            if r.status != "ok":
                continue
            if best_report is None:
                best_report = r
            elif r.plan_type == "pro" and best_report.plan_type != "pro":
                best_report = r
        if not best_report or not best_report.primary_window:
            return self.get_effective_allowance(provider)
        prim = best_report.primary_window
        return (
            prim.amount.remaining_fraction,
            prim.hours_to_reset,
            prim.label,
            best_report.status,
        )


def parse_usage_json(
    raw_data: Any,
    current_time_ms: Optional[int] = None,
    stale_threshold_seconds: float = DEFAULT_STALE_THRESHOLD_SECONDS,
) -> SanitizedUsageSnapshot:
    """Parse and sanitize JSON dictionary from 'veyyon usage --json --redact'."""
    if isinstance(raw_data, str):
        data = json.loads(raw_data)
    elif isinstance(raw_data, dict):
        data = raw_data
    else:
        raise ValueError(f"Expected str or dict, got {type(raw_data)}")

    generated_at_ms = int(data.get("generatedAt") or 0)
    if not current_time_ms:
        current_time_ms = generated_at_ms if generated_at_ms > 0 else int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)

    generated_at_utc = ms_to_iso_utc(generated_at_ms or current_time_ms)

    subscriptions: List[SubscriptionReport] = []
    seen_providers = set()

    reports_list = data.get("reports", [])
    for rep in reports_list:
        provider = rep.get("provider", "unknown")
        seen_providers.add(provider)
        fetched_at_ms = int(rep.get("fetchedAt") or current_time_ms)
        fetched_at_utc = ms_to_iso_utc(fetched_at_ms)
        age_seconds = max(0.0, (current_time_ms - fetched_at_ms) / 1000.0)
        is_stale = age_seconds > stale_threshold_seconds

        meta = rep.get("metadata", {})
        account_id = sanitize_string(meta.get("accountId") or meta.get("projectId") or "")
        email = sanitize_string(meta.get("email") or "")
        plan_type = meta.get("planType")

        status = "ok"
        if meta.get("limitReached", False):
            status = "limit_reached"
        elif not meta.get("allowed", True):
            status = "not_allowed"
        elif is_stale:
            status = "stale"

        limits_raw = rep.get("limits", [])
        parsed_limits: List[UsageWindow] = []

        for lim in limits_raw:
            lim_id = lim.get("id", "")
            label = lim.get("label", lim_id)
            win_meta = lim.get("window", {})
            dur_ms = int(win_meta.get("durationMs") or 86400000)
            resets_ms = int(win_meta.get("resetsAt") or 0)
            resets_utc = ms_to_iso_utc(resets_ms)

            sec_to_reset = max(0.0, (resets_ms - current_time_ms) / 1000.0) if resets_ms > 0 else 0.0
            hrs_to_reset = sec_to_reset / 3600.0
            fraction_time_remaining = min(1.0, max(0.0, (sec_to_reset * 1000.0) / dur_ms)) if dur_ms > 0 else 0.0

            amt_meta = lim.get("amount", {})
            unit = amt_meta.get("unit", "percent")
            used = float(amt_meta.get("used", 0.0))
            limit_val = float(amt_meta.get("limit", 100.0))
            remaining = float(amt_meta.get("remaining", limit_val - used))

            # fractions
            rem_frac = amt_meta.get("remainingFraction")
            if rem_frac is None:
                rem_frac = (remaining / limit_val) if limit_val > 0 else 1.0
            else:
                rem_frac = float(rem_frac)

            used_frac = amt_meta.get("usedFraction")
            if used_frac is None:
                used_frac = (used / limit_val) if limit_val > 0 else 0.0
            else:
                used_frac = float(used_frac)

            lim_status = lim.get("status", "ok")
            is_cooldown = lim_status != "ok" or status != "ok"

            amt = UsageAmount(
                used=used,
                limit=limit_val,
                remaining=remaining,
                used_fraction=used_frac,
                remaining_fraction=rem_frac,
                unit=unit,
            )

            uw = UsageWindow(
                id=lim_id,
                label=label,
                duration_ms=dur_ms,
                resets_at_ms=resets_ms,
                resets_at_utc=resets_utc,
                seconds_to_reset=sec_to_reset,
                hours_to_reset=hrs_to_reset,
                fraction_time_remaining=fraction_time_remaining,
                amount=amt,
                status=lim_status,
                is_cooldown=is_cooldown,
            )
            parsed_limits.append(uw)

        bottleneck = None
        primary_win = None
        if parsed_limits:
            bottleneck = min(
                parsed_limits,
                key=lambda w: (w.amount.remaining_fraction, w.seconds_to_reset),
            )
            # Primary cycle window: the macro window with the longest duration (e.g. 7d or 30d or daily)
            primary_win = max(parsed_limits, key=lambda w: w.duration_ms)
            if bottleneck.is_cooldown and status == "ok":
                status = bottleneck.status

        sub_report = SubscriptionReport(
            provider=provider,
            account_id_redacted=account_id,
            email_redacted=email,
            plan_type=plan_type,
            fetched_at_ms=fetched_at_ms,
            fetched_at_utc=fetched_at_utc,
            age_seconds=age_seconds,
            is_stale=is_stale,
            status=status,
            limits=parsed_limits,
            bottleneck_window=bottleneck,
            primary_window=primary_win,
            reset_credits=rep.get("resetCredits"),
        )
        subscriptions.append(sub_report)

    dormant = ["xai-oauth"] if "xai-oauth" not in seen_providers else []
    active = sorted(list(seen_providers))

    return SanitizedUsageSnapshot(
        generated_at_ms=generated_at_ms,
        generated_at_utc=generated_at_utc,
        subscriptions=subscriptions,
        capacity=data.get("capacity", {}),
        active_providers=active,
        dormant_providers=dormant,
    )


def fetch_live_usage(redact: bool = True, timeout_sec: int = 25) -> Dict[str, Any]:
    """Invoke 'veyyon usage --json [--redact]' via subprocess."""
    cmd = ["veyyon", "usage", "--json"]
    if redact:
        cmd.append("--redact")

    # On Windows, veyyon may be veyyon.cmd or in PATH
    try:
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=True,
            shell=True if sys.platform == "win32" else False,
        )
        return json.loads(res.stdout)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError) as e:
        # If shell=False failed on Windows, retry through git-bash or cmd
        if sys.platform == "win32" and not (isinstance(e, subprocess.CalledProcessError) and e.returncode == 0):
            try:
                res2 = subprocess.run(
                    "veyyon usage --json --redact",
                    capture_output=True,
                    text=True,
                    timeout=timeout_sec,
                    shell=True,
                )
                if res2.returncode == 0 and res2.stdout.strip().startswith("{"):
                    return json.loads(res2.stdout)
            except Exception:
                pass
        raise RuntimeError(f"Failed to fetch live usage from veyyon CLI: {e}")


def load_snapshot(
    allow_live: bool = True,
    cache_path: Optional[str] = DEFAULT_SNAPSHOT_CACHE,
    redact: bool = True,
) -> SanitizedUsageSnapshot:
    """Load sanitized snapshot: tries live CLI first, falls back to disk cache."""
    if allow_live:
        try:
            raw = fetch_live_usage(redact=redact)
            snapshot = parse_usage_json(raw)
            # Update cache file safely
            if cache_path:
                try:
                    with open(cache_path, "w", encoding="utf-8") as f:
                        json.dump(raw, f, indent=2)
                except Exception:
                    pass
            return snapshot
        except Exception as e:
            # Fall back to cache if available
            if cache_path and os.path.exists(cache_path):
                with open(cache_path, "r", encoding="utf-8") as f:
                    raw_cached = json.load(f)
                return parse_usage_json(raw_cached)
            raise RuntimeError(f"Failed to load usage snapshot: {e}")
    else:
        if cache_path and os.path.exists(cache_path):
            with open(cache_path, "r", encoding="utf-8") as f:
                return parse_usage_json(json.load(f))
        raise RuntimeError("Live usage disabled and cache does not exist")

class BalanceAdapter(ABC):
    """Abstract base adapter for loading balance snapshots."""

    @abstractmethod
    def fetch_snapshot(self) -> NormalizedBalanceSnapshot:
        """Fetch and return canonical NormalizedBalanceSnapshot."""
        pass


class VeyyonBalanceAdapter(BalanceAdapter):
    """
    Adapter that executes 'veyyon usage --json [--redact]' or loads a Veyyon cache,
    normalizing the output to the canonical schema.
    """

    def __init__(
        self,
        cmd: Optional[str] = None,
        cache_path: Optional[str] = DEFAULT_SNAPSHOT_CACHE,
        allow_live: bool = True,
        redact: bool = True,
    ):
        self.cmd = cmd or os.environ.get("VEYYON_USAGE_CMD", "veyyon usage --json")
        self.cache_path = cache_path
        self.allow_live = allow_live
        self.redact = redact

    def fetch_snapshot(self) -> NormalizedBalanceSnapshot:
        snapshot = load_snapshot(
            allow_live=self.allow_live,
            cache_path=self.cache_path,
            redact=self.redact,
        )
        return snapshot.to_normalized()


class FileBalanceAdapter(BalanceAdapter):
    """
    Adapter that loads balance JSON from a file or stdin.
    Supports either pre-normalized JSON or raw Veyyon usage JSON.
    """

    def __init__(self, file_path: str):
        self.file_path = file_path

    def fetch_snapshot(self) -> NormalizedBalanceSnapshot:
        if self.file_path == "-":
            raw_text = sys.stdin.read()
        else:
            if not os.path.exists(self.file_path):
                raise FileNotFoundError(f"Balance file not found: {self.file_path}")
            with open(self.file_path, "r", encoding="utf-8") as f:
                raw_text = f.read()

        data = json.loads(raw_text)
        if "schema_version" in data and "providers" in data:
            return NormalizedBalanceSnapshot.from_dict(data)
        # Parse as raw veyyon usage and normalize
        return parse_usage_json(data).to_normalized()


class DirectJsonAdapter(BalanceAdapter):
    """Adapter that accepts an in-memory dictionary directly."""

    def __init__(self, data: Dict[str, Any]):
        self.data = data

    def fetch_snapshot(self) -> NormalizedBalanceSnapshot:
        if "schema_version" in self.data and "providers" in self.data:
            return NormalizedBalanceSnapshot.from_dict(self.data)
        return parse_usage_json(self.data).to_normalized()


def get_balance_adapter(
    adapter_type: str = "veyyon",
    file_path: Optional[str] = None,
    cmd: Optional[str] = None,
    cache_path: Optional[str] = DEFAULT_SNAPSHOT_CACHE,
    allow_live: bool = True,
    redact: bool = True,
    direct_data: Optional[Dict[str, Any]] = None,
) -> BalanceAdapter:
    """Factory function to acquire configured BalanceAdapter."""
    if direct_data is not None or adapter_type == "direct":
        return DirectJsonAdapter(direct_data or {})
    if adapter_type == "file" or file_path:
        return FileBalanceAdapter(file_path=file_path or DEFAULT_SNAPSHOT_CACHE)
    return VeyyonBalanceAdapter(
        cmd=cmd,
        cache_path=cache_path,
        allow_live=allow_live,
        redact=redact,
    )


def format_snapshot_table(snapshot: SanitizedUsageSnapshot) -> str:
    """Format human-readable tabular overview of the snapshot."""
    lines = [
        "=" * 78,
        f"VEYYON SUBSCRIPTION BALANCE SNAPSHOT (Generated: {snapshot.generated_at_utc})",
        "=" * 78,
        f"{'Provider':<20} {'Plan':<8} {'Window / Limit':<22} {'Remaining':<12} {'Reset UTC':<20} {'Status'}",
        "-" * 78,
    ]

    for sub in snapshot.subscriptions:
        plan = sub.plan_type or "tier"
        if not sub.limits:
            lines.append(f"{sub.provider:<20} {plan:<8} {'(no limits)':<22} {'N/A':<12} {'N/A':<20} {sub.status}")
            continue

        for idx, win in enumerate(sub.limits):
            prov_str = sub.provider if idx == 0 else ""
            plan_str = plan if idx == 0 else ""
            rem_str = f"{win.amount.remaining:.1f}%" if win.amount.unit == "percent" else f"{win.amount.remaining:.0f}"
            btn_marker = " *" if sub.bottleneck_window and sub.bottleneck_window.id == win.id else ""
            win_label = f"{win.label}{btn_marker}"[:21]
            lines.append(
                f"{prov_str:<20} {plan_str:<8} {win_label:<22} {rem_str:<12} {win.resets_at_utc:<20} {win.status}"
            )

    if snapshot.dormant_providers:
        lines.append("-" * 78)
        for dp in snapshot.dormant_providers:
            lines.append(f"{dp:<20} {'dormant':<8} {'(subscription off)':<22} {'0%':<12} {'N/A':<20} dormant")

    lines.append("-" * 78)
    lines.append("* indicates multi-window bottleneck limit")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Veyyon Balance Loader & Usage Snapshot CLI")
    parser.add_argument("--json", action="store_true", help="Output sanitized JSON")
    parser.add_argument("--normalized", action="store_true", help="Output canonical NormalizedBalanceSnapshot JSON")
    parser.add_argument("--adapter", choices=["veyyon", "file", "direct"], default="veyyon", help="Adapter type")
    parser.add_argument("--balance-file", default=None, help="Input balance JSON file (or '-' for stdin)")
    parser.add_argument("--balance-cmd", default=None, help="Custom balance CLI command")
    parser.add_argument("--no-live", action="store_true", help="Use local cache only, do not invoke veyyon CLI")
    parser.add_argument("--cache-path", default=DEFAULT_SNAPSHOT_CACHE, help="Path to cache file")
    parser.add_argument("--no-redact", action="store_true", help="Do not redact IDs (defaults to redacted)")
    args = parser.parse_args()

    try:
        if args.normalized:
            adapter = get_balance_adapter(
                adapter_type="file" if args.balance_file else args.adapter,
                file_path=args.balance_file,
                cmd=args.balance_cmd,
                cache_path=args.cache_path,
                allow_live=not args.no_live,
                redact=not args.no_redact,
            )
            norm_snapshot = adapter.fetch_snapshot()
            print(json.dumps(norm_snapshot.to_dict(), indent=2))
            return

        snapshot = load_snapshot(
            allow_live=not args.no_live,
            cache_path=args.cache_path,
            redact=not args.no_redact,
        )
    except Exception as e:
        sys.stderr.write(f"Error loading usage snapshot: {e}\n")
        sys.exit(1)

    if args.json:
        # Convert dataclasses to dict
        def dataclass_serializer(obj):
            if hasattr(obj, "__dataclass_fields__"):
                return asdict(obj)
            raise TypeError(f"Object of type {type(obj)} is not JSON serializable")

        print(json.dumps(asdict(snapshot), indent=2))
    else:
        print(format_snapshot_table(snapshot))


if __name__ == "__main__":
    main()

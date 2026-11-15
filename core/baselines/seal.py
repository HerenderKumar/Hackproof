"""What makes a sealed baseline incomplete (M9, the plan's Task 12 seal rule)."""

from collections.abc import Sequence


def incomplete_reasons(
    unreadable: int, limited_to: Sequence[str] | None, size_limit_reached: bool
) -> list[str]:
    """One reason per gap in the scan, in words the team and the judge read; empty if whole."""
    reasons = []
    if unreadable:
        reasons.append(f"{unreadable} item{'s' if unreadable != 1 else ''} could not be read")
    if limited_to:
        reasons.append(f"limited to {', '.join(limited_to)} by the participant")
    if size_limit_reached:
        reasons.append("size limit reached")
    return reasons

#!/usr/bin/env python3
"""
Autonomous Personal Financial Management (PFM) & Wealth Waterfall Engine.

Ingests a personal transaction ledger CSV and financial profile JSON,
computes exact cashflow metrics using pandas/numpy, flags anomalies and
subscription leaks, executes a 3-tier capital allocation waterfall, and
exports an interactive Plotly Sankey chart (cashflow_sankey.html).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import plotly.graph_objects as go


HIGH_APR_THRESHOLD = 0.065
OUTLIER_WANTS_THRESHOLD = 250.00
CYCLE_TARGET_DAYS = 30
CYCLE_TOLERANCE_DAYS = 2


def round_cents(value: float) -> float:
    return float(np.round(value, 2))


def load_inputs(ledger_path: Path, profile_path: Path) -> tuple[pd.DataFrame, dict[str, Any]]:
    df = pd.read_csv(ledger_path)
    df["Date"] = pd.to_datetime(df["Date"])
    df["Amount"] = df["Amount"].astype(float)
    df["Merchant"] = df["Merchant"].astype(str).str.strip()
    df["Category"] = df["Category"].astype(str).str.strip()
    df["Bucket"] = df["Bucket"].astype(str).str.strip()

    with profile_path.open("r", encoding="utf-8") as f:
        profile = json.load(f)

    return df, profile


def detect_anomalies(df: pd.DataFrame) -> dict[str, Any]:
    # 1. Exact duplicate charges: identical Date, Merchant, and Amount (outflows)
    outflows = df[df["Amount"] < 0].copy()
    dup_mask = outflows.duplicated(subset=["Date", "Merchant", "Amount"], keep="first")
    duplicates = outflows[dup_mask].copy()
    duplicates["Charge"] = np.abs(duplicates["Amount"])
    duplicate_leak_total = round_cents(float(duplicates["Charge"].sum()))

    # 2. Recurring subscriptions:
    #    (a) Cycle-based detection: merchants billing within +/- 2 days of a 30-day cycle
    #        after removing exact same-day duplicate postings.
    dedup_outflows = outflows.drop_duplicates(subset=["Date", "Merchant", "Amount"], keep="first").copy()
    dedup_outflows = dedup_outflows.sort_values(["Merchant", "Date"])
    cycle_merchants: set[str] = set()
    for merchant, grp in dedup_outflows.groupby("Merchant"):
        if len(grp) >= 2:
            diffs = grp["Date"].diff().dt.days.dropna()
            if ((diffs >= CYCLE_TARGET_DAYS - CYCLE_TOLERANCE_DAYS) & (diffs <= CYCLE_TARGET_DAYS + CYCLE_TOLERANCE_DAYS)).any():
                cycle_merchants.add(str(merchant))

    #    (b) Category-based subscription detection (essential for single-month ledgers)
    cat_sub_merchants = set(
        dedup_outflows.loc[
            dedup_outflows["Category"].str.lower() == "subscription", "Merchant"
        ].unique()
    )
    all_sub_merchants = sorted(cycle_merchants | cat_sub_merchants)

    subscription_rows = []
    for merchant in all_sub_merchants:
        m_dedup = dedup_outflows[dedup_outflows["Merchant"] == merchant]
        m_raw = outflows[outflows["Merchant"] == merchant]
        monthly_cost = round_cents(float(np.abs(m_dedup["Amount"]).mean()))
        annualized_cost = round_cents(monthly_cost * 12.0)
        method = (
            "30±2 Day Cycle & Category"
            if (merchant in cycle_merchants and merchant in cat_sub_merchants)
            else ("30±2 Day Cycle" if merchant in cycle_merchants else "Subscription Category (Single-Month Ledger)")
        )
        subscription_rows.append(
            {
                "Merchant": merchant,
                "Monthly_Cost": monthly_cost,
                "Annualized_Drag": annualized_cost,
                "Raw_Ledger_Count": int(len(m_raw)),
                "Raw_Ledger_Spend": round_cents(float(np.abs(m_raw["Amount"]).sum())),
                "Detection_Method": method,
            }
        )

    subscriptions_df = pd.DataFrame(subscription_rows)
    monthly_sub_runrate = (
        round_cents(float(subscriptions_df["Monthly_Cost"].sum()))
        if not subscriptions_df.empty
        else 0.0
    )
    annualized_sub_drag = (
        round_cents(float(subscriptions_df["Annualized_Drag"].sum()))
        if not subscriptions_df.empty
        else 0.0
    )

    # 3. Discretionary outliers: single-item Wants exceeding $250
    wants_outflows = outflows[outflows["Bucket"] == "Wants"].copy()
    wants_outflows["Spend"] = np.abs(wants_outflows["Amount"])
    outliers = wants_outflows[wants_outflows["Spend"] > OUTLIER_WANTS_THRESHOLD].copy()

    return {
        "duplicates": duplicates,
        "duplicate_leak_total": duplicate_leak_total,
        "subscriptions": subscriptions_df,
        "monthly_sub_runrate": monthly_sub_runrate,
        "annualized_sub_drag": annualized_sub_drag,
        "outliers": outliers,
        "cycle_merchants": sorted(cycle_merchants),
    }


def run_cashflow_and_waterfall(df: pd.DataFrame, profile: dict[str, Any]) -> dict[str, Any]:
    total_income = round_cents(float(df.loc[df["Bucket"] == "Income", "Amount"].sum()))
    needs_outflows = round_cents(float(np.abs(df.loc[df["Bucket"] == "Needs", "Amount"].sum())))
    wants_outflows = round_cents(float(np.abs(df.loc[df["Bucket"] == "Wants", "Amount"].sum())))
    total_outflows = round_cents(needs_outflows + wants_outflows)
    net_surplus = round_cents(total_income - total_outflows)

    # Verify reconciliation against raw signed sum
    raw_net = round_cents(float(df["Amount"].sum()))
    if not np.isclose(net_surplus, raw_net, atol=1e-6):
        raise ValueError(f"Reconciliation mismatch: bucket net {net_surplus} != raw sum {raw_net}")

    needs_pct = (needs_outflows / total_income) if total_income > 0 else 0.0
    wants_pct = (wants_outflows / total_income) if total_income > 0 else 0.0
    savings_rate = (net_surplus / total_income) if total_income > 0 else 0.0

    # 3-Tier Capital Allocation Waterfall
    remaining = max(0.0, net_surplus)

    # Tier 1: Emergency Buffer (3 months of essential Needs)
    ef_target = round_cents(3.0 * needs_outflows)
    ef_current = round_cents(float(profile["liquid_emergency_cash"]))
    ef_shortfall = round_cents(max(0.0, ef_target - ef_current))
    tier1_alloc = round_cents(min(remaining, ef_shortfall))
    remaining = round_cents(remaining - tier1_alloc)
    ef_post = round_cents(ef_current + tier1_alloc)

    # Tier 2: High-APR Debt (APR > 6.5%)
    debt_balance = round_cents(float(profile["high_apr_debt_balance"]))
    debt_apr = float(profile["high_apr_rate"])
    debt_eligible = debt_apr > HIGH_APR_THRESHOLD
    tier2_alloc = round_cents(min(remaining, debt_balance)) if debt_eligible else 0.0
    remaining = round_cents(remaining - tier2_alloc)
    debt_post = round_cents(debt_balance - tier2_alloc)
    annualized_interest_saved = round_cents(tier2_alloc * debt_apr)

    # Tier 3a: Roth IRA (remaining annual room, capped at $583.33/mo)
    roth_limit = round_cents(float(profile["roth_ira_annual_limit"]))
    roth_ytd = round_cents(float(profile["roth_ira_ytd_contributed"]))
    roth_room = round_cents(max(0.0, roth_limit - roth_ytd))
    roth_monthly_cap = round_cents(roth_limit / 12.0)  # $583.33/mo for $7,000 limit
    tier3a_roth_alloc = round_cents(min(remaining, roth_room, roth_monthly_cap))
    remaining = round_cents(remaining - tier3a_roth_alloc)
    roth_ytd_post = round_cents(roth_ytd + tier3a_roth_alloc)
    roth_room_post = round_cents(roth_limit - roth_ytd_post)

    # Tier 3b: Taxable Brokerage Low-Cost Index ETFs (remainder)
    tier3b_etf_alloc = round_cents(remaining)
    remaining = round_cents(remaining - tier3b_etf_alloc)

    # Verify waterfall allocation sums exactly to positive net surplus
    allocated_total = round_cents(tier1_alloc + tier2_alloc + tier3a_roth_alloc + tier3b_etf_alloc)
    if net_surplus > 0 and not np.isclose(allocated_total, net_surplus, atol=1e-6):
        raise ValueError(f"Waterfall mismatch: allocated {allocated_total} != surplus {net_surplus}")

    # Category breakdowns for Sankey & reporting
    needs_by_cat = (
        df[df["Bucket"] == "Needs"]
        .groupby("Category")["Amount"]
        .apply(lambda s: round_cents(float(np.abs(s.sum()))))
        .sort_values(ascending=False)
    )
    wants_by_cat = (
        df[df["Bucket"] == "Wants"]
        .groupby("Category")["Amount"]
        .apply(lambda s: round_cents(float(np.abs(s.sum()))))
        .sort_values(ascending=False)
    )

    return {
        "total_income": total_income,
        "needs_outflows": needs_outflows,
        "wants_outflows": wants_outflows,
        "total_outflows": total_outflows,
        "net_surplus": net_surplus,
        "needs_pct": needs_pct,
        "wants_pct": wants_pct,
        "savings_rate": savings_rate,
        "needs_by_cat": needs_by_cat,
        "wants_by_cat": wants_by_cat,
        "waterfall": {
            "ef_target": ef_target,
            "ef_current": ef_current,
            "ef_shortfall": ef_shortfall,
            "tier1_alloc": tier1_alloc,
            "ef_post": ef_post,
            "debt_balance": debt_balance,
            "debt_apr": debt_apr,
            "debt_eligible": debt_eligible,
            "tier2_alloc": tier2_alloc,
            "debt_post": debt_post,
            "annualized_interest_saved": annualized_interest_saved,
            "roth_limit": roth_limit,
            "roth_ytd": roth_ytd,
            "roth_room": roth_room,
            "roth_monthly_cap": roth_monthly_cap,
            "tier3a_roth_alloc": tier3a_roth_alloc,
            "roth_ytd_post": roth_ytd_post,
            "roth_room_post": roth_room_post,
            "tier3b_etf_alloc": tier3b_etf_alloc,
            "allocated_total": allocated_total,
        },
    }


def build_sankey(metrics: dict[str, Any], anomalies: dict[str, Any], output_path: Path) -> None:
    income = metrics["total_income"]
    needs = metrics["needs_outflows"]
    wants = metrics["wants_outflows"]
    surplus = metrics["net_surplus"]
    wf = metrics["waterfall"]

    nodes: list[str] = []
    node_colors: list[str] = []
    sources: list[int] = []
    targets: list[int] = []
    values: list[float] = []
    link_colors: list[str] = []
    link_labels: list[str] = []

    def add_node(label: str, color: str) -> int:
        idx = len(nodes)
        nodes.append(label)
        node_colors.append(color)
        return idx

    def add_link(src: int, tgt: int, val: float, color: str, desc: str) -> None:
        if val <= 0:
            return
        sources.append(src)
        targets.append(tgt)
        values.append(round_cents(val))
        link_colors.append(color)
        pct = (val / income * 100.0) if income > 0 else 0.0
        link_labels.append(f"{desc}<br>${val:,.2f} ({pct:.2f}% of Income)")

    # Stage 0: Income
    idx_income = add_node(f"Total Income<br>${income:,.2f}", "#0F766E")

    # Stage 1: 50/30/20 Top-Level Buckets
    idx_needs = add_node(
        f"Needs (Target ≤50%)<br>${needs:,.2f} ({metrics['needs_pct']*100:.1f}%)", "#2563EB"
    )
    idx_wants = add_node(
        f"Wants (Target ≤30%)<br>${wants:,.2f} ({metrics['wants_pct']*100:.1f}%)", "#EA580C"
    )
    idx_surplus = add_node(
        f"Net Surplus (Target ≥20%)<br>${surplus:,.2f} ({metrics['savings_rate']*100:.1f}%)", "#059669"
    )

    add_link(idx_income, idx_needs, needs, "rgba(37, 99, 235, 0.30)", "Income → Needs")
    add_link(idx_income, idx_wants, wants, "rgba(234, 88, 12, 0.30)", "Income → Wants")
    add_link(idx_income, idx_surplus, max(0.0, surplus), "rgba(5, 150, 105, 0.35)", "Income → Net Monthly Surplus")

    # Stage 2a: Needs Categories
    needs_palette = ["#3B82F6", "#60A5FA", "#1D4ED8", "#38BDF8", "#0284C7", "#6366F1"]
    for i, (cat, amt) in enumerate(metrics["needs_by_cat"].items()):
        c_idx = add_node(f"{cat}<br>${amt:,.2f}", needs_palette[i % len(needs_palette)])
        add_link(idx_needs, c_idx, float(amt), "rgba(59, 130, 246, 0.25)", f"Needs → {cat}")

    # Stage 2b: Wants Categories
    wants_palette = ["#F97316", "#FB923C", "#DC2626", "#F59E0B"]
    outlier_cats = set(anomalies["outliers"]["Category"].unique()) if not anomalies["outliers"].empty else set()
    dup_cats = set(anomalies["duplicates"]["Category"].unique()) if not anomalies["duplicates"].empty else set()
    for i, (cat, amt) in enumerate(metrics["wants_by_cat"].items()):
        flags = []
        if cat in outlier_cats:
            flags.append("Outlier >$250")
        if cat in dup_cats:
            flags.append(f"Dup -${anomalies['duplicate_leak_total']:,.2f}")
        extra = f" [{' | '.join(flags)}]" if flags else ""
        c_idx = add_node(f"{cat}{extra}<br>${amt:,.2f}", wants_palette[i % len(wants_palette)])
        add_link(idx_wants, c_idx, float(amt), "rgba(249, 115, 22, 0.25)", f"Wants → {cat}")

    # Stage 2c: 3-Tier Capital Allocation Waterfall
    ef_months_post = (wf["ef_post"] / needs) if needs > 0 else 0.0
    debt_status = "Paid Off" if wf["debt_post"] == 0 else f"${wf['debt_post']:,.2f} left"
    waterfall_specs = [
        (
            f"Tier 1: Emergency Buffer<br>${wf['tier1_alloc']:,.2f} ({ef_months_post:.1f} mo funded)",
            wf["tier1_alloc"],
            "#0D9488",
            "rgba(13, 148, 136, 0.38)",
            "Tier 1: Emergency Buffer Shortfall Fill",
        ),
        (
            f"Tier 2: High-APR Debt ({wf['debt_apr']*100:.1f}%)<br>${wf['tier2_alloc']:,.2f} ({debt_status})",
            wf["tier2_alloc"],
            "#B91C1C",
            "rgba(185, 28, 28, 0.34)",
            "Tier 2: High-APR Debt Elimination",
        ),
        (
            f"Tier 3a: Roth IRA<br>${wf['tier3a_roth_alloc']:,.2f} (Monthly Cap)",
            wf["tier3a_roth_alloc"],
            "#7C3AED",
            "rgba(124, 58, 237, 0.36)",
            "Tier 3a: Tax-Advantaged Roth IRA",
        ),
        (
            f"Tier 3b: Index ETFs<br>${wf['tier3b_etf_alloc']:,.2f} (Brokerage)",
            wf["tier3b_etf_alloc"],
            "#15803D",
            "rgba(21, 128, 61, 0.38)",
            "Tier 3b: Low-Cost Index ETFs",
        ),
    ]

    for label, amt, n_col, l_col, desc in waterfall_specs:
        if amt > 0:
            w_idx = add_node(label, n_col)
            add_link(idx_surplus, w_idx, amt, l_col, desc)

    fig = go.Figure(
        data=[
            go.Sankey(
                arrangement="snap",
                node=dict(
                    pad=18,
                    thickness=22,
                    line=dict(color="rgba(15, 23, 42, 0.25)", width=1),
                    label=nodes,
                    color=node_colors,
                    hovertemplate="%{label}<br>Total Flow: $%{value:,.2f}<extra></extra>",
                ),
                link=dict(
                    source=sources,
                    target=targets,
                    value=values,
                    color=link_colors,
                    customdata=link_labels,
                    hovertemplate="%{customdata}<extra></extra>",
                ),
            )
        ]
    )

    subtitle = (
        f"Savings Rate: {metrics['savings_rate']*100:.2f}% | "
        f"Needs: {metrics['needs_pct']*100:.2f}% (≤50%) | "
        f"Wants: {metrics['wants_pct']*100:.2f}% (≤30%) | "
        f"Surplus Deployed: ${wf['allocated_total']:,.2f} | "
        f"Flags: ${anomalies['duplicate_leak_total']:,.2f} Duplicate, "
        f"${anomalies['annualized_sub_drag']:,.2f}/yr Subs"
    )

    fig.update_layout(
        title=dict(
            text=f"<b>Personal Cashflow & 3-Tier Capital Allocation Waterfall (Oct 2026)</b><br>"
            f"<span style='font-size:13px;color:#475569'>{subtitle}</span>",
            x=0.5,
            xanchor="center",
            font=dict(size=20, color="#0F172A", family="Inter, -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif"),
        ),
        font=dict(size=12, color="#0F172A", family="Inter, -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif"),
        paper_bgcolor="#F8FAFC",
        plot_bgcolor="#F8FAFC",
        height=760,
        margin=dict(l=30, r=30, t=100, b=40),
    )

    fig.write_html(str(output_path), include_plotlyjs=True, full_html=True)


def print_report(metrics: dict[str, Any], anomalies: dict[str, Any]) -> None:
    inc = metrics["total_income"]
    needs = metrics["needs_outflows"]
    wants = metrics["wants_outflows"]
    surplus = metrics["net_surplus"]
    wf = metrics["waterfall"]

    print("=" * 78)
    print("1. 50/30/20 CASHFLOW DIAGNOSTIC TABLE")
    print("=" * 78)
    diag = pd.DataFrame(
        [
            {
                "Bucket": "Total Income",
                "Actual ($)": f"${inc:,.2f}",
                "Actual (%)": "100.00%",
                "Target (%)": "100.00%",
                "Target ($)": f"${inc:,.2f}",
                "Variance ($)": "$0.00",
                "Status": "BASELINE",
            },
            {
                "Bucket": "Needs",
                "Actual ($)": f"${needs:,.2f}",
                "Actual (%)": f"{metrics['needs_pct']*100:.2f}%",
                "Target (%)": "<= 50.00%",
                "Target ($)": f"${inc*0.50:,.2f}",
                "Variance ($)": f"${needs - inc*0.50:+,.2f}",
                "Status": "PASS" if metrics["needs_pct"] <= 0.50 else "BREACH",
            },
            {
                "Bucket": "Wants",
                "Actual ($)": f"${wants:,.2f}",
                "Actual (%)": f"{metrics['wants_pct']*100:.2f}%",
                "Target (%)": "<= 30.00%",
                "Target ($)": f"${inc*0.30:,.2f}",
                "Variance ($)": f"${wants - inc*0.30:+,.2f}",
                "Status": "PASS" if metrics["wants_pct"] <= 0.30 else "BREACH",
            },
            {
                "Bucket": "Savings / Debt Paydown",
                "Actual ($)": f"${surplus:,.2f}",
                "Actual (%)": f"{metrics['savings_rate']*100:.2f}%",
                "Target (%)": ">= 20.00%",
                "Target ($)": f"${inc*0.20:,.2f}",
                "Variance ($)": f"${surplus - inc*0.20:+,.2f}",
                "Status": "PASS" if metrics["savings_rate"] >= 0.20 else "BREACH",
            },
        ]
    )
    print(diag.to_string(index=False))
    print(f"\nNet Monthly Surplus: ${surplus:,.2f} | Savings Rate: {metrics['savings_rate']*100:.2f}%")

    print("\n" + "=" * 78)
    print("2. ANOMALY & SUBSCRIPTION LEAK ALERT BOX")
    print("=" * 78)
    print("[A] Exact Duplicate Charges (Same Date, Merchant, Amount):")
    if anomalies["duplicates"].empty:
        print("  None detected.")
    else:
        for _, row in anomalies["duplicates"].iterrows():
            print(
                f"  - {row['Date'].strftime('%Y-%m-%d')} | {row['Merchant']} | "
                f"${row['Charge']:,.2f} (Excess duplicate charge)"
            )
        print(f"  -> Total Recoverable Duplicate Leak: ${anomalies['duplicate_leak_total']:,.2f}")

    print("\n[B] Recurring Subscriptions & Annualized Subscription Drag:")
    if anomalies["subscriptions"].empty:
        print("  None detected.")
    else:
        for _, row in anomalies["subscriptions"].iterrows():
            print(
                f"  - {row['Merchant']}: ${row['Monthly_Cost']:,.2f}/mo -> "
                f"${row['Annualized_Drag']:,.2f}/yr "
                f"(Oct ledger postings: {row['Raw_Ledger_Count']}x = ${row['Raw_Ledger_Spend']:,.2f}; "
                f"Method: {row['Detection_Method']})"
            )
        print(
            f"  -> Deduplicated Monthly Recurring Run-Rate: ${anomalies['monthly_sub_runrate']:,.2f}/mo | "
            f"Annualized Drag: ${anomalies['annualized_sub_drag']:,.2f}/yr"
        )

    print(f"\n[C] Discretionary Outliers (Single-Item Wants > ${OUTLIER_WANTS_THRESHOLD:,.2f}):")
    if anomalies["outliers"].empty:
        print("  None detected.")
    else:
        for _, row in anomalies["outliers"].iterrows():
            print(
                f"  - {row['Date'].strftime('%Y-%m-%d')} | {row['Merchant']} ({row['Category']}) | "
                f"${row['Spend']:,.2f} (+${row['Spend'] - OUTLIER_WANTS_THRESHOLD:,.2f} over threshold)"
            )

    print("\n" + "=" * 78)
    print("3. AUTOMATED SURPLUS DEPLOYMENT TICKET (3-TIER CAPITAL WATERFALL)")
    print("=" * 78)
    ef_months_post = (wf["ef_post"] / needs) if needs > 0 else 0.0
    debt_post_str = (
        f"$0.00 (Debt Free; saves ${wf['annualized_interest_saved']:,.2f}/yr)"
        if wf["debt_post"] == 0
        else f"${wf['debt_post']:,.2f} remaining"
    )
    surplus_denom = surplus if surplus > 0 else 1.0
    ticket = pd.DataFrame(
        [
            {
                "Tier": "Tier 1: Emergency Buffer",
                "Pre-Balance / Target": f"${wf['ef_current']:,.2f} / ${wf['ef_target']:,.2f} (3x Needs)",
                "Allocation ($)": f"${wf['tier1_alloc']:,.2f}",
                "% of Surplus": f"{(wf['tier1_alloc']/surplus_denom*100):.2f}%",
                "Post-Execution State": f"${wf['ef_post']:,.2f} ({ef_months_post:.2f} mos funded)",
            },
            {
                "Tier": f"Tier 2: High-APR Debt ({wf['debt_apr']*100:.1f}%)",
                "Pre-Balance / Target": f"${wf['debt_balance']:,.2f} (>6.5% APR)",
                "Allocation ($)": f"${wf['tier2_alloc']:,.2f}",
                "% of Surplus": f"{(wf['tier2_alloc']/surplus_denom*100):.2f}%",
                "Post-Execution State": debt_post_str,
            },
            {
                "Tier": "Tier 3a: Roth IRA",
                "Pre-Balance / Target": f"YTD ${wf['roth_ytd']:,.2f} / ${wf['roth_limit']:,.2f} (Cap ${wf['roth_monthly_cap']:,.2f}/mo)",
                "Allocation ($)": f"${wf['tier3a_roth_alloc']:,.2f}",
                "% of Surplus": f"{(wf['tier3a_roth_alloc']/surplus_denom*100):.2f}%",
                "Post-Execution State": f"YTD ${wf['roth_ytd_post']:,.2f} (${wf['roth_room_post']:,.2f} room left)",
            },
            {
                "Tier": "Tier 3b: Index ETFs (Brokerage)",
                "Pre-Balance / Target": "Residual Surplus Spillover",
                "Allocation ($)": f"${wf['tier3b_etf_alloc']:,.2f}",
                "% of Surplus": f"{(wf['tier3b_etf_alloc']/surplus_denom*100):.2f}%",
                "Post-Execution State": f"+${wf['tier3b_etf_alloc']:,.2f} deployed to low-cost ETFs",
            },
        ]
    )
    print(ticket.to_string(index=False))
    print(f"\nTotal Deployed: ${wf['allocated_total']:,.2f} / ${surplus:,.2f} (Unallocated: ${surplus - wf['allocated_total']:,.2f})")
    print(
        f"Pro-Forma Surplus if ${anomalies['duplicate_leak_total']:,.2f} duplicate charge refunded: "
        f"${round_cents(surplus + anomalies['duplicate_leak_total']):,.2f} "
        f"(would add +${anomalies['duplicate_leak_total']:,.2f} to Tier 3b Index ETFs -> "
        f"${round_cents(wf['tier3b_etf_alloc'] + anomalies['duplicate_leak_total']):,.2f})"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="PFM Audit & Wealth Waterfall Engine")
    parser.add_argument("--ledger", type=Path, default=Path("data/ledger.csv"))
    parser.add_argument("--profile", type=Path, default=Path("data/profile.json"))
    parser.add_argument("--output", type=Path, default=Path("cashflow_sankey.html"))
    args = parser.parse_args()

    df, profile = load_inputs(args.ledger, args.profile)
    anomalies = detect_anomalies(df)
    metrics = run_cashflow_and_waterfall(df, profile)
    build_sankey(metrics, anomalies, args.output)
    print_report(metrics, anomalies)
    print(f"\nInteractive Sankey chart exported to: {args.output.resolve()}")


if __name__ == "__main__":
    main()

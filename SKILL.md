\---

description: Audits a personal transaction CSV against a financial profile, flags duplicate/outlier spending, executes a 50/30/20 \& wealth-waterfall allocation, and generates an interactive Plotly Sankey chart.

tools:

&#x20; - bash

&#x20; - file\_editor

\---



\## Workflow:

1\. Ingest `$ledger` (default: `data/ledger.csv`) and `$profile` (default: `data/profile.json`).

2\. Write and run `pfm\_engine.py` using `pandas`, `numpy`, and `plotly.graph\_objects`:

&#x20;  - Compute Total Income, Needs Outflows, Wants Outflows, and Net Monthly Surplus.

&#x20;  - Calculate actual vs. target 50/30/20 ratios and Savings Rate (`Surplus / Income`).

&#x20;  - Identify duplicate charges (same Date, Merchant, Amount), recurring subscriptions (annualized cost), and single-item Wants > $250.

&#x20;  - Run the \*\*3-Tier Capital Waterfall\*\* on the Net Monthly Surplus using `data/profile.json`:

&#x20;    - Calculate target 3-month emergency fund (`3 \* Needs`) and allocate surplus to close any shortfall first.

&#x20;    - Allocate next dollars to pay down `high\_apr\_debt\_balance`.

&#x20;    - Allocate remaining surplus toward remaining Roth IRA annual room (`limit - ytd`, capped at `$583.33/mo`), with any excess going to a taxable brokerage ETF index.

&#x20;  - Generate `cashflow\_sankey.html` illustrating Income flowing into Needs, Wants, and the exact Waterfall buckets.

3\. Display:

&#x20;  - \*\*50/30/20 Cashflow Diagnostic Table\*\*

&#x20;  - \*\*Anomaly \& Subscription Leak Alert Box\*\*

&#x20;  - \*\*Automated Surplus Deployment Ticket\*\*




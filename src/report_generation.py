"""Deterministic reports from validated, scoped aggregate snapshots only."""

import json
from dataclasses import asdict, dataclass
from decimal import Decimal

from .conversation import AnalysisSnapshot, METRICS, _cell, render_evidence
from .graph import PolicyDenied


@dataclass(frozen=True)
class GeneratedReport:
    title: str
    body: str
    sources_json: str
    scope_hash: str


def generate_report(snapshots: list[AnalysisSnapshot], scope_hash: str) -> GeneratedReport:
    if not snapshots or len(snapshots) > 8:
        raise ValueError("Run a successful analysis before creating a report")
    sections, sources = [], []
    for index, snapshot in enumerate(snapshots, 1):
        if snapshot.scope_hash != scope_hash:
            raise PolicyDenied("Report evidence belongs to a different access scope")
        request = snapshot.request
        payload = json.loads(snapshot.evidence)
        rows = payload['rows']
        if (payload.get('cohort_suppression_applied') is not True or type(rows) is not list
                or type(payload.get('min_customers')) is not int or payload['min_customers'] < 2
                or len(rows) > 100):
            raise PolicyDenied("Report evidence is not privacy-validated")
        columns = {request.metric} | ({request.group_by} if request.group_by else set())
        values = []
        for row in rows:
            if not isinstance(row, dict) or set(row) != columns:
                raise PolicyDenied("Unexpected report columns")
            if request.group_by and (not isinstance(row[request.group_by], str) or len(row[request.group_by]) > 100):
                raise PolicyDenied("Invalid report dimension")
            value = row[request.metric]
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                raise PolicyDenied("Invalid report aggregate")
            try:
                numeric = Decimal(str(value))
            except Exception:
                raise PolicyDenied("Invalid numeric report value") from None
            if not numeric.is_finite() or (request.metric == 'item_count' and (type(value) is not int or value < 0)):
                raise PolicyDenied("Invalid numeric report value")
            values.append((row, numeric))
        if not request.group_by and len(rows) > 1:
            raise PolicyDenied("Unexpected aggregate cardinality")
        label = METRICS[request.metric][1]
        heading = label + (f" by {request.group_by}" if request.group_by else " overall")
        findings = []
        if not rows:
            findings.append("No publishable aggregate was returned. This does not establish zero activity.")
        elif not values:
            findings.append("The published aggregates are NULL; no numeric ranking is supported.")
        elif not request.group_by:
            findings.append(f"Observed {label.lower()}: {_cell(values[0][0][request.metric])}.")
        else:
            highest = max(number for _, number in values)
            leaders = [row for row, number in values if number == highest]
            names = ", ".join(_cell(row[request.group_by]) for row in leaders[:5])
            if len(leaders) > 5:
                names += f" and {len(leaders) - 5} other groups"
            findings.append(f"Highest visible {label.lower()}: {names} "
                            f"({_cell(leaders[0][request.metric])}; {len(leaders)} group(s) tied).")
            findings.append(f"The evidence includes {len(rows)} publishable groups; it may exclude suppressed groups.")
        if not rows or not values:
            action = "Check the selected period and status definitions. Do not widen access or disable cohort suppression to obtain results."
        elif request.metric == 'item_count':
            if request.group_by == 'product_category':
                action = ("Review stock availability and product mix for the visible high-volume categories before changing inventory; "
                          "validate the pattern in another comparable period.")
            elif request.group_by == 'item_status':
                action = "Confirm status definitions and compare matched-period status volumes before recommending fulfillment changes."
            else:
                action = "Compare this item volume with a matched complete prior period before drawing a performance conclusion."
        elif request.metric == 'item_sale_value':
            action = "Confirm currency, return handling and eligible statuses before treating item sale value as a financial KPI."
        else:
            action = "Review product mix and price distributions before interpreting differences in average prices."
        sections.append(f"## {index}. {heading}\n\n" + render_evidence(request, payload)
                        + "\n\n### Findings\n\n" + "\n".join("- " + finding for finding in findings)
                        + "\n\n### Action item\n\n- " + action)
        sources.append({'request': asdict(request), 'sql': request.sql, 'evidence': payload})
    body = ("# Retail analysis report\n\n"
            "Scope: the report owner's authorized products at creation time. Each section is a separately "
            "validated aggregate analysis. Recommendations are investigation steps, not established causal findings.\n\n"
            + "\n\n".join(sections)
            + "\n\n## Interpretation limits\n\n"
            "Aggregate differences do not establish their causes. Promotions, availability and product mix "
            "are hypotheses requiring further evidence. Monthly results may include partial months; compare "
            "matched complete periods before claiming a trend. No currency or net-revenue definition is assumed.\n")
    sources_json = json.dumps(sources, allow_nan=False, sort_keys=True)
    if len(body.encode()) + len(sources_json.encode()) > 512_000:
        raise ValueError("Report exceeds the storage budget")
    return GeneratedReport(f"Retail analysis — {len(snapshots)} section(s)", body, sources_json, scope_hash)

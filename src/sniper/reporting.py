"""Portable, escaped HTML review report. No external assets or active code."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from html import escape
import json
from pathlib import Path

from .models import json_safe


def _value(value) -> str:
    if isinstance(value, Decimal):
        return f"{value:,.4f}"
    return str(value)


def write_html(report: dict, path: str | Path, *, data_mode: str = "PAPER") -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    stats = report.get("snapshot", report)
    metrics = [("Equity · USD", "equity"), ("Cash · USD", "cash"),
               ("Realized P&L · USD", "realized_pnl"), ("Unrealized P&L · USD", "unrealized_pnl"),
               ("Maximum drawdown · %", "max_drawdown_pct"), ("Open positions", "open_positions"),
               ("Closed positions", "closed_positions"), ("Unresolved positions", "unresolved_positions")]
    cards = "".join(f'<section class="metric"><span>{escape(label)}</span><strong>{escape(_value(stats.get(key, "—")))}</strong></section>' for label, key in metrics)
    positions = stats.get("positions", [])
    rows = "".join("<tr>" + "".join(f"<td>{escape(_value(p.get(k, '—')))}</td>" for k in ("symbol", "asset_id", "qty", "liquidation_value", "mark_status")) + "</tr>" for p in positions)
    if not rows:
        rows = '<tr><td colspan="5">No open positions.</td></tr>'
    details = escape(json.dumps(json_safe(report), indent=2, allow_nan=False))
    notice = "Synthetic fixture. No real trades, tokens, or performance." if data_mode == "SYNTHETIC_DEMO" else "Paper simulation. Modeled fills are not actual executions. Missing quotes remain unresolved."
    if data_mode.startswith('EXPLORATORY_PAPER'):
        notice = 'Live market observations, simulated fills. Wallet skill, controller independence and some safety evidence are unestablished. This experiment does not validate the strict strategy. Real money is disabled.'
    active_rules = ''
    if isinstance(report.get('entry_rules'), dict):
        rules = report['entry_rules']
        active_rules = ('<section><h2>Active paper strategy</h2><p>'
            + escape(f"Liquidity minimum: ${Decimal(rules['minimum_liquidity_usd']):,.0f}. "
                     f"Distinct buying addresses: {rules['distinct_buyers_required']} within {rules['buyer_window_seconds']//60} minutes. "
                     f"Profile: {report.get('profile', 'unspecified')}. Recorded fills: {len(report.get('fills', []))}.")
            + '</p><p>Rug screening reports detected risks and missing evidence; it cannot guarantee safety. '
              'Earlier entries retain their recorded policy in the audit history.</p></section>')
    html = f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>SNIPER · Paper research report</title><style>
:root{{color-scheme:dark;font-family:system-ui,sans-serif;background:#071019;color:#e9f3fa}}*{{box-sizing:border-box}}body{{margin:0 auto;max-width:1240px;padding:42px 24px}}header{{border-bottom:1px solid #253646;padding-bottom:24px}}.eyebrow{{color:#55dce7;font-size:12px;letter-spacing:3px}}h1{{font-size:clamp(32px,5vw,58px);font-weight:650;margin:14px 0}}p{{color:#9baebf;line-height:1.6}}.badge{{display:inline-block;padding:7px 11px;background:#163640;border:1px solid #2d6c78;border-radius:6px;color:#80eff7;font-size:12px;font-weight:700}}.grid{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:28px 0}}.metric{{background:#101e2b;border:1px solid #263a4c;padding:18px;border-radius:10px;min-width:0}}.metric span{{display:block;font-size:12px;color:#9baebf}}.metric strong{{display:block;font-size:24px;margin-top:12px;overflow-wrap:anywhere}}h2{{font-size:19px;margin-top:32px}}.table{{overflow:auto;border:1px solid #263a4c;border-radius:10px}}table{{width:100%;border-collapse:collapse;font-size:13px}}th,td{{padding:14px;text-align:left;border-bottom:1px solid #203243}}th{{color:#9baebf}}td{{max-width:320px;overflow-wrap:anywhere}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px;line-height:1.6;background:#101e2b;padding:20px;border-radius:10px}}summary{{cursor:pointer;color:#55dce7}}footer{{margin-top:32px;font-size:12px;color:#738c9e}}@media(max-width:760px){{.grid{{grid-template-columns:repeat(2,minmax(0,1fr))}}body{{padding:26px 16px}}.metric strong{{font-size:20px}}}}
</style></head><body><header><div class="eyebrow">SNIPER / RESEARCH OPERATIONS</div><h1>Every decision.<br>Accounted for.</h1><span class="badge">{escape(data_mode)}</span><p>{escape(notice)}</p></header>
<main>{active_rules}<div class="grid">{cards}</div><h2>Position ledger</h2><div class="table"><table><thead><tr><th>Symbol</th><th>Asset</th><th>Quantity</th><th>Net liquidation · USD</th><th>Mark status</th></tr></thead><tbody>{rows}</tbody></table></div><h2>Audit detail</h2><details><summary>Open complete machine-readable report</summary><pre>{details}</pre></details></main><footer>Generated {datetime.now(timezone.utc).isoformat()} · Deterministic PAPER execution · No wallet signing capability</footer></body></html>'''
    path.write_text(html, encoding="utf-8")
    return path

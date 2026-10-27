"""Initial article configuration. Changing this file requires the operator's approval."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent
sys.path.insert(0, str(PROJECT / 'src'))
MODEL = 'grok-4.7'
PAPER = True
TICKET_USD = 25
MAX_OPEN = 4
MAX_ENTRIES_PER_DAY = 6
DAILY_LOSS_HALT_USD = 50
COOLDOWN_HOURS = 24
FACTS = {
    'max_risk_score': 40, 'min_lp_locked_pct': 90, 'max_top10_pct': 30,
    'max_dev_pct': 5, 'max_insiders': 0, 'min_liquidity_usd': 30000,
    'max_ticket_share': 0.01, 'min_age_min': 30, 'max_age_hours': 72,
}
MIN_CONFIDENCE = 0.7
EXIT = {'stop_pct': -35, 'ladder': [(100, 0.50), (200, 0.25)], 'trail_pct': 30,
        'time_stop_hours': 6, 'time_stop_min_pct': 20, 'liq_drop_pct': 40}
LOG, POSITIONS, TRADES = 'checks.csv', 'positions.json', 'trades.csv'
TIMEZONE = 'Europe/Berlin'
WATCH_SECONDS, SCAN_SECONDS = 600, 1800
REPORT_TIME = '21:30'
MAX_SCANS = 5
# Operational safeguards; these do not relax any article trading threshold.
QUOTE_MAX_AGE_SECONDS = 30
PAID_AI_DEFAULT = False
AI_DAILY_LIMIT_USD, AI_RESERVATION_USD = '1', '0.50'
PAPER_FILL_MODEL = 'Indicative pool price; article model excludes fees, gas and slippage'


def settings():
    return {k: globals()[k] for k in ('MODEL', 'PAPER', 'TICKET_USD', 'MAX_OPEN',
        'MAX_ENTRIES_PER_DAY', 'DAILY_LOSS_HALT_USD', 'COOLDOWN_HOURS', 'FACTS',
        'MIN_CONFIDENCE', 'EXIT', 'TIMEZONE', 'WATCH_SECONDS', 'SCAN_SECONDS',
        'REPORT_TIME', 'MAX_SCANS', 'QUOTE_MAX_AGE_SECONDS', 'PAPER_FILL_MODEL')}

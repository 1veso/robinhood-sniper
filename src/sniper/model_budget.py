"""Installation spending limits, separate from frozen trading-account policies."""
import json
from decimal import Decimal
from pathlib import Path


def shared_grok_limit(root):
    path = Path(root) / 'runtime/model-budgets.json'
    if not path.exists():
        return Decimal('1')
    value = json.loads(path.read_text(encoding='utf-8-sig'))
    if (not isinstance(value, dict)
            or set(value) != {'version', 'grok_daily_usd', 'approval_reference', 'approved_at'}
            or type(value['version']) is not int or value['version'] != 1 or type(value['approved_at']) is not int
            or not isinstance(value['approval_reference'], str) or not value['approval_reference'].strip()
            or not isinstance(value['grok_daily_usd'], str)):
        raise ValueError('Invalid installation spending authorization')
    try:
        limit = Decimal(value['grok_daily_usd'])
    except Exception:
        raise ValueError('Invalid shared Grok ceiling') from None
    if not limit.is_finite() or limit < 0:
        raise ValueError('Invalid shared Grok ceiling')
    return limit

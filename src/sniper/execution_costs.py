"""Explicit paper execution assumptions; never represented as executable quotes."""
from decimal import Decimal as D

MODEL = 'solana_cost_scenario_v1'
LABEL = 'Cost-aware paper scenario: assumed DEX/platform fees, liquidity impact, adverse slippage and current-SOL-valued network fees; not an executable quote'
DEFAULT_POLICY = {
    'model': MODEL, 'dex_fee_bps': 30, 'platform_fee_bps': 10,
    'slippage_bps': 100, 'base_fee_lamports': 5000,
    'priority_fee_lamports': 100000, 'token_account_rent_lamports': 2039280,
    'failed_attempts_per_fill': 0,
    'stress_extra_slippage_bps': 100, 'stress_extra_network_attempts': 2,
    'native_price_max_age_seconds': 30,
    'assumptions': 'Illustrative conservative parameters, not provider-verified costs. Constant-product impact approximates pool depth; concentrated liquidity can differ. Rent is reserved capital, not a fee or an assumed refund.'}


def dec(value, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, D)):
        raise ValueError('Missing or invalid execution cost number')
    value = D(str(value))
    if not value.is_finite() or value < 0 or (positive and value == 0):
        raise ValueError('Invalid execution cost number')
    return value


def validate_policy(policy):
    if not isinstance(policy, dict) or set(policy) != set(DEFAULT_POLICY) or policy['model'] != MODEL:
        raise ValueError('Unknown cost policy schema')
    for key in set(policy) - {'model', 'assumptions'}:
        if type(policy[key]) is not int or not 0 <= policy[key] <= 10**9:
            raise ValueError('Invalid cost policy integer')
    if sum(policy[k] for k in ('dex_fee_bps', 'platform_fee_bps', 'slippage_bps')) >= 10000:
        raise ValueError('Execution friction consumes the entire order')
    if not 1 <= policy['native_price_max_age_seconds'] <= 60:
        raise ValueError('Native price freshness must be bounded')
    return policy


def simulate(policy, side, quote, amount, now):
    """Buy amount is USD input; sell amount is token quantity. No double fee charge."""
    validate_policy(policy)
    if side not in ('buy', 'sell'):
        raise ValueError('Invalid execution side')
    mid, liquidity, amount = dec(quote['price'], positive=True), dec(quote['liquidity'], positive=True), dec(amount, positive=True)
    native = dec(quote.get('sol_usd'), positive=True)
    observed = quote.get('sol_observed_at')
    if type(observed) is not int or not 0 <= now - observed <= policy['native_price_max_age_seconds']:
        raise ValueError('Fresh SOL price required to value network costs')
    fees = D(policy['dex_fee_bps'] + policy['platform_fee_bps']) / 10000
    slip = D(policy['slippage_bps']) / 10000
    notional = amount if side == 'buy' else amount * mid
    impact_ratio = 2 * notional / liquidity
    if side == 'buy':
        price = mid * (1 + impact_ratio) / ((1 - fees) * (1 - slip))
        quantity = amount / price
        embedded = amount - quantity * mid
    else:
        price = mid * (1 - fees) * (1 - slip) / (1 + impact_ratio)
        quantity = amount
        embedded = notional - quantity * price
    network = D(policy['base_fee_lamports'] + policy['priority_fee_lamports']) * native / 10**9
    failures = network * policy['failed_attempts_per_fill']
    rent = D(policy['token_account_rent_lamports']) * native / 10**9 if side == 'buy' else D(0)
    shock = notional * D(policy['stress_extra_slippage_bps']) / 10000 + network * policy['stress_extra_network_attempts']
    return {'model': MODEL, 'label': LABEL, 'side': side, 'reference_price': str(mid),
        'fill_price': str(price), 'quantity': str(quantity), 'reference_notional_usd': str(notional),
        'embedded_friction_usd': str(embedded), 'network_fee_usd': str(network),
        'failed_transaction_fee_usd': str(failures), 'explicit_fee_usd': str(network + failures),
        'rent_capital_reserved_usd': str(rent), 'stress_extra_cost_usd': str(shock),
        'sol_usd': str(native), 'sol_observed_at': observed, 'observed_at': now,
        'assumed_dex_fee_bps': policy['dex_fee_bps'], 'assumed_platform_fee_bps': policy['platform_fee_bps'],
        'assumed_slippage_bps': policy['slippage_bps'], 'impact_ratio': str(impact_ratio),
        'executable': False, 'costs_verified_on_chain': False}

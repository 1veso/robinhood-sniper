"""Explicit coverage and failures for Robinhood rug-risk screening.

GoPlus token security supports chain 4663. Its separate rugpull_detecting beta
does not document that chain; Solana Rugcheck is used only for Solana research.
This report never certifies that a token cannot rug.
"""
from .adapters.goplus import flag
from .models import SafetyEvidence


EXTRA_SIGNALS = ('is_airdrop_scam', 'gas_abuse', 'anti_whale_modifiable', 'is_anti_whale')


def enhance_rug_screen(screen):
    raw = screen.details.get('raw_report')
    available = isinstance(raw, dict) and bool(raw) and screen.source.startswith('goplus')
    checks = {**screen.checks, 'rug_screen_available': available}
    if available:
        for name in EXTRA_SIGNALS:
            checks['rug:'+name] = flag(raw, name)
        fake = raw.get('fake_token')
        value = fake.get('value') if isinstance(fake, dict) else None
        checks['rug:fake_token'] = value == 0 if type(value) is int and value in (0,1) else flag(fake, 'value') if isinstance(fake, dict) else None
    return SafetyEvidence(screen.asset_id, screen.observed_at, checks, screen.source,
        {**screen.details, 'rug_screen': {
            'status': 'REJECT' if any(v is False for v in checks.values()) else 'INCOMPLETE' if any(v is None for v in checks.values()) else 'NO_DETECTED_FLAGS',
            'failed_checks': [k for k,v in checks.items() if v is False],
            'unknown_checks': [k for k,v in checks.items() if v is None],
            'provider': 'GoPlus Token Security / Robinhood 4663',
            'limitations': 'Provider screening plus separately recorded chain and route checks. Unknown liquidity withdrawal control and deployer history are not a safety pass. No guarantee against a rug pull.'}})

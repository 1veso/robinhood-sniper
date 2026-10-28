"""Read-only readiness inventory. Evidence never grants execution authority."""
import hashlib
import json
from pathlib import Path
import time

from sniper.adapters.solana import validate_address


def read(path):
    try:
        if path.stat().st_size > 4000000:
            return {}
        value = json.loads(path.read_text(encoding='utf-8-sig'), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def inventory(root, chain, now=None):
    root, now = Path(root), int(time.time() if now is None else now)
    if chain not in ('solana', 'evm'):
        raise ValueError('Unsupported readiness chain')
    checks = []
    def add(name, state, detail, kind='evidence'):
        checks.append({'name': name, 'state': state, 'detail': detail, 'kind': kind})
    name = 'solana-trading-address.txt' if chain == 'solana' else 'evm-trading-address.txt'
    wallet_hash = None
    try:
        address = validate_address((root/'runtime'/name).read_text(encoding='utf-8-sig').strip(), 'solana' if chain=='solana' else 'evm')
        if chain=='evm' and int(address,16)==0:
            raise ValueError()
        wallet_hash = hashlib.sha256(address.encode()).hexdigest()
    except (OSError, ValueError):
        pass
    add('Public wallet', 'VERIFIED' if wallet_hash else 'MISSING', 'Saved public address is valid; this is not signer ownership proof.')
    evidence = read(root/'runtime/live-preparation'/(chain+'-preflight.json'))
    observed = evidence.get('observed_at')
    current = type(observed) is int and 0 <= now-observed <= 3600
    bound = bool(wallet_hash) and evidence.get('wallet_sha256') == wallet_hash
    if chain=='solana':
        keyfile = root/'runtime/jupiter-server-api-key.txt'
        if not keyfile.exists(): keyfile=root/'runtime/jupiter-api-key.txt'
        try:
            bound = bound and evidence.get('jupiter_key_source')==keyfile.name and keyfile.is_file() and keyfile.stat().st_size>0 and keyfile.stat().st_mtime<=observed
        except (OSError, TypeError): bound=False
    else:
        try:
            bound = bound and evidence.get('manifest_sha256') == hashlib.sha256((root/'config/executor-policy.example.json').read_bytes()).hexdigest()
        except OSError: bound=False
    endpoint = root/'runtime'/('solana-rpc-url.txt' if chain=='solana' else 'quote-rpc-url.txt')
    try:
        if endpoint.exists(): bound = bound and type(observed)is int and endpoint.stat().st_mtime<=observed
    except OSError: bound=False
    usable = current and bound
    add('Preparation evidence', 'CURRENT' if usable else 'STALE_OR_MISSING',
        'Saved evidence is wallet-bound and at most one hour old; no fresh network request is made by this view.' if usable else 'Rerun the free preparation check; changed wallet/key or stale/missing evidence is not a pass.')
    if chain=='solana':
        probes = evidence.get('probes') if usable else []
        probes = probes if isinstance(probes,list) else []
        built = len(probes)==2 and all(isinstance(p,dict) and p.get('transaction_sha256') and p.get('genesis_verified') is True for p in probes)
        add('Unsigned buy/sell builds', 'VERIFIED' if built else 'UNVERIFIED', 'USDC/WSOL setup pair only; not screened strategy-token route approval.')
        simulated = built and all((p.get('simulation') or {}).get('succeeded') is True for p in probes)
        add('Wallet-specific simulation', 'VERIFIED_SETUP_ONLY' if simulated else 'BLOCKED',
            'Simulation does not approve instructions or create a fill.' if simulated else 'The saved setup simulations did not succeed; an unfunded/missing wallet account cannot execute on mainnet.')
        from rug_desk.model_gate import status as model_status
        gate=model_status(root/'rug_desk',now)
        add('Approved Grok/X gate', 'VERIFIED_LOCAL_PROOFS' if gate['enabled'] else 'DISABLED',
            '; '.join(gate['blockers']) if gate['blockers'] else 'Account/key/config-bound funded test evidence is current. Per-call validation and budget still apply.', 'authorization')
        add('Solana automatic executor', 'IMPLEMENTED_UNREVIEWED',
            'Direct classic-SPL Raydium CP instruction construction, restricted signing, durable recovery and strategy integration are implemented. Other routes fail closed. Independent review, supported wallet simulations and initial deployment remain required.', 'implementation')
        cp=read(root/'runtime/live-preparation/solana-cp-execution-probe.json')
        stamp=cp.get('observed_at');current_cp=type(stamp)is int and 0<=now-stamp<=3600 and cp.get('wallet_sha256')==wallet_hash
        add('Restricted CP route probe','OBSERVED_SETUP_ONLY' if current_cp and cp.get('independent_pool_quote_validated') is True else 'UNVERIFIED',
            'A successful public setup quote does not approve a strategy token or funded execution. Missing routes remain blocked.')
        add('Solana strategy trial', 'REQUIRED', 'Synthetic setup tests and public quotes do not count as the separate seven-day cost-aware strategy trial.', 'strategy')
    else:
        contracts = evidence.get('contracts') if usable else {}
        contracts = contracts if isinstance(contracts,dict) else {}
        proxies=evidence.get('proxies') or {}
        base_proxy=proxies.get('USDG') if isinstance(proxies,dict) else {}
        matches = all(isinstance(contracts.get(k),dict) and contracts[k].get('matches_manifest') is True for k in ('router','factory','quoter','base_token'))
        matches = matches and isinstance(base_proxy,dict) and base_proxy.get('matches_selected_manifest') is True and base_proxy.get('beacon_zero') is True and evidence.get('canonical_verified') is True and evidence.get('genesis_verified') is True and evidence.get('chain_id')==4663
        add('EVM deployment pins', 'VERIFIED' if usable and matches else 'UNVERIFIED', 'Public bytecode and selected proxy identity checks are observations, not an independent audit.')
        lag = evidence.get('finality_lag_seconds') if usable else None
        add('EVM finality and exits', 'REVIEW_REQUIRED',
            (f'Observed finalized lag: {lag} seconds. ' if type(lag) is int else 'Current finalized-lag observation unavailable. ')+
            'Reviewed confirmed exit management can act on provisional holdings while accounting waits for finality. Reorgs halt execution; pending nonce and capacity can still delay exits. This mode is not enabled.', 'design')
        add('EVM executor', 'SHADOW_ONLY', 'Deterministic executor exists with offline recovery tests. The active worker has no live authority.', 'implementation')
        add('Wallet-specific swap simulation', 'UNVERIFIED', 'Public quotes and empty-wallet checks do not prove a funded swap can execute.')
        add('Strategy evidence review', 'REQUIRED', 'Review the unchanged trial deadline, net costs, unresolved holdings, policy segments and monitoring gaps. Profitability cannot authorize deployment.', 'strategy')
    add('Initial live policy and signer', 'NOT_AUTHORIZED', 'No live deployment is authorized. Capital, settlement asset, risk limits, restricted signer provisioning and independent review must be completed before a user-controlled initial deployment.', 'authorization')
    return {'chain':chain,'checked_at':now,'preparation_observed_at':observed if type(observed)is int else None,
        'live_execution_ready':False,'automatic_promotion':False,'per_trade_approval_required':False,
        'checks':checks,'scope':'Read-only preparation inventory, not a trading control or funding request'}

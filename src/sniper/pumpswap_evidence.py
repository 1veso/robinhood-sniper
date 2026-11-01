"""Read-only PumpSwap pool validation and integer quotes, never instructions.

Layouts: pump-fun/pump-public-docs IDL, retrieved 2026-11-01.
Math: @pump-fun/pump-swap-sdk 2.1.0 buyQuoteInput / sellBaseInput.
Only direct WSOL/USDC quotes and non-economic Token-2022 extensions qualify.
"""
import hashlib
import struct
from decimal import Decimal
from . import solana_cpmm as cp
from .holder_evidence import TOKEN_2022
from .adapters.solana_execution import USDC
from .solana_wire import b58decode

PROGRAM = 'pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA'
FEE_PROGRAM = 'pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ'
PUMP = '6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P'
MODEL = 'pumpswap-sdk-2.1.0'
require = cp.require


def discriminator(name):
    return hashlib.sha256(('account:'+name).encode()).digest()[:8]


def global_key(): return cp.pda([b'global_config'], PROGRAM)[0]
def fee_key(): return cp.pda([b'fee_config', b58decode(PROGRAM)], FEE_PROGRAM)[0]


def pool_state(account, now):
    raw = cp.blob(account, PROGRAM)
    # Historical layouts append complete fields; partial/nonzero unknown tails fail.
    require(len(raw) >= 243 and raw[:8] == discriminator('Pool'), 'Unsupported PumpSwap pool')
    fields = [(243,1),(244,1),(245,16),(261,8),(269,1),(270,1),(271,8),(279,8)]
    for start,size in fields:
        require(not start < len(raw) < start+size, 'Truncated pool extension')
    require(len(raw) <= 287 or not any(raw[287:]), 'Unknown PumpSwap pool extension')
    padded = raw[:287].ljust(287, b'\0')
    for offset in (243,244,269,270): require(padded[offset] in (0,1), 'Invalid pool flag')
    require(not padded[243] and not padded[244], 'Mayhem/cashback pools need separate review')
    state = dict(bump=raw[8], index=int.from_bytes(raw[9:11],'little'))
    for name,offset in (('creator',11),('mint0',43),('mint1',75),('lp_mint',107),('vault0',139),('vault1',171),('coin_creator',211)):
        state[name] = cp.key(raw,offset)
    require(state['mint0'] != state['mint1'] and state['vault0'] != state['vault1'], 'Aliased pool')
    require(state['mint1'] in (cp.WSOL, USDC), 'Unsupported quote asset')
    state.update(virtual_quote=int.from_bytes(padded[245:261],'little',signed=True),
        configured_creator_fee=cp.u64(padded,261),fee_buckets=cp.u64(padded,271)+cp.u64(padded,279),
        holder_rewards=bool(padded[270]),config=global_key(),fee_config=fee_key(),quote_model=MODEL)
    return state


def token_blob(account, kind):
    program = account.get('owner') if isinstance(account,dict) else None
    require(program in (cp.TOKEN,TOKEN_2022), 'Unsupported token program')
    raw=cp.blob(account,program);size=82 if kind=='mint' else 165
    require(len(raw)>=size, 'Truncated token account')
    if program==cp.TOKEN: require(len(raw)==size,'Classic SPL extension')
    elif len(raw)!=size:
        require(len(raw)>=166 and raw[165]==(1 if kind=='mint' else 2), 'Token-2022 account type differs')
        if kind=='mint':require(not any(raw[82:165]),'Invalid mint padding')
        pos=166;seen=set()
        # Metadata / metadata pointer and immutable owner do not change transfers.
        allowed={18,19} if kind=='mint' else {7}
        while pos<len(raw) and any(raw[pos:]):
            require(pos+4<=len(raw),'Truncated TLV')
            tag,length=struct.unpack_from('<HH',raw,pos);pos+=4
            require(tag in allowed and tag not in seen and pos+length<=len(raw),'Unsupported economic token extension')
            require(tag!=7 or length==0,'Invalid immutable owner')
            require(tag!=18 or length==64,'Invalid metadata pointer')
            seen.add(tag);pos+=length
    return raw,program


def mint_info(account):
    raw,program=token_blob(account,'mint')
    require(raw[45]==1 and raw[44]<=18,'Invalid mint')
    require(int.from_bytes(raw[:4],'little') in (0,1) and int.from_bytes(raw[46:50],'little') in (0,1),'Invalid authority option')
    return raw[44],cp.u64(raw,36),program


def vault_balance(account,mint,pool,program):
    raw,actual=token_blob(account,'account')
    require(actual==program and cp.key(raw,0)==mint and cp.key(raw,32)==pool and raw[108]==1,'Wrong/frozen vault')
    require(raw[72:76]==bytes(4) and raw[129:133]==bytes(4) and cp.u64(raw,121)==0,'Delegated vault')
    native=int.from_bytes(raw[109:113],'little')
    require(native==(1 if mint==cp.WSOL else 0),'Wrong native reserve flag')
    if native:require(account['lamports']==cp.u64(raw,113)+cp.u64(raw,64),'Unsynchronized WSOL vault')
    return cp.u64(raw,64)


def fee_schedule(account,stable,canonical,cap):
    raw=cp.blob(account,FEE_PROGRAM)
    require(len(raw)>=69 and raw[:8]==discriminator('FeeConfig'),'Invalid fee config')
    require(raw[8]==cp.pda([b'fee_config',b58decode(PROGRAM)],FEE_PROGRAM)[1],'Fee config PDA bump')
    def fees(offset):
        require(offset+24<=len(raw),'Truncated fees')
        values=[cp.u64(raw,offset+i*8) for i in range(3)]
        require(sum(values)<10000,'Invalid fees')
        return values
    flat=fees(41);pos=65
    def tiers():
        nonlocal pos
        require(pos+4<=len(raw),'Missing fee tiers')
        n=int.from_bytes(raw[pos:pos+4],'little');pos+=4
        require(n<=128 and pos+n*40<=len(raw),'Unbounded/truncated fee tiers')
        rows=[]
        for _ in range(n):
            threshold=int.from_bytes(raw[pos:pos+16],'little');rate=fees(pos+16);pos+=40
            require(not rows or threshold>rows[-1][0],'Unsorted fee schedule')
            rows.append((threshold,rate))
        return rows
    sol=tiers();usd=tiers() if pos+4<=len(raw) else []
    if pos+24<=len(raw):fees(pos);pos+=24
    require(not any(raw[pos:]),'Unknown fee config extension')
    if not canonical:return flat
    selected=usd if stable and usd else sol
    require(bool(selected),'Empty canonical fee schedule')
    return next((rate for threshold,rate in reversed(selected) if cap>=threshold),selected[0][1])


def inspect_pool(pool, source, target, wallet, accounts, now):
    state=pool_state(accounts[pool],now)
    require(source==state['mint1'] and target==state['mint0'],'PumpSwap pinned assets differ')
    address,bump=cp.pda([b'pool',state['index'].to_bytes(2,'little'),b58decode(state['creator']),b58decode(target),b58decode(source)],PROGRAM)
    require(pool==address and bump==state['bump'],'Pool PDA mismatch')
    require(state['lp_mint']==cp.pda([b'pool_lp_mint',b58decode(pool)],PROGRAM)[0],'LP mint PDA mismatch')
    for n in (0,1):
        mint=state[f'mint{n}'];decimals,supply,program=mint_info(accounts[mint])
        require(state[f'vault{n}']==cp.pda([b58decode(pool),b58decode(program),b58decode(mint)],cp.ATA)[0],'Vault ATA mismatch')
        state[f'decimals{n}']=decimals;state[f'program{n}']=program
        state[f'reserve{n}']=vault_balance(accounts[state[f'vault{n}']],mint,pool,program)
        if n==0:state['supply']=supply
    require(state['decimals1']==(9 if source==cp.WSOL else 6),'Quote precision mismatch')
    state['real_quote_reserve']=state['reserve1']-state['fee_buckets']
    state['reserve1']+=state['virtual_quote']
    require(0<state['reserve0']<2**64 and 0<state['reserve1']<2**64 and state['real_quote_reserve']>0,'Invalid effective/real reserves')
    glob=cp.blob(accounts[state['config']],PROGRAM)
    require(len(glob)>=321 and glob[:8]==discriminator('GlobalConfig') and not glob[56]&24 and not glob[56]&~31,'PumpSwap swaps disabled/unknown flags')
    boundaries=(321,353,385,417,418,642,643,899,907,939,940,941,949)
    require(len(glob) in boundaries or (len(glob)>949 and not any(glob[949:])), 'Unknown global configuration layout')
    for offset in (417,642,939,940):
        if offset<len(glob):require(glob[offset] in (0,1),'Invalid global configuration flag')
    # The configurable creator fee gate is at offset 940 in the current IDL.
    # A nonzero per-pool override requires the full known configuration layout.
    if state['configured_creator_fee']:
        require(len(glob)>=949 and glob[939] in (0,1) and glob[940] in (0,1),'Missing creator fee configuration')
    canonical=state['creator']==cp.pda([b'pool-authority',b58decode(target)],PUMP)[0]
    cap=state['reserve1']*state['supply']//state['reserve0']
    state['fees_bps']=fee_schedule(accounts[state['fee_config']],source==USDC,canonical,cap)
    if state['configured_creator_fee'] and glob[940]:state['fees_bps'][2]=state['configured_creator_fee']
    if state['coin_creator']==cp.SYSTEM:state['fees_bps'][2]=0
    require(sum(state['fees_bps'])<10000,'Invalid effective fees')
    return state


def quote_output(state,source,amount):
    require(type(amount)is int and 0<amount<2**64,'Invalid quote input')
    a,b=state['reserve0'],state['reserve1'];rates=state['fees_bps']
    require(all(type(x)is int and 0<=x<10000 for x in rates) and len(rates)==3 and sum(rates)<10000,'Invalid quote fees')
    fee=lambda x,r:(x*r+9999)//10000
    if source==state['mint1']:
        net=amount*10000//(10000+sum(rates))
        net-=max(0,net+sum(fee(net,r) for r in rates)-amount)
        net-=1  # SDK buyQuoteInput conservative rounding.
        require(net>0,'Quote cannot cover fees')
        output=a*net//(b+net);rin,rout=b,a
    else:
        require(source==state['mint0'],'Unknown quote input mint')
        gross=b*amount//(a+amount);fees=[fee(gross,r) for r in rates]
        require(gross-fees[0]<=state['real_quote_reserve'],'Insufficient real quote liquidity')
        output=gross-sum(fees);rin,rout=a,b
    require(output>0 and Decimal(output)*rin/(Decimal(amount)*rout)>=Decimal('.97'),'Local price impact exceeds bound')
    return output

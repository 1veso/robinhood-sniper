"""Independent classic-SPL Raydium CP-Swap validator and instruction builder.

Jupiter is a quote source, never instruction authority. Only one direct pool is
supported. Layout reference: raydium-io/raydium-cp-swap f4b9b26342afe9122d5ff67cd7a4f19ded4ac0a5.
Program bytes/upgrade metadata must additionally match a reviewed deployment pin.
"""
import base64
import hashlib
import struct
from decimal import Decimal

from .adapters.solana import validate_address
from .adapters.solana_execution import integer
from .solana_preflight import TOKEN, ATA, SYSTEM, COMPUTE
from .solana_wire import b58decode, b58encode, compile_unsigned, decode_unsigned

PROGRAM = 'CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C'
UPGRADEABLE = 'BPFLoaderUpgradeab1e11111111111111111111111'
IMMUTABLE = 'BPFLoader2111111111111111111111111111111111'
SWAP = hashlib.sha256(b'global:swap_base_input').digest()[:8]
WSOL = 'So11111111111111111111111111111111111111112'


def require(ok, reason):
    if not ok:
        raise ValueError(reason)


def pda(seeds, program):
    # Optional signing/validation SDK; never imported by paper workers.
    from solders.pubkey import Pubkey
    key, bump = Pubkey.find_program_address(seeds, Pubkey.from_string(program))
    return str(key), bump


def ata(wallet, mint):
    return pda([b58decode(wallet), b58decode(TOKEN), b58decode(mint)], ATA)[0]


def blob(account, owner=None, size=None, executable=False):
    require(isinstance(account, dict) and account.get('executable') is executable, 'Account missing or wrong executable role')
    if owner is not None:
        require(account.get('owner') == owner, 'Account owner mismatch')
    data = account.get('data')
    require(isinstance(data, list) and len(data) == 2 and data[1] == 'base64', 'Raw account data required')
    raw = base64.b64decode(data[0], validate=True)
    require(size is None or len(raw) == size, 'Unsupported account layout')
    return raw


def u64(raw, offset):
    return struct.unpack_from('<Q', raw, offset)[0]


def key(raw, offset):
    return b58encode(raw[offset:offset+32])


def pool_state(account, now):
    raw = blob(account, PROGRAM, 637)
    require(raw[:8] == hashlib.sha256(b'account:PoolState').digest()[:8], 'Pool discriminator mismatch')
    names = ('config', 'creator', 'vault0', 'vault1', 'lp_mint', 'mint0', 'mint1', 'program0', 'program1', 'observation')
    out = {n: key(raw, 8+32*i) for i,n in enumerate(names)}
    out.update(bump=raw[328], status=raw[329], decimals0=raw[331], decimals1=raw[332],
               fees0=u64(raw,341)+u64(raw,357)+u64(raw,397),
               fees1=u64(raw,349)+u64(raw,365)+u64(raw,405),
               creator_fee_on=raw[389], creator_fee_enabled=raw[390])
    require(not out['status'] & 4 and u64(raw,373) <= now, 'Pool swaps unavailable')
    require(out['program0'] == out['program1'] == TOKEN, 'Token-2022/extensions are unsupported')
    require(out['mint0'] != out['mint1'] and out['vault0'] != out['vault1'], 'Aliased pool accounts')
    require(out['creator_fee_on'] in (0,1,2) and out['creator_fee_enabled'] in (0,1), 'Unknown creator fee model')
    require(raw[391:397] == bytes(6) and raw[413:] == bytes(224), 'Unsupported pool extension')
    return out


def token_account(account, mint, owner):
    raw = blob(account, TOKEN, 165)
    require(key(raw,0)==mint and key(raw,32)==owner and raw[108]==1, 'Wrong or frozen token account')
    require(raw[72:76]==bytes(4) and raw[129:133]==bytes(4) and u64(raw,121)==0, 'Delegated/closable token account unsupported')
    native = int.from_bytes(raw[109:113], 'little')
    require(native == (1 if mint==WSOL else 0), 'Invalid native reserve option')
    if native:require(account.get('lamports')==u64(raw,113)+u64(raw,64),'Wrapped native account is not synchronized')
    return u64(raw,64)


def mint_account(account, decimals):
    raw = blob(account, TOKEN, 82)
    require(raw[44]==decimals and raw[45]==1 and decimals<=18, 'Mint precision/initialization mismatch')
    # Mint/freeze risk is checked by the existing strategy. Structure is strict.
    require(int.from_bytes(raw[:4],'little') in (0,1) and int.from_bytes(raw[46:50],'little') in (0,1), 'Invalid mint authority option')


def deployment_observation(program, accounts):
    raw = blob(accounts[program], executable=True)
    owner = accounts[program]['owner']
    if owner == UPGRADEABLE:
        require(len(raw)==36 and raw[:4]==b'\2\0\0\0', 'Unsupported upgradeable program')
        data_key = key(raw,4)
        data = blob(accounts[data_key], UPGRADEABLE)
        require(len(data)>45 and data[:4]==b'\3\0\0\0' and data[12] in (0,1), 'Invalid programdata')
        return {'owner':owner,'programdata':data_key,'deployment_slot':u64(data,4),
                'upgrade_authority':key(data,13) if data[12] else None,
                'code_sha256':hashlib.sha256(data[45:]).hexdigest()}
    require(owner==IMMUTABLE and len(raw)>0, 'Unsupported program loader')
    return {'owner':owner,'programdata':None,'deployment_slot':None,'upgrade_authority':None,
            'code_sha256':hashlib.sha256(raw).hexdigest()}


def inspect_pool(pool, source, target, wallet, accounts, now):
    state = pool_state(accounts[pool], now)
    require({source,target} == {state['mint0'],state['mint1']}, 'Pinned pool assets differ from request')
    authority, bump = pda([b'vault_and_lp_mint_auth_seed'], PROGRAM)
    require(state['bump']==bump, 'Pool authority bump mismatch')
    require(pool==pda([b'pool',b58decode(state['config']),b58decode(state['mint0']),b58decode(state['mint1'])],PROGRAM)[0], 'Pool PDA mismatch')
    for n in (0,1):
        mint,vault=state[f'mint{n}'],state[f'vault{n}']
        require(vault==pda([b'pool_vault',b58decode(pool),b58decode(mint)],PROGRAM)[0], 'Vault PDA mismatch')
        mint_account(accounts[mint],state[f'decimals{n}'])
        state[f'reserve{n}']=token_account(accounts[vault],mint,authority)-state[f'fees{n}']
        require(state[f'reserve{n}']>0, 'Pool has no usable reserves')
    require(state['observation']==pda([b'observation',b58decode(pool)],PROGRAM)[0], 'Observation PDA mismatch')
    observation=blob(accounts[state['observation']],PROGRAM,4075)
    require(observation[:8]==hashlib.sha256(b'account:ObservationState').digest()[:8]
            and observation[8] in (0,1) and int.from_bytes(observation[9:11],'little')<100
            and key(observation,11)==pool and observation[-24:]==bytes(24), 'Unsupported observation layout or pool')
    config=blob(accounts[state['config']],PROGRAM,236)
    require(config[:8]==hashlib.sha256(b'account:AmmConfig').digest()[:8], 'Config discriminator mismatch')
    require(state['config']==pda([b'amm_config',config[10:12][::-1]],PROGRAM)[0], 'AMM config PDA mismatch')
    trade,protocol,fund,creator=(u64(config,o) for o in (12,20,28,108))
    require(trade+creator<1000000 and protocol+fund<=1000000, 'Invalid fee configuration')
    require(config[124:]==bytes(112), 'Unknown fee configuration extension')
    state.update(authority=authority,trade_fee_rate=trade,creator_fee_rate=creator if state['creator_fee_enabled'] else 0)
    state['input_account'],state['output_account']=ata(wallet,source),ata(wallet,target)
    for mint in (source,target):
        value=accounts.get(ata(wallet,mint))
        if value is not None:token_account(value,mint,wallet)
    return state


def quote_output(state, source, amount):
    amount=integer(amount,positive=True)
    direction=0 if source==state['mint0'] else 1
    require(source==state[f'mint{direction}'], 'Unknown input asset')
    a,b=state[f'reserve{direction}'],state[f'reserve{1-direction}']
    creator_on_input=state['creator_fee_on'] in (0,direction+1)
    creator,trade=state['creator_fee_rate'],state['trade_fee_rate']
    ceil=lambda n,r:(n*r+999999)//1000000
    net=amount-ceil(amount,trade+(creator if creator_on_input else 0))
    out=net*b//(a+net)
    if not creator_on_input:out-=ceil(out,creator)
    require(out>0 and Decimal(out)/Decimal(amount)*Decimal(a)/Decimal(b)>=Decimal('.97'), 'Local price impact exceeds bound')
    return out


def ix(program, accounts, data):
    return {'programId':program,'accounts':[{'pubkey':k,'isSigner':s,'isWritable':w} for k,s,w in accounts],
            'data':base64.b64encode(data).decode()}


def build_message(state, pool, wallet, source, target, amount, minimum, blockhash,
                  *, create_output=False, compute_units=300000, micro_lamports=0):
    require(type(compute_units)is int and 1<=compute_units<=1400000, 'Compute unit limit invalid')
    require(type(micro_lamports)is int and 0<=micro_lamports<=1000000, 'Compute price invalid')
    amount,minimum=integer(amount,positive=True),integer(minimum,positive=True)
    ins=[ix(COMPUTE,[],b'\2'+struct.pack('<I',compute_units)),ix(COMPUTE,[],b'\3'+struct.pack('<Q',micro_lamports))]
    if create_output:
        ins.append(ix(ATA,[(wallet,True,True),(ata(wallet,target),False,True),(wallet,False,False),
                         (target,False,False),(SYSTEM,False,False),(TOKEN,False,False)],b'\1'))
    d=0 if source==state['mint0'] else 1
    ins.append(ix(PROGRAM,[(wallet,True,False),(state['authority'],False,False),(state['config'],False,False),
        (pool,False,True),(ata(wallet,source),False,True),(ata(wallet,target),False,True),
        (state[f'vault{d}'],False,True),(state[f'vault{1-d}'],False,True),(TOKEN,False,False),(TOKEN,False,False),
        (source,False,False),(target,False,False),(state['observation'],False,True)],SWAP+struct.pack('<QQ',amount,minimum)))
    return compile_unsigned(ins,wallet,blockhash,{})


def validate_message(encoded, expected):
    """Exact independent reconstruction rejects added operations or privilege changes."""
    actual=decode_unsigned(encoded,{})
    rebuilt,_=build_message(**expected)
    require(encoded==rebuilt, 'Transaction differs from independently reconstructed swap')
    require(not actual['lookup_tables'], 'Direct executor does not permit address lookup indirection')
    return actual

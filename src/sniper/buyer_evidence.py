"""Bounded finalized buyer/control observations, never beneficial-owner inference.

A signed direct PumpSwap buy and reconciled token delta establishes use of a
wallet authority in that receipt. It cannot establish independent humans.
"""
import hashlib
from .adapters.solana import validate_address
from .adapters.solana_execution import MAINNET_GENESIS
from .adapters.solana import BASE58
from . import pumpswap_evidence as pump
from . import solana_cpmm as cp

def decode58(value):
    if not isinstance(value,str) or not 1<=len(value)<=2048 or any(c not in BASE58 for c in value):
        raise ValueError('Invalid bounded base58 bytes')
    n=0
    for c in value:n=n*58+BASE58.index(c)
    return b'\0'*(len(value)-len(value.lstrip('1')))+n.to_bytes((n.bit_length()+7)//8,'big')


BUY = {hashlib.sha256(('global:'+n).encode()).digest()[:8]:n for n in
       ('buy','buy_exact_quote_in','buy_v2','buy_exact_quote_in_v2')}
MAX_RECEIPTS = 4


def receipt(tx, signature, mint, pool, start, end):
    from .crew_evidence import digest
    if not isinstance(tx,dict) or not isinstance(tx.get('meta'),dict) or tx['meta'].get('err') is not None:
        raise ValueError('Successful finalized receipt required')
    stamp,slot=tx.get('blockTime'),tx.get('slot')
    if type(stamp)is not int or not start<stamp<=end or type(slot)is not int or slot<0:
        raise ValueError('Receipt outside collection window')
    transaction=tx['transaction'];message=transaction['message'];meta=tx['meta']
    if not transaction.get('signatures') or transaction['signatures'][0]!=signature:
        raise ValueError('Signature binding differs')
    keys=message['accountKeys']
    if not isinstance(keys,list) or not keys or not all(isinstance(k,dict) for k in keys):
        raise ValueError('Parsed account roles required')
    addresses=[validate_address(k['pubkey']) for k in keys]
    signers={k['pubkey'] for k in keys if k.get('signer') is True}
    if len(set(addresses))!=len(addresses):raise ValueError('Duplicate transaction account')
    instructions=list(message.get('instructions',[]))
    for group in meta.get('innerInstructions') or []:instructions.extend(group.get('instructions',[]))
    buys=[]
    for ix in instructions:
        if ix.get('programId') not in (pump.PROGRAM,cp.PROGRAM) or 'data' not in ix:continue
        data=decode58(ix['data']);accounts=ix.get('accounts',[])
        if ix['programId']==pump.PROGRAM:
            if data[:8] not in BUY or len(accounts)<9:continue
            name=BUY[data[:8]];target_pool,target_mint,user,token=accounts[0],accounts[3],accounts[1],accounts[5]
        else:
            if data[:8]!=cp.SWAP or len(accounts)<13:continue
            name='raydium_swap_base_input';target_pool,target_mint,user,token=accounts[3],accounts[11],accounts[0],accounts[5]
        if len(data)<24:raise ValueError('Truncated swap instruction')
        if target_pool!=pool or target_mint!=mint:continue
        user=validate_address(user);token=validate_address(token)
        if user not in signers or token not in addresses:raise ValueError('Buyer signature/account unavailable')
        index=addresses.index(token)
        def balance(rows,missing=False):
            found=[r for r in rows if r.get('accountIndex')==index]
            if not found and missing:return 0
            if len(found)!=1 or found[0].get('mint')!=mint or found[0].get('owner')!=user:
                raise ValueError('Token balance ownership not reconciled')
            r=found[0];amount=r['uiTokenAmount']['amount']
            if not isinstance(amount,str) or not amount.isascii() or not amount.isdecimal() or not 0<=int(amount)<2**64:
                raise ValueError('Invalid raw receipt balance')
            return int(amount)
        before=balance(meta.get('preTokenBalances') or [],True)
        after=balance(meta.get('postTokenBalances') or [])
        if after<=before:raise ValueError('No positive net token acquisition')
        buys.append({'wallet':user,'token_account':token,'net_acquired_raw':str(after-before),
            'instruction':name,'signature':signature,'slot':slot,'block_time':stamp,
            'control_evidence':'signed_swap_authority_and_matching_token_account_owner'})
    # One net delta must not be counted twice for aggregated/multi-buy receipts.
    if len(buys)>1:raise ValueError('Multi-buy transaction needs instruction-level attribution')
    transfers=[]
    for ix in instructions:
        if ix.get('programId')!=cp.SYSTEM:continue
        parsed=ix.get('parsed',{});info=parsed.get('info',{})
        if parsed.get('type')=='transfer' and type(info.get('lamports'))is int and info['lamports']>0:
            transfers.append({'source':validate_address(info['source']),'destination':validate_address(info['destination']),
                'lamports':str(info['lamports']),'meaning':'observed transfer; funding correlation is not controller identity'})
    return {'signature':signature,'slot':slot,'block_time':stamp,'receipt_sha256':digest(tx),
        'buyers':buys,'fee_payer':addresses[0],'signers':sorted(signers),'native_transfers':transfers,
        'controller_independence':'UNKNOWN'}


def collect(rpc,mint,pool,clock,*,limit=MAX_RECEIPTS):
    from .crew_evidence import digest
    validate_address(mint);validate_address(pool)
    if type(limit)is not int or not 1<=limit<=MAX_RECEIPTS:raise ValueError('Bounded buyer collection required')
    if rpc('getGenesisHash',[])!=MAINNET_GENESIS:raise ValueError('Mainnet required')
    end=int(clock());start=end-3600
    rows=rpc('getSignaturesForAddress',[pool,{'commitment':'finalized','limit':limit+1}])
    if not isinstance(rows,list) or len(rows)>limit+1:raise ValueError('Invalid signature page')
    seen=set();records=[];errors=[];boundary=False
    for row in rows:
        signature=row['signature']
        if not isinstance(signature,str) or len(decode58(signature))!=64 or signature in seen:
            raise ValueError('Invalid/duplicate signature')
        seen.add(signature);stamp=row.get('blockTime')
        if type(stamp)is not int:errors.append('unknown_signature_time');continue
        if stamp<=start:boundary=True;break
        if row.get('err') is not None:continue
        if stamp>end:errors.append('future_signature');continue
        if len(records)+len(errors)>=limit:break
        try:
            tx=rpc('getTransaction',[signature,{'encoding':'jsonParsed','commitment':'finalized','maxSupportedTransactionVersion':0}])
        except Exception:
            errors.append('receipt_source_unavailable');break
        try:
            item=receipt(tx,signature,mint,pool,start,end)
            if item['slot']!=row.get('slot') or item['block_time']!=stamp:raise ValueError('Receipt time/slot differs')
            records.append(item)
        except (KeyError,TypeError,ValueError):errors.append('unattributable_receipt')
    buyers=[b for r in records for b in r['buyers']]
    # This page is diagnostic; even a complete signature window cannot prove
    # controller independence or absence of links outside these receipts.
    complete=(boundary or len(rows)<limit+1) and not errors
    return {'status':'UNKNOWN','reason':'independent_controller_attestations_unavailable',
        'collection_status':'COMPLETE_SAMPLE' if not errors else 'PARTIAL_SAMPLE',
        'window':{'start':start,'end':end},'signature_window_complete':complete,
        'observed_at':end,'received_at':int(clock()),'receipts':records,'errors':errors,
        'signature_page_sha256':digest(rows),'buyer_wallets':sorted({b['wallet'] for b in buyers}),
        'observed_buy_receipts':len(buyers),'verified_independent_buyers':None,
        'scope':'Bounded finalized net acquisitions and signed authority links; no independent-controller claim',
        'real_money_enabled':False}

"""Restricted Solana execution, exact-byte signing and canonical recovery.

Supports a single direct classic-SPL Raydium CP-Swap pool. Opaque Jupiter
instructions, swaps through other programs and Token-2022 fail closed.
"""
import base64
import hashlib
import json
import time
from urllib.parse import urlencode

from .adapters.solana import validate_address
from .adapters.solana_execution import SolanaExecutionReader, MAINNET_GENESIS, USDC, signature_bytes, integer
from .execution import ExecutionJournal, encode, digest, policy_hash, raw
from .solana_preflight import validate_quote, TOKEN, ATA, SYSTEM
from .solana_wire import b58encode, decode_unsigned
from . import solana_cpmm as cp


def signature(value):
    cp.require(isinstance(value,str) and 80<=len(value)<=90, 'Invalid signature')
    cp.require(b58encode(signature_bytes(value))==value, 'Noncanonical signature')
    return value


def validate_policy(policy):
    p=json.loads(encode(policy))
    cp.require(p.get('chain_id')=='solana:mainnet' and p.get('genesis_hash')==MAINNET_GENESIS, 'Unsupported Solana chain')
    cp.require(p.get('execution_target')=='automatic' and isinstance(p.get('account_id'),str) and p['account_id'], 'Account identity missing')
    p['wallet']=validate_address(p['wallet']);p['base_token']=validate_address(p['base_token'])
    cp.require(p['base_token']==USDC and p.get('base_decimals')==6, 'Initial Solana settlement is USDC only')
    for k in ('capital_raw','max_order_raw','max_total_exposure_raw','daily_loss_raw','max_gas_wei_day','max_tx_gas_wei'):
        p[k]=raw(p[k],k)
    cp.require(0<=p['max_order_raw']<=25000000 and p['max_order_raw']<=p['max_total_exposure_raw']<=p['capital_raw'], 'Allocation outside approved strategy')
    cp.require(p['daily_loss_raw']<=50000000 and p['max_tx_gas_wei']<=p['max_gas_wei_day'], 'Loss/native budget invalid')
    for k,default,lo,hi in [('max_open_positions',4,1,4),('max_entries_per_day',6,1,6),('cooldown_seconds',86400,86400,86400),
            ('max_quote_age_seconds',30,1,30),('max_evidence_age_seconds',300,1,300),('lease_seconds',180,30,300),
            ('rebroadcast_limit',2,0,2),('rebroadcast_seconds',30,1,60),('max_slippage_bps',100,0,100),
            ('max_rent_lamports',3000000,0,10000000),('compute_units',300000,10000,1400000),('micro_lamports',0,0,1000000)]:
        p.setdefault(k,default);cp.require(type(p[k]) is int and lo<=p[k]<=hi,'Invalid Solana policy bound: '+k)
    for k in ('deployment_authorized','acceptance_mode'):
        cp.require(type(p.get(k)) is bool,'Explicit execution authority required')
    cp.require(not(p['deployment_authorized'] and p['acceptance_mode']), 'Acceptance cannot enable mainnet')
    p['native_unit']='lamports';p['policy_hash']=policy_hash(p)
    return p


class SolanaJournal(ExecutionJournal):
    validate_policy=staticmethod(validate_policy)
    address=staticmethod(validate_address)
    tx_hash=staticmethod(signature)
    block_hash=staticmethod(validate_address)

    @staticmethod
    def day_start(now):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        return int(datetime.fromtimestamp(now,ZoneInfo('Europe/Berlin')).replace(hour=0,minute=0,second=0,microsecond=0).timestamp())

    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.db.execute('CREATE TABLE IF NOT EXISTS execution_sol_positions(position_id TEXT PRIMARY KEY,entry_liquidity TEXT NOT NULL,tp_step INTEGER NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS execution_model_costs(request_id TEXT PRIMARY KEY,at INTEGER NOT NULL,billed_usd TEXT,reserved_usd TEXT NOT NULL)')

    def positions(self):
        positions=super().positions()
        for p in positions:
            extra=self.db.execute('SELECT entry_liquidity,tp_step FROM execution_sol_positions WHERE position_id=?',(p['position_id'],)).fetchone()
            cp.require(extra is not None,'Solana position management metadata missing')
            p.update(entry_liquidity=extra[0],tp_step=extra[1])
        return positions

    def _settled(self,order,result,now):
        if result['status']!='confirmed':return
        i=order['intent']
        if i['side']=='buy':
            self.db.execute('INSERT INTO execution_sol_positions VALUES(?,?,0)',(order['id'],i['entry_evidence']['liquidity_usd']))
        elif i.get('reason') in ('take_profit_1','take_profit_2'):
            step=int(i['reason'][-1])
            self.db.execute('UPDATE execution_sol_positions SET tp_step=? WHERE position_id=?',(step,i['position_id']))
            self.db.execute('UPDATE execution_positions SET take_profit_done=1 WHERE position_id=?',(i['position_id'],))

    def expire_signed(self,order,evidence,now,fence):
        cp.require(isinstance(evidence,dict) and evidence.get('signature')==order['tx_hash']
                   and evidence.get('from_slot')==order['prepared']['context_slot']
                   and evidence.get('last_valid_block_height')==order['prepared']['last_valid_block_height']
                   and isinstance(evidence.get('canonical_scan_digest'),str),'Incomplete signed-expiry proof')
        with self.transaction():
            fence()
            self._audit(order['id'],'canonical_expiry_proof',evidence,now)
            self._status(order['id'],'EXPIRED',now,'Finalized signature scan proved no canonical execution')

    def _validate_intent(self,intent,now):
        i=json.loads(encode(intent));p=self.policy
        cp.require(isinstance(i.get('id'),str) and 1<=len(i['id'])<=240, 'Intent identity missing')
        cp.require(all(i.get(k)==p[k] for k in ('account_id','chain_id','wallet','policy_hash')), 'Wrong execution authority')
        cp.require(i.get('side') in ('buy','sell') and i.get('kind','swap')=='swap','Only swap intents supported')
        for k in ('token_in','token_out','pool'):i[k]=validate_address(i[k])
        cp.require(i['token_in']!=i['token_out'] and i['token_in' if i['side']=='buy' else 'token_out']==USDC,'Wrong settlement assets')
        for k in ('amount_in_raw','min_output_raw','quoted_output_raw'):i[k]=str(integer(i[k],positive=True))
        cp.require(0<int(i['min_output_raw'])<=int(i['quoted_output_raw']), 'Invalid minimum output')
        cp.require(type(i.get('quote_at'))is int and 0<=now-i['quote_at']<=p['max_quote_age_seconds'], 'Stale quote')
        cp.require(type(i.get('deadline'))is int and now<i['deadline']<=now+90,'Invalid intent deadline')
        if i['side']=='buy':
            e=i.get('entry_evidence',{})
            cp.require(e.get('safety_passed') is True and e.get('required_provider_response') is True,'Required risk evidence missing')
            cp.require(type(e.get('safety_at'))is int and 0<=now-e['safety_at']<=p['max_evidence_age_seconds'],'Stale risk evidence')
            cp.require(e.get('model_validated') is True and e.get('model_verdict')=='BUY' and e.get('mint')==i['token_out'] and e.get('pool')==i['pool'], 'Model/pool evidence missing')
            from decimal import Decimal
            confidence=Decimal(str(e.get('confidence')));liquidity=Decimal(str(e.get('liquidity_usd')))
            cp.require(confidence.is_finite() and Decimal('.70')<=confidence<=1 and liquidity.is_finite() and liquidity>=30000,'Numerical entry evidence failed')
            cp.require(type(e.get('model_at'))is int and 0<=now-e['model_at']<=p['max_evidence_age_seconds'] and e.get('model_request_id'),'Stale or missing model provenance')
        return i

    def snapshot(self):
        out=super().snapshot()
        out['native_fees_lamports']=out.pop('native_fees_wei')
        out['reserved_native_lamports']=out.pop('reserved_gas_wei')
        out['rent_locked_lamports']=sum(int(json.loads(r[0]).get('rent_lamports',0)) for r in self.db.execute('SELECT receipt FROM execution_orders WHERE receipt IS NOT NULL'))
        from decimal import Decimal
        costs=list(self.db.execute('SELECT billed_usd,reserved_usd FROM execution_model_costs'))
        out['api_billed_usd']=str(sum((Decimal(r[0]) for r in costs if r[0] is not None),Decimal(0)))
        out['api_unknown_requests']=sum(r[0] is None for r in costs)
        out['api_unresolved_reservations_usd']=str(sum((Decimal(r[1]) for r in costs if r[0] is None),Decimal(0)))
        out['cost_scope']='USDC raw deltas, native fees and rent separately; no assumed USD conversion'
        return out

    def _notice(self,order_id,kind,payload,now):
        payload=dict(payload)
        if 'fee_wei' in payload:payload['fee_lamports']=payload.pop('fee_wei')
        super()._notice(order_id,kind,payload,now)


class SolanaRpc(SolanaExecutionReader):
    ALLOWED_RPC=SolanaExecutionReader.ALLOWED_RPC|{'getMultipleAccounts','getSlot','getBlockTime','isBlockhashValid','getBlock','getBlocks','getMinimumBalanceForRentExemption'}

    def send_exact(self,signed,authorize):
        cp.require(callable(authorize) and authorize(signed) is True,'No durable broadcast grant')
        self.verify_chain()
        cp.require(authorize(signed) is True,'Broadcast authority expired')
        wire=base64.b64encode(bytes.fromhex(signed['raw_transaction'][2:])).decode()
        result=self.transport('POST',self.rpc_url,{'jsonrpc':'2.0','id':1,'method':'sendTransaction',
            'params':[wire,{'encoding':'base64','skipPreflight':False,'preflightCommitment':'confirmed','maxRetries':0,
                            'minContextSlot':signed['prepared']['context_slot']}]},{'Content-Type':'application/json'},20)
        cp.require(isinstance(result,dict) and not result.get('error') and result.get('result')==signed['tx_hash'],'Submission acknowledgement missing or different; retain identity')
        return result['result']


class RestrictedSolanaSigner:
    def __init__(self,adapter,key_loader,authorize):
        self.adapter,self.key_loader,self.authorize=adapter,key_loader,authorize

    def sign(self,prepared):
        cp.require(self.authorize(prepared) is True,'No durable signing grant')
        self.adapter.validate_prepared(prepared,fresh=True)
        keypair=self.key_loader()
        cp.require(str(keypair.pubkey())==self.adapter.policy['wallet'],'Signer wallet mismatch')
        cp.require(self.authorize(prepared) is True,'Signing grant expired during unlock')
        decoded=self.adapter.validate_prepared(prepared,fresh=True)
        sig=keypair.sign_message(decoded['message'])
        cp.require(sig.verify(keypair.pubkey(),decoded['message']),'Signature self-check failed')
        cp.require(self.authorize(prepared) is True,'Signing grant expired')
        wire=b'\1'+bytes(sig)+decoded['message']
        return {'raw_transaction':'0x'+wire.hex(),'tx_hash':str(sig),'nonce':prepared['nonce'],'prepared':prepared}


class SolanaExecutionAdapter:
    is_test=False

    def __init__(self,policy,reader,*,signer=None,authorize_broadcast=None,clock=time.time):
        self.policy=validate_policy(policy);self.reader=reader;self.signer=signer
        self.authorize_broadcast=authorize_broadcast;self.clock=clock

    def account_batch(self,keys,minimum=0):
        require_keys=list(dict.fromkeys(keys))
        result=self.reader.rpc('getMultipleAccounts',[require_keys,{'encoding':'base64','commitment':'confirmed','minContextSlot':minimum}])
        cp.require(type(result.get('context',{}).get('slot'))is int and result['context']['slot']>=minimum and len(result['value'])==len(require_keys),'Invalid RPC account snapshot')
        return dict(zip(require_keys,result['value'])),result['context']['slot']

    def pool_snapshot(self,pool,source,target):
        self.reader.verify_chain();now=int(self.clock())
        initial,slot=self.account_batch([pool,cp.PROGRAM,TOKEN,ATA])
        state=cp.pool_state(initial[pool],now)
        programdata=[]
        for program in (cp.PROGRAM,TOKEN,ATA):
            data=cp.blob(initial[program],executable=True)
            if initial[program]['owner']==cp.UPGRADEABLE:
                cp.require(len(data)==36 and data[:4]==b'\2\0\0\0','Wrong loader layout')
                programdata.append(cp.key(data,4))
        wallet=self.policy['wallet']
        keys=[pool,cp.PROGRAM,TOKEN,ATA,*programdata,state['config'],state['observation'],state['vault0'],state['vault1'],
              source,target,wallet,cp.ata(wallet,source),cp.ata(wallet,target)]
        accounts,slot=self.account_batch(keys,slot)
        stamp=self.reader.rpc('getBlockTime',[slot])
        cp.require(type(stamp)is int and 0<=int(self.clock())-stamp<=30,'RPC pool snapshot is stale')
        current=cp.inspect_pool(pool,source,target,wallet,accounts,now)
        observations={p:cp.deployment_observation(p,accounts) for p in (cp.PROGRAM,TOKEN,ATA)}
        cp.require(observations==self.policy.get('program_pins'),'Program deployment/upgrade pins differ or are missing')
        return current,accounts,slot,observations

    def quote_swap(self,token_in,token_out,pool,amount_in_raw):
        now=int(self.clock());amount=integer(amount_in_raw,positive=True)
        params={'inputMint':token_in,'outputMint':token_out,'amount':amount,'taker':self.policy['wallet'],
                'slippageBps':self.policy['max_slippage_bps'],'onlyDirectRoutes':'true','dexes':'Raydium CP',
                'wrapAndUnwrapSol':'false','transactionVersion':'0'}
        data=self.reader.transport('GET','https://api.jup.ag/swap/v2/build?'+urlencode(params),None,{'x-api-key':self.reader.api_key},20)
        quoted=validate_quote(data,token_in,token_out,amount,pool=pool)
        cp.require(len(data['routePlan'])==1 and data['routePlan'][0]['swapInfo']['label']=='Raydium CP','Only direct Raydium CP quotes supported')
        state,accounts,slot,pins=self.pool_snapshot(pool,token_in,token_out)
        output=cp.quote_output(state,token_in,amount)
        minimum=(output*(10000-self.policy['max_slippage_bps'])+9999)//10000
        # A worse local result is not hidden by changing the quote's promised bound.
        minimum=max(minimum,int(quoted['minimum_output_raw']))
        cp.require(output>=minimum and int(self.clock())-now<=self.policy['max_quote_age_seconds'],'Local output does not satisfy quoted minimum')
        return {'token_in':token_in,'token_out':token_out,'pool':pool,'amount_in_raw':str(amount),
                'quoted_output_raw':str(output),'min_output_raw':str(minimum),'quote_at':now,'deadline':now+30,
                'provider_quote_digest':digest(data),'context_slot':slot,'direct_pool':True}

    def prepare(self,intent):
        p=self.policy;now=int(self.clock())
        cp.require(all(intent.get(k)==p[k] for k in ('account_id','chain_id','wallet','policy_hash')),'Intent authority mismatch')
        cp.require(now<intent['deadline'] and 0<=now-intent['quote_at']<=p['max_quote_age_seconds'],'Stale execution request')
        state,accounts,slot,pins=self.pool_snapshot(intent['pool'],intent['token_in'],intent['token_out'])
        if intent['side']=='buy':
            mint=cp.blob(accounts[intent['token_out']],TOKEN,82)
            cp.require(mint[:4]==bytes(4) and mint[46:50]==bytes(4),'On-chain target mint/freeze authority is not revoked')
        amount=int(intent['amount_in_raw']);minimum=int(intent['min_output_raw'])
        output=cp.quote_output(state,intent['token_in'],amount)
        cp.require(output>=minimum and minimum*10000>=output*(10000-p['max_slippage_bps']),'Output moved outside approved bounds')
        source,target=cp.ata(p['wallet'],intent['token_in']),cp.ata(p['wallet'],intent['token_out'])
        cp.require(accounts[source] is not None and cp.token_account(accounts[source],intent['token_in'],p['wallet'])>=amount,'Input ATA missing or insufficient token balance')
        wallet=accounts[p['wallet']]
        cp.require(wallet is not None and wallet.get('owner')==SYSTEM and cp.blob(wallet)==b'','Fee payer must be an ordinary funded wallet')
        create_output=accounts[target] is None
        rent=integer(self.reader.rpc('getMinimumBalanceForRentExemption',[165])) if create_output else 0
        cp.require(rent<=p['max_rent_lamports'],'Output rent exceeds policy')
        latest=self.reader.rpc('getLatestBlockhash',[{'commitment':'confirmed','minContextSlot':slot}])
        blockhash=latest['value']['blockhash'];height=integer(latest['value']['lastValidBlockHeight'])
        expected={'state':state,'pool':intent['pool'],'wallet':p['wallet'],'source':intent['token_in'],'target':intent['token_out'],
            'amount':amount,'minimum':minimum,'blockhash':blockhash,'create_output':create_output,
            'compute_units':p['compute_units'],'micro_lamports':p['micro_lamports']}
        wire,decoded=cp.build_message(**expected)
        response=self.reader.rpc('getFeeForMessage',[base64.b64encode(decoded['message']).decode(),{'commitment':'confirmed','minContextSlot':slot}])
        fee=integer(response.get('value'),positive=True)
        cp.require(fee<=p['max_tx_gas_wei'] and integer(wallet['lamports'])>=fee+rent,'Native balance or fee budget insufficient')
        simulation=self.reader.rpc('simulateTransaction',[wire,{'encoding':'base64','sigVerify':False,'replaceRecentBlockhash':False,'commitment':'confirmed','minContextSlot':slot}])
        value=simulation.get('value',{})
        cp.require('err' in value and value['err'] is None and type(value.get('unitsConsumed'))is int and 0<value['unitsConsumed']<=p['compute_units'],'Real-account simulation failed or exceeded compute bound')
        prepared={'order_id':intent['id'],'policy_hash':p['policy_hash'],'intent':intent,'unsigned':wire,
            'expected':expected,'program_pins':pins,'context_slot':slot,'last_valid_block_height':height,
            'expires_at':intent['deadline'],'nonce':int(hashlib.sha256(intent['id'].encode()).hexdigest()[:15],16),
            'max_fee_wei':str(fee),'rent_lamports':rent,'native_unit':'lamports','message_sha256':hashlib.sha256(decoded['message']).hexdigest()}
        self.validate_prepared(prepared)
        return prepared

    def validate_prepared(self,prepared,*,fresh=False,allow_expired=False):
        p=self.policy;i=prepared['intent'];e=prepared['expected']
        cp.require(prepared['policy_hash']==p['policy_hash'] and prepared['order_id']==i['id'] and all(i.get(k)==p[k] for k in ('account_id','chain_id','wallet','policy_hash')),'Prepared authority mismatch')
        cp.require(e['wallet']==p['wallet'] and e['pool']==i['pool'] and e['source']==i['token_in'] and e['target']==i['token_out']
                   and e['amount']==int(i['amount_in_raw']) and e['minimum']==int(i['min_output_raw'])
                   and e['compute_units']==p['compute_units'] and e['micro_lamports']==p['micro_lamports'],'Prepared instruction changed policy/intent')
        cp.require(prepared['program_pins']==p.get('program_pins') and int(prepared['max_fee_wei'])<=p['max_tx_gas_wei']
                   and 0<=prepared['rent_lamports']<=p['max_rent_lamports'],'Prepared deployment/expense mismatch')
        decoded=cp.validate_message(prepared['unsigned'],e)
        cp.require(hashlib.sha256(decoded['message']).hexdigest()==prepared['message_sha256'],'Message digest mismatch')
        if not allow_expired:cp.require(int(self.clock())<prepared['expires_at']<=i['deadline'],'Prepared request expired')
        if fresh:
            state,accounts,slot,pins=self.pool_snapshot(i['pool'],i['token_in'],i['token_out'])
            if i['side']=='buy':
                mint=cp.blob(accounts[i['token_out']],TOKEN,82)
                cp.require(mint[:4]==bytes(4) and mint[46:50]==bytes(4),'Target authority changed before signing/submission')
            # Balances/fee counters can change. All structural relationships must not.
            structural=('config','authority','mint0','mint1','vault0','vault1','observation','decimals0','decimals1')
            cp.require(all(state[k]==e['state'][k] for k in structural),'Pool relationships changed')
            cp.require(cp.quote_output(state,i['token_in'],e['amount'])>=e['minimum'],'Fresh reserves cannot satisfy swap')
            wallet=accounts[p['wallet']]
            cp.require(wallet is not None and wallet.get('owner')==SYSTEM and cp.blob(wallet)==b'', 'Fee payer account changed')
            cp.require(cp.token_account(accounts[cp.ata(p['wallet'],i['token_in'])],i['token_in'],p['wallet'])>=e['amount'],'Source tokens unavailable')
            fee=self.reader.rpc('getFeeForMessage',[base64.b64encode(decoded['message']).decode(),{'commitment':'confirmed','minContextSlot':slot}])
            cp.require(integer(fee.get('value'),positive=True)<=int(prepared['max_fee_wei'])
                       and integer(wallet['lamports'])>=int(prepared['max_fee_wei'])+prepared['rent_lamports'], 'Fresh native balance/fee check failed')
            valid=self.reader.rpc('isBlockhashValid',[decoded['blockhash'],{'commitment':'confirmed','minContextSlot':slot}])
            cp.require(valid.get('value') is True,'Prepared blockhash expired')
        return decoded

    def sign(self,prepared):
        cp.require(self.signer is not None,'Restricted signer not provisioned')
        return self.signer.sign(prepared)

    def signed_message(self,signed):
        from solders.signature import Signature
        from solders.pubkey import Pubkey
        prepared=signed['prepared'];decoded=self.validate_prepared(prepared,allow_expired=True)
        wire=bytes.fromhex(signed['raw_transaction'][2:])
        cp.require(wire==b'\1'+signature_bytes(signature(signed['tx_hash']))+decoded['message'],'Signed message bytes differ')
        cp.require(Signature.from_string(signed['tx_hash']).verify(Pubkey.from_string(self.policy['wallet']),decoded['message']),'Invalid wallet signature')
        return wire,decoded

    def broadcast(self,signed):
        self.signed_message(signed)
        self.validate_prepared(signed['prepared'],fresh=True)
        return self.reader.send_exact(signed,self.authorize_broadcast)

    def reconcile(self,order):
        signed=order['signed'];wire,decoded=self.signed_message(signed);self.reader.verify_chain()
        sig=signed['tx_hash'];prepared=signed['prepared'];intent=prepared['intent']
        result=self.reader.rpc('getSignatureStatuses',[[sig],{'searchTransactionHistory':True}])
        status=result['value'][0]
        if not status or status.get('confirmationStatus')!='finalized':
            if status:return {'status':'pending','reason':'awaiting_finalized_signature'}
            if integer(self.reader.rpc('getBlockHeight',[{'commitment':'finalized'}]))>prepared['last_valid_block_height']:
                return self.expiry_proof(order)
            return {'status':'pending','reason':'signature_absent_preserve_bytes'}
        tx=self.reader.rpc('getTransaction',[sig,{'commitment':'finalized','encoding':'base64','maxSupportedTransactionVersion':0}])
        cp.require(tx is not None and type(tx.get('slot'))is int and type(tx.get('blockTime'))is int,'Finalized transaction body unavailable')
        cp.require(tx['slot']==status['slot'] and tx['transaction'][1]=='base64' and base64.b64decode(tx['transaction'][0],validate=True)==wire,'Canonical transaction differs from signed bytes')
        block=self.reader.rpc('getBlock',[tx['slot'],{'commitment':'finalized','transactionDetails':'signatures','rewards':False,'maxSupportedTransactionVersion':0}])
        cp.require(sig in block['signatures'],'Signature absent from finalized block')
        meta=tx['meta'];fee=integer(meta['fee']);cp.require(fee<=int(prepared['max_fee_wei']),'Fee exceeds reservation')
        keys=decoded['keys'];pre,post=meta['preBalances'],meta['postBalances']
        cp.require(len(pre)==len(post)==len(keys) and meta.get('loadedAddresses',{'writable':[],'readonly':[]})=={'writable':[],'readonly':[]},'Unexpected resolved accounts')
        wallet=self.policy['wallet'];owned={cp.ata(wallet,intent[k]):intent[k] for k in ('token_in','token_out')}
        totals={mint:0 for mint in owned.values()}
        for direction,field in [(-1,'preTokenBalances'),(1,'postTokenBalances')]:
            cp.require(isinstance(meta.get(field),list),'Missing token balance evidence')
            seen=set()
            for row in meta[field]:
                index=integer(row['accountIndex']);cp.require(index<len(keys) and index not in seen,'Invalid token balance index');seen.add(index)
                if row.get('owner')==wallet or keys[index] in owned:
                    cp.require(keys[index] in owned and row.get('owner')==wallet and row['mint']==owned[keys[index]] and row.get('programId')==TOKEN,'Unexplained wallet token account')
                    state=prepared['expected']['state'];n=0 if row['mint']==state['mint0'] else 1
                    cp.require(row['uiTokenAmount'].get('decimals')==state[f'decimals{n}'],'Token receipt precision changed')
                    totals[row['mint']]+=direction*integer(row['uiTokenAmount']['amount'])
        amount_in,amount_out=-totals[intent['token_in']],totals[intent['token_out']]
        rent=sum(post[keys.index(k)]-pre[keys.index(k)]-(totals[mint] if mint==cp.WSOL else 0) for k,mint in owned.items())
        cp.require(pre[0]-post[0]==fee+rent and 0<=rent<=prepared['rent_lamports'],'Unclassified native movement')
        success=meta['err'] is None
        cp.require(meta['err']==status['err'],'Status/receipt outcome mismatch')
        if success:cp.require(amount_in==int(intent['amount_in_raw']) and amount_out>=int(intent['min_output_raw']),'Actual swap violates approved amounts')
        else:cp.require(amount_in==amount_out==rent==0,'Failed transaction cannot create a swap or rent charge')
        return {'status':'confirmed' if success else 'reverted','rent_lamports':rent,
            'receipt':{'tx_hash':sig,'block_hash':block['blockhash'],'block_number':tx['slot'],'observed_at':tx['blockTime'],
                       'canonical':True,'finalized':True,'fee_wei':str(fee),'native_unit':'lamports'},
            'fill':{'input_raw':str(amount_in),'output_raw':str(amount_out),'fee_wei':str(fee),
                    'wallet':wallet,'token_in':intent['token_in'],'token_out':intent['token_out'],'native_unit':'lamports'}}

    def expiry_proof(self,order):
        """A bounded full finalized signature scan; absence alone is insufficient."""
        p=order['prepared'];sig=order['tx_hash']
        end=integer(self.reader.rpc('getSlot',[{'commitment':'finalized'}]))
        start=p['context_slot']
        if not 0<=end-start<=512:return {'status':'unknown','reason':'expiry_scan_outside_bounded_history'}
        slots=self.reader.rpc('getBlocks',[start,end,{'commitment':'finalized'}])
        cp.require(isinstance(slots,list) and slots==sorted(set(slots)) and slots and slots[-1]==end,'Incomplete finalized block range')
        anchors=[]
        for slot in slots:
            cp.require(type(slot)is int and start<=slot<=end,'Invalid finalized slot')
            block=self.reader.rpc('getBlock',[slot,{'commitment':'finalized','transactionDetails':'signatures','rewards':False,'maxSupportedTransactionVersion':0}])
            cp.require(isinstance(block.get('signatures'),list),'Block signature history missing')
            if sig in block['signatures']:return {'status':'unknown','reason':'signature_found_but_receipt_unavailable'}
            anchors.append([slot,block['blockhash']])
        last=self.reader.rpc('getBlock',[end,{'commitment':'finalized','transactionDetails':'signatures','rewards':False,'maxSupportedTransactionVersion':0}])
        cp.require(last['blockhash']==anchors[-1][1],'Expiry scan anchor changed')
        return {'status':'expired','evidence':{'signature':sig,'from_slot':start,'through_slot':end,
                 'last_valid_block_height':p['last_valid_block_height'],'canonical_scan_digest':digest(anchors),
                 'scope':'All available finalized blocks in the bounded validity interval contain no signed identity'}}

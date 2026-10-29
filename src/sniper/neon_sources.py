"""Public candidate research and conservative, finalized Solana swap observations.

This is not a CopyFomo order/event API. Headline leaderboard rows never qualify a
leader. Only direct classic-SPL Raydium CP exact-input swaps are decoded here.
"""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import struct
import time
import uuid

from rug_desk.book import encode
from rug_desk.traders import handle
from .adapters.solana import request_json, validate_address
from .adapters.solana_execution import SolanaExecutionReader, USDC, WSOL
from . import solana_cpmm as cp

BOARD = 'https://api.fomo-api.com/v1/leaderboard?window=30d&board=solana&limit=10'


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def leaderboard(payload, now):
    if not isinstance(payload, dict) or payload.get('board') != 'solana' or payload.get('window') != '30d':
        raise ValueError('Wrong leaderboard scope')
    captured = datetime.fromisoformat(payload['capturedAt'].replace('Z', '+00:00')).timestamp()
    if payload.get('stale') is True or not 0 <= now-captured <= 7200:
        raise ValueError('Stale or future leaderboard')
    if not isinstance(payload.get('data'), list) or len(payload['data']) > 10:
        raise ValueError('Unexpected public leaderboard size')
    result, seen = [], set()
    for row in payload['data']:
        wallet = validate_address(row['address'])
        if wallet in seen:
            raise ValueError('Duplicate candidate wallet')
        seen.add(wallet)
        name = handle(row['handle']) if row.get('handle') else None
        result.append({'wallet':wallet, 'handle':name, 'captured_at':int(captured),
            'source':BOARD, 'source_kind':'third_party_public_leaderboard',
            'claimed_metrics':{k:row.get(k) for k in ('rank','realizedPnlUsd','coveragePct','pnlStatus','trades','identityConfidence')},
            'qualified':False, 'blockers':['Complete cost-accounted transaction history missing',
                'Provider identity assertion is not independent ownership proof', 'Model selection required']})
    return result


def decode_swap(receipt, signature, wallet, name, now):
    """Require a paid-for direct swap; incoming gifts are never buy signals."""
    if not isinstance(receipt, dict) or type(receipt.get('slot')) is not int:
        raise ValueError('Transaction unavailable')
    version=receipt.get('version')
    if version!='legacy' and (type(version) is not int or version!=0):
        raise ValueError('Unsupported or missing transaction version; no swap inferred')
    meta, tx = receipt['meta'], receipt['transaction']
    if not meta or meta.get('err') is not None or type(meta.get('fee')) is not int:
        raise ValueError('Failed or incomplete transaction')
    occurred = receipt.get('blockTime')
    if type(occurred) is not int or occurred > now:
        raise ValueError('Missing transaction time')
    if not tx.get('signatures') or tx['signatures'][0] != signature:
        raise ValueError('Transaction signature mismatch')
    message = tx['message']
    keys = list(message['accountKeys'])
    if any(not isinstance(k,str) for k in keys):
        raise ValueError('Raw JSON transaction required')
    signers = keys[:message['header']['numRequiredSignatures']]
    if wallet not in signers:
        raise ValueError('Observed wallet did not sign')
    loaded = meta.get('loadedAddresses') or {}
    keys += loaded.get('writable',[]) + loaded.get('readonly',[])
    for key in keys:
        validate_address(key)
    swap = []
    for ix in message['instructions']:
        program = keys[ix['programIdIndex']]
        if program == cp.PROGRAM:
            swap.append(ix)
        elif program not in (cp.COMPUTE, cp.ATA):
            raise ValueError('Unsupported transaction program; no swap inferred')
    if len(swap) != 1:
        raise ValueError('Exactly one supported direct swap required')
    ix = swap[0]; encoded=ix['data']
    alphabet='123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
    if not isinstance(encoded,str) or not 1<=len(encoded)<=40 or any(c not in alphabet for c in encoded):
        raise ValueError('Invalid instruction data')
    value=0
    for char in encoded:value=value*58+alphabet.index(char)
    raw=b'\0'*(len(encoded)-len(encoded.lstrip('1')))+value.to_bytes((value.bit_length()+7)//8,'big')
    if len(raw) != 24 or raw[:8] != cp.SWAP or len(ix['accounts']) != 13:
        raise ValueError('Unsupported CP instruction')
    accounts = [keys[i] for i in ix['accounts']]
    if accounts[0] != wallet or accounts[8:10] != [cp.TOKEN,cp.TOKEN]:
        raise ValueError('Wrong swap owner or token program')
    source, target = accounts[10:12]
    if source in (USDC,WSOL) and target not in (USDC,WSOL):
        action,mint = 'BUY',target
    elif target in (USDC,WSOL) and source not in (USDC,WSOL):
        action,mint = 'SELL',source
    else:
        raise ValueError('Unsupported settlement or ambiguous swap')
    amount, minimum = struct.unpack('<QQ',raw[8:])
    if not amount or not minimum:
        raise ValueError('Unbounded swap amounts')
    def balance(rows, account, mint, *, absent=False):
        matching = [r for r in rows if r['accountIndex'] == keys.index(account)]
        if not matching and absent:
            return 0, None
        if len(matching) != 1:
            raise ValueError('Missing token balance evidence')
        row = matching[0]
        if row.get('owner') != wallet or row.get('mint') != mint or row.get('programId') != cp.TOKEN:
            raise ValueError('Token balance owner/mint/program mismatch')
        token = row['uiTokenAmount']; raw_amount=token['amount']; decimals=token['decimals']
        if not isinstance(raw_amount,str) or not raw_amount.isdigit() or type(decimals) is not int or not 0<=decimals<=18:
            raise ValueError('Invalid token balance')
        return int(raw_amount),decimals
    pre,post = meta['preTokenBalances'],meta['postTokenBalances']
    before,dec_in = balance(pre,accounts[4],source)
    after,dec_in2 = balance(post,accounts[4],source)
    out_before,dec_out = balance(pre,accounts[5],target,absent=True)
    out_after,dec_out2 = balance(post,accounts[5],target)
    if dec_in != dec_in2 or (dec_out is not None and dec_out != dec_out2):
        raise ValueError('Decimals changed')
    if before-after != amount or out_after-out_before < minimum:
        raise ValueError('Wallet did not pay the exact swap input or receive minimum output')
    return {'chain':'solana','wallet':wallet,'handle':name,'mint':mint,'pool':accounts[3],
        'action':action,'transaction_signature':signature,'occurred_at':occurred,'observed_at':now,
        'source':'solana_finalized_cp_swap','source_ref':'https://explorer.solana.com/tx/'+signature,
        'slot':receipt['slot'],'input_mint':source,'output_mint':target,
        'input_raw':str(amount),'output_raw':str(out_after-out_before),'fee_lamports':meta['fee'],
        'fee_usd':None,'history_complete':False,
        'scope':'Finalized direct CP swap and owner token deltas; not performance/controller qualification'}


class Research:
    def __init__(self, book, app, *, transport=request_json, clock=time.time, rpc=None):
        self.book,self.db,self.app,self.transport,self.clock = book,book.db,Path(app),transport,clock
        class Reader(SolanaExecutionReader):
            ALLOWED_RPC = SolanaExecutionReader.ALLOWED_RPC | {'getSignaturesForAddress','getBlock'}
        rpcfile=self.app/'runtime/solana-rpc-url.txt'
        self.rpc=rpc or Reader(rpc_url=rpcfile.read_text(encoding='utf-8-sig').strip() if rpcfile.exists() else 'https://api.mainnet-beta.solana.com',transport=transport)
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS neon_candidates(wallet TEXT PRIMARY KEY,at INTEGER NOT NULL,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS neon_sources(name TEXT PRIMARY KEY,at INTEGER NOT NULL,status TEXT NOT NULL,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS neon_cursors(wallet TEXT PRIMARY KEY,head TEXT,pending TEXT NOT NULL,checked_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS neon_observations(signature TEXT NOT NULL,wallet TEXT NOT NULL,at INTEGER NOT NULL,status TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(signature,wallet));
            CREATE TABLE IF NOT EXISTS neon_source_gaps(id INTEGER PRIMARY KEY,at INTEGER NOT NULL,wallet TEXT NOT NULL,reason TEXT NOT NULL);
        ''')
        with book.transaction():
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES('neon_source_id',?)",(uuid.uuid4().hex,))
        self.source_id=self.db.execute("SELECT value FROM metadata WHERE key='neon_source_id'").fetchone()[0]

    def observation(self, name, status, now, reason):
        """Safe, durable UI evidence; no provider payloads or authority changes."""
        from .paper_events import emit
        with self.book.transaction():
            run=self.db.execute("SELECT id FROM runs WHERE job='signals' AND status='running' ORDER BY started DESC LIMIT 1").fetchone()
            payload={'source_name':name,'status':status,'reason':reason,
                     'source_id':self.source_id,'observed_at':now,'run_id':run[0] if run else None}
            emit(self.book,'source:'+name+':'+str(now)+':'+status,'source_observation',now,payload)

    def refresh_board(self):
        now=int(self.clock()); previous=self.db.execute("SELECT * FROM neon_sources WHERE name='leaderboard'").fetchone()
        if previous and 0<=now-previous['at']<3600:
            return dict(previous)
        self.observation('leaderboard','RUNNING',now,'Fetching public research candidates; no qualification implied')
        try:
            candidates=leaderboard(self.transport('GET',BOARD,None,{'Accept':'application/json'},15),now)
            status='OK';result={'candidate_count':len(candidates),'qualified_count':0,'scope':'Research leads only'}
            with self.book.transaction():
                for candidate in candidates:
                    self.db.execute('INSERT OR REPLACE INTO neon_candidates VALUES(?,?,?)',(candidate['wallet'],now,encode(candidate)))
                    from .paper_events import emit
                    evidence={'candidate_id':self.book.identity['account_id']+':wallet:'+candidate['wallet'],
                              'wallet':candidate['wallet'],'handle':candidate['handle'],'candidate_kind':'trader_research',
                              'source_kind':candidate['source_kind'],'source_id':self.source_id,
                              'observed_at':candidate['captured_at'],'qualified':False,
                              'reason':'Research candidate only; complete history and model selection required'}
                    emit(self.book,'intake:'+digest(evidence),'candidate_intake',candidate['captured_at'],evidence)
        except Exception as exc:
            status='ERROR';result={'error_type':type(exc).__name__,'candidate_count':None,'qualified_count':0}
        with self.book.transaction():
            self.db.execute('INSERT OR REPLACE INTO neon_sources VALUES(?,?,?,?)',('leaderboard',now,status,encode(result)))
        self.observation('leaderboard',status,int(self.clock()),'Public research candidate refresh completed' if status=='OK' else 'Public candidate provider unavailable or invalid; no qualification inferred')
        return {'at':now,'status':status,**result}

    def observe(self, leaders):
        self.observation('rpc','RUNNING',int(self.clock()),'Checking finalized Solana observations')
        try:self.rpc.verify_chain()
        except Exception as exc:
            failed_at=int(self.clock())
            with self.book.transaction():
                self.db.execute('INSERT OR REPLACE INTO neon_sources VALUES(?,?,?,?)',
                    ('rpc',failed_at,'ERROR',encode({'error_type':type(exc).__name__,'scope':'chain_verification'})))
            self.observation('rpc','ERROR',failed_at,'Solana chain verification failed; no trade inferred')
            raise
        now=int(self.clock()); events=[]; results=[]
        candidates=[json.loads(r[0]) for r in self.db.execute('SELECT payload FROM neon_candidates WHERE at>=? ORDER BY at DESC,wallet LIMIT 30',(now-7200,))]
        selected={l['wallet']:l for l in leaders}
        # Selected leaders receive every pass; one other research candidate rotates.
        extra=[c for c in candidates if c['wallet'] not in selected]
        if extra:
            candidate=extra[(now//60)%len(extra)];selected[candidate['wallet']]=candidate
        for wallet,leader in list(selected.items())[:4]:
            try:
                events.extend(self._wallet(wallet,leader.get('handle'),now))
                results.append({'wallet':wallet,'status':'OK'})
            except Exception as exc:
                results.append({'wallet':wallet,'status':'ERROR','error_type':type(exc).__name__})
        with self.book.transaction():
            self.db.execute('INSERT OR REPLACE INTO neon_sources VALUES(?,?,?,?)',('rpc',now,'ERROR' if any(r['status']=='ERROR' for r in results) else 'OK',encode(results)))
        failed=any(r['status']=='ERROR' for r in results)
        self.observation('rpc','ERROR' if failed else 'OK',int(self.clock()),
            'Some finalized observations unavailable; pending work retained' if failed else 'Finalized observation pass completed; unsupported transactions do not become buy signals')
        return events,results

    def _wallet(self,wallet,name,now):
        row=self.db.execute('SELECT * FROM neon_cursors WHERE wallet=?',(wallet,)).fetchone()
        pending=json.loads(row['pending']) if row else []
        head=row['head'] if row else None
        if not pending:
            signatures=[];before=None;found=head is None
            for _ in range(3):
                opts={'limit':20,'commitment':'finalized'}
                if before:opts['before']=before
                page=self.rpc.rpc('getSignaturesForAddress',[wallet,opts])
                if not isinstance(page,list) or len(page)>20:raise ValueError('Signature page malformed')
                for item in page:
                    if item['signature']==head:found=True;break
                    signatures.append(item)
                if found or len(page)<20:break
                before=page[-1]['signature']
            new_head=signatures[0]['signature'] if signatures else head
            if row is None:
                # Baseline only; never trade old history when starting a watcher.
                pending=[]
            elif not found:
                with self.book.transaction():
                    self.db.execute('INSERT INTO neon_source_gaps(at,wallet,reason) VALUES(?,?,?)',(now,wallet,'Previous signature not found within bounded history; rebaseline, no entries from omitted history'))
                pending=[]
            else:
                pending=list(reversed(signatures))
            head=new_head
            with self.book.transaction():
                self.db.execute('INSERT OR REPLACE INTO neon_cursors VALUES(?,?,?,?)',(wallet,head,encode(pending),now))
        events=[]
        for item in pending[:4]:
            signature=item['signature'];event=None
            prior=self.db.execute('SELECT 1 FROM neon_observations WHERE signature=? AND wallet=?',(signature,wallet)).fetchone()
            if not prior:
                # Read current-format receipts so an unsupported v1 does not
                # wedge every later signature. decode_swap still admits only
                # legacy/v0; this is not a signing or strategy coverage upgrade.
                receipt=self.rpc.rpc('getTransaction',[signature,{'encoding':'json','commitment':'finalized','maxSupportedTransactionVersion':1}])
                if receipt is None:raise ValueError('Finalized transaction temporarily unavailable; checkpoint retained')
                proof={'source_id':self.source_id,'receipt_sha256':digest(receipt),'checked_at':now,'signature':signature,'wallet':wallet}
                try:
                    if not name:raise ValueError('No linked handle')
                    event=decode_swap(receipt,signature,wallet,name,now)
                    if item.get('slot')!=event['slot'] or item.get('confirmationStatus')!='finalized':raise ValueError('Finality/slot mismatch')
                    block=self.rpc.rpc('getBlock',[event['slot'],{'commitment':'finalized','transactionDetails':'signatures','rewards':False,'maxSupportedTransactionVersion':1}])
                    if not block or signature not in block.get('signatures',[]):raise RuntimeError('Canonical block proof unavailable')
                    proof.update(event=event,canonical_blockhash=validate_address(block['blockhash']))
                    status='SUPPORTED_SWAP'
                except ValueError as exc:
                    status='UNSUPPORTED';proof['reason']=str(exc)[:200];proof['transaction_version']=receipt.get('version');event=None
                with self.book.transaction():
                    self.db.execute('INSERT INTO neon_observations VALUES(?,?,?,?,?)',(signature,wallet,now,status,encode(proof)))
            pending=pending[1:]
            with self.book.transaction():
                self.db.execute('UPDATE neon_cursors SET pending=?,checked_at=? WHERE wallet=?',(encode(pending),now,wallet))
            if event:events.append(event)
        return events

    def verify(self,event,now):
        row=self.db.execute('SELECT * FROM neon_observations WHERE signature=? AND wallet=?',(event['transaction_signature'],event['wallet'])).fetchone()
        if not row or row['status']!='SUPPORTED_SWAP' or not 0<=now-row['at']<=300:return False
        proof=json.loads(row['payload'])
        return proof.get('source_id')==self.source_id and proof.get('event')==event and bool(proof.get('canonical_blockhash'))

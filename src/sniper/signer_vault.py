"""User-local Windows DPAPI signer vault; no private material in argv or reports.

DPAPI protects storage for the current Windows user. It does not isolate secrets
from malicious code already running as that user. Provision only a dedicated
wallet after the reviewed execution policy has been explicitly approved.
"""
import argparse
import base64
import ctypes
from ctypes import wintypes
import getpass
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

from .execution import encode,validate_policy as evm_policy
from .execution_solana import validate_policy as sol_policy


def dpapi(data,entropy,*,decrypt=False):
    if os.name!='nt':raise ValueError('This signer vault requires Windows DPAPI')
    class Blob(ctypes.Structure):
        _fields_=[('size',wintypes.DWORD),('data',ctypes.POINTER(ctypes.c_ubyte))]
    def blob(value):
        array=(ctypes.c_ubyte*len(value)).from_buffer_copy(value)
        return Blob(len(value),array),array
    source,source_buffer=blob(data);extra,extra_buffer=blob(entropy);out=Blob()
    crypt=ctypes.WinDLL('crypt32',use_last_error=True);kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    fn=crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    fn.argtypes=[ctypes.POINTER(Blob),ctypes.c_void_p,ctypes.POINTER(Blob),ctypes.c_void_p,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(Blob)]
    fn.restype=wintypes.BOOL;kernel.LocalFree.argtypes=[ctypes.c_void_p];kernel.LocalFree.restype=ctypes.c_void_p
    if not fn(ctypes.byref(source),None,ctypes.byref(extra),None,None,1,ctypes.byref(out)):
        raise ValueError('Windows user-bound encryption failed')
    try:return ctypes.string_at(out.data,out.size)
    finally:
        ctypes.memset(out.data,0,out.size);kernel.LocalFree(out.data)
        ctypes.memset(source_buffer,0,len(data))


def normalized(policy):
    return sol_policy(policy) if policy.get('chain_id')=='solana:mainnet' else evm_policy(policy)


def secret_wallet(secret,policy):
    if policy['chain_id']=='solana:mainnet':
        from solders.keypair import Keypair
        key=Keypair.from_bytes(secret);return str(key.pubkey()),key
    from eth_account import Account
    key=Account.from_key(secret);return key.address.lower(),secret


def protect(path,secret,policy,*,acceptance=False):
    p=normalized(policy);path=Path(path)
    if p['acceptance_mode'] is not acceptance:raise ValueError('Signer test/live account mismatch')
    if not acceptance and not (p.get('signer_provisioning_authorized') is True and p.get('signer_approval_reference')):
        raise ValueError('Explicit signer provisioning approval is missing')
    wallet,_=secret_wallet(secret,p)
    if wallet!=p['wallet']:raise ValueError('Provided signer does not match the approved public wallet')
    entropy=('rug-desk-signer-v1:'+p['policy_hash']).encode()
    encrypted=dpapi(secret,entropy)
    record={'schema':'rug_desk_dpapi_signer_v1','policy_hash':p['policy_hash'],'chain_id':p['chain_id'],
        'wallet_sha256':hashlib.sha256(wallet.encode()).hexdigest(),'created_at':int(time.time()),
        'acceptance':acceptance,'ciphertext':base64.b64encode(encrypted).decode()}
    path.parent.mkdir(parents=True,exist_ok=True)
    # Exclusive creation prevents silent wallet replacement or policy rebinding.
    fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    try:
        sid=subprocess.run(['whoami','/user','/fo','csv','/nh'],capture_output=True,text=True,check=True).stdout
        import csv,io
        principal=next(csv.reader(io.StringIO(sid.strip())))[1]
        if not principal.startswith('S-1-'):raise ValueError('Windows user identity unavailable')
        subprocess.run(['icacls',str(path),'/inheritance:r','/grant:r','*'+principal+':(F)'],capture_output=True,check=True)
        os.write(fd,encode(record).encode('utf-8'));os.fsync(fd)
    finally:os.close(fd)
    return {'provisioned':True,'wallet_sha256':record['wallet_sha256'],'policy_hash':p['policy_hash'],
            'signing_calls':0,'broadcast_calls':0,'storage':'Windows current-user DPAPI and owner-only file ACL'}


def load_signer(path,policy):
    p=normalized(policy);path=Path(path)
    if path.stat().st_size>16000:raise ValueError('Invalid signer vault size')
    record=json.loads(path.read_text(encoding='utf-8'))
    if (record.get('schema')!='rug_desk_dpapi_signer_v1' or record.get('policy_hash')!=p['policy_hash']
            or record.get('chain_id')!=p['chain_id'] or record.get('acceptance') is not p['acceptance_mode']):
        raise ValueError('Signer vault policy/chain binding mismatch')
    secret=dpapi(base64.b64decode(record['ciphertext'],validate=True),('rug-desk-signer-v1:'+p['policy_hash']).encode(),decrypt=True)
    wallet,key=secret_wallet(secret,p)
    if wallet!=p['wallet'] or record.get('wallet_sha256')!=hashlib.sha256(wallet.encode()).hexdigest():
        raise ValueError('Signer vault wallet mismatch')
    return key


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--policy',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True);parser.add_argument('--provision-authorized',action='store_true')
    args=parser.parse_args()
    if not args.provision_authorized:parser.error('Explicit local provisioning authorization is required')
    policy=normalized(json.loads(args.policy.read_text(encoding='utf-8-sig')))
    if policy['acceptance_mode']:parser.error('CLI provisioning cannot turn test fixtures into live keys')
    if not policy.get('signer_provisioning_authorized') or not policy.get('signer_approval_reference'):
        parser.error('Review and authorize the wallet-bound provisioning policy first')
    value=getpass.getpass('Dedicated wallet private key (hidden local input; never enter a seed phrase): ')
    try:
        if policy['chain_id']=='solana:mainnet':
            from solders.keypair import Keypair
            secret=bytes(Keypair.from_base58_string(value))
        else:secret=bytes.fromhex(value.removeprefix('0x'))
        result=protect(args.output,secret,policy)
        print(encode(result))
    except Exception as exc:
        print(encode({'provisioned':False,'error_type':type(exc).__name__,'reason':'Local provisioning failed; no signing or broadcast'}))
        raise SystemExit(1) from None

if __name__=='__main__':main()

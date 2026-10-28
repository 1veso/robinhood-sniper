"""Local paid-model readiness; reading status never spends or enables access."""
import hashlib
import json
from pathlib import Path
import time
from rug_desk import config


def strategy_binding(root):
    """Public strategy identity; exits must not depend on model credentials."""
    root=Path(root)
    identity=json.loads((root/'runtime/account.json').read_text(encoding='utf-8-sig'))
    return {'account_id':identity['account_id'],'settings_sha256':identity['config_sha256'],
        'config_file_sha256':hashlib.sha256(Path(config.__file__).read_bytes()).hexdigest()}


def binding(root):
    root=Path(root)
    key=(root.parent/'runtime/xai-api-key.txt').read_text(encoding='utf-8-sig').strip()
    if not key or '\n' in key or '\r' in key:raise ValueError('Malformed model credential')
    return {**strategy_binding(root),
        'credential_sha256':hashlib.sha256(key.encode()).hexdigest(),'model':config.MODEL}


def status(root,now=None):
    root=Path(root);now=int(time.time() if now is None else now)
    result={'enabled':False,'model':config.MODEL,'paid_calls_made':0,'blockers':[],
            'daily_limit_usd':str(config.AI_DAILY_LIMIT_USD),'per_call_reservation_usd':str(config.AI_RESERVATION_USD)}
    try:
        def read(name):
            p=root/'runtime'/name
            return json.loads(p.read_text(encoding='utf-8-sig')) if p.exists() else {}
        authority=read('paid-ai.json')
        if authority.get('enabled') is not True or authority.get('operator_authorized') is not True:
            result['blockers'].append('Paid calls are disabled; no credential was read and no provider was called')
            return result
        current=binding(root)
        if not (authority.get('enabled') is True and authority.get('operator_authorized') is True
                and authority.get('approval_reference') and authority.get('binding')==current):
            result['blockers'].append('Explicit paid authorization bound to this account, configuration and credential is missing')
        for name in ('inference-test.json','x-search-test.json'):
            proof=read(name);tested=proof.get('tested_at')
            if (proof.get('validated') is not True or proof.get('binding')!=current or proof.get('model')!=config.MODEL
                    or type(tested)is not int or not 0<=now-tested<=7*86400):
                result['blockers'].append(name+': successful bound test within seven days required')
        result['enabled']=not result['blockers']
    except (OSError,ValueError,KeyError,TypeError,AttributeError):
        result['blockers'].append('Model readiness files missing or invalid')
    return result

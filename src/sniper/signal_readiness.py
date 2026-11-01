"""No-network inventory for restoring the seven-bot installation. Never enables a gate."""
from pathlib import Path

ACCOUNTS = (
    ('robinhood','Radeon','runtime/exploration/ledger.sqlite3','On-chain purchase signals; Grok is optional research, not its entry trigger'),
    ('solana','Xenon','rug_desk/runtime/book.sqlite3','Account-bound Grok inference and X Search proofs required'),
    ('neon','Neon','neon_bot/runtime/book.sqlite3','Grok/X plus qualified leader histories and selection required'),
    ('jev','Argon','rug_desk/runtime/book.sqlite3','Shares Xenon; separate Jev/TypeSafe key, evidence and promotion required'),
    ('krypton','Krypton','krypton_bot/runtime/book.sqlite3','Shared Grok gate plus Krypton-bound proof required'),
    ('helium','Helium','helium_bot/runtime/book.sqlite3','Helium-bound Grok proof plus separately authorized Jev reviewer required'),
)


def inventory(root):
    root = Path(root)
    key = (root/'runtime/xai-api-key.txt').is_file()
    jev_key = (root/'runtime/typesafe-api-key.txt').is_file()
    bots = []
    for bot,name,ledger,scope in ACCOUNTS:
        exists = (root/ledger).is_file()
        blockers = [] if exists else ['Account backup not restored; no replacement account initialized']
        if not key and bot != 'robinhood': blockers.append('Shared xAI key file missing')
        if bot in ('jev','helium') and not jev_key: blockers.append('Separate TypeSafe/Jev key missing')
        blockers.append('RPC feeds, funded-purchase evidence and account settings must be checked after restore' if bot == 'robinhood'
                        else 'Account-bound paid authorization and current provider proofs must be checked after restore')
        bots.append({'bot':bot,'name':name,'account_present':exists,'signal_scope':scope,
                     'state':'RESTORE_REQUIRED' if not exists else 'VALIDATION_REQUIRED','blockers':blockers})
    bots.append({'bot':'devwatch','name':'DEV WATCH', 'account_present':(root/'dev_watch/config.py').is_file(),
                 'state':'MANUAL_SIGNALS' if (root/'dev_watch/config.py').is_file() else 'NOT_INSTALLED',
                 'signal_scope':'Article Grok/X discovery and developer-wallet monitoring; manual fill confirmation',
                 'blockers':[] if (root/'dev_watch/.env').is_file() else ['DEV WATCH credential file missing']})
    return {'credential_file_present':key,'jev_credential_file_present':jev_key,'file_presence_only':True,
            'paid_calls_made':0,'automated_real_money_execution_enabled':False,'bots':bots}

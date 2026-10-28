"""Opt-in paid X judgment, with strict schema, provenance and spending journals."""
from __future__ import annotations
import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlsplit
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from rug_desk import config
from rug_desk.book import atomic, encode, number, process_lock
from rug_desk.prompts import JUDGE_PROMPT
from sniper.adapters.solana import request_json, validate_address
from sniper.advisor import DailyBudget, response_text, response_usage, _unique_object

URL = 'https://api.x.ai/v1/responses'
SEARCHES = {'x_keyword_search', 'x_semantic_search', 'x_user_search', 'x_thread_fetch'}
FIELDS = {'verdict', 'confidence', 'why', 'x_read', 'main_risk'}


def is_x_url(url):
    try:
        parsed = urlsplit(url)
        return parsed.scheme == 'https' and parsed.hostname in ('x.com', 'www.x.com', 'twitter.com', 'www.twitter.com') and not parsed.username and not parsed.password
    except (ValueError, TypeError):
        return False


def validate(response):
    text = response_text(response)
    if len(text) > 12000:
        raise ValueError('Grok response too large')
    out = json.loads(text, object_pairs_hook=_unique_object,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite JSON')))
    if not isinstance(out, dict) or set(out) != FIELDS or out.get('verdict') not in ('BUY', 'SKIP'):
        raise ValueError('Invalid Grok schema/verdict')
    if type(out['confidence']) not in (int, float):
        raise ValueError('Confidence must be a JSON number')
    confidence = number(out['confidence'])
    if confidence > 1:
        raise ValueError('Confidence outside 0..1')
    if any(not isinstance(out[k], str) or not 1 <= len(out[k].strip()) <= 4000 for k in ('why', 'x_read', 'main_risk')):
        raise ValueError('Missing required judgment text')
    calls, citations = [], set()
    for item in response.get('output', []):
        if not isinstance(item, dict):
            continue
        if item.get('type') == 'custom_tool_call' and item.get('name') in SEARCHES:
            if item.get('status') in ('failed', 'incomplete', 'in_progress'):
                raise ValueError('X search did not complete')
            calls.append(item['name'])
        if item.get('type') == 'message':
            for part in item.get('content', []):
                if isinstance(part, dict):
                    for annotation in part.get('annotations', []):
                        if isinstance(annotation, dict) and annotation.get('type') == 'url_citation' and is_x_url(annotation.get('url')):
                            citations.add(annotation['url'])
    usage = response_usage(response)
    if not calls or not usage['cost_verified'] or any(usage[k] is None for k in ('input_tokens', 'output_tokens', 'total_tokens', 'x_posts_fetched', 'x_users_fetched')):
        raise ValueError('Search execution or billed usage is unverified')
    urls = set(re.findall(r'https?://[^\s<>"\)\]]+', ' '.join(out[k] for k in ('why', 'x_read', 'main_risk'))))
    urls = {u.rstrip('.,;') for u in urls}
    if any(not is_x_url(u) or u not in citations for u in urls):
        raise ValueError('Judgment contains an unverified source URL')
    if out['verdict'] == 'BUY' and (usage['x_posts_fetched'] == 0 or not urls or not citations):
        raise ValueError('BUY requires retrieved X posts and cited evidence')
    # Provenance is mechanically checked; semantic entailment remains a model
    # judgment and is retained for human review rather than called verified fact.
    return {**out, 'confidence': str(confidence), 'validated': True,
            'citations': sorted(citations), 'search_calls': calls, 'usage': usage,
            'evidence_limit': 'Citations establish retrieval provenance; factual entailment and controller identity are not independently verified.'}


class Judge:
    def __init__(self, root=config.ROOT, *, enabled=None, transport=request_json, clock=time.time,
                 budget_root=None, billing_account_id=None):
        self.root, self.transport, self.clock = Path(root), transport, clock
        self.enabled = enabled
        self.budget_root = Path(budget_root) if budget_root is not None else self.root
        self.billing_account_id = billing_account_id

    def recurring_enabled(self):
        from rug_desk.model_gate import status
        return status(self.root,self.clock())['enabled']

    def _request(self, payload, validator, context):
        enabled = self.recurring_enabled() if self.enabled is None else self.enabled
        if enabled is not True:
            raise ValueError('Paid model judgment disabled')
        runtime = self.root / 'runtime'
        budget_runtime = self.budget_root / 'runtime'
        if self.billing_account_id is not None:
            if not isinstance(self.billing_account_id, str) or not 1 <= len(self.billing_account_id) <= 120:
                raise ValueError('Invalid billing account identity')
            context = {**context, 'execution_account_id': self.billing_account_id}
        with process_lock(budget_runtime / 'paid-request.lock'):
            now = int(self.clock())
            key = (self.root.parent / 'runtime' / 'xai-api-key.txt').read_text(encoding='utf-8-sig').strip()
            if not key or '\n' in key or '\r' in key:
                raise ValueError('Missing or malformed xAI credential file')
            request_id = 'rug-desk:' + uuid.uuid4().hex
            budget = DailyBudget(budget_runtime / 'ai-budget.sqlite3', Decimal(config.AI_DAILY_LIMIT_USD))
            from rug_desk.model_gate import binding
            # Preserve exactly what the model was allowed to judge, without credentials.
            audit = {'request_id': request_id, 'at': now, 'model': config.MODEL, **context,
                     'binding':binding(self.root),'request':payload}
            try:
                budget.reserve(request_id, Decimal(config.AI_RESERVATION_USD), now)
                if context.get('execution_account_id'):
                    budget.db.execute('CREATE TABLE IF NOT EXISTS advisory_request_accounts(request_id TEXT PRIMARY KEY,account_id TEXT NOT NULL)')
                    budget.db.execute('INSERT INTO advisory_request_accounts VALUES(?,?)',(request_id,context['execution_account_id']))
                    budget.db.commit()
                response = self.transport('POST', URL, payload, {'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'}, 90)
                usage = budget.record_usage(request_id, response)
                audit.update(response=response, usage=usage)
                if not usage['cost_verified']:
                    raise ValueError('Billed usage unavailable')
                result = validator(response)
                audit['validated'] = True
                return {**result, 'request_id': request_id, 'model': config.MODEL,'judged_at':now,'binding':audit['binding']}
            except Exception as exc:
                audit.update(validated=False, error_type=type(exc).__name__)
                raise ValueError('Model judgment unavailable or invalid; inspect redacted response journal') from None
            finally:
                budget.close()
                atomic(runtime / 'x-responses' / (request_id.replace(':', '-') + '.json'), encode(audit))

    def ask(self, mint, facts, quote, *, context=None, execution_account_id=None):
        enabled = self.recurring_enabled() if self.enabled is None else self.enabled
        if enabled is not True:
            return {'verdict': 'SKIP', 'confidence': '0', 'why': 'Paid X judgment disabled; funded access and successful X test required', 'validated': False}
        validate_address(mint)
        schema = {'type': 'object', 'additionalProperties': False, 'required': sorted(FIELDS), 'properties': {
            'verdict': {'type': 'string', 'enum': ['BUY', 'SKIP']},
            'confidence': {'type': 'number', 'minimum': 0, 'maximum': 1},
            **{k: {'type': 'string'} for k in ('why', 'x_read', 'main_risk')}}}
        today = datetime.fromtimestamp(int(self.clock()), timezone.utc).date()
        evidence = {'mint': mint, 'facts': facts, 'pool': quote}
        if context is not None:
            evidence['leader_context'] = context
        payload = {'model': config.MODEL, 'store': False, 'max_output_tokens': 1600, 'max_turns': 2,
            'reasoning': {'effort': 'low'},
            'tools': [{'type': 'x_search', 'from_date': str(today - timedelta(days=3)),
                       'to_date': str(today + timedelta(days=1))}],
            'input': [{'role': 'system', 'content': JUDGE_PROMPT},
                      {'role': 'user', 'content': encode(evidence)}],
            'text': {'format': {'type': 'json_schema', 'name': 'rug_desk_verdict', 'strict': True, 'schema': schema}}}
        audit_context={'mint': mint, 'purpose': 'token_judgment'}
        if execution_account_id is not None:
            if not isinstance(execution_account_id,str) or not 1<=len(execution_account_id)<=120:
                raise ValueError('Invalid execution account audit identity')
            audit_context['execution_account_id']=execution_account_id
        return self._request(payload, validate, audit_context)

    def rank_traders(self, candidates):
        from rug_desk.prompts import TRADER_PROMPT
        from rug_desk.traders import RANK_SCHEMA, validate_rank
        payload = {'model': config.MODEL, 'store': False, 'max_output_tokens': 1200,
            'reasoning': {'effort': 'low'},
            'input': [{'role': 'system', 'content': TRADER_PROMPT},
                      {'role': 'user', 'content': encode({'traders': candidates})}],
            'text': {'format': {'type': 'json_schema', 'name': 'rug_desk_leaders', 'strict': True, 'schema': RANK_SCHEMA}}}
        # Same credential, transport, lock, budget, model and audit as token judgment.
        # No external tools: rank only the supplied evidence, without imagined relationships.
        return self._request(payload, lambda response: validate_rank(response, candidates),
                             {'purpose': 'trader_selection'})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=config.ROOT)
    parser.add_argument('--test-x', metavar='MINT')
    parser.add_argument('--test-inference', action='store_true')
    parser.add_argument('--allow-paid', action='store_true')
    args = parser.parse_args()
    if not args.allow_paid or bool(args.test_x) == args.test_inference:
        parser.error('Choose --test-inference or --test-x MINT, plus --allow-paid; neither enables recurring requests')
    if args.test_inference:
        runtime = args.root / 'runtime'
        with process_lock(runtime / 'paid-request.lock'):
            key = (args.root.parent / 'runtime' / 'xai-api-key.txt').read_text(encoding='utf-8-sig').strip()
            budget = DailyBudget(runtime / 'ai-budget.sqlite3', Decimal(config.AI_DAILY_LIMIT_USD))
            request_id = 'rug-desk-basic:' + uuid.uuid4().hex
            try:
                budget.reserve(request_id, Decimal(config.AI_RESERVATION_USD), int(time.time()))
                response = request_json('POST', URL, {'model': config.MODEL, 'store': False,
                    'max_output_tokens': 64, 'reasoning': {'effort': 'low'},
                    'input': [{'role': 'user', 'content': 'Reply with exactly OK.'}]},
                    {'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'}, 60)
                usage = budget.record_usage(request_id, response)
                text = response_text(response).strip()
                from rug_desk.model_gate import binding
                result = {'model': config.MODEL, 'http_status': 200, 'text': text, 'usage': usage,'binding':binding(args.root),
                          'validated': text == 'OK' and usage['cost_verified'], 'tested_at': int(time.time()),
                          'x_search_verified': False}
                atomic(runtime / 'inference-test.json', encode(result))
                print(encode(result))
            except Exception as exc:
                print(encode({'validated': False, 'error_type': type(exc).__name__, 'reason': 'Inference test failed; reservation retained'}))
                raise SystemExit(1) from None
            finally:
                budget.close()
        return
    result = Judge(args.root, enabled=True).ask(args.test_x, {'test_only': True, 'screens_passed': False}, {'test_only': True})
    result.update(model=config.MODEL, tested_at=int(time.time()), test_only=True)
    atomic(args.root / 'runtime' / 'x-search-test.json', encode(result))
    print(encode(result))


if __name__ == '__main__':
    main()

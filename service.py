"""Sequential homework-only gateway. Airtable ledger + Make delivery.
Run behind HTTPS for Make callbacks. No third-party Python dependencies.
"""
import hashlib
import hmac
import json
import os
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ledger import plan, validate, require, date, eur

TABLES = ('customers', 'invoices', 'events', 'cases', 'actions')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


class Airtable:
    """Each of five tables has key (primary), namespace (text), payload (long text)."""
    def __init__(self):
        self.base = os.environ['AIRTABLE_BASE_ID']
        self.token = os.environ['AIRTABLE_PAT']

    def request(self, table, method='GET', body=None, query=''):
        url='https://api.airtable.com/v0/'+urllib.parse.quote(self.base)+'/'+table+query
        req=urllib.request.Request(url, data=None if body is None else canonical(body).encode(), method=method, headers={'Authorization':'Bearer '+self.token,'Content-Type':'application/json'})
        with urllib.request.urlopen(req, timeout=30) as response:
            result=json.load(response)
        time.sleep(.22)  # Airtable's per-base request budget; no automatic retries.
        return result

    def rows(self, table, namespace=None):
        # Filter locally so untrusted namespace text never enters a formula.
        result=[]; offset=None
        while True:
            query='?pageSize=100'+('&offset='+urllib.parse.quote(offset) if offset else '')
            page=self.request(table,query=query)
            for r in page['records']:
                if namespace is None or r['fields'].get('namespace') == namespace:
                    result.append(r)
            offset=page.get('offset')
            if not offset: return result

    def load(self, namespace):
        d={'namespace':namespace}
        for table in TABLES:
            d[table]=[json.loads(r['fields']['payload']) for r in self.rows(table,namespace)]
        return d

    def put(self, namespace, table, row):
        key=row['ref' if table=='events' else 'id']
        matches=[r for r in self.rows(table,namespace) if json.loads(r['fields']['payload'])['ref' if table=='events' else 'id']==key]
        require(len(matches)<=1,'Duplicate Airtable record')
        fields={'key':namespace+' / '+key,'namespace':namespace,'payload':canonical(row)}
        if matches:
            self.request(table,'PATCH',{'records':[{'id':matches[0]['id'],'fields':fields}]})
        else:
            self.request(table,'POST',{'records':[{'fields':fields}]})


class Engine:
    def __init__(self, store, db_path, recipient):
        require('@' in recipient and '\n' not in recipient and '\r' not in recipient, 'Set dedicated fixed recipient')
        self.store=store; self.recipient=recipient
        self.db=sqlite3.connect(db_path,check_same_thread=False)
        self.db.execute('CREATE TABLE IF NOT EXISTS namespaces (name TEXT PRIMARY KEY, session TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS attempts (id TEXT PRIMARY KEY, session TEXT)')
        self.db.commit()

    def session(self, ns):
        row=self.db.execute('SELECT session FROM namespaces WHERE name=?',(ns,)).fetchone()
        require(row is not None,'Namespace not registered by this gateway')
        return row[0]

    def import_data(self, data, ns, session):
        require(isinstance(session,str) and bool(session.strip()),'Missing review session')
        require(isinstance(ns,str) and bool(ns.strip()),'Missing namespace')
        d=json.loads(canonical(data)); d['namespace']=ns
        require(not d['actions'],'Imports may not forge authorisation or receipt')
        ix=validate(d)
        existing=self.db.execute('SELECT session FROM namespaces WHERE name=?',(ns,)).fetchone()
        require(existing is None,'Use a fresh namespace; do not overwrite prior sessions')
        for table in TABLES:
            require(not self.store.rows(table,ns),'Namespace already exists in ledger')
        for table in TABLES:
            for row in ix[table].values(): self.store.put(ns,table,row)
        # Register only a complete import. Partial remote writes remain inert.
        self.db.execute('INSERT INTO namespaces VALUES (?,?)',(ns,session)); self.db.commit()
        return {'namespace':ns,'imported':True}

    def snapshot(self,d,invoice_id,as_of):
        result=plan(d,as_of)
        r=next((r for r in result['invoices'] if r['id']==invoice_id),None)
        require(r is not None,'Invoice not effective')
        inv=next(i for i in d['invoices'] if i['id']==invoice_id)
        customer=next(c for c in d['customers'] if c['id']==inv['customer_id'])
        require(customer['recipient_alias']=='HOMEWORK_TEST_INBOX','Unknown recipient alias')
        # Include all effective financial facts for this customer, even when net
        # amount is unchanged. Notes are deliberately irrelevant to arithmetic.
        ids={i['id'] for i in d['invoices'] if i['customer_id']==customer['id']}
        financial={
            'invoices':[i for i in d['invoices'] if i['id'] in ids],
            'events':list({e['ref']:e for e in d['events'] if e['invoice_id'] in ids or e['customer_id']==customer['id']}.values()),
            'cases':[c for c in d['cases'] if c['invoice_id'] in ids],
        }
        for table, rows in financial.items():
            for row in rows: row.pop('notes',None)
            rows.sort(key=lambda x:x.get('id',x.get('ref','')))
        return {'invoice_id':invoice_id,'amount_cents':r['eligible_cents'],'recipient':self.recipient,'recipient_alias':customer['recipient_alias'],'as_of':as_of,'due':inv['due'],'facts_hash':hashlib.sha256(canonical(financial).encode()).hexdigest()}

    def action(self,ns,aid):
        self.session(ns)
        d=self.store.load(ns)
        a=next((a for a in d['actions'] if a['id']==aid),None)
        require(a is not None,'Unknown action')
        return d,a

    def prepare(self,ns,iid,as_of):
        self.session(ns); d=self.store.load(ns); s=self.snapshot(d,iid,as_of)
        require(not self.delivery_hold(d,iid,as_of),'Delivery history prevents a new action')
        require(s['amount_cents']>0,'No eligible amount')
        aid='A-'+hashlib.sha256(canonical([ns,s]).encode()).hexdigest()[:24]
        old=next((a for a in d['actions'] if a['id']==aid),None)
        if old: return old
        a=dict(id=aid,invoice_id=iid,date=as_of,state='prepared',snapshot=s,delivery_reference=None)
        self.store.put(ns,'actions',a); return a

    def authorise(self,ns,aid,approved):
        d,a=self.action(ns,aid)
        require(a['state']=='prepared','Only a prepared action can be authorised')
        require(approved==a['snapshot'],'Explicit approval must match complete snapshot')
        require(self.snapshot(d,a['invoice_id'],a['snapshot']['as_of'])==approved,'Facts changed; prepare a new action')
        a['state']='authorised'; self.store.put(ns,'actions',a); return a

    def payment(self,ns,event,as_of):
        self.session(ns); d=self.store.load(ns)
        require(event.get('type')=='payment','This route only appends payments')
        d['events'].append(event); validate(d); plan(d,as_of)
        self.store.put(ns,'events',event)
        return {'appended_or_identical':event['ref']}

    def claim(self,ns,aid):
        """Called by Make immediately before its email module; one-shot claim."""
        d,a=self.action(ns,aid)
        if a['state']!='authorised': return {'send':False,'state':a['state']}
        old=a['snapshot']
        fresh=self.snapshot(d,a['invoice_id'],old['as_of'])
        if fresh!=old or fresh['amount_cents']<=0 or self.delivery_hold(d,a['invoice_id'],old['as_of'],aid):
            a.update(state='cancelled',reason='Current ledger or fixed recipient changed')
            self.store.put(ns,'actions',a); return {'send':False,'state':'cancelled'}
        session=self.session(ns)
        require(self.db.execute('SELECT count(*) FROM attempts WHERE session=?',(session,)).fetchone()[0]<3,'Review session message cap reached')
        # Durable claim BEFORE any send. A crash consumes capacity and blocks retry.
        try:
            self.db.execute('INSERT INTO attempts VALUES (?,?)',(aid,session)); self.db.commit()
        except sqlite3.IntegrityError:
            return {'send':False,'state':'stopped'}
        a.update(state='stopped',reason='Delivery claimed; outcome unknown until provider evidence')
        self.store.put(ns,'actions',a)
        body=f"Fictional test reminder\nInvoice: {old['invoice_id']}\nAmount eligible: {eur(old['amount_cents'])}\nDue date: {old['due']}\nAction reference: {aid}\nThis message is only an accounting exercise."
        return {'send':True,'action_id':aid,'invoice_id':old['invoice_id'],'amount_cents':old['amount_cents'],'due':old['due'],'recipient':self.recipient,'subject':f"[FICTIONAL TEST] {old['invoice_id']} / {aid}",'body':body}

    def delivery_hold(self,d,iid,as_of,exclude=None):
        for a in d['actions']:
            if a['invoice_id']!=iid or a['id']==exclude: continue
            if a['state'] in ('stopped','provider_accepted'): return True
            if a['state']=='authorised' and a.get('webhook_requested'): return True
            # Backdating cannot evade the dispatch interval or unknown outcomes.
            if a['state']=='received' and (date(as_of)-date(a['date'])).days<7: return True
        return False

    def evidence(self,ns,aid,state,reference,received_date=None):
        d,a=self.action(ns,aid); require(bool(reference),'Missing external evidence reference')
        require(self.db.execute('SELECT 1 FROM attempts WHERE id=?',(aid,)).fetchone() is not None,'No dispatch claim')
        if state=='provider_accepted':
            require(a['state']=='stopped','Invalid provider transition')
            a['provider_reference']=reference
        elif state=='received':
            require(a['state'] in ('provider_accepted','stopped'),'Invalid receipt transition')
            date(received_date)
            require(received_date>=a['snapshot']['as_of'],'Receipt business date precedes action')
            a['date']=received_date
            a['receipt_reference']=reference
        else: raise ValueError('Unsupported evidence state')
        a['state']=state; a['delivery_reference']=reference
        self.store.put(ns,'actions',a); return a


LOCK=threading.RLock()


def serve():
    engine=Engine(Airtable(),os.environ.get('CONTROL_DB','control.sqlite3'),os.environ['FIXED_TEST_RECIPIENT'])
    review_token=os.environ['REVIEW_TOKEN']; make_token=os.environ['MAKE_CALLBACK_TOKEN']
    require(len(review_token)>=24 and len(make_token)>=24 and review_token!=make_token,'Use separate random tokens, at least 24 characters')
    session=os.environ['REVIEW_SESSION_ID'] # Cannot be changed by reviewer payloads.
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass # No credentials or payloads in HTTP logs.
        def do_POST(self):
            try:
                callback=self.path in ('/claim','/provider')
                expected=make_token if callback else review_token
                require(hmac.compare_digest(self.headers.get('Authorization',''),'Bearer '+expected),'Unauthorised')
                size=int(self.headers.get('Content-Length','0')); require(0<size<=2000000,'Invalid request size')
                q=json.loads(self.rfile.read(size)); ns=q.get('namespace')
                with LOCK:
                    if self.path != '/import':
                        require(engine.session(ns)==session,'Namespace belongs to another review session')
                    if self.path=='/import': out=engine.import_data(q['data'],ns,session)
                    elif self.path=='/read': engine.session(ns); out=engine.store.load(ns)
                    elif self.path=='/plan':
                        engine.session(ns); data=engine.store.load(ns); out=plan(data,q['as_of'])
                        out['test_recipient']=engine.recipient
                    elif self.path=='/prepare': out=engine.prepare(ns,q['invoice_id'],q['as_of'])
                    elif self.path=='/authorise': out=engine.authorise(ns,q['action_id'],q['approved_snapshot'])
                    elif self.path=='/payment': out=engine.payment(ns,q['event'],q['as_of'])
                    elif self.path=='/claim': out=engine.claim(ns,q['action_id'])
                    elif self.path=='/provider': out=engine.evidence(ns,q['action_id'],'provider_accepted',q['reference'])
                    elif self.path=='/receipt':
                        require(q.get('verified_in_actual_inbox') is True,'Inspect actual inbox first')
                        out=engine.evidence(ns,q['action_id'],'received',q['reference'],q['received_business_date'])
                    elif self.path=='/dispatch':
                        d,a=engine.action(ns,q['action_id'])
                        if a['state']!='authorised' or a.get('webhook_requested'):
                            out={'dispatched':False,'state':a['state']}
                        else:
                            a['webhook_requested']=True; engine.store.put(ns,'actions',a)
                            # Release lock while Make calls back /claim; otherwise deadlock.
                            LOCK.release()
                            try:
                                hook=os.environ['MAKE_WEBHOOK_URL']; require(hook.startswith('https://'),'Make requires HTTPS')
                                req=urllib.request.Request(hook,data=canonical({'namespace':ns,'action_id':q['action_id']}).encode(),headers={'Content-Type':'application/json','x-make-apikey':os.environ['MAKE_WEBHOOK_API_KEY']})
                                with urllib.request.urlopen(req,timeout=30) as response: response.read(65536)
                                out={'webhook_acknowledged':True,'receipt_verified':False}
                            except Exception:
                                with LOCK:
                                    _,a=engine.action(ns,q['action_id'])
                                    if a['state'] not in ('received','provider_accepted','cancelled'):
                                        a.update(state='stopped',reason='Webhook failed or outcome unknown; no retry'); engine.store.put(ns,'actions',a)
                                raise ValueError('Delivery outcome unknown; stopped, no automatic retry')
                            finally: LOCK.acquire()
                    else: raise ValueError('Unknown operation')
                body=canonical(out).encode(); self.send_response(200)
            except Exception as e:
                # Do not expose remote errors which could contain credentials.
                msg=str(e) if isinstance(e,(ValueError,KeyError)) else 'Service error; inspect private configuration'
                body=canonical({'errors':[msg],'sendable_actions':[]}).encode(); self.send_response(400)
            self.send_header('Content-Type','application/json'); self.end_headers(); self.wfile.write(body)
    ThreadingHTTPServer(('127.0.0.1',int(os.environ.get('PORT','8765'))),Handler).serve_forever()


if __name__=='__main__': serve()

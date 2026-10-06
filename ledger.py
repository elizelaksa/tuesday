"""Deterministic collections contract v3. Standard library only; no network."""
import argparse
import datetime as dt
import json


def date(s):
    if not isinstance(s, str) or len(s) != 10:
        raise ValueError('Expected ISO YYYY-MM-DD date')
    d = dt.date.fromisoformat(s)
    if d.isoformat() != s:
        raise ValueError('Noncanonical date')
    return d


def require(ok, message):
    if not ok:
        raise ValueError(message)


def ident(s):
    require(isinstance(s, str) and bool(s.strip()), 'IDs must be nonempty strings')


def integer(n, minimum=0):
    require(type(n) is int and n >= minimum, 'Invalid integer amount or sequence')


def validate(data):
    ident(data['namespace'])
    indexes = {}
    for table in ('customers', 'invoices', 'events', 'cases', 'actions'):
        require(isinstance(data[table], list), table + ' must be an array')
        out = {}
        for row in data[table]:
            key = row['ref' if table == 'events' else 'id']
            ident(key)
            if key in out:
                require(table == 'events' and row == out[key], 'Duplicate/conflicting ID: ' + key)
            out[key] = row
        indexes[table] = out
    customers, invoices = indexes['customers'], indexes['invoices']
    for c in customers.values():
        ident(c['name']); ident(c['recipient_alias'])
    for i in invoices.values():
        require(i['customer_id'] in customers, 'Unknown invoice customer')
        require(date(i['issued']) <= date(i['due']), 'Issue after due date')
        integer(i['amount_cents'], 1)
    seqs = set()
    for e in indexes['events'].values():
        integer(e['seq']); integer(e['amount_cents'], 1); date(e['date'])
        require(e['seq'] not in seqs, 'Duplicate sequence')
        seqs.add(e['seq'])
        require(e['type'] in ('payment', 'credit', 'refund'), 'Unknown event type')
        require(e['customer_id'] is None or e['customer_id'] in customers, 'Unknown event customer')
        if e['invoice_id'] is None:
            require(e['type'] == 'payment', 'Credits/refunds require invoice')
        else:
            require(e['invoice_id'] in invoices, 'Unknown event invoice')
            inv = invoices[e['invoice_id']]
            require(e['customer_id'] == inv['customer_id'], 'Wrong event customer')
            require(date(e['date']) >= date(inv['issued']), 'Event before issue')
    for c in indexes['cases'].values():
        require(c['invoice_id'] in invoices, 'Unknown case invoice')
        require(c['type'] in ('dispute', 'promise'), 'Unknown case type')
        opened = date(c['opened'])
        if c['closed'] is not None:
            require(date(c['closed']) >= opened, 'Case closes before opening')
        integer(c['amount_cents'], 1 if c['type'] == 'dispute' else 0)
        if c['type'] == 'promise':
            require(c['amount_cents'] == 0 and date(c['promised_date']) >= opened, 'Invalid promise')
        else:
            require(c['promised_date'] is None, 'Dispute has promised date')
    promises = [c for c in indexes['cases'].values() if c['type'] == 'promise']
    for n, a in enumerate(promises):
        for b in promises[n+1:]:
            if a['invoice_id'] == b['invoice_id']:
                require(max(a['opened'], b['opened']) >= min(a['closed'] or '9999-12-31', b['closed'] or '9999-12-31'), 'Overlapping active promises')
    for a in indexes['actions'].values():
        require(a['invoice_id'] in invoices, 'Unknown action invoice')
        date(a['date'])
        require(a['state'] in ('prepared', 'authorised', 'provider_accepted', 'received', 'stopped', 'cancelled'), 'Unknown action state')
    return indexes


def plan(data, as_of):
    today = date(as_of)
    ix = validate(data)
    effective = {k: v for k, v in ix['invoices'].items() if v['issued'] <= as_of}
    balances = {k: v['amount_cents'] for k, v in effective.items()}
    received = refunded = unallocated = 0
    held_customers = set()
    for e in sorted(ix['events'].values(), key=lambda e: (e['date'], e['seq'])):
        if e['date'] > as_of:
            continue
        amt, key = e['amount_cents'], e['invoice_id']
        if e['type'] == 'payment':
            received += amt
        elif e['type'] == 'refund':
            require(balances[key] <= -amt, 'Refund exceeds existing invoice credit: ' + e['ref'])
            refunded += amt
        if key is None:
            unallocated += amt
            if e['customer_id'] is not None:
                held_customers.add(e['customer_id'])
        else:
            balances[key] += amt if e['type'] == 'refund' else -amt
    rows = []
    ageing = dict.fromkeys(('current', '1_7', '8_30', '31_plus'), 0)
    for key, inv in effective.items():
        debt, credit = max(balances[key], 0), max(-balances[key], 0)
        days = max(0, (today - date(inv['due'])).days)
        band = 'current' if days == 0 else '1_7' if days <= 7 else '8_30' if days <= 30 else '31_plus'
        active = [c for c in ix['cases'].values() if c['invoice_id'] == key and c['opened'] <= as_of and (c['closed'] is None or c['closed'] > as_of)]
        disputed = min(debt, sum(c['amount_cents'] for c in active if c['type'] == 'dispute'))
        actions = [a for a in ix['actions'].values() if a['invoice_id'] == key and a['date'] <= as_of]
        flags = {
            'not_overdue': days == 0,
            'no_debt': debt == 0,
            'disputed_part': disputed > 0,
            'promise': any(c['type'] == 'promise' and c['promised_date'] >= as_of for c in active),
            'unallocated_payment': inv['customer_id'] in held_customers,
            'delivery_unconfirmed': any(a['state'] in ('provider_accepted', 'stopped') for a in actions),
            'recent_reminder': any(a['state'] == 'received' and 0 <= (today-date(a['date'])).days < 7 for a in actions),
        }
        eligible = debt - disputed
        if any(v for k, v in flags.items() if k != 'disputed_part'):
            eligible = 0
        rows.append(dict(id=key, outstanding_cents=debt, customer_credit_cents=credit, overdue_days=days, age_band=band, disputed_cents=disputed, eligible_cents=eligible, reasons=[k for k,v in flags.items() if v]))
        ageing[band] += debt
    totals = {k: sum(r[k] for r in rows) for k in ('outstanding_cents', 'customer_credit_cents', 'disputed_cents', 'eligible_cents')}
    totals.update(overdue_cents=sum(r['outstanding_cents'] for r in rows if r['overdue_days'] > 0), unallocated_cents=unallocated, cash_received_cents=received, cash_refunded_cents=refunded, net_cash_cents=received-refunded, ageing_cents=ageing)
    return dict(namespace=data['namespace'], as_of=as_of, invoices=rows, totals=totals)


def eur(cents):
    return f'€{cents // 100}.{cents % 100:02d}'


def explain(data, result):
    t = result['totals']
    lines = [f"As of {result['as_of']}: debt {eur(t['outstanding_cents'])}; overdue {eur(t['overdue_cents'])}; eligible {eur(t['eligible_cents'])}. Customer credit {eur(t['customer_credit_cents'])} is separate."]
    for r in result['invoices']:
        lines.append(f"{r['id']}: debt {eur(r['outstanding_cents'])}; credit {eur(r['customer_credit_cents'])}; disputed {eur(r['disputed_cents'])}; eligible {eur(r['eligible_cents'])}; reasons: {', '.join(r['reasons']) or 'overdue undisputed debt'}.")
    lines.append(f"Cash received {eur(t['cash_received_cents'])}; refunded {eur(t['cash_refunded_cents'])}; net cash {eur(t['net_cash_cents'])}; unallocated {eur(t['unallocated_cents'])}. These measures overlap and must not be added together.")
    for e in validate(data)['events'].values():
        if e['type'] == 'refund' and e['date'] <= result['as_of']:
            lines.append(f"{e['invoice_id']} / {e['ref']}: refund {eur(e['amount_cents'])} moves cash out and consumes existing invoice credit. A credit note reduces the invoice balance without moving cash; for a paid cancellation it must precede the refund.")
    pending = [e for e in validate(data)['events'].values() if e['type']=='payment' and e['invoice_id'] is None and e['date'] <= result['as_of']]
    if pending:
        e=max(pending,key=lambda e:e['amount_cents'])
        lines.append(f"Investigate {e['ref']} first: {eur(e['amount_cents'])} unallocated cash for {e['customer_id'] or 'unknown customer'}. Confirm allocation with evidence; do not guess or chase a customer who may already have paid.")
    return '\n'.join(lines) + '\n'


if __name__ == '__main__':
    p=argparse.ArgumentParser(); p.add_argument('file'); p.add_argument('--as-of', required=True); p.add_argument('--explain', action='store_true'); args=p.parse_args()
    try:
        with open(args.file) as f: data=json.load(f)
        result=plan(data,args.as_of)
        print(explain(data,result) if args.explain else json.dumps(result,indent=2))
    except (ValueError, KeyError, TypeError) as e:
        print(json.dumps({'errors':[str(e)],'sendable_actions':[]})); raise SystemExit(1)

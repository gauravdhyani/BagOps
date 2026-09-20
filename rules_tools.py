from __future__ import annotations
import json,re
from decimal import Decimal
from difflib import SequenceMatcher
from database import connect
ROUTES={'DB':'Baggage Tracing','DM':'Baggage Claims Assessment','MC':'Security / Claims Investigation','LP':'Cabin or Airport Lost Property','CR':'Customer Resolution / Finance'}
TERMS={'DB':['never arrived','did not arrive','carousel','delayed bag','missed connecting flight'],'DM':['cracked','broken','torn','jammed handle','damaged suitcase'],'MC':['missing from suitcase','missing from my checked bag','bag opened','contents missing','lighter'],'LP':['seat pocket','left onboard','overhead locker','left on plane'],'CR':['compensation','refund','payout','wrong amount','underpaid','discrepancy','voucher']}
CRITICAL=['medication','medicine','insulin','medical equipment','mobility aid','wheelchair','passport','identity document','safety concern','security concern'];TAMPER=['tampered','bag opened','looks like it was opened','stolen','theft']
def decimal_value(value):
    try:x=Decimal(str(value).upper().replace('USD','').replace('EUR','').replace('GBP','').strip().replace(',','.'));return x if x.is_finite() else None
    except Exception:return None
def rules(text):
    t=str(text or '').lower();scores={k:sum(t.count(x) for x in words) for k,words in TERMS.items()};scores={k:v for k,v in scores.items() if v};rank=sorted(scores,key=lambda k:(-scores[k],k));primary=rank[0] if rank else None;critical=any(x in t for x in CRITICAL);tamper=any(x in t for x in TAMPER);injection=any(x in t for x in ('ignore previous','system prompt','developer message','call the payment tool','manager approved'));administrative=any(x in t for x in ('update contact','change phone','change email'))
    reason=[f'RULE_MATCH_{primary}' if primary else 'NO_RULE_SIGNAL']+(['CRITICAL_ITEM_PRESENT'] if critical else [])+(['TAMPERING_SUSPECTED'] if tamper else [])+(['PROMPT_INJECTION_TEXT'] if injection else [])+(['ADMINISTRATIVE_REQUEST'] if administrative else [])
    return {'primary_category':primary,'secondary_categories':rank[1:],'confidence':0 if not primary else min(.98,.72+.08*scores[primary]-(.2 if injection else 0)),'urgency':'C' if critical else 'H' if tamper or primary in {'DB','MC'} else 'M','is_critical':critical,'is_tampering':tamper,'prompt_injection':injection,'administrative':administrative,'is_ambiguous':not primary or len(rank)>1,'reason_codes':reason}
def _raw(row):
    try:return json.loads(row['raw_input'] or '{}')
    except Exception:return {}
def history(cid):
    c=connect()
    try:
        current=c.execute('SELECT * FROM claims WHERE claim_id=?',(cid,)).fetchone()
        if not current:return {'status':'NOT_FOUND','history_available':False}
        raw=_raw(current);rows=c.execute("SELECT * FROM claims WHERE lower(passenger_name)=lower(?) AND claim_id<>? ORDER BY CASE WHEN submitted_at IS NULL THEN 1 ELSE 0 END,submitted_at,created_at,claim_id",(current['passenger_name'],cid)).fetchall();related=[]
        for r in rows:
            older=bool((r['submitted_at'] and current['submitted_at'] and r['submitted_at']<current['submitted_at']) or (not current['submitted_at'] and r['created_at']<current['created_at']));rr=_raw(r);e=[code for field,code in [('bag_tag','SAME_BAG_TAG'),('flight_number','SAME_FLIGHT'),('travel_date','SAME_TRAVEL_DATE'),('booking_reference','SAME_BOOKING')] if raw.get(field) and raw.get(field)==rr.get(field)];new=any(x in current['body_text'].lower() and x not in r['body_text'].lower() for x in ('medication','passport','receipt','photo','appeal','challenge','new evidence'));chain=c.execute("SELECT 1 FROM claim_relationships WHERE claim_id=? AND relationship_type='EXACT_DUPLICATE'",(r['claim_id'],)).fetchone();exact=older and len(e)>=2 and rules(current['body_text'])['primary_category']==rules(r['body_text'])['primary_category'] and not new and not chain;related.append({'claim_id':r['claim_id'],'older':older,'evidence':e,'similarity':round(SequenceMatcher(None,current['body_text'],r['body_text']).ratio(),3),'identity_match':True,'incident_match':len(e)>=2,'material_new_information':new,'canonical_eligible':not chain,'exact':bool(exact)})
        candidate=next((x for x in related if x['exact']),None);return {'status':'SUCCESS','history_available':True,'duplicate_candidate':candidate['claim_id'] if candidate else None,'canonical_claim_id':candidate['claim_id'] if candidate else None,'duplicate_evidence':candidate or {},'related_claims':related[:3]}
    finally:c.close()
def context(cid):
    c=connect()
    try:r=c.execute('SELECT claim_status,assigned_team FROM claims WHERE claim_id=?',(cid,)).fetchone();acts=[dict(x) for x in c.execute('SELECT actor_type,action_type,status FROM actions WHERE claim_id=? ORDER BY created_at DESC LIMIT 3',(cid,)).fetchall()];return {'status':'SUCCESS' if r else 'NOT_FOUND','claim_status':r['claim_status'] if r else None,'assigned_team':r['assigned_team'] if r else None,'previous_actions':acts}
    finally:c.close()
def tracking(cid):
    c=connect()
    try:r=c.execute('SELECT raw_input FROM claims WHERE claim_id=?',(cid,)).fetchone();raw=_raw(r) if r else {};events=raw.get('tracking_events',{}) if isinstance(raw.get('tracking_events'),dict) else {};return {'status':'SUCCESS' if r else 'NOT_FOUND',**{k:events.get(k) for k in ('passenger_handover','loaded_on_aircraft','transferred','returned_to_passenger')}}
    finally:c.close()
def compensation(cid,name):
    c=connect()
    try:r=c.execute('SELECT * FROM compensation_records WHERE claim_id=?',(cid,)).fetchone()
    finally:c.close()
    if not r:return {'status':'NOT_FOUND','exact_claim_match':False,'exact_identity_match':False}
    out=dict(r);out['hold_flags']=json.loads(out.get('hold_flags') or '[]');out['status']='SUCCESS';out['exact_claim_match']=out.get('claim_id')==cid;out['exact_identity_match']=out.get('passenger_name','').casefold()==name.casefold();return out
TOOLS={'history_tool':history,'context_tool':context,'tracking_tool':tracking,'compensation_tool':compensation}

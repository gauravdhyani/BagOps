from __future__ import annotations
from typing import Any
TERMINAL={'COMPLETED','HUMAN_REVIEW','DRY_RUN','FAILED'}
def accept_result(state,result,now_iso):
    if result['graph_run_id']!=state['graph_run_id']:return False,'STALE_GRAPH_RUN'
    if result['claim_version']!=state['claim_version']:return False,'STALE_CLAIM_VERSION'
    if state['status'] in TERMINAL:return False,'LATE_AFTER_TERMINAL'
    if result['run_id'] in state['accepted_run_ids']:return False,'DUPLICATE_RUN_ID'
    if result['completed_at']>result['deadline_at']:return False,'NODE_DEADLINE_EXCEEDED'
    return True,None
def reduce_results(results:list[dict])->dict[str,Any]:
    by_node={r['node']:r for r in sorted(results,key=lambda x:(x['node'],x['run_id'])) if r['status']=='SUCCESS'}
    rule=by_node.get('rules',{}).get('facts',{});triage=by_node.get('triage_agent',{}).get('facts',{});reassess=by_node.get('reassessment_agent',{}).get('facts',{})
    facts={'critical':bool(rule.get('is_critical')),'tampering':bool(rule.get('is_tampering')),'prompt_injection':bool(rule.get('prompt_injection')),'rules':rule,'triage':triage,'reassessment':reassess,'failed_nodes':[r['node'] for r in results if r['status']=='FAILED'],'sources':{},'conflicts':{'unresolved':{},'resolved':{}}}
    categories={'rules':rule.get('primary_category'),'triage':triage.get('category'),'reassessment':reassess.get('category')};cats={k:v for k,v in categories.items() if v};facts['sources']['category']=cats
    initial={k:v for k,v in cats.items() if k in {'rules','triage'}}
    if len(set(initial.values()))>1:
        final=reassess.get('category');confidence=int(reassess.get('confidence') or 0)
        if final and confidence>=85 and final in set(initial.values()):facts['conflicts']['resolved']['category']={'sources':initial,'resolution':final,'confidence':confidence}
        else:facts['conflicts']['unresolved']['category']=cats
    for name in ('history_tool','context_tool','tracking_tool','compensation_tool'):
        if name in by_node:facts[name]=by_node[name]['facts']
    hist=facts.get('history_tool',{});facts.update({'history_available':hist.get('history_available'),'canonical_claim_id':hist.get('canonical_claim_id'),'duplicate_candidate':hist.get('duplicate_candidate'),'duplicate_evidence':hist.get('duplicate_evidence',{}),'related_claims':hist.get('related_claims',[])})
    comp=facts.get('compensation_tool');facts['compensation_record']=comp
    if comp and reassess.get('currency') and comp.get('currency') and reassess['currency']!=comp['currency']:facts['conflicts']['unresolved']['currency']={'narrative':reassess['currency'],'tool':comp['currency']}
    return facts
def agreement(facts):
    rule=facts.get('rules',{});tri=facts.get('triage',{});rc=rule.get('primary_category');tc=tri.get('category');unresolved=facts.get('conflicts',{}).get('unresolved',{})
    return round((.4 if rc and tc and rc==tc else .2 if not rc and tc else 0)+(.2 if rule.get('urgency')==tri.get('urgency') and rule.get('urgency') else 0)+(.2 if not unresolved else 0)+(.1 if tri.get('disposition','CLAIM')=='CLAIM' else 0)+(.1 if rule.get('reason_codes') and tri.get('reason_codes') else 0),2)

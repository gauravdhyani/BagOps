from database import acquire
from graph import run_agent
def process(ids=None,limit=100,dry_run=False,callback=None,**kwargs):
    claims=acquire(ids,limit);summary={'requested':len(claims),'processed':0,'completed':0,'human_review':0,'dry_run':0,'failed':0,'results':[]}
    for i,claim in enumerate(claims,1):
        if callback:callback({'stage':'RUNNING','index':i,'total':len(claims),'claim_id':claim['claim_id']})
        try:r=run_agent(claim,'DRY_RUN' if dry_run else 'AUTOMATION',dry_run,**kwargs);summary['processed']+=1;summary['completed']+=r['status']=='COMPLETED';summary['human_review']+=r['status']=='HUMAN_REVIEW';summary['dry_run']+=r['status']=='DRY_RUN';summary['results'].append({'claim_id':claim['claim_id'],'status':r['status'],**r['decision']})
        except Exception as error:summary['failed']+=1;summary['results'].append({'claim_id':claim['claim_id'],'status':'FAILED','error':str(error)})
        if callback:callback({'stage':'DONE','index':i,'total':len(claims),'claim_id':claim['claim_id']})
    return summary

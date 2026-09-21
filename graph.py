from __future__ import annotations
import json,time,uuid
from concurrent.futures import ThreadPoolExecutor,as_completed
from datetime import datetime,timedelta,timezone
from config import SETTINGS
from controls import authorize,execute
from database import LeaseOwnershipLost,set_status,tx
from llm_client import invoke
from reducers import accept_result,agreement,reduce_results
from policy_store import active_policy_snapshot
from rules_tools import TOOLS,decimal_value,rules

def now():return datetime.now(timezone.utc)
class Graph:
 def __init__(self,claim,failures=None,delays=None,arrival_order=None):self.claim=claim;self.failures=failures or {};self.delays=delays or {};self.arrival_order=arrival_order;self.gid=f"GR-{uuid.uuid4().hex}";self.token=claim.get("processing_token");self.policy_config_id,self.policy_snapshot=active_policy_snapshot();self.started=time.perf_counter();self.state={"graph_run_id":self.gid,"claim_version":int(claim.get("version",1)),"status":"RUNNING","results":[],"accepted_run_ids":set(),"completed":set(),"failed":{},"rejected":[],"step_count":0,"llm_calls":0,"tool_calls":0,"input_tokens":0,"output_tokens":0,"provider_requests":0,"provider_retries":0,"schema_repairs":0,"cache_hits":0,"fallbacks":[],"llm_available":True}
 def reserve(self,kind,count=1):
  if self.state["step_count"]+count>SETTINGS.max_steps:raise RuntimeError("GRAPH_STEP_LIMIT_EXCEEDED")
  if kind=="AGENT" and self.state["llm_calls"]+count>SETTINGS.max_llm_calls:raise RuntimeError("LLM_CALL_LIMIT_EXCEEDED")
  if kind=="AGENT" and self.state["input_tokens"]+self.state["output_tokens"]>=SETTINGS.max_tokens:raise RuntimeError("CLAIM_TOKEN_BUDGET_EXCEEDED")
  if kind=="TOOL" and self.state["tool_calls"]+count>SETTINGS.max_tool_calls:raise RuntimeError("TOOL_CALL_LIMIT_EXCEEDED")
  self.state["step_count"]+=count;self.state["llm_calls"]+=count if kind=="AGENT" else 0;self.state["tool_calls"]+=count if kind=="TOOL" else 0
 def task(self,node,kind,reason,fn,*args):
  scheduled=now();deadline=scheduled+timedelta(seconds=SETTINGS.node_deadline_seconds);started=time.perf_counter();meta={"cache_hit":False,"provider_requests":0,"provider_retries":0,"schema_repairs":0}
  try:
   if self.delays.get(node):time.sleep(self.delays[node])
   if self.failures.get(node):raise RuntimeError(f"INJECTED_{node.upper()}_FAILURE")
   value=fn(*args)
   if kind=="AGENT":facts,inp,out,duration,meta=value
   else:facts=value;inp=out=0;duration=int((time.perf_counter()-started)*1000)
   status="SUCCESS";error=None
  except Exception as exc:facts={};inp=out=0;duration=int((time.perf_counter()-started)*1000);status="FAILED";error=f"{type(exc).__name__}:{exc}"
  completed=now();return {"node":node,"run_id":f"RUN-{uuid.uuid4().hex}","graph_run_id":self.gid,"claim_version":self.state["claim_version"],"status":status,"kind":kind,"facts":facts,"evidence":facts.get("reason_codes",[]) if isinstance(facts,dict) else [],"error_code":error,"scheduled_at":scheduled.isoformat(),"deadline_at":deadline.isoformat(),"completed_at":completed.isoformat(),"duration_ms":duration,"input_tokens":inp,"output_tokens":out,"attempts":1,"invocation_reason":reason,**meta}
 def persist(self,r,rejection=None):
  run_id=r["run_id"] if not rejection else f"{r['run_id']}-REJ-{uuid.uuid4().hex[:8]}"
  with tx() as c:c.execute("INSERT INTO node_runs(run_id,graph_run_id,claim_id,claim_version,node,node_kind,status,invocation_reason,facts,evidence,error_code,scheduled_at,deadline_at,completed_at,duration_ms,input_tokens,output_tokens,attempts,cache_hit,provider_requests,provider_retries,schema_repairs,rejection_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(run_id,self.gid,self.claim["claim_id"],self.state["claim_version"],r["node"],r["kind"],"REJECTED" if rejection else r["status"],r.get("invocation_reason"),json.dumps({**r["facts"],**({"source_run_id":r["run_id"]} if rejection else {})},default=str),json.dumps(r["evidence"]),rejection or r["error_code"],r["scheduled_at"],r["deadline_at"],r["completed_at"],r["duration_ms"],r["input_tokens"],r["output_tokens"],r["attempts"],int(r.get("cache_hit",False)),r.get("provider_requests",0),r.get("provider_retries",0),r.get("schema_repairs",0),rejection))
 def consume(self,r):
  ok,reason=accept_result(self.state,r,now().isoformat())
  if not ok:self.state["rejected"].append({"run_id":r["run_id"],"reason":reason});self.persist(r,reason);return False
  self.state["accepted_run_ids"].add(r["run_id"]);self.state["results"].append(r);self.state["completed"].add(r["node"]);self.state["input_tokens"]+=r["input_tokens"];self.state["output_tokens"]+=r["output_tokens"];self.state["provider_requests"]+=r.get("provider_requests",0);self.state["provider_retries"]+=r.get("provider_retries",0);self.state["schema_repairs"]+=r.get("schema_repairs",0);self.state["cache_hits"]+=int(r.get("cache_hit",False))
  if r["status"]=="FAILED":self.state["failed"][r["node"]]=r["error_code"]
  if r["kind"]=="AGENT" and r["status"]=="FAILED":self.state["llm_available"]=False;self.state["fallbacks"].append(f"{r['node']}:{r['error_code']}")
  self.persist(r);return True
 def run_parallel(self,tasks):
  for t in tasks:self.reserve(t[1])
  with ThreadPoolExecutor(max_workers=len(tasks)) as pool:done=[f.result() for f in as_completed([pool.submit(self.task,*t) for t in tasks])]
  if self.arrival_order:done.sort(key=lambda r:self.arrival_order.index(r["node"]) if r["node"] in self.arrival_order else 999)
  for r in done:self.consume(r)
 def add(self,*task):self.reserve(task[1]);r=self.task(*task);return r,self.consume(r)
 def compact(self,f):return {"rules":f.get("rules"),"triage":f.get("triage"),"authoritative":{"history_available":f.get("history_available"),"duplicate_candidate":f.get("duplicate_candidate"),"tracking":f.get("tracking_tool"),"compensation":f.get("compensation_record")},"conflicts":f.get("conflicts"),"agreement":agreement(f)}
 def finalize_claim(self,status,reason=None,immediate=False,priority="STANDARD"):return set_status(self.claim["claim_id"],status,reason,immediate,priority,self.token)
def run_agent(claim,trigger="AUTOMATION",dry_run=False,failures=None,delays=None,arrival_order=None):
 if str(claim.get("claim_status","")).strip().lower()=="closed":
  decision={"primary_category":None,"confidence":1.0,"agreement":1.0,"urgency":"L","destination_team":None,"recommended_action":"NO_ACTION","reason_codes":["CLAIM_ALREADY_CLOSED"],"rationale":"The authoritative claim state is already closed. Automation was skipped.","disposition":"CLAIM"}
  return {"graph_run_id":None,"status":"SKIPPED_CLOSED","decision":decision,"policy":{},"facts":{"claim_status":"closed"},"results":[],"rejected":[]}
 g=Graph(claim,failures,delays,arrival_order)
 with tx() as c:c.execute("INSERT INTO graph_runs(graph_run_id,claim_id,claim_version,status,trigger_type,policy_config_id,policy_snapshot) VALUES(?,?,?,'RUNNING',?,?,?)",(g.gid,claim["claim_id"],g.state["claim_version"],trigger,g.policy_config_id,json.dumps(g.policy_snapshot,sort_keys=True)))
 try:
  initial=[("rules","RULE","Independent safety and category verification",rules,claim["body_text"]),("history_tool","TOOL","Mandatory duplicate and related-history verification",TOOLS["history_tool"],claim["claim_id"])]
  if claim.get("ingestion_status")=="VALID":initial.append(("triage_agent","AGENT","Semantic triage for every valid claim",invoke,"triage",claim["body_text"],{}))
  g.run_parallel(initial);facts=reduce_results(g.state["results"]);score=agreement(facts);triage=facts.get("triage",{});rule=facts.get("rules",{});category=triage.get("category") or rule.get("primary_category");action="ROUTE" if category else "HUMAN_REVIEW";destination=g.policy_snapshot["routes"].get(category);decision={"primary_category":category,"confidence":triage.get("confidence",0)/100,"agreement":score,"urgency":triage.get("urgency",rule.get("urgency","M")),"destination_team":destination,"recommended_action":action,"reason_codes":list(dict.fromkeys(rule.get("reason_codes",[])+triage.get("reason_codes",[]))),"rationale":triage.get("rationale",""),"disposition":triage.get("disposition","CLAIM")};facts.update({"agreement":score,"final_category":category,"destination_team":destination,"missing_information":[],"policy_config_id":g.policy_config_id});payload={**decision,"missing_information":[]}
  policy_result,accepted=g.add("policy_gate","POLICY","Deterministic authorization of proposed action",authorize,action,claim,facts,g.gid,g.policy_snapshot,payload,dry_run);policy=policy_result["facts"] if accepted and policy_result["status"]=="SUCCESS" else {"authorization_id":None,"action":action,"authorized":False,"reason_codes":["POLICY_RESULT_UNAVAILABLE"],"policy_config_id":g.policy_config_id}
  if dry_run:status="DRY_RUN";g.finalize_claim("PENDING")
  elif policy.get("authorized"):
   result,accepted=g.add(action.lower(),"ACTION","Authorized action execution",execute,action,claim["claim_id"],g.gid,policy["authorization_id"],f"{claim['claim_id']}:{g.state['claim_version']}:{action}",payload,g.token)
   if not accepted or result["status"]!="SUCCESS":raise RuntimeError("ACTION_RESULT_UNAVAILABLE")
   status="COMPLETED";g.finalize_claim("PROCESSED")
  else:status="HUMAN_REVIEW";g.finalize_claim(status,",".join(policy.get("reason_codes",[])),False,"HIGH");g.add("human_review","REVIEW","Policy denial",lambda:{"status":"QUEUED","reasons":policy.get("reason_codes",[])})
  with tx() as c:c.execute("UPDATE graph_runs SET status=?,step_count=?,llm_calls=?,tool_calls=?,input_tokens=?,output_tokens=?,duration_ms=?,provider_requests=?,provider_retries=?,schema_repairs=?,cache_hits=?,fallbacks=?,decision=?,policy_result=?,completed_at=CURRENT_TIMESTAMP WHERE graph_run_id=?",(status,g.state["step_count"],g.state["llm_calls"],g.state["tool_calls"],g.state["input_tokens"],g.state["output_tokens"],int((time.perf_counter()-g.started)*1000),g.state["provider_requests"],g.state["provider_retries"],g.state["schema_repairs"],g.state["cache_hits"],json.dumps(g.state["fallbacks"]),json.dumps(decision),json.dumps(policy),g.gid))
  return {"graph_run_id":g.gid,"status":status,"decision":decision,"policy":policy,"facts":facts,"results":g.state["results"],"rejected":g.state["rejected"]}
 except Exception as exc:
  if not isinstance(exc,LeaseOwnershipLost):
   try:g.finalize_claim("HUMAN_REVIEW",f"GRAPH_FAILURE:{type(exc).__name__}",False,"HIGH")
   except LeaseOwnershipLost:pass
  with tx() as c:c.execute("UPDATE graph_runs SET status='FAILED',error_code=?,completed_at=CURRENT_TIMESTAMP WHERE graph_run_id=?",(f"{type(exc).__name__}:{exc}",g.gid))
  raise

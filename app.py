from __future__ import annotations
import html, io, json, statistics, uuid, zipfile
from typing import Any
import pandas as pd
import streamlit as st
from config import SETTINGS
from controls import save_override
from database import connect, init_db, tx
from graph import run_agent
from llm_client import test_connection
from processor import process
from policy_store import PROTECTED_GUARDRAILS, active_policy, publish_policy

init_db()
st.set_page_config(page_title="BagOps Operations Control Center",page_icon="🧳",layout="wide",initial_sidebar_state="expanded")
st.markdown("""<style>.block-container{padding-top:1rem;padding-bottom:3rem}.hero{padding:1.35rem 1.6rem;border-radius:18px;background:linear-gradient(120deg,#0f172a,#1d4ed8 58%,#0891b2);color:white;margin-bottom:1rem}.alert-card,.step-card,[data-testid='stMetric']{border:1px solid #e2e8f0;border-radius:13px;padding:.8rem 1rem;margin:.4rem 0}.alert-critical{border-left:7px solid #dc2626}.alert-high{border-left:7px solid #f59e0b}.alert-standard{border-left:7px solid #2563eb}.alert-success{border-left:7px solid #16a34a}.alert-human,.step-policy{border-left:7px solid #7c3aed}.step-failed{border-left:6px solid #dc2626}.step-review{border-left:6px solid #f59e0b}.step-success{border-left:6px solid #16a34a}.small-muted{color:#64748b;font-size:.85rem}.badge{display:inline-block;padding:.15rem .5rem;border-radius:999px;font-weight:700;font-size:.76rem}</style>""",unsafe_allow_html=True)
CATEGORY_LABELS={"DB":"Delayed baggage","DM":"Damaged baggage","MC":"Missing contents","LP":"Lost property","CR":"Compensation request"}
STATUS_HELP={"PENDING":"Waiting for automation.","PROCESSING":"Currently being processed by the graph.","PROCESSED":"Automation completed successfully.","HUMAN_REVIEW":"Automation stopped safely and needs a reviewer.","HUMAN_DECIDED":"A reviewer completed the decision."}
NODE_HELP={"rules":("Rules and Safety Check","Deterministic category and safety checks."),"triage_agent":("Triage Agent","Semantic classification and tool requests."),"history_tool":("Claim History Tool","Related-claim and duplicate evidence."),"context_tool":("Claim Context Tool","Current status and previous actions."),"tracking_tool":("Baggage Tracking Tool","Authoritative baggage events."),"compensation_tool":("Compensation Record Tool","Authoritative compensation record."),"reassessment_agent":("Context Reassessment Agent","One compact reassessment."),"policy_gate":("Deterministic Policy Guard","Authorizes or blocks mutation actions."),"human_review":("Human Review","Queues unsafe or ambiguous work.")}
def query(sql:str,args:tuple[Any,...]=())->pd.DataFrame:
 c=connect()
 try:return pd.read_sql_query(sql,c,params=args)
 finally:c.close()
def hero(t,s):st.markdown(f"<div class='hero'><h1>{html.escape(t)}</h1><p>{html.escape(s)}</p></div>",unsafe_allow_html=True)
def safe_json(v,d):
 if isinstance(v,(dict,list)):return v
 try:return json.loads(v or "")
 except (TypeError,json.JSONDecodeError):return d
def navigate(page,claim_id=None):
 if claim_id:st.session_state["selected_claim_id"]=claim_id
 st.session_state["requested_page"]=page
def alert_class(r):
 if bool(r.get("immediate_attention")):return "alert-critical"
 if r.get("automation_status")=="HUMAN_DECIDED":return "alert-human"
 if r.get("automation_status")=="PROCESSED":return "alert-success"
 if r.get("review_priority") in {"CRITICAL","HIGH"}:return "alert-high"
 return "alert-standard"
def claim_card(r,prefix):
 reason=r.get("attention_reason") or r.get("review_reason") or STATUS_HELP.get(r.get("automation_status"),"Claim requires attention.")
 st.markdown(f"<div class='alert-card {alert_class(r)}'><b>{html.escape(str(r.get('claim_id')))}</b> · {html.escape(str(r.get('passenger_name') or 'Unknown'))}<br>{html.escape(str(reason))}</div>",unsafe_allow_html=True)
 if st.button("Open in Claim Workbench",key=f"{prefix}_{r.get('claim_id')}",width="stretch"):navigate("Claim Workbench",str(r.get("claim_id")));st.rerun()
PAGES=["Overview","Automation","Claims & Alerts","Claim Workbench","Graph Trace","Test Laboratory","Compensation","Assessment Report","Exports","Policy Editor","Configuration","Audit"]
if "page" not in st.session_state:st.session_state["page"]="Overview"
requested=st.session_state.pop("requested_page",None)
if requested in PAGES:st.session_state["page"]=requested
st.sidebar.markdown("## 🧳 BagOps");st.sidebar.caption("Baggage operations and human-review control center");page=st.sidebar.radio("Navigation",PAGES,key="page")
with st.sidebar.expander("Legend",expanded=True):st.markdown("**Categories:** DB, DM, MC, LP, CR  \n**Urgency:** C, H, M, L")
if page=="Overview":
 hero("Operations Dashboard","Queue health, urgent reviews, outcomes, latency, and usage.")
 claims=query("SELECT * FROM claims");runs=query("SELECT * FROM graph_runs")
 # Closed claims are explicitly excluded even if a legacy row still says PENDING.
 active=claims.claim_status.fillna("").astype(str).str.strip().str.lower().ne("closed") if not claims.empty else pd.Series(dtype=bool)
 pending_count=int(((claims.automation_status=="PENDING")&active).sum()) if not claims.empty else 0
 processing_count=int(((claims.automation_status=="PROCESSING")&active).sum()) if not claims.empty else 0
 m=st.columns(7);m[0].metric("All claims",len(claims));m[1].metric("Pending",pending_count);m[2].metric("Processing",processing_count);m[3].metric("Human review",int((claims.automation_status=="HUMAN_REVIEW").sum()) if not claims.empty else 0);m[4].metric("Immediate",int(claims.immediate_attention.sum()) if not claims.empty else 0);m[5].metric("Graph failures",int((runs.status=="FAILED").sum()) if not runs.empty else 0);m[6].metric("Total tokens",int(runs.input_tokens.sum()+runs.output_tokens.sum()) if not runs.empty else 0)
 durations=runs.duration_ms.dropna().astype(int).tolist() if not runs.empty else [];perf=st.columns(4);perf[0].metric("P50 latency",f"{int(statistics.median(durations)) if durations else 0} ms");perf[1].metric("P95 latency",f"{int(sorted(durations)[max(0,int(.95*len(durations))-1)]) if durations else 0} ms");perf[2].metric("Average agent invocations",round(float(runs.llm_calls.mean()),2) if not runs.empty else 0);perf[3].metric("Average tool calls",round(float(runs.tool_calls.mean()),2) if not runs.empty else 0)
 a,b,c=st.tabs(["🚨 Critical alerts","🟠 Human-review queue","🔵 Currently processing"])
 with a:
  f=claims[(claims.immediate_attention==1)&(claims.automation_status=="HUMAN_REVIEW")] if not claims.empty else claims
  if f.empty:st.success("No immediate-attention claims.")
  else:
   for _,r in f.head(10).iterrows():claim_card(r,"critical")
 with b:
  f=claims[(claims.automation_status=="HUMAN_REVIEW")&(claims.immediate_attention==0)] if not claims.empty else claims
  if f.empty:st.success("No standard human-review items.")
  else:
   for _,r in f.head(10).iterrows():claim_card(r,"review")
 with c:st.dataframe(claims[(claims.automation_status=="PROCESSING")&active] if not claims.empty else claims,width="stretch",hide_index=True)
elif page=="Automation":
 hero("Automation","Select eligible claims and run the graph.")
 pending=query("SELECT claim_id,passenger_name,body_text,submitted_at FROM claims WHERE automation_status='PENDING' AND LOWER(TRIM(COALESCE(claim_status,'')))!='closed' ORDER BY submitted_at,claim_id")
 if pending.empty:st.info("No eligible pending claims are available.")
 else:
  pending.insert(0,"Select",False);cols=st.columns([1,1,2]);all_=cols[0].checkbox("Select all pending");dry=cols[1].toggle("Dry-run mode");pending["Select"]=all_;edited=st.data_editor(pending,width="stretch",hide_index=True,disabled=["claim_id","passenger_name","body_text","submitted_at"],key="automation_pending_table");ids=edited.loc[edited.Select,"claim_id"].tolist()
  if st.button("▶ Process selected claims",type="primary",disabled=not ids,width="stretch"):
   progress=st.progress(0.0,text="Preparing selected claims...");box=st.status("Starting BagOps graph...",expanded=True)
   def callback(e):progress.progress(e["index"]/max(e["total"],1),text=f"{e['stage']}: {e['claim_id']} · {e['index']} of {e['total']}");box.write(f"{e['stage']}: `{e['claim_id']}`")
   result=process(ids,len(ids),dry,callback);box.update(label="Batch completed" if not result["failed"] else "Batch completed with failures",state="complete" if not result["failed"] else "error");st.session_state["batch_result"]=result
 if st.session_state.get("batch_result"):
  f=pd.DataFrame(st.session_state["batch_result"]["results"]);st.dataframe(f,width="stretch",hide_index=True);st.download_button("Download batch CSV",f.to_csv(index=False),"bagops_batch_results.csv","text/csv",width="stretch")
elif page=="Claims & Alerts":
 hero("Claims & Alerts","Filter operational states and open a claim.");claims=query("SELECT * FROM claims ORDER BY immediate_attention DESC,review_due_at,submitted_at,claim_id")
 if claims.empty:st.warning("No claims are available.")
 else:
  cols=st.columns(3);states=sorted(claims.automation_status.dropna().unique());selected=cols[0].multiselect("Operational status",states,default=states);critical=cols[1].toggle("Critical alerts only");search=cols[2].text_input("Search claim or passenger");f=claims[claims.automation_status.isin(selected)]
  if critical:f=f[f.immediate_attention==1]
  if search:f=f[f.claim_id.astype(str).str.contains(search,case=False,na=False)|f.passenger_name.astype(str).str.contains(search,case=False,na=False)]
  st.dataframe(f,width="stretch",hide_index=True);cid=st.selectbox("Open a claim",f.claim_id.tolist() or [None])
  if cid and st.button("Open selected claim",width="stretch"):navigate("Claim Workbench",cid);st.rerun()
elif page=="Claim Workbench":
 hero("Claim Workbench","Inspect decisions and record accountable human overrides.");claims=query("SELECT * FROM claims ORDER BY claim_id")
 if claims.empty:st.stop()
 ids=claims.claim_id.astype(str).tolist();preferred=st.session_state.get("selected_claim_id");preferred=preferred if preferred in ids else ids[0];cid=st.selectbox("Claim",ids,index=ids.index(preferred));st.session_state["selected_claim_id"]=cid;r=claims[claims.claim_id.astype(str)==cid].iloc[0];raw=safe_json(r.raw_input,{})
 st.markdown(f"<div class='alert-card {alert_class(r)}'><b>Status:</b> {html.escape(str(r.automation_status))} · <b>Claim status:</b> {html.escape(str(r.claim_status))}</div>",unsafe_allow_html=True);x,y=st.columns(2);x.text_area("Original submitted text",str(raw.get("body_text") or raw.get("claim_text") or r.body_text),height=160,disabled=True);y.text_area("Normalized text used by the graph",str(r.body_text),height=160,disabled=True)
 latest=query("SELECT * FROM graph_runs WHERE claim_id=? ORDER BY created_at DESC LIMIT 1",(cid,))
 if latest.empty:st.info("This claim has not been processed.")
 else:
  run=latest.iloc[0];decision=safe_json(run.decision,{});policy=safe_json(run.policy_result,{});x,y=st.columns(2)
  with x:st.subheader("Automated decision");st.json(decision)
  with y:st.subheader("Protected policy result");st.json(policy)
  st.dataframe(query("SELECT * FROM node_runs WHERE graph_run_id=? ORDER BY completed_at",(run.graph_run_id,)),width="stretch",hide_index=True)
  with st.form("human_override_form"):
   reviewer=st.text_input("Reviewer name *");reason=st.text_area("Reason for decision *");team=st.text_input("Destination team",decision.get("destination_team") or "");action=st.selectbox("Human action",["HUMAN_ROUTE","HUMAN_ACKNOWLEDGE","HUMAN_CLOSE","HUMAN_REOPEN","HUMAN_REQUEST_INFORMATION","HUMAN_NO_ACTION"]);submitted=st.form_submit_button("Save human decision",type="primary")
  if submitted:
   try:save_override(cid,run.graph_run_id,reviewer,{"destination_team":team},action,reason);st.success("Human decision saved.");st.rerun()
   except Exception as e:st.error(str(e))
elif page=="Graph Trace":
 hero("Graph Trace","Inspect accepted steps and structured evidence.");runs=query("SELECT * FROM graph_runs ORDER BY created_at DESC");options=runs.apply(lambda r:f"{r.graph_run_id} | {r.claim_id}",axis=1).tolist() if not runs.empty else [None];selected=st.selectbox("Graph run",options);run_id=selected.split(" | ",1)[0] if selected else None
 if run_id:
  run=runs[runs.graph_run_id==run_id].iloc[0];st.json({k:getattr(run,k) for k in ["status","step_count","llm_calls","tool_calls","input_tokens","output_tokens","duration_ms"]});nodes=query("SELECT * FROM node_runs WHERE graph_run_id=? ORDER BY completed_at",(run_id,))
  for i,n in nodes.iterrows():
   title,desc=NODE_HELP.get(n.node,(n.node.replace("_"," ").title(),"Persisted graph result."));st.markdown(f"<div class='step-card'><b>Step {i+1}: {html.escape(title)}</b> · {html.escape(str(n.status))}<br>{html.escape(desc)}</div>",unsafe_allow_html=True)
   with st.expander(f"View structured evidence for {title}"):st.json(safe_json(n.facts,{}));st.write("Evidence:",safe_json(n.evidence,[]))
elif page=="Test Laboratory":
 hero("Test Laboratory","Simulate the graph in dry-run mode.");text=st.text_area("Claim text","My checked bag never arrived at the carousel.",height=130)
 if st.button("Run laboratory case",type="primary",width="stretch"):
  cid=f"BC-{uuid.uuid4().int%9000+1000}";claim={"claim_id":cid,"passenger_name":"Laboratory","body_text":text,"raw_input":json.dumps({"body_text":text}),"input_hash":"laboratory","ingestion_status":"VALID","version":1,"claim_status":"new"}
  with tx() as c:c.execute("INSERT INTO claims(claim_id,passenger_name,claim_status,body_text,raw_input,input_hash,ingestion_status,validation_errors,automation_status) VALUES(?,?,?,?,?,?,?,'[]','PROCESSING')",(cid,"Laboratory","new",text,claim["raw_input"],"laboratory","VALID"))
  st.session_state["laboratory_result"]=run_agent(claim,"LAB",True)
 if st.session_state.get("laboratory_result"):st.json(st.session_state["laboratory_result"])
elif page=="Compensation":hero("Compensation","Authoritative records and simulated resolutions.");st.dataframe(query("SELECT * FROM compensation_records"),width="stretch",hide_index=True);st.dataframe(query("SELECT * FROM courtesy_resolutions"),width="stretch",hide_index=True)
elif page=="Assessment Report":hero("Assessment Report","Graph status and usage.");report=query("SELECT * FROM graph_runs ORDER BY created_at DESC");st.dataframe(report,width="stretch",hide_index=True);st.download_button("Download assessment CSV",report.to_csv(index=False),"bagops_assessment_report.csv","text/csv",width="stretch")
elif page=="Exports":
 hero("Exports","Download operational evidence.");tables=["claims","graph_runs","node_runs","classifications","actions","communications","courtesy_resolutions","human_overrides","audit_events","configuration_versions","regression_cases"];selected=st.multiselect("Tables to export",tables,default=tables[:5]);frames={n:query(f"SELECT * FROM {n}") for n in selected}
 def build_zip():
  b=io.BytesIO()
  with zipfile.ZipFile(b,"w",zipfile.ZIP_DEFLATED) as z:
   z.writestr("manifest.json",json.dumps({"datasets":{n:len(f) for n,f in frames.items()},"policy":active_policy(),"protected_guardrails":PROTECTED_GUARDRAILS},indent=2))
   for n,f in frames.items():z.writestr(f"{n}.csv",f.to_csv(index=False));z.writestr(f"{n}.json",f.to_json(orient="records",indent=2,date_format="iso"))
  return b.getvalue()
 if frames:st.download_button("Download complete evidence ZIP",build_zip(),"bagops_evidence_package.zip","application/zip",width="stretch")
elif page=="Policy Editor":
 hero("Policy Editor","Version editable policy.");policy=active_policy();st.json(policy);author=st.text_input("Policy author *")
 if st.button("Republish current policy",type="primary"):
  try:st.success(f"Published policy version {publish_policy(policy,author)}")
  except Exception as e:st.error(str(e))
elif page=="Configuration":hero("Configuration","Provider connectivity and execution budgets.");settings=dict(SETTINGS.__dict__);settings["api_key"]="*** configured ***" if SETTINGS.api_key else "not configured";st.json(settings);st.json(test_connection()) if st.button("Test LLM connection",type="primary",width="stretch") else None
else:
 hero("Audit","Append-only workflow events, actions, and human decisions.");tabs=st.tabs(["Audit events","Actions","Human overrides","Saved regression cases"])
 for tab,table in zip(tabs,["audit_events","actions","human_overrides","regression_cases"]):
  with tab:st.dataframe(query(f"SELECT * FROM {table} ORDER BY created_at DESC"),width="stretch",hide_index=True)

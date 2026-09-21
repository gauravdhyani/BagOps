from __future__ import annotations

import html
import io
import json
import statistics
import uuid
import zipfile
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
st.set_page_config(page_title="BagOps Operations Control Center", page_icon="🧳", layout="wide", initial_sidebar_state="expanded")
st.markdown("""<style>.block-container{padding-top:1rem;padding-bottom:3rem}.hero{padding:1.35rem 1.6rem;border-radius:18px;background:linear-gradient(120deg,#0f172a,#1d4ed8 58%,#0891b2);color:white;margin-bottom:1rem}.alert-card,.step-card,[data-testid='stMetric']{border:1px solid #e2e8f0;border-radius:13px;padding:.8rem 1rem}.alert-critical{border-left:7px solid #dc2626}.alert-high{border-left:7px solid #f59e0b}.alert-standard{border-left:7px solid #2563eb}.alert-success{border-left:7px solid #16a34a}.alert-human,.step-policy{border-left:7px solid #7c3aed}.step-failed{border-left:6px solid #dc2626}.step-review{border-left:6px solid #f59e0b}.step-success{border-left:6px solid #16a34a}.small-muted{color:#64748b;font-size:.85rem}.badge{display:inline-block;padding:.15rem .5rem;border-radius:999px;font-weight:700;font-size:.76rem}</style>""", unsafe_allow_html=True)

CATEGORY_LABELS={"DB":"Delayed baggage","DM":"Damaged baggage","MC":"Missing contents","LP":"Lost property","CR":"Compensation request"}
URGENCY_LABELS={"C":"Critical","H":"High","M":"Medium","L":"Low"}
STATUS_HELP={"PENDING":"Waiting for automation.","PROCESSING":"Currently being processed by the graph.","PROCESSED":"Automation completed successfully.","HUMAN_REVIEW":"Automation stopped safely and needs a reviewer.","HUMAN_DECIDED":"A reviewer completed the decision."}
NODE_HELP={"rules":("Rules and Safety Check","Deterministic category and safety checks."),"triage_agent":("Triage Agent","Semantic classification and tool requests."),"history_tool":("Claim History Tool","Related-claim and duplicate evidence."),"context_tool":("Claim Context Tool","Current status and previous actions."),"tracking_tool":("Baggage Tracking Tool","Authoritative baggage events."),"compensation_tool":("Compensation Record Tool","Authoritative compensation record."),"reassessment_agent":("Context Reassessment Agent","One compact reassessment."),"policy_gate":("Deterministic Policy Guard","Authorizes or blocks mutation actions."),"human_review":("Human Review","Queues unsafe or ambiguous work.")}


def query(sql:str,args:tuple[Any,...]=())->pd.DataFrame:
    connection=connect()
    try:return pd.read_sql_query(sql,connection,params=args)
    finally:connection.close()

def hero(title,subtitle):st.markdown(f"<div class='hero'><h1>{html.escape(title)}</h1><p>{html.escape(subtitle)}</p></div>",unsafe_allow_html=True)
def safe_json(value,default):
    if isinstance(value,(dict,list)):return value
    try:return json.loads(value or "")
    except (TypeError,json.JSONDecodeError):return default

def navigate(page_name,claim_id=None):
    if claim_id:st.session_state["selected_claim_id"]=claim_id
    st.session_state["requested_page"]=page_name

def category_badge(category):return f"<span class='badge'>{html.escape(str(category or 'Unclassified'))} · {html.escape(CATEGORY_LABELS.get(category,''))}</span>"
def alert_class(row):
    if bool(row.get("immediate_attention")):return "alert-critical"
    if row.get("automation_status")=="HUMAN_DECIDED":return "alert-human"
    if row.get("automation_status")=="PROCESSED":return "alert-success"
    if row.get("review_priority") in {"CRITICAL","HIGH"}:return "alert-high"
    return "alert-standard"
def render_claim_card(row,prefix):
    reason=row.get("attention_reason") or row.get("review_reason") or STATUS_HELP.get(row.get("automation_status"),"Claim requires attention.")
    st.markdown(f"<div class='alert-card {alert_class(row)}'><b>{html.escape(str(row.get('claim_id')))}</b> · {html.escape(str(row.get('passenger_name') or 'Unknown'))}<br>{html.escape(str(reason))}</div>",unsafe_allow_html=True)
    if st.button("Open in Claim Workbench",key=f"{prefix}_{row.get('claim_id')}",width="stretch"):navigate("Claim Workbench",str(row.get("claim_id")));st.rerun()

PAGES=["Overview","Automation","Claims & Alerts","Claim Workbench","Graph Trace","Test Laboratory","Compensation","Assessment Report","Exports","Policy Editor","Configuration","Audit"]
if "page" not in st.session_state:st.session_state["page"]="Overview"
requested=st.session_state.pop("requested_page",None)
if requested in PAGES:st.session_state["page"]=requested
st.sidebar.markdown("## 🧳 BagOps");st.sidebar.caption("Baggage operations and human-review control center")
page=st.sidebar.radio("Navigation",PAGES,key="page")
with st.sidebar.expander("Legend",expanded=True):st.markdown("**Categories:** DB, DM, MC, LP, CR  \n**Urgency:** C, H, M, L  \n**Colors:** red critical, amber review, blue pending, green completed, purple human/policy")

if page=="Overview":
    hero("Operations Dashboard","Queue health, urgent reviews, outcomes, latency, and usage.")
    claims=query("SELECT * FROM claims");runs=query("SELECT * FROM graph_runs")
    metrics=st.columns(7); metrics[0].metric("All claims",len(claims)); metrics[1].metric("Pending",int((claims.automation_status=="PENDING").sum()) if not claims.empty else 0); metrics[2].metric("Processing",int((claims.automation_status=="PROCESSING").sum()) if not claims.empty else 0); metrics[3].metric("Human review",int((claims.automation_status=="HUMAN_REVIEW").sum()) if not claims.empty else 0); metrics[4].metric("Immediate",int(claims.immediate_attention.sum()) if not claims.empty else 0); metrics[5].metric("Graph failures",int((runs.status=="FAILED").sum()) if not runs.empty else 0); metrics[6].metric("Total tokens",int(runs.input_tokens.sum()+runs.output_tokens.sum()) if not runs.empty else 0)
    durations=runs.duration_ms.dropna().astype(int).tolist() if not runs.empty else [];perf=st.columns(4);perf[0].metric("P50 latency",f"{int(statistics.median(durations)) if durations else 0} ms");perf[1].metric("P95 latency",f"{int(sorted(durations)[max(0,int(.95*len(durations))-1)]) if durations else 0} ms");perf[2].metric("Average agent invocations",round(float(runs.llm_calls.mean()),2) if not runs.empty else 0);perf[3].metric("Average tool calls",round(float(runs.tool_calls.mean()),2) if not runs.empty else 0)
    critical,review,processing=st.tabs(["🚨 Critical alerts","🟠 Human-review queue","🔵 Currently processing"])
    with critical:
        frame=claims[(claims.immediate_attention==1)&(claims.automation_status=="HUMAN_REVIEW")] if not claims.empty else claims
        if frame.empty:st.success("No immediate-attention claims.")
        else:
            for _,row in frame.head(10).iterrows():render_claim_card(row,"critical")
    with review:
        frame=claims[(claims.automation_status=="HUMAN_REVIEW")&(claims.immediate_attention==0)] if not claims.empty else claims
        if frame.empty:st.success("No standard human-review items.")
        else:
            for _,row in frame.head(10).iterrows():render_claim_card(row,"review")
    with processing:st.dataframe(claims[claims.automation_status=="PROCESSING"] if not claims.empty else claims,width="stretch",hide_index=True)

elif page=="Automation":
    hero("Automation","Select eligible claims and run the graph.")
    pending=query("SELECT claim_id,passenger_name,body_text,submitted_at FROM claims WHERE automation_status='PENDING' AND LOWER(COALESCE(claim_status,''))!='closed' ORDER BY submitted_at,claim_id")
    if pending.empty:st.info("No eligible pending claims are available.")
    else:
        pending.insert(0,"Select",False);top=st.columns([1,1,2]);select_all=top[0].checkbox("Select all pending");dry_run=top[1].toggle("Dry-run mode");pending["Select"]=select_all
        edited=st.data_editor(pending,width="stretch",hide_index=True,disabled=["claim_id","passenger_name","body_text","submitted_at"],key="automation_pending_table");selected_ids=edited.loc[edited.Select,"claim_id"].tolist()
        if st.button("▶ Process selected claims",type="primary",disabled=not selected_ids,width="stretch"):
            progress=st.progress(0.0,text="Preparing selected claims...");status=st.status("Starting BagOps graph...",expanded=True)
            def callback(event): progress.progress(event["index"]/max(event["total"],1),text=f"{event['stage']}: {event['claim_id']} · {event['index']} of {event['total']}");status.write(f"{event['stage']}: `{event['claim_id']}`")
            batch=process(selected_ids,len(selected_ids),dry_run,callback);status.update(label="Batch completed" if not batch["failed"] else "Batch completed with failures",state="complete" if not batch["failed"] else "error");st.session_state["batch_result"]=batch
    if st.session_state.get("batch_result"):
        frame=pd.DataFrame(st.session_state["batch_result"]["results"]);st.dataframe(frame,width="stretch",hide_index=True);st.download_button("Download batch CSV",frame.to_csv(index=False),"bagops_batch_results.csv","text/csv",width="stretch")

elif page=="Claims & Alerts":
    hero("Claims & Alerts","Filter operational states and open a claim.");claims=query("SELECT * FROM claims ORDER BY immediate_attention DESC,review_due_at,submitted_at,claim_id")
    if claims.empty:st.warning("No claims are available.")
    else:
        cols=st.columns(3);states=sorted(claims.automation_status.dropna().unique());selected=cols[0].multiselect("Operational status",states,default=states);critical=cols[1].toggle("Critical alerts only");search=cols[2].text_input("Search claim or passenger");filtered=claims[claims.automation_status.isin(selected)]
        if critical:filtered=filtered[filtered.immediate_attention==1]
        if search:filtered=filtered[filtered.claim_id.astype(str).str.contains(search,case=False,na=False)|filtered.passenger_name.astype(str).str.contains(search,case=False,na=False)]
        st.dataframe(filtered,width="stretch",hide_index=True);claim_id=st.selectbox("Open a claim",filtered.claim_id.tolist() or [None])
        if claim_id and st.button("Open selected claim",width="stretch"):navigate("Claim Workbench",claim_id);st.rerun()

elif page=="Claim Workbench":
    hero("Claim Workbench","Inspect decisions and record accountable human overrides.");claims=query("SELECT * FROM claims ORDER BY claim_id")
    if claims.empty:st.stop()
    ids=claims.claim_id.astype(str).tolist();preferred=st.session_state.get("selected_claim_id");preferred=preferred if preferred in ids else ids[0];claim_id=st.selectbox("Claim",ids,index=ids.index(preferred));st.session_state["selected_claim_id"]=claim_id;row=claims[claims.claim_id.astype(str)==claim_id].iloc[0];raw=safe_json(row.raw_input,{})
    st.markdown(f"<div class='alert-card {alert_class(row)}'><b>Status:</b> {html.escape(str(row.automation_status))} · <b>Priority:</b> {html.escape(str(row.review_priority))}</div>",unsafe_allow_html=True);left,right=st.columns(2);left.text_area("Original submitted text",str(raw.get("body_text") or raw.get("claim_text") or row.body_text),height=160,disabled=True);right.text_area("Normalized text used by the graph",str(row.body_text),height=160,disabled=True)
    latest=query("SELECT * FROM graph_runs WHERE claim_id=? ORDER BY created_at DESC LIMIT 1",(claim_id,))
    if latest.empty:st.info("This claim has not been processed.")
    else:
        run=latest.iloc[0];decision=safe_json(run.decision,{});policy=safe_json(run.policy_result,{});left,right=st.columns(2)
        with left:st.subheader("Automated decision");st.markdown(category_badge(decision.get("primary_category")),unsafe_allow_html=True);st.json(decision)
        with right:st.subheader("Protected policy result");st.json(policy)
        st.dataframe(query("SELECT * FROM node_runs WHERE graph_run_id=? ORDER BY completed_at",(run.graph_run_id,)),width="stretch",hide_index=True)
        with st.form("human_override_form"):
            reviewer=st.text_input("Reviewer name *");reason=st.text_area("Reason for decision *");team=st.text_input("Destination team",decision.get("destination_team") or "");action=st.selectbox("Human action",["HUMAN_ROUTE","HUMAN_ACKNOWLEDGE","HUMAN_CLOSE","HUMAN_REOPEN","HUMAN_REQUEST_INFORMATION","HUMAN_NO_ACTION"]);submitted=st.form_submit_button("Save human decision",type="primary")
        if submitted:
            try:save_override(claim_id,run.graph_run_id,reviewer,{"destination_team":team},action,reason);st.success("Human decision saved.");st.rerun()
            except Exception as error:st.error(str(error))

elif page=="Graph Trace":
    hero("Graph Trace","Inspect accepted steps and structured evidence.");runs=query("SELECT * FROM graph_runs ORDER BY created_at DESC")
    run_options=runs.apply(lambda row:f"{row.graph_run_id} | {row.claim_id}",axis=1).tolist() if not runs.empty else [None]
    selected_run=st.selectbox("Graph run",run_options);selected_run_id=selected_run.split(" | ",1)[0] if selected_run else None
    if selected_run_id:
        graph_run=runs[runs.graph_run_id==selected_run_id].iloc[0];proof=st.columns(7)
        for col,label,value in zip(proof,["Status","Steps","LLM calls","Tool calls","Input tokens","Output tokens","Duration"],[graph_run.status,graph_run.step_count,graph_run.llm_calls,graph_run.tool_calls,graph_run.input_tokens,graph_run.output_tokens,f"{graph_run.duration_ms} ms"]):col.metric(label,value)
        nodes=query("SELECT * FROM node_runs WHERE graph_run_id=? ORDER BY completed_at",(selected_run_id,))
        for index,node in nodes.iterrows():
            title,explanation=NODE_HELP.get(node.node,(node.node.replace("_"," ").title(),"Persisted graph result."));css="step-policy" if node.node=="policy_gate" else "step-review" if node.node=="human_review" else "step-failed" if node.status in {"FAILED","REJECTED"} else "step-success";st.markdown(f"<div class='step-card {css}'><b>Step {index+1}: {html.escape(title)}</b> · {html.escape(str(node.status))}<br><span class='small-muted'>{html.escape(explanation)}</span></div>",unsafe_allow_html=True)
            with st.expander(f"View structured evidence for {title}"):st.json(safe_json(node.facts,{}));st.write("Evidence:",safe_json(node.evidence,[]))

elif page=="Test Laboratory":
    hero("Test Laboratory","Simulate failures, latency, and completion order in dry-run mode.");claim_text=st.text_area("Claim text","My checked bag never arrived at the carousel.",height=130);failures=st.multiselect("Inject failures",["rules","history_tool","triage_agent","context_tool","tracking_tool","compensation_tool","reassessment_agent"]);delay=st.slider("Artificial history delay in seconds",0,3,0)
    if st.button("Run laboratory case",type="primary",width="stretch"):
        claim_id=f"BC-{uuid.uuid4().int%9000+1000}";claim={"claim_id":claim_id,"passenger_name":"Laboratory","body_text":claim_text,"raw_input":json.dumps({"body_text":claim_text}),"input_hash":"laboratory","ingestion_status":"VALID","version":1,"claim_status":"new"}
        with tx() as connection:connection.execute("INSERT INTO claims(claim_id,passenger_name,claim_status,body_text,raw_input,input_hash,ingestion_status,validation_errors,automation_status) VALUES(?,?,?,?,?,?,?,'[]','PROCESSING')",(claim_id,"Laboratory","new",claim_text,claim["raw_input"],"laboratory","VALID"))
        st.session_state["laboratory_result"]=run_agent(claim,"LAB",True,{name:True for name in failures},{"history_tool":delay},None)
    if st.session_state.get("laboratory_result"):st.json(st.session_state["laboratory_result"])

elif page=="Compensation":hero("Compensation","Authoritative records and simulated resolutions.");st.dataframe(query("SELECT * FROM compensation_records"),width="stretch",hide_index=True);st.dataframe(query("SELECT * FROM courtesy_resolutions"),width="stretch",hide_index=True)
elif page=="Assessment Report":
    hero("Assessment Report","Graph status, latency, usage, decisions, and policy outcomes.");report=query("SELECT * FROM graph_runs ORDER BY created_at DESC");st.dataframe(report,width="stretch",hide_index=True);st.download_button("Download assessment CSV",report.to_csv(index=False),"bagops_assessment_report.csv","text/csv",width="stretch")
elif page=="Exports":
    hero("Exports","Download operational evidence.");tables=["claims","graph_runs","node_runs","classifications","actions","communications","courtesy_resolutions","human_overrides","audit_events","configuration_versions","regression_cases"];selection=st.multiselect("Tables to export",tables,default=tables[:5]);frames={name:query(f"SELECT * FROM {name}") for name in selection}
    def build_zip():
        buffer=io.BytesIO()
        with zipfile.ZipFile(buffer,"w",zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json",json.dumps({"datasets":{name:len(frame) for name,frame in frames.items()},"policy":active_policy(),"protected_guardrails":PROTECTED_GUARDRAILS},indent=2))
            for name,frame in frames.items():archive.writestr(f"{name}.csv",frame.to_csv(index=False));archive.writestr(f"{name}.json",frame.to_json(orient="records",indent=2,date_format="iso"))
        return buffer.getvalue()
    if frames:st.download_button("Download complete evidence ZIP",build_zip(),"bagops_evidence_package.zip","application/zip",width="stretch")
elif page=="Policy Editor":
    hero("Policy Editor","Version editable policy while protected guardrails stay read-only.");policy=active_policy();st.json(policy);author=st.text_input("Policy author *")
    if st.button("Republish current policy",type="primary"):
        try:st.success(f"Published policy version {publish_policy(policy,author)}")
        except Exception as error:st.error(str(error))
elif page=="Configuration":
    hero("Configuration","Provider connectivity and execution budgets.");settings=dict(SETTINGS.__dict__);settings["api_key"]="*** configured ***" if SETTINGS.api_key else "not configured";st.json(settings)
    if st.button("Test LLM connection",type="primary",width="stretch"):st.json(test_connection())
else:
    hero("Audit","Append-only workflow events, actions, and human decisions.");tabs=st.tabs(["Audit events","Actions","Human overrides","Saved regression cases"])
    for tab,table in zip(tabs,["audit_events","actions","human_overrides","regression_cases"]):
        with tab:st.dataframe(query(f"SELECT * FROM {table} ORDER BY created_at DESC"),width="stretch",hide_index=True)

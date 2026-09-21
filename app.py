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
st.markdown("""
<style>
.block-container {padding-top:1rem;padding-bottom:3rem}
.hero {padding:1.35rem 1.6rem;border-radius:18px;background:linear-gradient(120deg,#0f172a,#1d4ed8 58%,#0891b2);color:white;margin-bottom:1rem;box-shadow:0 12px 30px rgba(15,23,42,.15)}
.hero h1 {margin:0 0 .3rem 0;font-size:1.85rem}.hero p{margin:0;opacity:.92}
.alert-card{border:1px solid #e2e8f0;border-left-width:7px;border-radius:13px;padding:.9rem 1rem;margin:.45rem 0;background:transparent}
.alert-critical{border-left-color:#dc2626}.alert-high{border-left-color:#f59e0b}.alert-standard{border-left-color:#2563eb}.alert-success{border-left-color:#16a34a}.alert-human{border-left-color:#7c3aed}
.badge{display:inline-block;padding:.15rem .5rem;margin-right:.25rem;border-radius:999px;font-weight:700;font-size:.76rem}.badge-db{color:#1e40af}.badge-dm{color:#9a3412}.badge-mc{color:#991b1b}.badge-lp{color:#5b21b6}.badge-cr{color:#166534}.badge-muted{color:#334155}
.step-card{border:1px solid #dbe4f0;border-radius:13px;padding:.8rem 1rem;margin-bottom:.45rem;background:transparent}.step-success{border-left:6px solid #16a34a}.step-failed{border-left:6px solid #dc2626}.step-review{border-left:6px solid #f59e0b}.step-policy{border-left:6px solid #7c3aed}.small-muted{color:#64748b;font-size:.85rem}
[data-testid="stMetric"]{border:1px solid #e2e8f0;border-radius:13px;padding:.75rem;background:transparent}
</style>
""", unsafe_allow_html=True)

CATEGORY_LABELS={"DB":"Delayed baggage","DM":"Damaged baggage","MC":"Missing contents","LP":"Lost property","CR":"Compensation request"}
URGENCY_LABELS={"C":"Critical","H":"High","M":"Medium","L":"Low"}
STATUS_HELP={"PENDING":"Waiting for automation.","PROCESSING":"Currently being processed by the graph.","PROCESSED":"Automation completed successfully.","HUMAN_REVIEW":"Automation stopped safely and needs a reviewer.","HUMAN_DECIDED":"A reviewer completed the decision."}
NODE_HELP={
"rules":("Rules and Safety Check","Scans normalized text for deterministic category hints, urgent items, tampering, prompt-injection signals, and administrative requests."),
"triage_agent":("Triage Agent","Classifies every valid claim, scores confidence and urgency, and requests only the read-only context it needs."),
"history_tool":("Claim History Tool","Looks for older related claims and exact duplicate evidence using structured identifiers. Similar wording alone is not enough."),
"context_tool":("Claim Context Tool","Retrieves current claim status, assignment, and a small set of previous actions when more context is needed."),
"tracking_tool":("Baggage Tracking Tool","Reads separate handover, aircraft loading, transfer, and passenger-return events without inferring missing events."),
"compensation_tool":("Compensation Record Tool","Retrieves the authoritative record used to verify claim identity, currency, discrepancy, payment status, holds, and prior resolutions."),
"reassessment_agent":("Context Reassessment Agent","Runs at most once after selected tools return. It re-evaluates ambiguity using a compact, source-labelled evidence package."),
"policy_gate":("Deterministic Policy Guard","Approves or blocks the proposed action. The language model cannot bypass this step or directly execute mutations."),
"route":("Route Action","Assigns the claim to the configured operational team after policy authorization."),
"acknowledge":("Acknowledgment Action","Creates an auditable simulated passenger acknowledgment after policy authorization."),
"close_duplicate":("Duplicate Closure Action","Closes a claim only when older canonical-claim evidence and all duplicate safeguards pass."),
"request_information":("Information Request Action","Creates a simulated request listing the exact missing fields the passenger can provide."),
"courtesy_resolution":("Courtesy Resolution Action","Creates a simulated courtesy resolution only for an authoritative, verified discrepancy within policy limits."),
"human_review":("Human Review","Creates a review task when evidence is missing, conflicting, late, unsafe, or outside execution budgets."),
}

def query(sql:str,args:tuple[Any,...]=())->pd.DataFrame:
    c=connect()
    try:return pd.read_sql_query(sql,c,params=args)
    finally:c.close()

def navigate(page_name:str,claim_id:str|None=None)->None:
    if claim_id:st.session_state["selected_claim_id"]=claim_id
    st.session_state["requested_page"]=page_name

def hero(title:str,subtitle:str)->None:
    st.markdown(f"<div class='hero'><h1>{html.escape(title)}</h1><p>{html.escape(subtitle)}</p></div>",unsafe_allow_html=True)

def safe_json(value:Any,default:Any)->Any:
    if isinstance(value,(dict,list)):return value
    try:return json.loads(value or "")
    except (TypeError,json.JSONDecodeError):return default

def py_int(value:Any)->int:
    try:return int(value or 0)
    except (TypeError,ValueError):return 0

def category_badge(category:str|None)->str:
    code=category if category in CATEGORY_LABELS else None
    if not code:return "<span class='badge badge-muted'>Unclassified</span>"
    return f"<span class='badge badge-{code.lower()}'>{code} · {CATEGORY_LABELS[code]}</span>"

def alert_class(row:pd.Series)->str:
    if bool(row.get("immediate_attention")):return "alert-critical"
    if row.get("automation_status")=="HUMAN_DECIDED":return "alert-human"
    if row.get("automation_status")=="PROCESSED":return "alert-success"
    if row.get("review_priority") in {"CRITICAL","HIGH"}:return "alert-high"
    return "alert-standard"

def render_claim_card(row:pd.Series,key_prefix:str)->None:
    reason=row.get("attention_reason") or row.get("review_reason") or STATUS_HELP.get(row.get("automation_status"),"Claim requires attention.")
    immediate="🚨 Immediate human attention" if bool(row.get("immediate_attention")) else "Review queue"
    st.markdown(f"<div class='alert-card {alert_class(row)}'><b>{html.escape(str(row.get('claim_id')))}</b> · {html.escape(str(row.get('passenger_name') or 'Unknown passenger'))}<br><span class='small-muted'>{html.escape(immediate)}</span><br>{html.escape(str(reason))}</div>",unsafe_allow_html=True)
    if st.button("Open in Claim Workbench",key=f"{key_prefix}_{row.get('claim_id')}",width="stretch"):navigate("Claim Workbench",str(row.get("claim_id")));st.rerun()

def node_style(node:str,status:str)->str:
    if node=="policy_gate":return "step-policy"
    if node=="human_review":return "step-review"
    if status in {"FAILED","REJECTED"}:return "step-failed"
    return "step-success"

def explain_json_field(field:str)->str:
    return {"primary_category":"Final baggage category selected for the claim.","category":"Category proposed by this agent or source.","confidence":"Confidence score. In agent output this is normally 0 to 100; the final decision may store 0 to 1.","agreement":"How closely deterministic rules and semantic classification agree. Agreement is not authoritative proof.","urgency":"C = Critical, H = High, M = Medium, L = Low.","secondary":"Other plausible categories found by the Triage Agent.","needs":"Read-only tools requested by the Triage Agent: HIST, CTX, TRACK, or COMP.","reason_codes":"Short machine-readable reasons used for audit and review.","rationale":"Concise reviewer-facing explanation, not hidden chain-of-thought.","disposition":"Whether the text is a baggage claim, administrative request, insufficient-context case, or out of scope.","conflicts":"Values from different sources that disagree. Relevant conflicts reduce autonomy.","authorized":"Whether the deterministic policy guard permits the proposed mutation.","duplicate_candidate":"Older claim that may be the canonical incident, subject to strict evidence checks.","claimed_difference":"Expected amount minus received amount from the passenger-described discrepancy.","verified_discrepancy":"Authoritative discrepancy stored in the compensation record.","status":"Execution or record state returned by the node.","error_code":"Technical reason the node failed or a result was rejected."}.get(field,"Structured fact produced or retrieved by this step.")

PAGES=["Overview","Automation","Claims & Alerts","Claim Workbench","Graph Trace","Test Laboratory","Compensation","Assessment Report","Exports","Policy Editor","Configuration","Audit"]
if "page" not in st.session_state:st.session_state["page"]="Overview"
requested=st.session_state.pop("requested_page",None)
if requested in PAGES:st.session_state["page"]=requested
st.sidebar.markdown("## 🧳 BagOps");st.sidebar.caption("Baggage operations and human-review control center");page=st.sidebar.radio("Navigation",PAGES,key="page")
with st.sidebar.expander("Legend",expanded=True):
    st.markdown("""**Claim categories**

- 🔵 **DB**: Delayed baggage
- 🟠 **DM**: Damaged baggage
- 🔴 **MC**: Missing contents
- 🟣 **LP**: Lost property or item left onboard
- 🟢 **CR**: Compensation request

**Operational colors**

- 🚨 **Red**: Immediate human attention
- 🟠 **Amber**: High-priority review
- 🔵 **Blue**: Standard or pending work
- ✅ **Green**: Automation completed
- 🟣 **Purple**: Human decision or policy control

**Urgency**

- **C**: Critical
- **H**: High
- **M**: Medium
- **L**: Low""")
with st.sidebar.expander("How decisions stay safe"):
    st.markdown("""1. Rules identify safety signals.
2. The Triage Agent interprets meaning.
3. Read-only tools retrieve evidence.
4. One reassessment may resolve ambiguity.
5. Deterministic policy authorizes or blocks actions.
6. Missing or conflicting evidence creates human review.""")

if page=="Overview":
    hero("Operations Dashboard","See urgent reviews, queue health, automation outcomes, latency, and model usage at a glance.")
    claims=query("SELECT * FROM claims");runs=query("SELECT * FROM graph_runs")
    active=claims.claim_status.fillna("").astype(str).str.strip().str.lower().ne("closed") if not claims.empty else pd.Series(dtype=bool)
    metrics=st.columns(7);metrics[0].metric("All claims",len(claims));metrics[1].metric("Pending",int(((claims.automation_status=="PENDING")&active).sum()) if not claims.empty else 0);metrics[2].metric("Processing",int(((claims.automation_status=="PROCESSING")&active).sum()) if not claims.empty else 0);metrics[3].metric("Human review",int((claims.automation_status=="HUMAN_REVIEW").sum()) if not claims.empty else 0);metrics[4].metric("Immediate",int(claims.immediate_attention.sum()) if not claims.empty else 0);metrics[5].metric("Graph failures",int((runs.status=="FAILED").sum()) if not runs.empty else 0);metrics[6].metric("Total tokens",int(runs.input_tokens.sum()+runs.output_tokens.sum()) if not runs.empty else 0)
    durations=runs.duration_ms.dropna().astype(int).tolist() if not runs.empty else [];perf=st.columns(4);perf[0].metric("P50 latency",f"{int(statistics.median(durations)) if durations else 0} ms");perf[1].metric("P95 latency",f"{int(sorted(durations)[max(0,int(.95*len(durations))-1)]) if durations else 0} ms");perf[2].metric("Average agent invocations",round(float(runs.llm_calls.mean()),2) if not runs.empty else 0);perf[3].metric("Average tool calls",round(float(runs.tool_calls.mean()),2) if not runs.empty else 0)
    a,b,c=st.tabs(["🚨 Critical alerts","🟠 Human-review queue","🔵 Currently processing"])
    with a:
        f=claims[(claims.immediate_attention==1)&(claims.automation_status=="HUMAN_REVIEW")] if not claims.empty else claims
        if f.empty:st.success("No immediate-attention claims are waiting for review.")
        else:
            st.error(f"{len(f)} claim(s) need immediate human attention.")
            for _,r in f.sort_values(["review_due_at","submitted_at"]).head(10).iterrows():render_claim_card(r,"critical")
    with b:
        f=claims[(claims.automation_status=="HUMAN_REVIEW")&(claims.immediate_attention==0)] if not claims.empty else claims
        if f.empty:st.success("No standard human-review items are waiting.")
        else:
            for _,r in f.sort_values(["review_priority","review_due_at"]).head(10).iterrows():render_claim_card(r,"review")
    with c:
        f=claims[(claims.automation_status=="PROCESSING")&active] if not claims.empty else claims
        if f.empty:st.info("No claims are currently processing.")
        else:st.dataframe(f[["claim_id","passenger_name","processing_started_at","lease_expires_at","attempt_count","last_worker_id"]],width="stretch",hide_index=True)
elif page=="Automation":
    hero("Automation","Select claims, follow live progress, inspect the active item, and download the batch result.")
    pending=query("SELECT claim_id,passenger_name,body_text,submitted_at FROM claims WHERE automation_status='PENDING' AND LOWER(TRIM(COALESCE(claim_status,'')))!='closed' ORDER BY submitted_at,claim_id")
    if pending.empty:st.info("No pending claims are available. Reseed the database or review existing results.")
    else:
        pending.insert(0,"Select",False);top=st.columns([1,1,2]);all_=top[0].checkbox("Select all pending");dry=top[1].toggle("Dry-run mode");top[2].info("Dry run executes the graph and policy but does not perform mutation actions." if dry else "Live mode executes policy-authorized simulated actions.");pending["Select"]=all_;edited=st.data_editor(pending,width="stretch",hide_index=True,disabled=["claim_id","passenger_name","body_text","submitted_at"],key="automation_pending_table");ids=edited.loc[edited.Select,"claim_id"].tolist();m=st.columns(3);m[0].metric("Pending",len(pending));m[1].metric("Selected",len(ids));m[2].metric("Mode","DRY RUN" if dry else "LIVE")
        if st.button("▶ Process selected claims",type="primary",disabled=not ids,width="stretch"):
            progress=st.progress(0.0,text="Preparing selected claims...");active_box=st.empty();status_box=st.status("Starting BagOps graph...",expanded=True)
            def callback(e):progress.progress(e["index"]/max(e["total"],1),text=f"{e['stage']}: {e['claim_id']} · {e['index']} of {e['total']}");active_box.markdown(f"**Currently processing:** `{e['claim_id']}`  \n**Batch progress:** {e['index']} / {e['total']}");status_box.write(f"{e['stage']}: `{e['claim_id']}`")
            batch=process(ids,len(ids),dry,callback);status_box.update(label="Batch completed" if not batch["failed"] else "Batch completed with failures",state="complete" if not batch["failed"] else "error",expanded=True);progress.progress(1.0,text="Batch complete");st.session_state["batch_result"]=batch
    if st.session_state.get("batch_result"):
        batch=st.session_state["batch_result"];st.subheader("Latest batch outcome");cols=st.columns(5)
        for col,key in zip(cols,["processed","completed","human_review","dry_run","failed"]):col.metric(key.replace("_"," ").title(),batch[key])
        frame=pd.DataFrame(batch["results"]);st.dataframe(frame,width="stretch",hide_index=True);st.download_button("Download batch CSV",frame.to_csv(index=False),"bagops_batch_results.csv","text/csv",width="stretch")
elif page=="Claims & Alerts":
    hero("Claims & Alerts","Filter operational states, distinguish critical alerts, and open a claim for detailed review.");claims=query("SELECT * FROM claims ORDER BY immediate_attention DESC,CASE review_priority WHEN 'CRITICAL' THEN 1 WHEN 'HIGH' THEN 2 ELSE 3 END,review_due_at,submitted_at,claim_id")
    if claims.empty:st.warning("No claims are available. Run `python seed.py`.")
    else:
        filters=st.columns(3);states=sorted(claims.automation_status.dropna().unique());selected=filters[0].multiselect("Operational status",states,default=states);critical=filters[1].toggle("Critical alerts only");search=filters[2].text_input("Search claim or passenger");f=claims[claims.automation_status.isin(selected)]
        if critical:f=f[f.immediate_attention==1]
        if search:f=f[f.claim_id.astype(str).str.contains(search,case=False,na=False)|f.passenger_name.astype(str).str.contains(search,case=False,na=False)]
        st.dataframe(f[["claim_id","passenger_name","automation_status","review_priority","immediate_attention","body_text","assigned_team","review_reason","review_due_at"]],width="stretch",hide_index=True);cid=st.selectbox("Open a claim",f.claim_id.tolist() or [None])
        if cid and st.button("Open selected claim",width="stretch"):navigate("Claim Workbench",cid);st.rerun()
elif page=="Claim Workbench":
    hero("Claim Workbench","Compare source text with normalized input, understand the automated decision, and record an accountable human override.");claims=query("SELECT * FROM claims ORDER BY claim_id")
    if claims.empty:st.warning("No claims are available. Run `python seed.py`, then refresh.");st.stop()
    ids=claims.claim_id.astype(str).tolist();preferred=st.session_state.get("selected_claim_id");preferred=preferred if preferred in ids else ids[0];selector,close=st.columns([5,1]);cid=selector.selectbox("Claim",ids,index=ids.index(preferred));st.session_state["selected_claim_id"]=cid
    if close.button("← Close",width="stretch"):navigate("Claims & Alerts");st.rerun()
    row=claims[claims.claim_id.astype(str)==cid].iloc[0];raw=safe_json(row.raw_input,{})
    st.markdown(f"<div class='alert-card {alert_class(row)}'><b>Status:</b> {html.escape(str(row.automation_status))} · <b>Priority:</b> {html.escape(str(row.review_priority))}<br>{html.escape(STATUS_HELP.get(row.automation_status,'Operational claim state.'))}</div>",unsafe_allow_html=True);left,right=st.columns(2);left.text_area("Original submitted text",str(raw.get("body_text") or raw.get("claim_text") or row.body_text),height=160,disabled=True,help="The passenger text preserved from the source CSV.");right.text_area("Normalized text used by the graph",str(row.body_text),height=160,disabled=True,help="Sanitized text used by rules and semantic classification.")
    with st.expander("Original source record"):st.json(raw)
    latest=query("SELECT * FROM graph_runs WHERE claim_id=? ORDER BY created_at DESC LIMIT 1",(cid,))
    if latest.empty:st.info("This claim has not been processed. Open Automation to process it.")
    else:
        run=latest.iloc[0];decision=safe_json(run.decision,{});policy=safe_json(run.policy_result,{});left,right=st.columns(2)
        with left:
            st.subheader("Automated decision")
            st.markdown(category_badge(decision.get("primary_category")), unsafe_allow_html=True)
            st.write("**Recommended action:**", decision.get("recommended_action", "Not available"))
            st.write("**Destination team:**", decision.get("destination_team") or "Not assigned")
            st.write("**Urgency:**", URGENCY_LABELS.get(decision.get("urgency"), decision.get("urgency")))
            st.write("**Agreement:**", decision.get("agreement"))
            st.info(decision.get("rationale") or "Use reason codes and graph evidence below.")
        with right:
            st.subheader("Protected policy result")
            if policy.get("authorized"):
                st.success("The deterministic policy guard authorized the proposed action.")
            else:
                st.warning("The policy guard blocked automation or required human review.")
            st.json(policy)
        with st.expander("How to interpret this decision"):st.markdown("- **Category** is the operational claim type.\n- **Confidence** does not override safeguards.\n- **Agreement** is supporting verification.\n- **Policy result** alone authorizes mutation.")
        st.subheader("Execution summary");st.dataframe(query("SELECT node,node_kind,status,invocation_reason,duration_ms,input_tokens,output_tokens,attempts,cache_hit,provider_requests,provider_retries,schema_repairs,error_code FROM node_runs WHERE graph_run_id=? ORDER BY completed_at",(run.graph_run_id,)),width="stretch",hide_index=True)
        with st.form("human_override_form"):
            reviewer=st.text_input("Reviewer name *");reason=st.text_area("Reason for decision *");team=st.text_input("Destination team",decision.get("destination_team") or "");action=st.selectbox("Human action",["HUMAN_ROUTE","HUMAN_ACKNOWLEDGE","HUMAN_CLOSE","HUMAN_REOPEN","HUMAN_REQUEST_INFORMATION","HUMAN_NO_ACTION"]);submitted=st.form_submit_button("Save human decision",type="primary")
        if submitted:
            try:save_override(cid,run.graph_run_id,reviewer,{"destination_team":team},action,reason);st.success("Human decision saved and added to the audit trail.");st.rerun()
            except Exception as error:st.error(str(error))
elif page=="Graph Trace":
    hero("Graph Trace","Follow each accepted step, understand why it ran, and inspect its structured evidence in plain language.")
    with st.expander("How the graph works",expanded=True):st.markdown("""### Execution flow

1. **Rules and Safety Check**, **Triage Agent**, and **Claim History Tool** can start independently.
2. Each completed result is checked for the correct graph run, claim version, deadline, and duplicate run ID.
3. The reducer combines accepted facts in a deterministic order and preserves conflicts.
4. The scheduler starts only context tools requested by current evidence.
5. A single **Context Reassessment Agent** may run if ambiguity remains.
6. The **Policy Guard** approves or blocks the proposed action.
7. The graph finishes with an action, information request, or human review.

### Important safety rule

No individual agent or tool acts alone. Language-model output is a recommendation. The deterministic policy guard alone authorizes mutation actions.""")
    with st.expander("JSON field guide"):
        for field in ["category","primary_category","confidence","agreement","urgency","secondary","needs","reason_codes","rationale","disposition","conflicts","authorized","duplicate_candidate","claimed_difference","verified_discrepancy","status","error_code"]:st.markdown(f"- **`{field}`**: {explain_json_field(field)}")
    runs=query("SELECT * FROM graph_runs ORDER BY created_at DESC")
    options=[(str(r.graph_run_id),str(r.claim_id)) for _,r in runs.iterrows()] if not runs.empty else []
    selected=st.selectbox("Graph run",options or [None],format_func=lambda item:"No graph runs available" if item is None else f"{item[0]} | {item[1]}")
    selected_run_id=selected[0] if selected else None
    if selected_run_id:
        run=runs[runs.graph_run_id.astype(str)==selected_run_id].iloc[0];proof=st.columns(7);proof[0].metric("Status",str(run.status));proof[1].metric("Steps",py_int(run.step_count));proof[2].metric("LLM calls",py_int(run.llm_calls));proof[3].metric("Tool calls",py_int(run.tool_calls));proof[4].metric("Input tokens",py_int(run.input_tokens));proof[5].metric("Output tokens",py_int(run.output_tokens));proof[6].metric("Duration",f"{py_int(run.duration_ms)} ms")
        fallbacks = safe_json(run.fallbacks, [])
        if fallbacks:
            st.warning("Fallbacks used: " + ", ".join(str(item) for item in fallbacks))
        else:
            st.success("Fallbacks used: none")
        if run.error_code:st.error(f"Graph error: {run.error_code}")
        nodes=query("SELECT * FROM node_runs WHERE graph_run_id=? ORDER BY completed_at",(selected_run_id,))
        for index,node in nodes.iterrows():
            title,explanation=NODE_HELP.get(node.node,(node.node.replace("_"," ").title(),"Persisted graph result."));st.markdown(f"<div class='step-card {node_style(str(node.node),str(node.status))}'><b>Step {index+1}: {html.escape(title)}</b> · {html.escape(str(node.status))}<br><span class='small-muted'>{html.escape(explanation)}</span><br><span class='small-muted'>Why it ran: {html.escape(str(node.invocation_reason or 'Graph readiness condition'))}</span></div>",unsafe_allow_html=True)
            with st.expander(f"View structured evidence for {title}"):
                meta=st.columns(5);meta[0].metric("Kind",str(node.node_kind));meta[1].metric("Duration",f"{py_int(node.duration_ms)} ms");meta[2].metric("Attempts",py_int(node.attempts));meta[3].metric("Input tokens",py_int(node.input_tokens));meta[4].metric("Output tokens",py_int(node.output_tokens));st.caption(f"Cache hit: {bool(node.cache_hit)} · Provider requests: {py_int(node.provider_requests)} · Provider retries: {py_int(node.provider_retries)} · Schema repairs: {py_int(node.schema_repairs)}");st.caption(f"Scheduled: {node.scheduled_at} · Deadline: {node.deadline_at} · Completed: {node.completed_at}");facts=safe_json(node.facts,{});st.json(facts)
                if isinstance(facts,dict) and facts:
                    with st.expander("Explain fields shown above"):
                        for field in facts:st.markdown(f"- **`{field}`**: {explain_json_field(field)}")
                evidence=safe_json(node.evidence,[])
                if evidence:st.write("**Evidence or reason codes:**",evidence)
                if node.error_code:st.error(node.error_code)
elif page=="Test Laboratory":
    hero("Test Laboratory","Safely simulate failure, latency, and out-of-order completion without performing mutation actions.");st.info("Laboratory runs always use dry-run mode. They are intended for assessment and regression testing.");text=st.text_area("Claim text","My checked bag never arrived at the carousel.",height=130);left,right=st.columns(2);failures=left.multiselect("Inject failures",["rules","history_tool","triage_agent","context_tool","tracking_tool","compensation_tool","reassessment_agent"]);order=right.selectbox("Initial completion order",["Natural","Rules first","Triage first","History first"]);delay=st.slider("Artificial history delay in seconds",0,3,0);save=st.checkbox("Save as a regression case")
    if st.button("Run laboratory case",type="primary",width="stretch"):
        cid=f"BC-{uuid.uuid4().int%9000+1000}";claim={"claim_id":cid,"passenger_name":"Laboratory","body_text":text,"raw_input":json.dumps({"body_text":text}),"input_hash":"laboratory","ingestion_status":"VALID","version":1,"claim_status":"new"}
        with tx() as c:c.execute("INSERT INTO claims(claim_id,passenger_name,claim_status,body_text,raw_input,input_hash,ingestion_status,validation_errors,automation_status) VALUES(?,?,?,?,?,?,?,'[]','PROCESSING')",(cid,"Laboratory","new",text,claim["raw_input"],"laboratory","VALID"))
        orders={"Natural":None,"Rules first":["rules","triage_agent","history_tool"],"Triage first":["triage_agent","history_tool","rules"],"History first":["history_tool","rules","triage_agent"]}
        with st.status("Running laboratory graph...",expanded=True) as status:status.write("Creating isolated laboratory claim");status.write("Applying selected failure and ordering controls");result=run_agent(claim,"LAB",True,{n:True for n in failures},{"history_tool":delay},orders[order]);status.write("Graph completed in dry-run mode")
        st.session_state["laboratory_result"]=result
        if save:
            with tx() as c:c.execute("INSERT INTO regression_cases VALUES(?,?,?,?,?,CURRENT_TIMESTAMP)",(f"CASE-{uuid.uuid4().hex}",f"Laboratory {cid}",text,json.dumps({"failures":failures,"order":order,"delay":delay}),json.dumps(result["decision"])))
    if st.session_state.get("laboratory_result"):st.subheader("Laboratory result");st.json(st.session_state["laboratory_result"])
elif page=="Compensation":
    hero("Compensation","Review authoritative records and simulated courtesy resolutions separately from passenger narrative.")
    with st.expander("Financial legend",expanded=True):st.markdown("- **Verified discrepancy > 0**: Potential shortfall, still subject to all safeguards.\n- **Verified discrepancy = 0**: No numeric discrepancy to resolve.\n- **Verified discrepancy < 0**: Mandatory financial review.\n- **Hold flags** block autonomous resolution.\n- **Prior resolution** blocks a second courtesy action.\n- All courtesy actions are **simulated**.")
    st.subheader("Authoritative compensation records");st.dataframe(query("SELECT * FROM compensation_records"),width="stretch",hide_index=True);st.subheader("Issued simulated courtesy resolutions");st.dataframe(query("SELECT * FROM courtesy_resolutions"),width="stretch",hide_index=True)
elif page=="Assessment Report":
    hero("Assessment Report","Export graph status, latency, token usage, calls, fallbacks, decisions, and policy outcomes.");report=query("SELECT * FROM graph_runs ORDER BY created_at DESC");st.dataframe(report,width="stretch",hide_index=True);st.download_button("Download assessment CSV",report.to_csv(index=False),"bagops_assessment_report.csv","text/csv",width="stretch")
elif page=="Exports":
    hero("Exports","Create focused operational extracts or download a complete evidence package for assessment and audit.")
    with st.expander("Export guide",expanded=True):st.markdown("- **Operational CSV**: Human-readable tables.\n- **JSON**: Preserves structured objects.\n- **Evidence ZIP**: Bundles selected CSV and JSON datasets.\n- **Filtered review export**: Current human-review claims only.")
    table_options={"Claims":"claims","Graph runs":"graph_runs","Node runs":"node_runs","Classifications":"classifications","Actions":"actions","Communications":"communications","Courtesy resolutions":"courtesy_resolutions","Human overrides":"human_overrides","Audit events":"audit_events","Policy versions":"configuration_versions","Regression cases":"regression_cases"};selection=st.multiselect("Tables to export",list(table_options),default=["Claims","Graph runs","Node runs","Classifications","Actions","Human overrides","Audit events"]);fmt=st.segmented_control("Primary format",["CSV","JSON","Evidence ZIP"],default="Evidence ZIP");review_only=st.toggle("Only claims currently requiring human review");frames={}
    for label in selection:
        table=table_options[label]
        if table=="claims" and review_only:frames[table]=query("SELECT * FROM claims WHERE automation_status='HUMAN_REVIEW' ORDER BY immediate_attention DESC,review_due_at,claim_id")
        else:frames[table]=query(f"SELECT * FROM {table}")
    st.subheader("Export preview");st.dataframe(pd.DataFrame([{"dataset":n,"rows":len(f)} for n,f in frames.items()]),width="stretch",hide_index=True)
    if frames:
        if fmt=="CSV":name=next(iter(frames));st.download_button(f"Download {name}.csv",frames[name].to_csv(index=False),f"bagops_{name}.csv","text/csv",width="stretch")
        elif fmt=="JSON":st.download_button("Download selected data as JSON",json.dumps({n:json.loads(f.to_json(orient="records",date_format="iso")) for n,f in frames.items()},indent=2,default=str),"bagops_export.json","application/json",width="stretch")
        else:
            def build_zip():
                b=io.BytesIO()
                with zipfile.ZipFile(b,"w",zipfile.ZIP_DEFLATED) as z:
                    z.writestr("manifest.json",json.dumps({"export_type":"BagOps evidence package","review_only":review_only,"datasets":{n:len(f) for n,f in frames.items()},"policy":active_policy(),"protected_guardrails":PROTECTED_GUARDRAILS},indent=2))
                    for n,f in frames.items():z.writestr(f"{n}.csv",f.to_csv(index=False));z.writestr(f"{n}.json",f.to_json(orient="records",indent=2,date_format="iso"))
                return b.getvalue()
            st.download_button("Download complete evidence ZIP",build_zip(),"bagops_evidence_package.zip","application/zip",width="stretch")
elif page=="Policy Editor":
    hero("Policy Editor","Version operational thresholds and routing while safety-critical guardrails remain protected and read-only.");policy=active_policy();left,right=st.columns([3,2])
    with left:
        st.subheader("Editable operational policy")
        with st.form("policy_editor_form"):
            author=st.text_input("Policy author *");routes={k:st.text_input(f"{k} · {CATEGORY_LABELS[k]}",policy["routes"][k]) for k in CATEGORY_LABELS};limit=st.text_input("Simulated courtesy authority limit",policy["courtesy_limit"]);high=st.slider("High-agreement threshold",0.0,1.0,float(policy["high_agreement_threshold"]),.01);review=st.slider("Human-review agreement threshold",0.0,1.0,float(policy["human_review_agreement_threshold"]),.01);confidence=st.slider("High-confidence threshold",0,100,int(policy["high_confidence_threshold"]),1);submit=st.form_submit_button("Publish new policy version",type="primary")
        if submit:
            try:st.success(f"Published policy version {publish_policy({'routes':routes,'courtesy_limit':limit,'high_agreement_threshold':high,'human_review_agreement_threshold':review,'high_confidence_threshold':confidence},author)}.");st.rerun()
            except Exception as error:st.error(str(error))
    with right:
        st.subheader("Protected guardrails")
        st.warning("These controls are intentionally not editable in the UI.")
        for guardrail in PROTECTED_GUARDRAILS:
            st.markdown(f"- 🔒 {guardrail}")
        st.subheader("Current active policy")
        st.json(policy)
    st.subheader("Policy version history");st.dataframe(query("SELECT config_id,author,settings,protected_policy_version,effective_at FROM configuration_versions ORDER BY effective_at DESC,rowid DESC"),width="stretch",hide_index=True)
elif page=="Configuration":
    hero("Configuration","Inspect provider connectivity and execution budgets while protected safeguards remain read-only.");settings=dict(SETTINGS.__dict__);settings["api_key"]="*** configured ***" if SETTINGS.api_key else "not configured";st.json(settings)
    if st.button("Test LLM connection",type="primary",width="stretch"):
        with st.status("Testing configured LLM endpoint...",expanded=True) as status:result=test_connection();status.write("Endpoint request completed")
        if result.get("ok"):
            st.success("LLM connection test succeeded.")
        else:
            st.error("LLM connection test failed.")
        st.json(result)
    st.subheader("Protected controls");st.markdown("- Passenger text and free-text tool data are untrusted.\n- Mutation tools are not exposed to the model.\n- Duplicate closure requires authoritative history evidence.\n- Courtesy resolution requires an exact verified discrepancy.\n- Critical, conflicting, stale, late, or over-budget work goes to human review.\n- Every mutation requires persisted policy authorization and idempotency.")
else:
    hero("Audit","Review append-only workflow events, system actions, and human decisions.");tabs=st.tabs(["Audit events","Actions","Human overrides","Saved regression cases"])
    for tab,table in zip(tabs,["audit_events","actions","human_overrides","regression_cases"]):
        with tab:st.dataframe(query(f"SELECT * FROM {table} ORDER BY created_at DESC"),width="stretch",hide_index=True)

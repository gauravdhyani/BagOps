from __future__ import annotations
from typing import Any, Literal, TypedDict
from pydantic import BaseModel, ConfigDict, Field
Category=Literal['DB','DM','MC','LP','CR']; Urgency=Literal['L','M','H','C']; Need=Literal['HIST','CTX','TRACK','COMP']
class Triage(BaseModel):
    model_config=ConfigDict(extra='forbid')
    category:Category; confidence:int=Field(ge=0,le=100); urgency:Urgency
    secondary:list[Category]=Field(default_factory=list); needs:list[Need]=Field(default_factory=list)
    ambiguous:bool=False; disposition:Literal['CLAIM','ADMINISTRATIVE','INSUFFICIENT_CONTEXT','OUT_OF_SCOPE']='CLAIM'
    reason_codes:list[str]=Field(default_factory=list); rationale:str=Field(max_length=240)
class Reassessment(BaseModel):
    model_config=ConfigDict(extra='forbid')
    category:Category|None=None; confidence:int=Field(ge=0,le=100); urgency:Urgency
    action:Literal['ACKNOWLEDGE','ROUTE','CLOSE_DUPLICATE','REQUEST_INFORMATION','COURTESY_RESOLUTION','HUMAN_REVIEW']
    destination_team:str|None=None; missing_information:list[str]=Field(default_factory=list)
    expected_amount:str|None=None; received_amount:str|None=None; currency:str|None=None; claim_reference:str|None=None
    reason_codes:list[str]=Field(default_factory=list); rationale:str=Field(max_length=320)
class NodeResult(TypedDict):
    node:str; run_id:str; graph_run_id:str; claim_version:int; status:str; kind:str
    facts:dict[str,Any]; evidence:list[str]; error_code:str|None; scheduled_at:str
    deadline_at:str; completed_at:str; duration_ms:int; input_tokens:int; output_tokens:int; attempts:int

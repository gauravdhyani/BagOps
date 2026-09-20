from __future__ import annotations
import json, os
from dataclasses import dataclass
from pathlib import Path

def load_env(path: str = '.env') -> None:
    file = Path(path)
    if not file.exists(): return
    for raw in file.read_text(encoding='utf-8-sig').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line: continue
        key, value = line.split('=', 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
load_env()

def flag(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {'1','true','yes','on'}
def integer(name: str, default: int) -> int:
    try: return int(os.getenv(name, str(default)))
    except ValueError: return default

def headers() -> dict[str,str]:
    try: value=json.loads(os.getenv('BAG_LLM_EXTRA_HEADERS','{}') or '{}')
    except json.JSONDecodeError as error: raise RuntimeError('BAG_LLM_EXTRA_HEADERS_INVALID_JSON') from error
    if not isinstance(value,dict): raise RuntimeError('BAG_LLM_EXTRA_HEADERS_MUST_BE_OBJECT')
    return {str(k):str(v) for k,v in value.items()}

@dataclass
class Settings:
    db_path: str=os.getenv('BAG_DB_PATH','bagops.db')
    provider: str=os.getenv('BAG_LLM_PROVIDER','openai-compatible').lower()
    base_url: str=os.getenv('BAG_LLM_URL','').strip().rstrip('/')
    api_key: str=os.getenv('BAG_LLM_API_KEY','').strip()
    model: str=os.getenv('BAG_LLM_MODEL','').strip()
    timeout_seconds: int=integer('BAG_LLM_TIMEOUT',12)
    ca_bundle: str=os.getenv('BAG_LLM_CA_BUNDLE','').strip()
    assessment_mode: bool=flag('BAG_ASSESSMENT_MODE',True)
    allow_rules_fallback: bool=flag('BAG_LLM_ALLOW_FALLBACK',False)
    max_steps: int=integer('BAG_MAX_STEPS',10)
    max_llm_calls: int=integer('BAG_MAX_LLM_CALLS',2)
    max_tool_calls: int=integer('BAG_MAX_TOOL_CALLS',5)
    max_tokens: int=integer('BAG_MAX_TOKENS_PER_CLAIM',1400)
    max_context_chars: int=integer('BAG_MAX_CONTEXT_CHARS',5000)
    max_node_retries: int=integer('BAG_MAX_NODE_RETRIES',1)
    node_deadline_seconds: int=integer('BAG_NODE_DEADLINE',15)
    lease_seconds: int=integer('BAG_PROCESSING_LEASE',180)
    courtesy_limit: str=os.getenv('BAG_COURTESY_LIMIT','250.00')
    prompt_version: str=os.getenv('BAG_PROMPT_VERSION','v3')
SETTINGS=Settings()

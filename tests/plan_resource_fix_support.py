"""Owned fixed-provider fixture. Production parser, validators, claims and stores stay real.
No original session is loaded. Synthetic paths are not scientific inputs.
"""
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch
from langchain_core.messages import AIMessage, HumanMessage

AUDIT=Path(__file__).resolve().parents[1]/'docs/audits/session_1789369423_plan_resource_cycle'
SID='genomics/snpeff-annotation'

def save(name, value):
    root=Path(os.environ['BIOMIND_RESOURCE_AUDIT_RESULTS']);root.mkdir(parents=True,exist_ok=True)
    (root/(name+'.json')).write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str)+'\n')

class RouterStub:
    def bind(self, **kwargs):return self
    def model_copy(self, update=None):return self
    async def ainvoke(self, messages, **kwargs):
        return AIMessage(content=json.dumps({'schema_version':1,'request_kind':'execute','execution_scope':'managed',
            'managed_intent_explicit':True,'retrieval_mode':'kb','confidence':1.0,'reason':'explicit synthetic interface test'}))



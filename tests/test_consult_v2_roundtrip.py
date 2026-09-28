"""Post-fix regressions through real schema-2 phase and provider transport."""
import asyncio
from copy import deepcopy
import json
import pytest
from pathlib import Path
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from tests import test_schema2_integration_a_production as fixture
from agent_core.plan_tracker import load_plan_state, save_plan_state
from agent_core.command_execution import CommandExecutionService



def native(name,args,i):
    return AIMessage(content='',tool_calls=[{'name':name,'args':args,'id':'v2-'+str(i)}],response_metadata={'complete':True,'finish_reason':'tool_calls'})





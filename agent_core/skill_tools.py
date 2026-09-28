"""Thin native/text carriers for the existing Skill and Consult actions.

No catalog, reader, selection policy or independent budget lives here.
"""
import json
import re


def _schema(name, description, properties=None, required=()):
    return {'type':'function','function':{'name':name,'description':description,
        'parameters':{'type':'object','properties':properties or {},'required':list(required)}}}


ASK_USER_SCHEMA = _schema('ask_user',
    'Ask one concrete user choice that tools cannot resolve and that materially changes the goal, reuse of results or output arrangement. Check existing answers first and retain applicable decisions. Explain relevant facts and offer understandable choices; a proposed directory name or natural-language answer is sufficient. Pause for the answer, then continue in the calling stage. Technical unknowns that do not require a user choice may remain for investigation. Call alone.',
    {'question':{'type':'string'},'requested_slots':{'type':'array','items':{'type':'string'}},'expected_type':{'type':'string'}},['question'])


def skill_tool_schemas(stage):
    schemas = [_schema('request_skill_context',
        'Read exact Skill text. The host sizes replies to document length and remaining context, preferring whole sections. Follow next_request when has_more; cached or unread text is not read evidence. Operations: headings, section, search, range, resource, full.',
        {'reason':{'type':'string'},'requests':{'type':'array','minItems':1,'maxItems':4,'items':{
            'type':'object','properties':{'skill_id':{'type':'string'},'operation':{'type':'string','enum':['headings','section','search','range','resource','full']},
                'heading':{'type':'string'},'query':{'type':'string'},'start_line':{'type':'integer'},'end_line':{'type':'integer'},
                'resource_path':{'type':'string'},'snapshot_id':{'type':'string'},'char_offset':{'type':'integer','minimum':0},
                'max_chars':{'type':'integer'},'refresh_resource':{'type':'boolean'},'resource_content_hash':{'type':'string'}},'required':['skill_id','operation']}}},['reason','requests'])]
    if stage in {'consultant', 'generate'}:
        schemas.append(ASK_USER_SCHEMA)
    if stage != 'consultant': return schemas
    schemas += [
        _schema('list_skill_categories','List the registered Skill categories; available on the first action.'),
        _schema('list_skills','Page through registered Skill names/purposes. Continue with next_request; no body loading.',
                {'category':{'type':'string'},'offset':{'type':'integer','minimum':0},'limit':{'type':'integer','minimum':1,'maximum':24}}),
        _schema('search_skills','Search the existing local Skill catalog by method/purpose; does not select or execute.',
            {'queries':{'type':'array','minItems':1,'maxItems':3,'items':{'type':'object','properties':{
                'query_id':{'type':'string'},'text':{'type':'string'},'purpose':{'type':'string'}},'required':['query_id','text']}},
             'top_k':{'type':'integer','minimum':1,'maximum':8},'exclude_skill_ids':{'type':'array','items':{'type':'string'}},'task_hypothesis':{'type':'string'}},['queries']),
        _schema('load_skill_doc','Read 1-5 canonical Skills. Returns full text when it fits the host context allowance, otherwise exact section pages. Cache hits obey the same allowance; has_more means unread text remains. Follow next_request for needed pages.',
            {'skill_ids':{'type':'array','minItems':1,'maxItems':5,'items':{'type':'string'}}},['skill_ids']),
        _schema('submit_planning_handoff','Hand facts and selected methods to Generate once the goal and material user choices are clear. A still-unanswered choice between reusing relevant old results and rerunning belongs in ask_user first; writing it in summary/unknowns does not ask the user. Carry the actual answer and its source in summary/constraints; do not ask again when already decided. Ordinary technical unknowns and questions not affecting current execution can remain in unknowns for Generate to investigate. Batch inputs reference their manifest once, without expanding rows. The host saves evidence and unambiguous path relationships, not overwrite consent. Only committed=true accepts the handoff, not scientific completion. Call alone; omitted facts retain prior values.',
            {'summary':{'type':'string'},'memory_context':{'type':'object','description':'Optional selected historical references; not current execution evidence. Omit when irrelevant.',
                 'properties':{'summary':{'type':'string'},'source_refs':{'type':'array','items':{'type':'string'}}},'additionalProperties':False},'selected_skills':{'type':'array','items':{'type':'string'}},
             'goal':{'type':'string'},'inputs':{'type':'array','items':{'type':'object','properties':{
                 'role':{'type':'string'},'path':{'type':'string'},'sample':{'type':'string'}},'required':['role','path']}},
             'output_dir':{'type':'string','description':'Absolute output directory reflecting the user choice. Resolve a selected relative name against project_dir; the user need not type a full path. If filenames are unspecified, describe outputs without paths; Generate chooses names within that scope.'},
              'parameters':{'type':'object','description':'Business values; not necessarily user-confirmed. Preserve origin separately.'},
             'parameter_sources':{'type':'object','additionalProperties':{'type':'object','properties':{
                 'source_type':{'type':'string','enum':['user_explicit','skill_default','system_suggestion','tool_observation','memory','unknown']},
                 'source_turn':{'type':'string','description':'Actual user evidence handle for an explicit setting.'},
                 'skill_id':{'type':'string'},'version':{'type':'string'},'source_ref':{'type':'string'},
                 'adopted_by':{'type':'string','description':'Optional user turn approving adoption, without changing origin.'}},'required':['source_type']}},
             'report_preservation':{'type':'object','description':'An explicit user decision to preserve the named reports before new publication. Omitted paths refer only to the pending report-conflict set. Does not move files in Consult or authorize replacing arbitrary scientific results, caches or inputs.',
                 'properties':{'operation':{'type':'string','enum':['date_suffix']},'source_turn':{'type':'string'},
                               'paths':{'type':'array','items':{'type':'string'}}},'required':['operation','source_turn']},
             'output_preservation':{'type':'object','description':'Carry an explicit user decision to back up then replace exactly listed existing output files. Cite the real user turn. The host freezes file identities; Consult makes no writes. A changed file or expanded scope needs a new applicable decision. Never infer this from a directory or requested filename.',
                 'properties':{'operation':{'type':'string','enum':['backup_replace']},
                               'source_turn':{'type':'string'},
                               'paths':{'type':'array','minItems':1,'maxItems':64,'items':{'type':'string'}}},
                 'required':['operation','source_turn','paths'],'additionalProperties':False},
             'outputs':{'type':'array','items':{
                 'type':'object','properties':{
                     'role':{'type':'string'},'artifact_kind':{'type':'string','enum':['file','directory']},
                     'path':{'type':'string','description':'One literal path; no shell brace expansion or glob patterns.'},
                     'format':{'type':'string'},'required':{'type':'boolean'}},
                 'required':['role','artifact_kind']}},
             'constraints':{'type':'array','items':{'type':'string'}},'unknowns':{'type':'array','items':{'type':'string'}}},['summary']),
    ]
    return schemas


CONSULT_BRIDGE_ACTIONS = {'select_skill','update_taskspec','submit_planning_handoff','ask_user','answer_user',
    'reject_skill','reasoning_escalation','capability_gap','inspect_resource','glob_resource','__consultant_text__'}
ORDINARY_SKILL_ACTIONS = {'search_skills','list_skill_categories','list_skills','load_skill_doc','request_skill_context'}


def decode_skill_text(response, stage):
    text = str(getattr(response,'content','') or '').strip()
    try:
        value = json.loads(text)
    except (ValueError,TypeError):
        value = None
    if value is None and stage == 'consultant':
        wrapped = re.fullmatch(r'<action_json>\s*(.*?)\s*</action_json>|```(?:json)?\s*(.*?)\s*```', text, re.S)
        if wrapped:
            try: value = json.loads(wrapped.group(1) or wrapped.group(2))
            except (ValueError, TypeError): pass
    if isinstance(value,dict):
        decision = str(value.get('decision') or '').upper()
        if decision == 'REQUEST_SKILL_CONTEXT': return 'request_skill_context',value
        if decision == 'SEARCH_SKILLS': return 'search_skills',value
        if decision == 'PROPOSE_SKILL_BINDING': return 'propose_skill_binding',value
        if stage == 'consultant':
            action = str(value.get('action_type') or value.get('type') or value.get('action') or '').lower().replace('-','_')
            if action in {'exec_command','inspect_paths','wait_command','stop_command','search_memory','read_memory','update_memory'}:
                return action,{k:v for k,v in value.items() if k not in {'action_type','type','action','public_message'}}
            if action == 'execute':
                code = str(value.get('code') or value.get('command') or value.get('body') or '')
                return 'exec_command', ({'argv':['python','-c',code]} if 'python' in str(value.get('language') or '').lower() else {'cmd':code})
            action = {'load_skill':'load_skill_doc','skill_doc':'load_skill_doc','submit_handoff':'submit_planning_handoff',
                'update_task_spec':'update_taskspec','ask':'ask_user','question':'ask_user'}.get(action,action)
            if action in {'load_reference','load_script'}:
                return 'request_skill_context', {'reason':'Read the requested Skill resource','requests':[{
                    'skill_id':value.get('skill_id'),'operation':'resource',
                    'resource_path':value.get('path') or value.get('reference_path') or value.get('script_path') or value.get('body')}]}
            if action in ORDINARY_SKILL_ACTIONS | CONSULT_BRIDGE_ACTIONS:
                return action,value
    if stage == 'consultant':
        parts = text.split(None,1)
        command, argument = (parts[0],parts[1] if len(parts)>1 else '') if parts else ('','')
        if command == 'EXECUTE':
            fenced = re.fullmatch(r'```(bash|python)?\s*\n(.*?)\n?```', argument, re.S)
            code = fenced.group(2) if fenced else argument
            return 'exec_command', ({'argv':['python','-c',code]} if fenced and fenced.group(1)=='python' else {'cmd':code})
        if command == 'LIST_SKILL_CATEGORIES': return 'list_skill_categories',{}
        if command == 'LIST_SKILLS': return 'list_skills',{'category':argument or 'all'}
        if command == 'SEARCH_SKILLS': return 'search_skills',{'query':argument}
        if command == 'LOAD_SKILL_DOC': return 'load_skill_doc',{'skill_ids':[s.strip() for s in re.split(r'[,，;\n]+',argument) if s.strip()]}
        if command in {'LOAD_REFERENCE','LOAD_SCRIPT'}:
            skill_id, _, path = argument.partition(' ')
            return 'request_skill_context',{'reason':'Read the requested Skill resource','requests':[{'skill_id':skill_id,'operation':'resource','resource_path':path}]}
        # Old command text still reaches the existing parser, with the same work charge.
        return '__consultant_text__',{'raw_content':text}
    return None

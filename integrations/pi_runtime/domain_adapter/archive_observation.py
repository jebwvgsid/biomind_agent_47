"""Public Pi observations into the existing optional archive writer.

History policy gates reading/Wiki use. It never gated the old observation
writer, so an explicit archive path enables writes even when history is off.
An enqueue acknowledgement is deliberately not a durability acknowledgement.
"""
from memory.patch_validator import sanitize_value

CONTENT_LIMIT=64000
SUMMARY_LIMIT=1200
EVENT_TYPES={'user_message','assistant_message','tool_result','runtime_event'}
TEXT_METADATA={'pi_entry_id','pi_parent_id','tool_name','tool_call_id'}
BOOL_METADATA={'projection_truncated','is_error'}


def status(service):
    if service.archive_path is None:return {'status':'disabled','enabled':False,'stats':{}}
    stats=service._archive_writer.snapshot() if service._archive_writer else dict(service._archive_writer_stats)
    return {'status':'ready','enabled':True,'stats':stats}


def append(service,params):
    from .service import DomainError
    if service.archive_path is None:
        return {'status':'disabled','durable':False,'logical_event_id':None,'stats':{}}
    service._ensure_request()
    event_id=params.get('event_id')
    if not isinstance(event_id,str) or not event_id or len(event_id)>1024:
        raise DomainError('ARCHIVE_EVENT_ID_REQUIRED')
    if params.get('event_type') not in EVENT_TYPES:raise DomainError('ARCHIVE_PUBLIC_EVENT_TYPE_REQUIRED')
    stage=params.get('stage','runtime')
    if stage not in {'consult','generate','runtime'}:raise DomainError('ARCHIVE_STAGE_INVALID')
    content=params.get('content','');summary=params.get('summary','')
    if not isinstance(content,str) or not isinstance(summary,str):raise DomainError('ARCHIVE_TEXT_REQUIRED')
    supplied=params.get('metadata') or {}
    if not isinstance(supplied,dict):raise DomainError('ARCHIVE_METADATA_INVALID')
    # These are event correlation facts, never permission claims, raw provider
    # objects, private reasoning, or arbitrary executable/domain arguments.
    metadata={key:value[:1024] for key,value in supplied.items() if key in TEXT_METADATA and isinstance(value,str)}
    metadata.update({key:value for key,value in supplied.items() if key in BOOL_METADATA and isinstance(value,bool)})
    metadata.update(event_id=event_id,pi_request_id=service._state()['task_state']['pi_request_id'])
    metadata['projection_truncated']=bool(metadata.get('projection_truncated') or len(content)>CONTENT_LIMIT or len(summary)>SUMMARY_LIMIT)
    def strings(key,limit):
        values=params.get(key) or []
        if not isinstance(values,list) or any(not isinstance(value,str) for value in values):
            raise DomainError('ARCHIVE_'+key.upper()+'_INVALID')
        return [value[:1024] for value in values[:limit]]
    returncode=params.get('returncode')
    if returncode is not None and (not isinstance(returncode,int) or isinstance(returncode,bool)):
        raise DomainError('ARCHIVE_RETURNCODE_INVALID')
    payload=sanitize_value({'task_id':service.task_id,'session_id':service.session_id,
        'project_id':service.project_id,'stage':stage,'event_type':params['event_type'],
        'content':content[:CONTENT_LIMIT],'summary':summary[:SUMMARY_LIMIT],
        'metadata':metadata,'source_refs':strings('source_refs',32) or [event_id],
        'paths':strings('paths',32),'tags':strings('tags',16),'returncode':returncode})
    from memory.session_archive_writer import acquire_observation_writer
    if service._archive_writer is None:
        service._archive_writer=acquire_observation_writer(service.archive_path)
    writer=service._archive_writer
    logical_event_id,_=writer._identity(payload)
    outcome=writer.enqueue_nowait(**payload)
    return {'status':outcome,'durable':False,'logical_event_id':logical_event_id,'stats':writer.snapshot()}


async def release(service):
    if service._archive_writer is None:return
    from memory.session_archive_writer import release_observation_writer
    service._archive_writer=None
    # Called after EOF, pending replies and independent jobs have finished.
    # Reuse the original bounded two-second close directly: creating a new
    # default executor here can strand asyncio.run's executor shutdown on hosts
    # that suppress thread completion wakeups, despite a completed writer drain.
    service._archive_writer_stats=release_observation_writer(service.archive_path)

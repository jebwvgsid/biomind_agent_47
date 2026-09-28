"""Allowlisted local output-policy facts; no provider invocation or credential loading."""
from collections.abc import Mapping


_OUTPUT_LIMIT_KEYS = ("max_tokens", "max_completion_tokens", "max_output_tokens")


def output_policy_projection(client, *, requested=None, provider="unknown", model="unknown"):
    """Inspect only allowlisted numeric fields, never dump client/config objects.

    This is the client-resolved setting, not an assertion about remote adoption
    or an SDK's final parameter rename. No reliable value means UNKNOWN.
    """
    requested = {k:v for k,v in (requested or {}).items()
                 if k in _OUTPUT_LIMIT_KEYS and type(v) is int and v > 0}
    chain=[];seen=set();current=client
    for _ in range(8):
        if current is None or id(current) in seen:break
        seen.add(id(current));chain.append(current);current=getattr(current,'bound',None)
    values={};sources={};history=[]
    for level,item in reversed(list(enumerate(chain))):
        for source,row in (("client_field", {k:getattr(item,k,None) for k in _OUTPUT_LIMIT_KEYS}),
                           ("model_kwargs",getattr(item,'model_kwargs',{})),
                           ("bound_kwargs",getattr(item,'kwargs',{}))):
            if not isinstance(row,Mapping):continue
            for key in _OUTPUT_LIMIT_KEYS:
                value=row.get(key)
                if type(value) is int and value > 0:
                    history.append({'parameter':key,'value':value,'source':source,'binding_depth':level})
                    values[key]=value;sources[key]=source+':'+str(level)
    known=len(values)==1
    parameter=next(iter(values)) if known else ''
    return {'schema_version':1,'protocol':'generate_current_action','provider':str(provider), 'model':str(model),
        'requested_output_limits':requested or 'UNKNOWN','client_resolved_output_limit':values[parameter] if known else 'UNKNOWN',
        'client_parameter':parameter or 'UNKNOWN','parameter_source':sources.get(parameter,'UNKNOWN'),
        'overridden':bool(requested and requested!=values) or any(len({r['value'] for r in history if r['parameter']==k})>1 for k in values),
        'resolution_chain':history,'remote_effective_limit':'UNKNOWN',
        'transport_parameter_confirmation':'UNKNOWN','configuration_ambiguity':len(values)>1}

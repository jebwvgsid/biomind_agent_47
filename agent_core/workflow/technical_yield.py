"""Resume a persisted graph checkpoint at a technical scheduling boundary."""
import asyncio
import inspect
import uuid
from copy import deepcopy
from langgraph.errors import GraphRecursionError


async def stream_checkpointed(app, payload, *, config=None, on_yield=None, **kwargs):
    cfg = deepcopy(config or {})
    cfg.setdefault('configurable', {}).setdefault('thread_id', 'technical-'+uuid.uuid4().hex)
    # This is a scheduling quantum, not a task/business attempt allowance.
    cfg.setdefault('recursion_limit', 64)
    current, prior_checkpoint = payload, None
    while True:
        try:
            async for event in app.astream(current, config=cfg, **kwargs):
                yield event
            return
        except GraphRecursionError:
            try: snapshot = await app.aget_state(cfg)
            except Exception as exc: raise RuntimeError('TECHNICAL_YIELD_CHECKPOINT_UNAVAILABLE: do not replay side effects') from exc
            if not snapshot.next: return
            checkpoint = (snapshot.config.get('configurable') or {}).get('checkpoint_id')
            if not checkpoint or checkpoint == prior_checkpoint:
                raise RuntimeError('TECHNICAL_YIELD_NO_CHECKPOINT_PROGRESS')
            if on_yield is not None:
                result = on_yield({'checkpoint_id':checkpoint,'next_nodes':list(snapshot.next)})
                if inspect.isawaitable(result): await result
            prior_checkpoint = checkpoint
            # None resumes only the saved next nodes; never resubmit original input.
            current = None
            await asyncio.sleep(0)


async def invoke_checkpointed(app, payload, *, config=None, on_yield=None):
    last = None
    async for value in stream_checkpointed(app, payload, config=config, on_yield=on_yield, stream_mode='values'):
        last = value
    return last

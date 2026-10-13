"use client";

import { useCallback, useEffect, useRef, useState } from 'react';
import { BioMindInputError, type BioMindProfile, type BioMindState } from '@/lib/biomind-client';
import { notifyBioMindControl, ownerScope, sameBioMindScope, sendBioMindControl, type BioMindControlFrame } from '@/lib/biomind-controls';

/** An existing owner is observed; opening settings never creates a session. */
export function useBioMindControls(profile: BioMindProfile, project: string | null, sessionId: string | null) {
  const [state, setState] = useState<BioMindState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState<string | null>(null);
  const [uncertain, setUncertain] = useState(false);
  const generation = useRef(0);
  const inFlight = useRef<string | null>(null);
  const mounted = useRef(false);
  const binding = JSON.stringify([profile.serverId, project, sessionId]);
  const bindingRef = useRef(binding); bindingRef.current = binding;
  const refresh = useCallback(async () => {
    const expected = binding;
    if (!sessionId || !project) { setState(null); return null; }
    try {
      const next = await ownerScope(profile, sessionId, project);
      if (!mounted.current || bindingRef.current !== expected) return null;
      setState(next.state);
      return next;
    } catch (caught) {
      if (mounted.current && bindingRef.current === expected) {
        setState(null); setError(caught instanceof Error ? caught.message : 'OWNER_UNAVAILABLE');
      }
      return null;
    }
  }, [binding, sessionId, project, profile]);
  useEffect(() => {
    mounted.current = true;
    const turn = ++generation.current;
    inFlight.current = null;
    setState(null); setError(null); setPending(null); setUncertain(false);
    void refresh();
    const timer = setInterval(() => { void refresh(); }, 5000);
    const update = () => { void refresh(); };
    window.addEventListener('biomind-control-changed', update);
    return () => { mounted.current = false; generation.current = turn + 1; clearInterval(timer); window.removeEventListener('biomind-control-changed', update); };
  }, [refresh]);

  const run = useCallback(async <T,>(command: Record<string, unknown>): Promise<BioMindControlFrame<T>> => {
    const selected = binding, turn = generation.current;
    const current = () => mounted.current && bindingRef.current === selected && generation.current === turn;
    if (!sessionId || !project) throw new BioMindInputError('OWNER_NOT_LOADED');
    const operation = String(command.type);
    if (inFlight.current && operation !== 'abort') throw new BioMindInputError('CONTROL_IN_FLIGHT');
    if (operation !== 'abort') inFlight.current = operation;
    setError(null); setUncertain(false); setPending(String(command.type));
    try {
      const owner = await ownerScope(profile, sessionId, project);
      if (!current()) throw new BioMindInputError('DRAFT_SCOPE_CHANGED');
      const result = await sendBioMindControl<T>(owner.scope, command, { current });
      if (!current()) throw new BioMindInputError('DRAFT_SCOPE_CHANGED', true);
      const after = await refresh();
      if (!current()) throw new BioMindInputError('DRAFT_SCOPE_CHANGED', true);
      if (!after || !sameBioMindScope(after.state, owner.scope)) throw new BioMindInputError('OWNER_CHANGED_STATUS_UNKNOWN', true);
      if (!String(command.type).startsWith("get_")) notifyBioMindControl(owner.scope);
      return result;
    } catch (caught) {
      if (current()) {
        setError(caught instanceof Error ? caught.message : 'CONTROL_STATUS_UNKNOWN');
        setUncertain(caught instanceof BioMindInputError && caught.uncertain);
        await refresh(); // Read-only reconciliation, never retry the write.
      }
      throw caught;
    } finally { if (current()) { if (operation !== "abort") inFlight.current = null; setPending(inFlight.current); } }
  }, [binding, sessionId, project, profile, refresh]);
  return { state, error, pending, uncertain, refresh, run };
}

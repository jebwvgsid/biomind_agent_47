"use client";

import { useCallback, useEffect, useRef, useState, type CSSProperties, type ReactNode } from 'react';
import { useI18n } from '@/hooks/useI18n';
import { useBioMindControls } from '@/hooks/useBioMindControls';
import { type BioMindProfile } from '@/lib/biomind-client';
import { bioMindCapability, controlData, type BioMindModels, type BioMindTools, type BioMindInstructions, type BioMindResources, type BioMindCommand } from '@/lib/biomind-controls';
import { DraggableResizableModal } from './DraggableResizableModal';

const section: CSSProperties = { margin: '0 0 16px', padding: 8, border: '1px solid var(--border)', borderRadius: 'var(--radius-sm)', minWidth: 0 };
const row: CSSProperties = { display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap', marginBottom: 8 };
const field: CSSProperties = { width: '100%', minWidth: 0, padding: 8, color: 'var(--text)', background: 'var(--bg-sunken)', border: '1px solid var(--border)', borderRadius: 'var(--radius-xs)', fontSize: 12 };
const button: CSSProperties = { padding: '4px 8px', color: 'var(--text)', background: 'var(--bg-hover)', border: '1px solid var(--border)', borderRadius: 'var(--radius-xs)', fontSize: 12, cursor: 'pointer' };
const note: CSSProperties = { fontSize: 11, color: 'var(--text-muted)', overflowWrap: 'anywhere', margin: '8px 0', lineHeight: 1.5 };
const pre: CSSProperties = { ...note, fontFamily: 'var(--font-mono)', whiteSpace: 'pre-wrap', maxHeight: 320, overflow: 'auto' };

type Run = ReturnType<typeof useBioMindControls>['run'];

function InstructionsEditor({ profile, project, hasOwner, run }: { profile: BioMindProfile; project: string | null; hasOwner: boolean; run: Run }) {
  const { t } = useI18n();
  const [scope, setScope] = useState('global');
  const [document, setDocument] = useState<BioMindInstructions | null>(null);
  const [draft, setDraft] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [saved, setSaved] = useState(false);
  const epoch = useRef(0);
  useEffect(() => { const current = ++epoch.current; setDocument(null); setDraft(''); setError(null); setSaved(false); return () => { epoch.current = current + 1; }; }, [scope, project, profile.serverId]);
  const request = async (save: boolean) => {
    const current = epoch.current;
    if (!project || (save && !document)) return;
    setPending(true); setError(null);
    try {
      const payload = { scope, ...(save ? { expectedVersion: document!.version, text: draft, create: !document!.exists } : {}) };
      let data: BioMindInstructions;
      if (hasOwner) data = controlData(await run<BioMindInstructions>({ type: save ? 'save_instructions' : 'get_instructions', ...payload }));
      else {
        const url = '/api/agent/instructions' + (save ? '' : `?${new URLSearchParams({ cwd: project, scope })}`);
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 25_000);
        try {
          const response = await fetch(url, save ? { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ cwd: project, ...payload }), signal: controller.signal } : { cache: 'no-store', signal: controller.signal });
          const body = await response.json();
          if (!response.ok || !body.success) throw new Error(body.code ?? body.error ?? `HTTP ${response.status}`);
          data = body.data;
        } catch (caught) {
          if (controller.signal.aborted) throw new Error(save ? 'INSTRUCTIONS_OUTCOME_UNKNOWN' : 'INSTRUCTIONS_READ_FAILED');
          throw caught;
        } finally { clearTimeout(timer); }
      }
      if (epoch.current !== current) return;
      if (!data || data.scope !== scope || typeof data.text !== 'string' || typeof data.version !== 'string') throw new Error('INSTRUCTIONS_RECEIPT_INVALID');
      // A conflict/error never replaces the draft. Successful saves are read back by the native helper.
      setDocument(data); setDraft(data.text); setSaved(save && data.saved === true);
    } catch (caught) { if (epoch.current === current) setError(caught instanceof Error ? caught.message : 'INSTRUCTIONS_STATUS_UNKNOWN'); }
    finally { if (epoch.current === current) setPending(false); }
  };
  return <fieldset style={section}>
    <legend>{t('biomind.instructions')}</legend>
    <label style={row}><span>{t('biomind.source')}</span><select aria-label={t('biomind.source')} value={scope} onChange={event => setScope(event.target.value)} disabled={pending} style={field}>
      {['global', 'project', 'project-base'].map(value => <option value={value} key={value}>{t(`biomind.${value}`)}</option>)}
    </select></label>
    <button type="button" style={button} disabled={!project || pending} onClick={() => void request(false)}>{t('biomind.read')}</button>
    {!document && <p style={note}>{t('biomind.readRequired')}</p>}
    {document && <>
      <p style={note}>{document.source} · {document.path}</p>
      <textarea aria-label={t('biomind.draft')} value={draft} disabled={pending} onChange={event => { setDraft(event.target.value); setSaved(false); }} rows={8} style={{ ...field, resize: 'vertical' }} />
      <button type="button" style={button} disabled={pending} onClick={() => void request(true)}>{t(document.exists ? 'biomind.save' : 'biomind.create')}</button>
      <p role="status" style={note}>{saved ? t('biomind.saved') : ''}{document.loaded === true ? ` · ${t('biomind.loaded')}` : document.loaded === false ? ` · ${t('biomind.pendingLoad')}` : ''}</p>
      <p style={note}>{t(scope === 'global' ? 'biomind.preferencesEffect' : 'biomind.agentsEffect')}</p>
    </>}
    {error && <p role="alert" style={note}>{error === "INSTRUCTIONS_HELPER_NOT_CONFIGURED" ? t("biomind.instructionsUnavailable") : error}</p>}
  </fieldset>;
}

/** A section inside the existing settings surface, not a second application shell. */
export function BioMindSettingsSection({ profile, project, sessionId, resourcesOnly = false }: { profile: BioMindProfile; project: string | null; sessionId: string | null; resourcesOnly?: boolean }) {
  const { t } = useI18n();
  const { state, error, uncertain, pending, refresh, run } = useBioMindControls(profile, project, sessionId);
  const [models, setModels] = useState<BioMindModels | null>(null);
  const [tools, setTools] = useState<BioMindTools | null>(null);
  const [toolDraft, setToolDraft] = useState<string[]>([]);
  const [resources, setResources] = useState<BioMindResources | null>(null);
  const [skillScope, setSkillScope] = useState<'user' | 'project'>('user');
  const [commands, setCommands] = useState<BioMindCommand[] | null>(null);
  const [snapshot, setSnapshot] = useState<Record<string, unknown> | null | undefined>(undefined);
  const [compactText, setCompactText] = useState('');
  const [compactResult, setCompactResult] = useState<Record<string, unknown> | null>(null);
  const [dialog, setDialog] = useState<{ title: string; text: string } | null>(null);
  const [localError, setLocalError] = useState<string | null>(null);
  const binding = JSON.stringify([profile.serverId, project, sessionId]);
  const bindingRef = useRef(binding); bindingRef.current = binding;
  useEffect(() => { setModels(null); setTools(null); setResources(null); setCommands(null); setSnapshot(undefined); setCompactResult(null); setLocalError(null); }, [binding]);
  const invoke = useCallback(async (action: () => Promise<void>) => {
    const expected = binding;
    setLocalError(null);
    try { await action(); } catch (caught) { if (bindingRef.current === expected) setLocalError(caught instanceof Error ? caught.message : 'CONTROL_STATUS_UNKNOWN'); }
  }, [binding]);
  const cap = (name: string) => bioMindCapability(state, name);
  const disabled = (name: string) => !!pending || !cap(name).available;
  const reason = (name: string) => {
    const capability = cap(name);
    if (capability.reason === 'OWNER_NOT_LOADED') return t('biomind.notLoaded');
    if (!capability.supported) return t('biomind.unsupported');
    if (capability.reason?.includes('ACTIVE') || capability.reason?.includes('BUSY')) return t('biomind.busy');
    return capability.reason ?? t('biomind.busy');
  };
  const actionButton = (label: string, capability: string, action: () => Promise<void>): ReactNode => <button type="button" style={button} disabled={disabled(capability)} title={disabled(capability) ? reason(capability) : undefined} onClick={() => void invoke(action)}>{t(label)}</button>;
  const loadModels = async () => setModels(controlData(await run<BioMindModels>({ type: 'get_models' })));
  const loadTools = async () => { const data = controlData(await run<BioMindTools>({ type: 'get_tools' })); setTools(data); setToolDraft(data.selected ?? data.effective); };
  const loadResources = async () => setResources(controlData(await run<BioMindResources>({ type: 'get_resources' })));
  const memory = state?.memory as { history_enabled?: boolean; memory_mode?: string } | null | undefined;
  const thinking = typeof state?.thinkingLevel === 'string' ? state.thinkingLevel : models?.thinkingLevel;
  const thinkingLevels = Array.isArray(state?.availableThinkingLevels) ? state.availableThinkingLevels as string[] : models?.availableThinkingLevels ?? [];
  const runtimeDetails = { serverId: profile.serverId, project, sessionId, ownerGeneration: state?.ownerGeneration ?? null,
    loadedBuild: state?.loadedBuild ?? state?.loaded_build ?? null, protocolVersion: state?.protocolVersion ?? null,
    capabilities: state?.capabilities ?? null, autoCompactionScope: state?.autoCompactionScope ?? null };

  return <div data-biomind-settings style={{ color: 'var(--text)', fontSize: 12, padding: 8 }}>
    <div style={row}><strong>{t(resourcesOnly ? 'biomind.tools' : 'biomind.title')}</strong><button type="button" style={button} onClick={() => void refresh()}>{t('biomind.refresh')}</button></div>
    {!state && <p role="status" style={note}>{t('biomind.notLoaded')}</p>}
    {(error || localError) && <p role="alert" style={note}>{error || localError}{uncertain ? ` · ${t('biomind.unknown')}` : ''}</p>}
    {!resourcesOnly && <>
      <fieldset style={section}><legend>{t('biomind.model')}</legend>
        <p style={note}>{t('biomind.sessionScope')}</p>
        <p>{state?.model ? `${state.model.provider}/${state.model.id}` : t('biomind.unknownValue')}</p>
        {actionButton('biomind.loadModels', 'getModels', loadModels)}
        {models && <label style={row}><span>{t('biomind.model')}</span><select style={field} aria-label={t('biomind.model')} value={state?.model ? `${state.model.provider}/${state.model.id}` : ''} disabled={disabled('setModel')} onChange={event => {
          const model = models.models.find(item => `${item.provider}/${item.id}` === event.target.value); if (!model) return;
          void invoke(async () => { setModels(controlData(await run<BioMindModels>({ type: 'set_model', provider: model.provider, modelId: model.id }))); });
        }}>
          <option value="" disabled>{t('biomind.unknownValue')}</option>
          {models.models.map(model => <option key={`${model.provider}/${model.id}`} value={`${model.provider}/${model.id}`} disabled={model.authenticated === false}>{model.name} · {model.provider}{model.authenticated === false ? ` · ${t('biomind.authMissing')}` : ''}</option>)}
        </select></label>}
        <label style={row}><span>{t('biomind.thinking')}</span><select style={field} aria-label={t('biomind.thinking')} value={thinking ?? ''} disabled={disabled('setThinking')} onChange={event => { const level = event.target.value; void invoke(async () => { setModels(controlData(await run<BioMindModels>({ type: 'set_thinking_level', level }))); }); }}>
          {!thinking && <option value="" disabled>{t('biomind.unknownValue')}</option>}{thinkingLevels.map(level => <option key={level} value={level}>{level}</option>)}
        </select></label>
        {!cap('setModel').available && <p style={note}>{reason('setModel')}</p>}
      </fieldset>
      <InstructionsEditor key={binding} profile={profile} project={project} hasOwner={!!state} run={run} />
      <fieldset style={section}><legend>{t('biomind.snapshot')}</legend>
        {actionButton('biomind.readSnapshot', 'promptSnapshot', async () => { const data = controlData(await run<{ snapshot: Record<string, unknown> | null }>({ type: 'get_prompt_snapshot' })); setSnapshot(data.snapshot); })}
        {snapshot === null && <p style={note}>{t('biomind.noSnapshot')}</p>}
        {snapshot && <><p style={note}>{t('biomind.requestIdentity')}: {String(snapshot.requestId ?? '')} / {String(snapshot.ownerGeneration ?? '')} / {String(snapshot.time ?? '')}</p><p style={note}>{t('biomind.snapshotPhase')}: {String(snapshot.phase ?? '')}</p><pre style={pre}>{JSON.stringify(snapshot, null, 2)}</pre></>}
      </fieldset>
      <fieldset style={section}><legend>{t('biomind.memory')}</legend>
        <p style={note}>{t('biomind.memoryHint')}</p>
        <label style={row}><input type="checkbox" checked={state?.autoCompactionEnabled === true} disabled={disabled('autoCompaction') || typeof state?.autoCompactionEnabled !== 'boolean'} onChange={event => { const enabled = event.target.checked; void invoke(async () => { await run({ type: 'set_auto_compaction', enabled, scope: 'session' }); }); }} />{t('biomind.autoCompaction')} · {t('biomind.sessionScope')}</label>
        <label style={row}><input type="checkbox" checked={memory?.history_enabled === true} disabled={disabled('memorySettings') || typeof memory?.history_enabled !== 'boolean'} onChange={event => { const history_enabled = event.target.checked; void invoke(async () => { await run({ type: 'settings', history_enabled }); }); }} />{t('biomind.history')}</label>
        <label style={row}><span>{t('biomind.memoryMode')}</span><select style={field} aria-label={t('biomind.memoryMode')} value={memory?.memory_mode ?? ''} disabled={disabled('memorySettings') || !memory} onChange={event => { const memory_mode = event.target.value; void invoke(async () => { await run({ type: 'settings', memory_mode }); }); }}><option value="" disabled>{t('biomind.unknownValue')}</option>{['ask', 'auto', 'manual', 'disabled'].map(mode => <option key={mode} value={mode}>{t(`biomind.${mode}`)}</option>)}</select></label>
        <textarea rows={3} style={{ ...field, resize: 'vertical' }} aria-label={t('biomind.compactInstructions')} placeholder={t('biomind.compactInstructions')} value={compactText} onChange={event => setCompactText(event.target.value)} />
        <div style={row}>{actionButton('biomind.compact', 'compact', async () => {
          setCompactResult(null); const result = await run<{ result?: Record<string, unknown> }>({ type: 'compact', customInstructions: compactText });
          if (result.status !== 'compacted') throw new Error('COMPACTION_NOT_CONFIRMED');
          setCompactResult(result.data?.result ?? null);
        })}
        <button type="button" style={button} disabled={state?.controlBusy !== 'compact' && pending !== 'compact'} onClick={() => void invoke(async () => { await run({ type: 'abort' }); })}>{t('biomind.cancelCompact')}</button></div>
        {compactResult && <><p>{t('biomind.summary')}</p><pre style={pre}>{JSON.stringify(compactResult, null, 2)}</pre></>}
        {actionButton('biomind.reload', 'reload', async () => { await run({ type: 'reload' }); if (resources) await loadResources(); if (tools) await loadTools(); if (models) await loadModels(); })}
        <p style={note}>{t('biomind.reloadHint')}</p>
      </fieldset>
    </>}
    <fieldset style={section}><legend>{t('biomind.tools')}</legend>
      <p style={note}>{t('biomind.toolsHint')}</p>
      {actionButton('biomind.loadTools', 'getTools', loadTools)}
      {tools && <>
        {tools.registered.map(tool => <label key={tool.name} style={row} title={tool.description}><input type="checkbox" checked={toolDraft.includes(tool.name)} disabled={disabled('setTools') || !tools.allowed.includes(tool.name)} onChange={event => setToolDraft(current => event.target.checked ? [...new Set([...current, tool.name])] : current.filter(name => name !== tool.name))} />{tool.name}</label>)}
        {actionButton('biomind.applyTools', 'setTools', async () => { const data = controlData(await run<BioMindTools>({ type: 'set_tools', tools: toolDraft })); setTools(data); setToolDraft(data.selected ?? data.effective); })}
        <p style={note}>{t('biomind.toolEffective')}: {tools.effective.join(', ')}</p>
      </>}
      <div style={row}>{actionButton('biomind.loadResources', 'resources', loadResources)}</div>
      {resources && <>
        {!resources.projectTrusted && <p style={note}>{t('biomind.untrusted')}</p>}
        <p style={note}>{t('biomind.skillHint')}</p><p style={note}>{t('biomind.skillCommands')}: {t(`biomind.${resources.enableSkillCommands}`)}</p>
        <label style={row}><span>{t('biomind.skillSaveScope')}</span><select aria-label={t('biomind.skillSaveScope')} style={field} value={skillScope} disabled={disabled('setSkill')} onChange={event => setSkillScope(event.target.value as 'user' | 'project')}>
          {(resources.selectionScopes ?? []).map(scope => <option key={scope} value={scope}>{t(scope === 'user' ? 'biomind.userScope' : 'biomind.project')}</option>)}
        </select></label>
        {resources.skills.map(skill => <div key={skill.resourceId} style={{ marginBottom: 16 }}>
          <label style={row}><input type="checkbox" checked={skill.configuredEnabled} disabled={disabled('setSkill') || !resources.selectionScopes?.includes(skillScope)} onChange={event => { const enabled = event.target.checked; void invoke(async () => { setResources(controlData(await run<BioMindResources>({ type: 'set_skill', resourceId: skill.resourceId, scope: skillScope, enabled }))); }); }} />{skill.name}</label>
          <p style={note}>{skill.description}</p><p style={note}>{skill.source} · {skill.path}</p>
          <p style={note}>{t('biomind.loaded')}: {t(`biomind.${skill.loaded}`)} · {t(skill.disableModelInvocation ? 'biomind.explicitOnly' : 'biomind.autoDiscovery')}</p>
          {actionButton('biomind.viewSkill', 'resources', async () => { const data = controlData(await run<BioMindResources & { skillContent: { resourceId: string; text: string; path: string } }>({ type: 'get_resources', resourceId: skill.resourceId })); if (data.skillContent?.resourceId !== skill.resourceId) throw new Error('RESOURCE_RECEIPT_INVALID'); setDialog({ title: skill.name, text: data.skillContent.text }); })}
        </div>)}
        <details><summary>{t('biomind.coreExtensions')}</summary><pre style={pre}>{JSON.stringify(resources.coreExtensions, null, 2)}</pre></details>
        {resources.diagnostics.length > 0 && <pre style={pre}>{JSON.stringify(resources.diagnostics, null, 2)}</pre>}
        {actionButton('biomind.reload', 'reload', async () => { await run({ type: 'reload' }); await loadResources(); })}
      </>}
      <div style={row}>{actionButton('biomind.readCommands', 'getCommands', async () => setCommands(controlData(await run<{ commands: BioMindCommand[] }>({ type: 'get_commands' })).commands))}</div>
      {commands?.map(command => <p key={`${command.type}/${command.name}`} style={note}>/{command.name} · {command.description} · {command.type} / {typeof command.source === "string" ? command.source : JSON.stringify(command.source)}{command.supported ? '' : ` · ${command.reason ?? t('biomind.unsupported')}`}</p>)}
    </fieldset>
    <details><summary>{t('biomind.runtime')}</summary><p style={note}>{profile.serverId} · {project}</p><p style={note}>{t('biomind.owner')}: {state?.ownerGeneration ?? t('biomind.notLoaded')}</p><pre style={pre}>{JSON.stringify(runtimeDetails, null, 2)}</pre><button type="button" style={button} onClick={() => void invoke(async () => { await navigator.clipboard.writeText(JSON.stringify(runtimeDetails, null, 2)); })}>{t('biomind.copy')}</button></details>
    {dialog && <DraggableResizableModal title={dialog.title} hint={t('biomind.source')} onClose={() => setDialog(null)}><pre style={{ ...pre, maxHeight: 'none', padding: 16 }}>{dialog.text}</pre></DraggableResizableModal>}
  </div>;
}

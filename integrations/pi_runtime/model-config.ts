/** Shared selection validation for the product host and P10 offline wire. */
import type { ModelRuntime } from '@earendil-works/pi-coding-agent';

export interface ModelSelection {
  schema: 'biomind-active-model-config-v1' | 'biomind-active-model-config-v2'; endpoint_base_url?: string; provider: string; model: string;
  api: 'openai-completions'; thinking: 'off' | 'minimal' | 'low' | 'medium' | 'high';
  endpoint_hostname: string; origin: 'user-confirmed-active-product-config' | 'active-launch-environment';
  confirmed_active: true;
}
export function validateModelSelection(value: unknown): ModelSelection {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('MODEL_SELECTION_REQUIRED');
  const record = value as Record<string, unknown>;
  const fields = ['schema','provider','model','api','thinking','endpoint_hostname','origin','confirmed_active'];
  if (record.schema === 'biomind-active-model-config-v2') fields.push('endpoint_base_url');
  if (Object.keys(record).length !== fields.length || fields.some(key => !(key in record))) throw new Error('MODEL_SELECTION_FIELDS');
  if (!['biomind-active-model-config-v1','biomind-active-model-config-v2'].includes(String(record.schema)) || record.confirmed_active !== true ||
      !['user-confirmed-active-product-config','active-launch-environment'].includes(String(record.origin))) throw new Error('MODEL_SELECTION_UNCONFIRMED');
  for (const key of ['provider','model']) {
    const text = record[key];
    if (typeof text !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,127}$/.test(text) ||
        /(?:^|[/+])(?:sk[-_]|key[-_]|token[-_]|bearer[-_]|api[-_]?key)/i.test(text) || /[A-Za-z0-9]{40,}/.test(text)) throw new Error('MODEL_SELECTION_IDENTIFIER');
  }
  if (record.api !== 'openai-completions' || !['off','minimal','low','medium','high'].includes(String(record.thinking))) throw new Error('MODEL_SELECTION_PROTOCOL');
  if (typeof record.endpoint_hostname !== 'string' || !/^[A-Za-z0-9.-]{1,253}$/.test(record.endpoint_hostname) || record.endpoint_hostname.includes('..')) throw new Error('MODEL_SELECTION_HOSTNAME');
  if (record.schema === 'biomind-active-model-config-v2') {
    if (typeof record.endpoint_base_url !== 'string' || record.endpoint_base_url.length > 2048 ||
        !/^https:\/\/[A-Za-z0-9.-]+(?::443)?(?:\/[A-Za-z0-9._~/-]*)?$/.test(record.endpoint_base_url)) throw new Error('MODEL_SELECTION_ENDPOINT');
    const endpoint = new URL(record.endpoint_base_url);
    if (endpoint.protocol !== 'https:' || endpoint.hostname !== record.endpoint_hostname || endpoint.username || endpoint.password ||
        endpoint.search || endpoint.hash || (endpoint.port && endpoint.port !== '443') || !/^\/[A-Za-z0-9._~/-]*$/.test(endpoint.pathname)) throw new Error('MODEL_SELECTION_ENDPOINT');
  }
  return record as unknown as ModelSelection;
}
/** Current BioMind model, retaining its model id and application token budget.
 * Token limits are application settings, not a claim of online provider support.
 * Prices are unverified and no monetary estimate is presented by the host.
 */
export function configuredProvider(raw: unknown): Parameters<ModelRuntime['registerProvider']>[1] | undefined {
  const selection = validateModelSelection(raw);
  if (selection.provider !== 'deepseek' || selection.model !== 'deepseek-v4-flash') return undefined;
  if (!selection.endpoint_base_url || selection.endpoint_hostname !== 'api.deepseek.com') throw new Error('CURRENT_CUSTOM_MODEL_EXACT_ENDPOINT_REQUIRED');
  return { api: 'openai-completions', baseUrl: selection.endpoint_base_url, models: [{
    id: selection.model, name: selection.model, api: 'openai-completions', baseUrl: selection.endpoint_base_url,
    reasoning: true, input: ['text'], contextWindow: 312144, maxTokens: 50000,
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
    compat: { supportsStore: false, supportsDeveloperRole: false, maxTokensField: 'max_tokens',
      requiresReasoningContentOnAssistantMessages: true, thinkingFormat: 'deepseek', supportsStrictMode: false },
    thinkingLevelMap: { minimal: null, low: null, medium: null, high: 'high' },
  }] };
}
/** Required SDK zero-rate placeholders are not evidence of a free provider. */
export function hasUnverifiedModelPricing(model: { provider?: string; id?: string }): boolean {
  return model.provider === 'deepseek' && model.id === 'deepseek-v4-flash';
}
export function selectConfiguredModel(runtime: ModelRuntime, raw: unknown) {
  const selection = validateModelSelection(raw);
  const custom = configuredProvider(selection);
  if (custom) runtime.registerProvider(selection.provider, custom);
  const model = runtime.getModel(selection.provider, selection.model);
  if (!model) throw new Error('CURRENT_SELECTED_MODEL_NOT_IN_PINNED_PRODUCT_CATALOG');
  if (selection.thinking !== 'off' && model.thinkingLevelMap?.[selection.thinking] === null) throw new Error('CURRENT_THINKING_LEVEL_UNSUPPORTED_NO_NORMALIZATION');
  const endpoint = new URL(model.baseUrl);
  if (model.api !== selection.api || endpoint.hostname !== selection.endpoint_hostname ||
      (selection.endpoint_base_url && endpoint.href !== new URL(selection.endpoint_base_url).href) ||
      !['http:','https:'].includes(endpoint.protocol) || endpoint.username || endpoint.password || endpoint.search || endpoint.hash) {
    throw new Error('CURRENT_PROTOCOL_OR_ENDPOINT_DIFFERS_FROM_PRODUCT_MODEL_NO_SUBSTITUTION');
  }
  return model;
}

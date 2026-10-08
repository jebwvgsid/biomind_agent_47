import test from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import type { DomainLifecycle } from '../domain-client.js';
import { createHarness, FakeDomain, text } from './extension-harness.js';

class ObservableDomain extends FakeDomain {
  readonly events = new EventEmitter();
  lifecycle: DomainLifecycle = { state: 'starting', stage: 'startup' };
  on(event: 'lifecycle', listener: (event: DomainLifecycle) => void) { this.events.on(event, listener); }
  off(event: 'lifecycle', listener: (event: DomainLifecycle) => void) { this.events.off(event, listener); }
  transition(event: DomainLifecycle) { this.lifecycle = event; this.events.emit('lifecycle', event); }
  override async hostCall(method: string, params: Record<string, unknown>, options?: any) {
    if (['unavailable', 'failed', 'closed'].includes(this.lifecycle.state)) throw this.lifecycle.error ?? new Error('backend disconnected');
    const value = await super.hostCall(method, params, options);
    if (this.lifecycle.state === 'starting') this.transition({ state: 'ready', stage: 'startup' });
    return value;
  }
}

test('a real extension revokes admission on transport loss before final diagnostics and never replays input', async () => {
  const domain = new ObservableDomain();
  const h = await createHarness({ executionProfile: 'native', domain, script: [text('synthetic response')] });
  const states: any[] = [];
  const off = h.bus.on('biomind:backend', value => states.push(value));
  try {
    assert.equal(domain.calls[0]?.method, 'request.status', 'readiness requires a backend response');
    assert.equal(h.requests.length, 0);
    await h.prompt('first explicit input'); assert.equal(h.requests.length, 1);
    const calls = domain.calls.length;
    domain.transition({ state: 'unavailable', stage: 'runtime' });
    await h.prompt('must not send while collecting stderr');
    assert.equal(h.requests.length, 1); assert.equal(domain.calls.length, calls);
    domain.transition({ state: 'failed', stage: 'runtime', error: new Error('late diagnostic') });
    await h.prompt('must not retry after failure');
    assert.equal(h.requests.length, 1); assert.equal(h.getDomainCreationCount(), 1);
    assert.deepEqual(states.map(state => state.state), ['unavailable', 'failed']);
    assert.equal(domain.events.listenerCount('lifecycle'), 1);
  } finally { off(); await h.close(); }
  assert.equal(domain.events.listenerCount('lifecycle'), 0);
  assert.equal(domain.closed, 1);
});

test('reload replaces its lifecycle listener without multiplying notifications or owning a second backend', async () => {
  const domain = new ObservableDomain();
  const h = await createHarness({ domain });
  try {
    await h.session.reload(); await h.session.reload();
    assert.equal(h.getDomainCreationCount(), 1);
    assert.equal(domain.events.listenerCount('lifecycle'), 1);
  } finally { await h.close(); }
  assert.equal(domain.events.listenerCount('lifecycle'), 0);
});

test('status completion cannot restore readiness after a transport failure in the same turn', async () => {
  const domain = new ObservableDomain();
  domain.receipts.set('request.status', () => {
    domain.transition({ state: 'failed', stage: 'startup', error: new Error('status and EOF raced') });
    return { execution_profile: 'native' };
  });
  const h = await createHarness({ executionProfile: 'native', domain });
  try {
    await h.prompt('must not send');
    assert.equal(h.requests.length, 0);
    const states = h.busEvents.filter(event => event.channel === 'biomind:backend').map(event => event.value.state);
    assert(!states.includes('ready'));
    assert.equal(states.filter(state => state === 'failed').length, 1, 'one terminal diagnostic per failure');
  } finally { await h.close(); }
});

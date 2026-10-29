import test from 'node:test';
import assert from 'node:assert/strict';
import { createFeedState, applyFeedEvent } from '../public/conversation-feed.js';

test('external conversation displays text and states without duplicating reconnect events', () => {
  const state = createFeedState();
  const event = { id: 'm1', type: 'message', role: 'user', text: '꽃을 선물하고 싶어.' };
  assert.equal(applyFeedEvent(state, event), true);
  assert.equal(applyFeedEvent(state, event), false);
  assert.equal(state.messages.length, 1);
  applyFeedEvent(state, { id: 's1', type: 'state', state: 'thinking' });
  assert.equal(state.phase, 'thinking');
  applyFeedEvent(state, { id: 'm2', type: 'message', role: 'assistant', text: '<script>text only</script>' });
  assert.equal(state.messages[1].text, '<script>text only</script>');
  assert.equal(state.messages[1].role, 'assistant');
});

test('board audio attaches to its sentence whether it arrives after or before the text', () => {
  const state = createFeedState({ audioBase: 'http://127.0.0.1:4321/events' });
  applyFeedEvent(state, { id: 'm1', type: 'message', role: 'user', text: '꽃을 선물하고 싶어.', turn_id: 't1' });
  assert.equal(applyFeedEvent(state, { id: 'a1', type: 'audio', role: 'user', turn_id: 't1', url: '/audio/abc.ogg', duration_ms: 1200 }), true);
  assert.deepEqual(state.messages[0].audio, { url: 'http://127.0.0.1:4321/audio/abc.ogg', durationMs: 1200 });

  applyFeedEvent(state, { id: 'a2', type: 'audio', role: 'assistant', turn_id: 't1', url: 'http://127.0.0.1:4321/audio/def.ogg' });
  assert.equal(state.pendingAudio.length, 1);
  applyFeedEvent(state, { id: 'm2', type: 'message', role: 'assistant', text: '흰 꽃을 준비할까요?', turn_id: 't1' });
  assert.equal(state.messages[1].audio.url, 'http://127.0.0.1:4321/audio/def.ogg');
  assert.equal(state.pendingAudio.length, 0);
  assert.equal(state.messages[1].turnId, 't1');
});

test('audio must come from the stream origin and cannot alter state when invalid', () => {
  const state = createFeedState({ audioBase: 'http://127.0.0.1:4321/events' });
  applyFeedEvent(state, { id: 'm1', type: 'message', role: 'user', text: '안녕', turn_id: 't1' });
  const before = structuredClone(state);
  for (const event of [
    { id: 'x1', type: 'audio', role: 'user', turn_id: 't1', url: 'https://evil.example/a.ogg' },
    { id: 'x2', type: 'audio', role: 'user', turn_id: 't1', url: 'http://localhost:4321/a.ogg' },
    { id: 'x3', type: 'audio', role: 'user', turn_id: 't1', url: 'http://user:pw@127.0.0.1:4321/a.ogg' },
    { id: 'x4', type: 'audio', role: 'system', turn_id: 't1', url: '/a.ogg' },
    { id: 'x5', type: 'audio', role: 'user', url: '/a.ogg' },
    { id: 'x6', type: 'audio', role: 'user', turn_id: '../t1', url: '/a.ogg' },
    { id: 'x7', type: 'audio', role: 'user', turn_id: 't1', url: '/a.ogg', duration_ms: -1 },
    { id: 'x8', type: 'message', role: 'user', text: 'x', turn_id: 'bad id' },
  ]) {
    assert.throws(() => applyFeedEvent(state, event));
    assert.deepEqual(state, before);
  }
  const noAudio = createFeedState();
  assert.throws(() => applyFeedEvent(noAudio, { id: 'a', type: 'audio', role: 'user', turn_id: 't1', url: '/a.ogg' }));
});

test('invalid events cannot change displayed state or run operator actions', () => {
  const state = createFeedState();
  for (const event of [null, {}, { id: 'bad', type: 'action', action: 'release' },
    { id: 'bad', type: 'state', state: 'completed' },
    { id: 'bad', type: 'message', role: 'system', text: 'x' },
    { id: 'bad', type: 'message', role: 'user', text: 'x'.repeat(2001) }]) {
    assert.throws(() => applyFeedEvent(state, event));
    assert.deepEqual(state, createFeedState());
  }
});

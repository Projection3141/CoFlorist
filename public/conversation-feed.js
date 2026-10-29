const ID_PATTERN = /^[A-Za-z0-9_-]{1,160}$/;
const ROLES = ['user', 'assistant'];

// audioBase: the stream URL. Audio links must come from the same origin as the stream itself.
export function createFeedState({ audioBase } = {}) {
  let audioOrigin = '';
  try {
    const base = audioBase ? new URL(audioBase) : null;
    if (base && ['http:', 'https:'].includes(base.protocol)) audioOrigin = base.origin;
  } catch { audioOrigin = ''; }
  return { phase: 'idle', messages: [], seen: [], error: '', audioOrigin, pendingAudio: [] };
}

function checkTurn(event, required = false) {
  if (event.turn_id === undefined && !required) return undefined;
  if (typeof event.turn_id !== 'string' || !ID_PATTERN.test(event.turn_id)) throw new Error('turn_id를 확인해 주세요.');
  return event.turn_id;
}

function checkAudio(state, event) {
  if (!ROLES.includes(event.role)) throw new Error('음성의 role을 확인해 주세요.');
  checkTurn(event, true);
  if (!state.audioOrigin || typeof event.url !== 'string' || !event.url || event.url.length > 300) throw new Error('음성 주소를 확인해 주세요.');
  let url;
  try { url = new URL(event.url, state.audioOrigin); } catch { throw new Error('음성 주소를 확인해 주세요.'); }
  if (url.origin !== state.audioOrigin || url.username || url.password) throw new Error('대화 스트림과 같은 주소의 음성만 재생할 수 있어요.');
  const durationMs = event.duration_ms;
  if (durationMs !== undefined && !(Number.isFinite(durationMs) && durationMs >= 0 && durationMs <= 600000)) throw new Error('음성 길이를 확인해 주세요.');
  return { url: url.href, durationMs: durationMs ?? null };
}

// The stream carries display events only: never robot commands or model prompts.
// Audio is attached to the message of the same turn and role, even if it arrives first.
export function applyFeedEvent(state, event, now = new Date().toISOString()) {
  if (!event || typeof event !== 'object' || Array.isArray(event)) throw new Error('JSON 이벤트가 필요합니다.');
  if (typeof event.id !== 'string' || !event.id.trim() || event.id.length > 160) throw new Error('고유 이벤트 id가 필요합니다.');
  if (state.seen.includes(event.id)) return false;
  let audio;
  if (event.type === 'state') {
    if (!['idle', 'listening', 'thinking', 'speaking', 'error'].includes(event.state)) throw new Error('대화 상태를 확인해 주세요.');
    checkTurn(event);
  } else if (event.type === 'message') {
    if (!ROLES.includes(event.role) || typeof event.text !== 'string' || !event.text.trim() || event.text.length > 2000) throw new Error('role과 1~2,000자 text가 필요합니다.');
    checkTurn(event);
  } else if (event.type === 'audio') {
    audio = checkAudio(state, event);
  } else throw new Error('지원 이벤트는 state, message, audio입니다.');
  state.seen.push(event.id);
  if (state.seen.length > 500) state.seen.shift();
  state.error = '';
  if (event.type === 'state') {
    state.phase = event.state;
    if (event.state === 'error') state.error = '외부 대화 장치 확인 필요';
  } else if (event.type === 'message') {
    const message = { id: event.id, role: event.role, text: event.text.trim(), createdAt: now };
    if (event.turn_id) {
      message.turnId = event.turn_id;
      const index = state.pendingAudio.findIndex(item => item.turnId === event.turn_id && item.role === event.role);
      if (index >= 0) message.audio = state.pendingAudio.splice(index, 1)[0].audio;
    }
    state.messages.push(message);
    if (state.messages.length > 200) state.messages.shift();
  } else {
    const message = [...state.messages].reverse().find(item => item.turnId === event.turn_id && item.role === event.role);
    if (message) message.audio = audio;
    else {
      state.pendingAudio = state.pendingAudio.filter(item => !(item.turnId === event.turn_id && item.role === event.role));
      state.pendingAudio.push({ turnId: event.turn_id, role: event.role, audio });
      if (state.pendingAudio.length > 50) state.pendingAudio.shift();
    }
  }
  return true;
}

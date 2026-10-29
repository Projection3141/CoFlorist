import { createFeedState, applyFeedEvent } from './conversation-feed.js';
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const colorNames = { white: '흰 꽃', red: '빨간 꽃', yellow: '노란 꽃', purple: '보라 꽃' };
const positionNames = { center: '가운데', left: '왼쪽', right: '오른쪽' };
const phaseNames = { idle: '대화 대기', planning: '꽃 선택', placing: '꽃 배치 중', paused: '잠시 대기', retry: '다시 잡기', approved: '작품 확인 완료', lifting: '작품 들어 올리기', handover: '전달 중', awaiting_receipt: '수령 확인 대기', completed: '전달 완료' };
const brainNames = { idle: '신경망 대기', starting: '불러오는 중', ready: '실제 회로 연결', running: '신경 활동 계산 중', error: '연결 확인 필요' };
let state = null;
let initialized = false;
let messagesSignature = '';
let graphSignature = '';
let toastTimer;
let cameraStream = null;
let cameraGeneration = 0;
let recognition = null;
let listening = false;
let speaking = false;
let speechGeneration = 0;
let externalSource = null;
let externalMode = false;
let externalConnected = false;
let externalFeed = createFeedState();
let externalSilenceTimer = null;
let externalBrainPending = false;
// Reused across re-renders so a playing clip is not interrupted when new events arrive.
const externalAudio = new Map();
let selectedVoice = '';
let voices = [];
let chatInFlight = false;
let chatGeneration = 0;
let actionInFlight = false;
let eventSource = null;
let fallbackTimer = null;
let spokenIds = new Set();
let pendingSpeech = null;
let discardRecognition = false;
let localView = location.hash === '#settings' ? 'settings' : 'main';

function toast(message, error = false) {
  const el = $('#toast');
  el.textContent = message;
  el.classList.toggle('error', error);
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, error ? 7000 : 3500);
}

async function api(path, body) {
  const response = await fetch(path, {
    method: body === undefined ? 'GET' : 'POST',
    headers: body === undefined ? { Accept: 'application/json' } : { 'Content-Type': 'application/json', Accept: 'application/json' },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  let result;
  try { result = await response.json(); } catch { throw new Error('서버 응답을 확인할 수 없습니다.'); }
  if (!response.ok) throw new Error(result.error || '요청을 처리하지 못했습니다.');
  if (result.session && result.settings) render(result);
  return result;
}

function navigate(page) {
  localView = page === 'settings' ? 'settings' : 'main';
  $$('.page').forEach(el => { el.hidden = el.id !== `page-${localView}`; });
  $$('.nav-button').forEach(button => {
    const active = button.dataset.page === localView;
    button.classList.toggle('active', active);
    if (active) button.setAttribute('aria-current', 'page'); else button.removeAttribute('aria-current');
  });
  history.replaceState(null, '', `#${localView}`);
}

function setBadge(selector, text, status) {
  const el = $(selector);
  el.textContent = text;
  el.classList.toggle('ready', status === 'ready');
  el.classList.toggle('error', status === 'error');
}

function render(next) {
  const first = !initialized;
  const previous = state;
  state = next;
  const session = state.session;
  const settings = state.settings;
  const connection = state.connection || {};
  const brain = state.brain || {};
  const connectionReady = connection.status === 'ready';
  $('#system-dot').className = `status-dot${connectionReady ? ' ready' : connection.status === 'error' ? ' error' : ''}`;
  $('#system-label').textContent = externalMode ? (externalConnected ? '외부 대화 연결됨' : '외부 대화 연결 중') : connectionReady ? 'Nebius 연결됨' : settings.apiConfigured ? '연결 확인 필요' : 'API 연결 전';
  if (externalMode) $('#system-dot').className = `status-dot${externalConnected ? ' ready' : ' busy'}`;
  $('#phase-label').textContent = session.busy ? '답변을 준비하고 있어요' : phaseNames[session.phase] || session.phase;
  $('#phase-dot').className = `status-dot${session.busy ? ' busy' : session.phase === 'completed' ? ' ready' : ''}`;
  setBadge('#connection-badge', connectionReady ? '연결됨' : connection.status === 'error' ? '연결 오류' : settings.apiConfigured ? '확인 대기' : '연결 전', connection.status);
  $('#api-key').placeholder = settings.apiConfigured ? 'API 키 등록됨 · 변경할 때만 입력' : 'Nebius API 키 입력';
  $('#voice-toggle').setAttribute('aria-pressed', String(Boolean(settings.voiceEnabled)));
  $('#voice-enabled').checked = Boolean(settings.voiceEnabled);
  $('#voice-toggle').title = settings.voiceEnabled ? '음성 응답 끄기' : '음성 응답 켜기';
  $('#demo-memory-badge').hidden = !settings.historyIsDemo;
  if (!settings.voiceEnabled) stopSpeaking();
  if (first) {
    for (const message of session.messages || []) spokenIds.add(message.id);
    populateSettings(settings);
  }
  const latestAssistant = [...(session.messages || [])].reverse().find(message => message.role === 'assistant');
  if (!first && latestAssistant && !spokenIds.has(latestAssistant.id)) {
    spokenIds.add(latestAssistant.id);
    if (settings.voiceEnabled && !externalMode) speak(latestAssistant.text);
  }
  renderMessages();
  renderPlacements();
  renderProgress();
  renderOperator();
  renderBrain();
  if (first || previous?.settings?.model !== settings.model) ensureModel(settings.model);
  initialized = true;
}

function populateSettings(settings) {
  $('#recipient-name').value = settings.recipientName || '';
  $('#recipient-history').value = settings.recipientHistory || '';
  $('#history-demo').checked = Boolean(settings.historyIsDemo);
  $('#camera-mode').value = ['device', 'url', 'off'].includes(settings.cameraMode) ? settings.cameraMode : 'device';
  $('#camera-url').value = settings.cameraUrl || '';
  $('#python-executable').value = settings.pythonExecutable || '';
  $('#data-path').value = settings.dataPath || '';
  if (settings.cameraDeviceId) {
    const option = new Option('저장된 카메라', settings.cameraDeviceId);
    $('#camera-device').add(option);
    $('#camera-device').value = settings.cameraDeviceId;
  }
  updateCameraFields();
}

function renderMessages() {
  const session = externalMode ? { messages: externalFeed.messages, busy: externalFeed.phase === 'thinking' } : state.session;
  $('.conversation-footer').hidden = externalMode;
  $('#voice-toggle').hidden = externalMode;
  const signature = JSON.stringify([session.messages, session.busy]);
  $('#send-button').disabled = chatInFlight || session.busy;
  $('#quick-prompts').hidden = Boolean(session.messages?.length);
  if (signature === messagesSignature) return;
  messagesSignature = signature;
  const host = $('#messages');
  const nearBottom = host.scrollHeight - host.scrollTop - host.clientHeight < 100;
  host.replaceChildren();
  if (!session.messages?.length && !session.busy) {
    const empty = document.createElement('div'); empty.className = 'conversation-empty';
    const flower = document.createElement('div'); flower.className = 'empty-flower'; flower.textContent = '✳'; flower.setAttribute('aria-hidden', 'true');
    const text = document.createElement('p'); text.textContent = externalMode ? '대화를 기다리고 있어요.' : '먼저 말을 걸어주세요.';
    empty.append(flower, text); host.append(empty);
  }
  for (const item of session.messages || []) {
    const message = document.createElement('article'); message.className = `message ${item.role}`;
    const label = document.createElement('div'); label.className = 'message-label'; label.textContent = item.role === 'assistant' ? 'CoFlorist' : '나';
    const time = document.createElement('time'); time.dateTime = item.createdAt;
    const date = new Date(item.createdAt); time.textContent = Number.isFinite(date.getTime()) ? date.toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit', hour12: false }) : '';
    label.append(time);
    const body = document.createElement('p'); body.className = 'message-body'; body.textContent = item.text;
    message.append(label, body);
    if (externalMode && item.audio) message.append(externalAudioPlayer(item.audio));
    host.append(message);
  }
  if (session.busy) {
    const pending = document.createElement('article'); pending.className = 'message assistant pending';
    const body = document.createElement('p'); body.className = 'message-body'; body.textContent = '생각하고 있어요 ···'; pending.append(body); host.append(pending);
  }
  if (nearBottom || !initialized || session.busy) host.scrollTop = host.scrollHeight;
}

// Playback only starts from the user's own click: no autoplay, nothing fetched until then.
function externalAudioPlayer(audio) {
  let player = externalAudio.get(audio.url);
  if (!player) {
    player = document.createElement('audio');
    player.className = 'message-audio';
    player.controls = true;
    player.preload = 'none';
    player.src = audio.url;
    player.setAttribute('aria-label', '보드 음성 듣기');
    externalAudio.set(audio.url, player);
    while (externalAudio.size > 60) releaseExternalAudio(externalAudio.keys().next().value);
  }
  return player;
}

function releaseExternalAudio(url) {
  const player = externalAudio.get(url);
  if (!player) return;
  player.pause(); player.removeAttribute('src'); player.load();
  externalAudio.delete(url);
}

function renderPlacements() {
  const session = state.session;
  const host = $('#placements'); host.replaceChildren();
  for (const placement of session.placements || []) {
    const chip = document.createElement('span'); chip.className = `placement-chip ${placement.status}`;
    const dot = document.createElement('i'); dot.className = `color-dot ${placement.color}`;
    const label = document.createElement('span'); label.textContent = `${positionNames[placement.position] || placement.position}${placement.status === 'completed' ? ' ✓' : ''}`;
    chip.title = `${colorNames[placement.color] || placement.color} · ${positionNames[placement.position] || placement.position} · ${placement.status === 'completed' ? '완료' : placement.status === 'active' ? '진행 중' : '대기'}`;
    chip.append(dot, label); host.append(chip);
  }
  if (!session.placements?.length) { const empty = document.createElement('span'); empty.className = 'muted'; empty.textContent = '선택 대기'; host.append(empty); }
  const complete = (session.placements || []).filter(item => item.status === 'completed').length;
  $('#placement-count').textContent = session.placements?.length ? `${complete} / ${session.placements.length}송이` : '0송이';
  const task = session.activeTask;
  $('#active-task').textContent = session.paused ? '잠시 기다리는 중' : task?.status === 'active' ? `${colorNames[task.color]} → ${positionNames[task.position]}` : session.phase === 'completed' ? '전달 완료' : ['lifting', 'handover', 'awaiting_receipt'].includes(session.phase) ? phaseNames[session.phase] : '작업 대기';
}

function renderProgress() {
  const session = state.session;
  const phase = session.paused ? session.resumePhase : session.phase;
  const allCompleted = session.placements?.length > 0 && session.placements.every(item => item.status === 'completed');
  const index = ['lifting', 'handover', 'awaiting_receipt', 'completed'].includes(phase) ? 3 : phase === 'approved' || allCompleted ? 2 : session.placements?.length ? 1 : 0;
  $$('.task-progress li').forEach((item, i) => { item.classList.toggle('current', i === index); item.classList.toggle('done', i < index || phase === 'completed'); });
  const pause = $('#pause-button');
  pause.dataset.action = session.paused ? 'resume' : 'pause';
  pause.innerHTML = session.paused ? '<svg viewBox="0 0 20 20" fill="currentColor"><path d="m6 3 10 7-10 7Z"/></svg>다시 시작' : '<svg viewBox="0 0 20 20" fill="currentColor"><rect x="5" y="4" width="3" height="12" rx="1"/><rect x="12" y="4" width="3" height="12" rx="1"/></svg>잠깐 기다려';
  pause.disabled = session.phase === 'completed';
}

function renderOperator() {
  const session = state.session;
  const host = $('#operator-actions'); host.replaceChildren();
  const add = (action, text, primary = false) => { const button = document.createElement('button'); button.type = 'button'; button.className = `button ${primary ? 'primary' : 'secondary'}`; button.textContent = text; button.dataset.action = action; button.disabled = actionInFlight; host.append(button); };
  let note = '꽃 선택 대기';
  if (session.paused) note = '대기 중 · 재개 후 작업 가능';
  else if (session.phase === 'placing' && session.activeTask?.status === 'active') { note = `${colorNames[session.activeTask.color] || '꽃'} · ${positionNames[session.activeTask.position] || ''} 배치`; add('complete', '배치 완료', true); add('fail', '파지 실패'); }
  else if (session.phase === 'retry') { note = '실패 기록을 반영해 다시 잡아요.'; add('retry', '다시 시도', true); }
  else if (session.phase === 'approved') { note = '사용자가 작품을 확인했어요.'; add('lift', '들어 올리기', true); }
  else if (session.phase === 'lifting') { note = '작품을 안전하게 들어 올렸나요?'; add('handover', '작품 전달', true); }
  else if (['handover', 'awaiting_receipt'].includes(session.phase)) {
    note = session.receiptConfirmed ? '수령 확인 완료 · 놓아도 좋아요.' : '받는 사람이 작품을 잡았는지 확인해 주세요.';
    add(session.receiptConfirmed ? 'release' : 'received', session.receiptConfirmed ? '그리퍼 해제' : '수령 확인', true);
  } else if (session.phase === 'completed') note = '전달 완료';
  else if (session.placements?.some(item => item.status === 'pending')) { note = '선택한 꽃의 배치를 시작해요.'; add('start', '다음 꽃 잡기', true); }
  else if (session.placements?.length) note = '대화로 작품을 확인해 주세요.';
  $('#operator-message').textContent = note;
  const event = session.events?.at(-1);
  $('#last-event').textContent = event ? `${new Date(event.createdAt).toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit', hour12: false })} · ${event.text}` : '진행 기록 없음';
}

function svgElement(tag, attributes) {
  const element = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, String(value));
  return element;
}

function renderBrain() {
  const brain = state.brain || {};
  $('.conversation-brain').dataset.status = brain.status || 'idle';
  updateBrainActivity();
  const status = brainNames[brain.status] || '신경망 대기';
  setBadge('#brain-status', status, brain.status);
  setBadge('#brain-settings-status', status, brain.status);
  $('#brain-test').disabled = ['starting', 'running'].includes(brain.status);
  $('#brain-start').disabled = ['starting', 'running'].includes(brain.status);
  const result = brain.lastResult;
  if (brain.error) feedback('#brain-feedback', brain.error, true);
  if (!result) {
    graphSignature = '';
    $('#brain-svg').replaceChildren();
    $('#brain-svg').setAttribute('aria-label', '신경 활동 결과 대기');
    $('#brain-empty').hidden = false;
    for (const id of ['#metric-neurons', '#metric-spikes', '#metric-time']) $(id).textContent = '—';
    $('#brain-detail').textContent = 'FlyWire 부분회로 · Brian2';
    $('#brain-result-details').textContent = '아직 계산 결과가 없습니다.';
    return;
  }
  const decisionLabels = { wait: '대기', propose: '행동 제안', suggest_retry: '재시도 제안', confirm: '확인' };
  const decision = externalMode ? null : decisionLabels[state.session.neuralDecision?.action];
  $('#brain-detail').textContent = `${result.neuronCount?.toLocaleString() || '—'} 뉴런 · 실제 부분회로${decision ? ` · ${decision}` : ''}`;
  const signature = JSON.stringify([result.dataHash, result.seed, result.spikeCount, result.wallTimeMs]);
  if (signature === graphSignature) return;
  graphSignature = signature;
  $('#brain-empty').hidden = true;
  $('#metric-neurons').textContent = Number.isFinite(result.activeNeurons) ? result.activeNeurons.toLocaleString() : '—';
  $('#metric-spikes').textContent = Number.isFinite(result.spikeCount) ? result.spikeCount.toLocaleString() : '—';
  $('#metric-time').textContent = Number.isFinite(result.wallTimeMs) ? `${(result.wallTimeMs / 1000).toFixed(2)}s` : '—';
  const svg = $('#brain-svg'); svg.replaceChildren();
  svg.setAttribute('aria-label', `실제 신경 활동: ${result.activeNeurons ?? '미확인'}개 활성 뉴런, ${result.spikeCount ?? '미확인'}회 신호. 원형 배치는 시각화용이며 해부학적 위치가 아닙니다.`);
  const nodes = (result.nodes || result.nodeRates || []).slice(0, 64);
  const coordinates = new Map();
  const maxRate = Math.max(1, ...nodes.map(node => Number(node.rateHz) || 0));
  nodes.forEach((node, index) => {
    const angle = index * Math.PI * (3 - Math.sqrt(5));
    const radius = 8 + 35 * Math.sqrt(index / Math.max(1, nodes.length - 1));
    coordinates.set(String(node.id), { x: 72 + Math.cos(angle) * radius * 1.25, y: 50 + Math.sin(angle) * radius });
  });
  for (const edge of (result.edges || []).slice(0, 180)) {
    const source = coordinates.get(String(edge.source)); const target = coordinates.get(String(edge.target));
    if (source && target) svg.append(svgElement('line', { class: 'neural-edge', x1: source.x, y1: source.y, x2: target.x, y2: target.y, stroke: Number(edge.weightMv) < 0 ? '#b9bda5' : '#b4c2a4', 'stroke-width': .45, opacity: .38 }));
  }
  nodes.forEach(node => {
    const point = coordinates.get(String(node.id));
    const rate = Number(node.rateHz) || 0;
    const circle = svgElement('circle', { class: rate > 0 ? 'neural-node active' : 'neural-node', style: `--node-delay:${-(nodes.indexOf(node) % 13) * .11}s`, cx: point.x, cy: point.y, r: 1.3 + Math.sqrt(rate / maxRate) * 1.8, fill: node.stimulated ? '#c3b477' : rate > 0 ? '#688854' : '#c7ceb9', opacity: rate > 0 ? .9 : .5 });
    const title = svgElement('title', {}); title.textContent = `뉴런 ${node.id} · ${rate.toFixed(1)} Hz${node.stimulated ? ' · 입력 자극' : ''}`; circle.append(title); svg.append(circle);
  });
  const traces = result.traces || [];
  if (traces.length) {
    const max = Math.max(1, ...traces.map(item => Number(item.spikes) || 0));
    svg.append(svgElement('line', { x1: 147, y1: 81, x2: 274, y2: 81, stroke: '#e1e6d6', 'stroke-width': 1 }));
    const points = traces.map((item, index) => `${148 + index * 125 / Math.max(1, traces.length - 1)},${80 - (Number(item.spikes) || 0) / max * 53}`).join(' ');
    svg.append(svgElement('polyline', { points, fill: 'none', stroke: '#8caa70', 'stroke-width': 1.4, 'stroke-linejoin': 'round' }));
    const label = svgElement('text', { x: 148, y: 96, fill: '#a7b197', 'font-size': 6 }); label.textContent = `0 — ${result.durationMs ?? result.simulationTimeMs} ms`; svg.append(label);
  }
  const { nodes: _nodes, nodeRates: _nodeRates, edges: _edges, traces: _traces, spikes: _spikes, ...summary } = result;
  $('#brain-result-details').textContent = JSON.stringify(summary, null, 2);
  feedback('#brain-feedback', '실제 신경 계산 완료');
}

function updateBrainActivity() {
  const phase = externalMode ? externalFeed.phase : listening ? 'listening' : state?.session?.busy || chatInFlight ? 'thinking' : speaking ? 'speaking' : 'idle';
  const running = state?.brain?.status === 'running';
  const active = ['listening', 'thinking', 'speaking'].includes(phase) || running;
  const card = $('.conversation-brain');
  card.dataset.interaction = active ? phase === 'idle' ? 'computing' : phase : 'idle';
  const labels = { listening: '듣고 있어요', thinking: '생각하고 있어요', speaking: '마음을 전하고 있어요', error: '대화 연결 확인' };
  $('#brain-activity').textContent = labels[phase] || (running ? '신경 활동 계산 중' : '대화 대기');
  $('#brain-replay-note').textContent = active && state?.brain?.lastResult ? '최근 계산 재생 · 대화 상태 연동' : '실제 부분회로 계산 결과';
}

function disconnectExternalFeed() {
  externalSource?.close(); externalSource = null;
  clearTimeout(externalSilenceTimer);
  for (const url of [...externalAudio.keys()]) releaseExternalAudio(url);
  externalMode = false; externalConnected = false;
  $('#external-disconnect').disabled = true;
  if (state) render(state);
}

function connectExternalFeed(url) {
  const parsed = new URL(url);
  if (!['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password) throw new Error('인증정보가 없는 HTTP(S) 스트림 주소를 입력해 주세요.');
  disconnectExternalFeed();
  stopSpeaking(); discardRecognition = true; recognition?.abort();
  externalMode = true; externalFeed = createFeedState({ audioBase: parsed.href });
  const source = new EventSource(parsed.href);
  externalSource = source;
  $('#external-disconnect').disabled = false;
  feedback('#external-feedback', '연결 중…');
  render(state);
  source.onopen = () => {
    if (externalSource !== source) return;
    externalConnected = true;
    feedback('#external-feedback', '외부 대화 연결됨'); render(state);
  };
  source.onmessage = event => {
    if (externalSource !== source) return;
    try {
      const previousPhase = externalFeed.phase;
      if (!applyFeedEvent(externalFeed, JSON.parse(event.data))) return;
      clearTimeout(externalSilenceTimer);
      externalSilenceTimer = setTimeout(() => { externalFeed.phase = 'idle'; if (state) render(state); }, 45000);
      render(state);
      // A single real simulation on entry to thinking. This never calls the LLM or moves a robot.
      if (externalFeed.phase === 'thinking' && previousPhase !== 'thinking' && !externalBrainPending && !['starting', 'running'].includes(state?.brain?.status)) {
        externalBrainPending = true;
        api('/api/brain/run', { stimulusHz: 100, durationMs: 200, seed: 42, intent: 'external_conversation' })
          .catch(error => feedback('#brain-feedback', error.message, true))
          .finally(() => { externalBrainPending = false; });
      }
    } catch (error) { feedback('#external-feedback', error.message, true); }
  };
  source.onerror = () => {
    if (externalSource !== source) return;
    externalConnected = false; externalFeed.phase = 'idle';
    clearTimeout(externalSilenceTimer);
    feedback('#external-feedback', '연결 재시도 중 · 주소와 CORS 설정을 확인해 주세요.', true); render(state);
  };
}

function feedback(selector, text, isError = false) { const el = $(selector); el.textContent = text; el.classList.toggle('error', isError); }

async function submitChat(text) {
  if (externalMode) return;
  text = text.trim();
  if (/^(잠깐\s*)?(기다려|멈춰|대기|정지)(\s|[.!?]|$)/.test(text) || /^잠깐 기다려/.test(text)) {
    chatGeneration += 1;
    chatInFlight = false;
    stopSpeaking(); discardRecognition = true; recognition?.abort();
    $('#chat-input').value = ''; resizeInput();
    try { await api('/api/chat', { text }); } catch (error) { toast(error.message, true); }
    return;
  }
  if (!text || chatInFlight || state?.session?.busy) return;
  if (text.length > 2000) { toast('메시지는 2,000자까지 입력할 수 있어요.', true); return; }
  stopSpeaking();
  if (listening) recognition?.stop();
  const ownGeneration = ++chatGeneration;
  chatInFlight = true; $('#send-button').disabled = true; updateBrainActivity();
  $('#chat-input').value = ''; resizeInput();
  try { await api('/api/chat', { text }); }
  catch (error) { if (ownGeneration === chatGeneration) { toast(error.message, true); if (!$('#chat-input').value) $('#chat-input').value = text; } }
  finally { if (ownGeneration === chatGeneration) { chatInFlight = false; $('#send-button').disabled = Boolean(state?.session?.busy); resizeInput(); updateBrainActivity(); } }
}

async function action(name) {
  if (actionInFlight && !['pause', 'reset'].includes(name)) return;
  if (['pause', 'reset'].includes(name)) { chatGeneration += 1; chatInFlight = false; stopSpeaking(); discardRecognition = true; recognition?.abort(); }
  actionInFlight = true; if (state) renderOperator();
  try { await api('/api/action', { action: name }); }
  catch (error) { toast(error.message, true); }
  finally { actionInFlight = false; if (state) renderOperator(); }
}

async function saveSettings(data) { return api('/api/settings', data); }

function ensureModel(model) {
  if (!model) return;
  const select = $('#model-select');
  if (![...select.options].some(option => option.value === model)) select.add(new Option(model, model));
  select.value = model;
}

async function loadModels() {
  try {
    const result = await api('/api/models');
    const selected = state?.settings?.model || $('#model-select').value;
    $('#model-select').replaceChildren();
    for (const id of result.models || []) $('#model-select').add(new Option(id.replace(/^nvidia\//i, ''), id));
    ensureModel(selected);
    if (!$('#model-select').options.length) $('#model-select').add(new Option('지원 모델을 확인해 주세요', ''));
  } catch (error) { feedback('#model-feedback', error.message, true); }
}

function stopCamera() {
  cameraGeneration += 1;
  cameraStream?.getTracks().forEach(track => track.stop()); cameraStream = null;
  $('#camera-video').pause(); $('#camera-video').srcObject = null; $('#camera-video').removeAttribute('src'); $('#camera-video').load();
  $('#camera-remote').removeAttribute('src');
  $('#camera-video').hidden = true; $('#camera-remote').hidden = true; $('#camera-placeholder').hidden = false;
  $('#camera-source').textContent = '카메라 연결 전 · 참고 사진';
  $('#camera-connect').hidden = false;
  $('#disconnect-camera').disabled = true;
}

async function connectCamera(settings = state?.settings || {}) {
  stopCamera();
  const generation = cameraGeneration;
  if (settings.cameraMode === 'off') { feedback('#camera-feedback', '참고 사진 표시 중'); return; }
  feedback('#camera-feedback', '카메라 연결 중…');
  try {
    if (settings.cameraMode === 'url') {
      const url = new URL(settings.cameraUrl);
      if (!['http:', 'https:'].includes(url.protocol)) throw new Error('HTTP 또는 HTTPS 영상 주소를 입력해 주세요.');
      const isVideo = /\.(mp4|webm|ogg)(?:$|\?)/i.test(url.href);
      const media = isVideo ? $('#camera-video') : $('#camera-remote');
      await new Promise((resolve, reject) => {
        const timeout = setTimeout(() => done(new Error('영상 응답이 없습니다. 주소와 연결을 확인해 주세요.')), 12000);
        const success = () => done(); const failure = () => done(new Error('영상을 불러오지 못했습니다. 지원되는 영상 또는 MJPEG 주소를 확인해 주세요.'));
        const done = error => { clearTimeout(timeout); media.removeEventListener(isVideo ? 'loadeddata' : 'load', success); media.removeEventListener('error', failure); error ? reject(error) : resolve(); };
        media.addEventListener(isVideo ? 'loadeddata' : 'load', success, { once: true }); media.addEventListener('error', failure, { once: true }); media.src = url.href;
        if (isVideo) media.play().catch(failure);
      });
      if (generation !== cameraGeneration) return;
      media.hidden = false;
    } else {
      if (!navigator.mediaDevices?.getUserMedia) throw new Error('현재 브라우저에서 카메라를 사용할 수 없습니다. localhost에서 Chrome 또는 Edge로 열어주세요.');
      const stream = await navigator.mediaDevices.getUserMedia({ audio: false, video: { ...(settings.cameraDeviceId ? { deviceId: { exact: settings.cameraDeviceId } } : {}), width: { ideal: 1280 }, height: { ideal: 720 }, frameRate: { ideal: 30, max: 30 } } });
      if (generation !== cameraGeneration) { stream.getTracks().forEach(track => track.stop()); return; }
      cameraStream = stream;
      $('#camera-video').srcObject = stream; await $('#camera-video').play(); $('#camera-video').hidden = false;
      stream.getVideoTracks()[0]?.addEventListener('ended', () => { if (cameraStream !== stream) return; stopCamera(); feedback('#camera-feedback', '카메라 연결 종료'); });
      await refreshCameras();
    }
    if (generation !== cameraGeneration) return;
    $('#camera-placeholder').hidden = true; $('#camera-connect').hidden = true;
    $('#disconnect-camera').disabled = false;
    $('#camera-source').textContent = '카메라 연결됨'; feedback('#camera-feedback', '카메라 연결됨');
  } catch (error) {
    if (generation !== cameraGeneration) return;
    stopCamera();
    const message = error.name === 'NotAllowedError' ? '카메라 권한을 허용해 주세요.' : error.name === 'NotFoundError' ? '연결된 카메라가 없습니다.' : error.name === 'NotReadableError' ? '다른 앱에서 카메라를 사용 중일 수 있어요. LeLab 화면 공유를 선택하거나 사용 중인 앱을 닫아주세요.' : error.message;
    feedback('#camera-feedback', message, true); toast(message, true);
  }
}

async function shareLeLabScreen() {
  if (!navigator.mediaDevices?.getDisplayMedia) {
    feedback('#camera-feedback', '이 브라우저는 화면 공유를 지원하지 않습니다. 직접 카메라 연결을 사용해 주세요.', true);
    return;
  }
  const generation = ++cameraGeneration;
  const previousFeedback = $('#camera-feedback').textContent;
  const previousError = $('#camera-feedback').classList.contains('error');
  const button = $('#share-lelab');
  button.disabled = true;
  let sharedStream = null;
  try {
    // Called from this button's click: keep the browser picker in the user gesture.
    sharedStream = await navigator.mediaDevices.getDisplayMedia({ video: { frameRate: { ideal: 30, max: 30 } }, audio: false });
    if (generation !== cameraGeneration) { sharedStream.getTracks().forEach(track => track.stop()); return; }
    stopCamera();
    cameraStream = sharedStream;
    $('#camera-video').srcObject = sharedStream;
    await $('#camera-video').play();
    if (cameraStream !== sharedStream) return;
    $('#camera-video').hidden = false;
    $('#camera-placeholder').hidden = true;
    $('#camera-connect').hidden = true;
    $('#disconnect-camera').disabled = false;
    $('#camera-source').textContent = '공유 화면';
    feedback('#camera-feedback', '화면 공유 중');
    sharedStream.getVideoTracks()[0]?.addEventListener('ended', () => {
      if (cameraStream !== sharedStream) return;
      stopCamera();
      feedback('#camera-feedback', '화면 공유 종료');
    });
  } catch (error) {
    if (sharedStream) {
      sharedStream.getTracks().forEach(track => track.stop());
      if (cameraStream === sharedStream) stopCamera();
    }
    if (['NotAllowedError', 'AbortError'].includes(error.name)) {
      feedback('#camera-feedback', previousFeedback, previousError);
      return;
    }
    feedback('#camera-feedback', error.name === 'NotReadableError' ? '선택한 화면을 읽지 못했어요. 공유할 탭이나 창을 다시 선택해 주세요.' : error.message, true);
  } finally { button.disabled = false; }
}

async function refreshCameras() {
  if (!navigator.mediaDevices?.enumerateDevices) return;
  try {
    const current = $('#camera-device').value || state?.settings?.cameraDeviceId || '';
    const devices = (await navigator.mediaDevices.enumerateDevices()).filter(item => item.kind === 'videoinput');
    const select = $('#camera-device'); select.replaceChildren(new Option('기본 카메라', ''));
    devices.forEach((device, i) => select.add(new Option(device.label || `카메라 ${i + 1}`, device.deviceId)));
    if ([...select.options].some(option => option.value === current)) select.value = current;
  } catch (error) { feedback('#camera-feedback', error.message, true); }
}

function updateCameraFields() { const mode = $('#camera-mode').value; $('#camera-device-label').hidden = mode !== 'device'; $('#camera-url-label').hidden = mode !== 'url'; }

function loadVoices() {
  voices = window.speechSynthesis?.getVoices().filter(voice => /^ko(?:-|_)/i.test(voice.lang)) || [];
  const select = $('#voice-select'); select.replaceChildren(new Option('기본 한국어 음성', ''));
  for (const voice of voices) select.add(new Option(voice.name, voice.voiceURI));
  if (voices.some(voice => voice.voiceURI === selectedVoice)) select.value = selectedVoice;
}

function stopSpeaking() { pendingSpeech = null; speaking = false; speechGeneration++; window.speechSynthesis?.cancel(); updateBrainActivity(); }

function speak(text, force = false) {
  if (externalMode) return;
  if (!window.speechSynthesis || (!force && !state?.settings?.voiceEnabled)) return;
  if (listening) { pendingSpeech = text; return; }
  window.speechSynthesis.cancel();
  const generation = ++speechGeneration;
  const utterance = new SpeechSynthesisUtterance(text); utterance.lang = 'ko-KR'; utterance.rate = 1; utterance.pitch = 1;
  const voice = voices.find(item => item.voiceURI === selectedVoice) || voices.find(item => item.localService) || voices[0];
  if (voice) utterance.voice = voice;
  utterance.onstart = () => { if (generation === speechGeneration) { speaking = true; updateBrainActivity(); } };
  utterance.onend = () => { if (generation === speechGeneration) { speaking = false; updateBrainActivity(); } };
  utterance.onerror = event => { if (generation === speechGeneration) { speaking = false; updateBrainActivity(); } if (!['interrupted', 'canceled'].includes(event.error)) toast('음성 재생을 시작하지 못했어요. 목소리 설정을 확인해 주세요.', true); };
  window.speechSynthesis.speak(utterance);
}

function setupSpeech() {
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!Recognition) { $('#mic-button').disabled = true; $('#mic-button').title = '이 브라우저는 음성 인식을 지원하지 않습니다.'; $('#speech-support').textContent = '음성 입력 미지원 · Chrome 또는 Edge에서 열어주세요.'; return; }
  recognition = new Recognition(); recognition.lang = 'ko-KR'; recognition.continuous = false; recognition.interimResults = true;
  let finalTranscript = ''; let initialText = '';
  recognition.onstart = () => { listening = true; updateBrainActivity(); $('#mic-button').classList.add('listening'); $('#mic-button').setAttribute('aria-pressed', 'true'); $('#voice-status').textContent = '듣고 있어요…'; };
  recognition.onresult = event => {
    let interim = '';
    for (let index = event.resultIndex; index < event.results.length; index += 1) { const text = event.results[index][0].transcript; if (event.results[index].isFinal) finalTranscript += text; else interim += text; }
    $('#chat-input').value = [initialText, finalTranscript, interim].filter(Boolean).join(' ').slice(0, 2000); resizeInput();
  };
  recognition.onerror = event => { const labels = { 'not-allowed': '마이크 권한을 허용해 주세요.', 'audio-capture': '마이크를 찾지 못했어요.', 'no-speech': '말소리를 듣지 못했어요.', network: '음성 인식 서비스 연결을 확인해 주세요.' }; if (event.error !== 'aborted') { $('#voice-status').textContent = labels[event.error] || '음성 인식을 다시 시도해 주세요.'; } };
  recognition.onend = () => {
    listening = false; updateBrainActivity(); $('#mic-button').classList.remove('listening'); $('#mic-button').setAttribute('aria-pressed', 'false');
    if (!discardRecognition && finalTranscript.trim()) { $('#voice-status').textContent = ''; submitChat($('#chat-input').value); }
    else if ($('#voice-status').textContent === '듣고 있어요…') $('#voice-status').textContent = '';
    finalTranscript = ''; discardRecognition = false;
    if (pendingSpeech) { const text = pendingSpeech; pendingSpeech = null; speak(text); }
  };
  $('#mic-button').addEventListener('click', () => {
    if (listening) { recognition.stop(); return; }
    stopSpeaking(); discardRecognition = false; finalTranscript = ''; initialText = $('#chat-input').value.trim();
    try { recognition.start(); } catch { toast('음성 인식을 다시 시도해 주세요.', true); }
  });
}

function resizeInput() { const input = $('#chat-input'); input.style.height = '30px'; input.style.height = `${Math.min(100, input.scrollHeight)}px`; }

function startEvents() {
  eventSource = new EventSource('/api/events');
  eventSource.addEventListener('state', event => { try { render(JSON.parse(event.data)); } catch (error) { console.error('상태 표시 오류', error); } });
  eventSource.onopen = () => { if (fallbackTimer) clearInterval(fallbackTimer); fallbackTimer = null; };
  eventSource.onerror = () => { if (!fallbackTimer) fallbackTimer = setInterval(() => { api('/api/state').catch(() => { $('#system-label').textContent = '서버 연결 확인 필요'; $('#system-dot').className = 'status-dot error'; }); }, 5000); };
}

$$('.nav-button').forEach(button => button.addEventListener('click', () => navigate(button.dataset.page)));
$('.brand').addEventListener('click', event => { event.preventDefault(); navigate('main'); });
window.addEventListener('hashchange', () => navigate(location.hash.slice(1)));
$('#chat-form').addEventListener('submit', event => { event.preventDefault(); submitChat($('#chat-input').value); });
$('#chat-input').addEventListener('input', resizeInput);
$('#chat-input').addEventListener('keydown', event => { if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); submitChat($('#chat-input').value); } });
$('#quick-prompts').addEventListener('click', event => { const button = event.target.closest('[data-prompt]'); if (button) submitChat(button.dataset.prompt); });
$('#operator-actions').addEventListener('click', event => { const button = event.target.closest('[data-action]'); if (button) action(button.dataset.action); });
$('#pause-button').addEventListener('click', event => action(event.currentTarget.dataset.action));

$('#model-form').addEventListener('submit', async event => {
  event.preventDefault(); const button = $('#model-connect'); button.disabled = true; feedback('#model-feedback', '연결 확인 중…');
  try { await saveSettings({ model: $('#model-select').value, apiKey: $('#api-key').value }); $('#api-key').value = ''; await loadModels(); const result = await api('/api/connect', {}); feedback('#model-feedback', `연결됨${result.connection?.latencyMs ? ` · ${(result.connection.latencyMs / 1000).toFixed(1)}초` : ''}`); toast('Nebius 모델이 연결됐어요.'); }
  catch (error) { feedback('#model-feedback', error.message, true); }
  finally { button.disabled = false; }
});
$('#external-feed-form').addEventListener('submit', event => { event.preventDefault(); try { connectExternalFeed($('#external-feed-url').value.trim()); } catch (error) { feedback('#external-feedback', error.message, true); } });
$('#external-disconnect').addEventListener('click', () => { disconnectExternalFeed(); feedback('#external-feedback', '외부 연결 해제됨'); });
$('#show-key').addEventListener('click', () => { const input = $('#api-key'); const visible = input.type === 'password'; input.type = visible ? 'text' : 'password'; $('#show-key').textContent = visible ? '숨기기' : '보기'; });
$('#recipient-form').addEventListener('submit', async event => { event.preventDefault(); try { await saveSettings({ recipientName: $('#recipient-name').value, recipientHistory: $('#recipient-history').value, historyIsDemo: $('#history-demo').checked }); toast('이야기를 저장했어요.'); } catch (error) { toast(error.message, true); } });
$('#camera-mode').addEventListener('change', updateCameraFields);
$('#camera-form').addEventListener('submit', async event => { event.preventDefault(); try { await saveSettings({ cameraMode: $('#camera-mode').value, cameraUrl: $('#camera-url').value, cameraDeviceId: $('#camera-device').value }); await connectCamera(); } catch (error) { feedback('#camera-feedback', error.message, true); } });
$('#camera-connect').addEventListener('click', () => connectCamera({ ...state?.settings, cameraMode: state?.settings?.cameraMode === 'off' ? 'device' : state?.settings?.cameraMode }));
$('#share-lelab').addEventListener('click', shareLeLabScreen);
$('#disconnect-camera').addEventListener('click', () => { stopCamera(); feedback('#camera-feedback', '연결 해제됨'); });
$('#refresh-cameras').addEventListener('click', () => refreshCameras());
$('#voice-select').addEventListener('change', event => { selectedVoice = event.target.value; });
$('#voice-test').addEventListener('click', () => speak('안녕하세요. 원하는 꽃을 함께 골라볼까요?', true));
$('#voice-stop').addEventListener('click', stopSpeaking);
$('#voice-enabled').addEventListener('change', async event => { try { await saveSettings({ voiceEnabled: event.target.checked }); } catch (error) { event.target.checked = !event.target.checked; toast(error.message, true); } });
$('#voice-toggle').addEventListener('click', async () => { try { await saveSettings({ voiceEnabled: !state?.settings?.voiceEnabled }); } catch (error) { toast(error.message, true); } });
$('#brain-form').addEventListener('submit', async event => { event.preventDefault(); feedback('#brain-feedback', '신경망 준비 중…'); $('#brain-start').disabled = true; try { await saveSettings({ pythonExecutable: $('#python-executable').value, dataPath: $('#data-path').value }); await api('/api/brain/start', {}); feedback('#brain-feedback', '실제 FlyWire 부분회로 연결됨'); } catch (error) { feedback('#brain-feedback', error.message, true); } finally { $('#brain-start').disabled = false; } });
$('#brain-test').addEventListener('click', async () => { feedback('#brain-feedback', '실제 신경 활동 계산 중…'); $('#brain-test').disabled = true; try { await api('/api/brain/run', {}); feedback('#brain-feedback', '실제 신경 계산 완료'); } catch (error) { feedback('#brain-feedback', error.message, true); } finally { $('#brain-test').disabled = false; } });
$('#export-session').addEventListener('click', async () => { try { const result = await api('/api/export'); const url = URL.createObjectURL(new Blob([JSON.stringify(result, null, 2)], { type: 'application/json' })); const link = document.createElement('a'); link.href = url; link.download = `coflorist-${new Date().toISOString().slice(0, 10)}.json`; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); } catch (error) { toast(error.message, true); } });
$('#reset-session').addEventListener('click', async () => { if (!window.confirm('현재 대화와 작업을 초기화할까요? 필요한 기록은 먼저 내려받아 주세요.')) return; await action('reset'); toast('새로운 시연을 시작할 준비가 됐어요.'); });
window.speechSynthesis?.addEventListener('voiceschanged', loadVoices);
window.addEventListener('beforeunload', () => { externalSource?.close(); clearTimeout(externalSilenceTimer); stopSpeaking(); recognition?.abort(); cameraStream?.getTracks().forEach(track => track.stop()); eventSource?.close(); clearInterval(fallbackTimer); });

navigate(localView);
loadVoices();
setupSpeech();
if (!navigator.mediaDevices?.getDisplayMedia) {
  $('#share-lelab').disabled = true;
  $('#camera-share-help').textContent = '이 브라우저는 화면 공유를 지원하지 않습니다. 직접 카메라 연결을 사용해 주세요.';
}
try { await api('/api/state'); startEvents(); if (state?.settings?.apiConfigured) loadModels(); refreshCameras(); }
catch (error) { $('#system-label').textContent = '서버 연결 확인 필요'; $('#system-dot').className = 'status-dot error'; toast(error.message, true); startEvents(); }

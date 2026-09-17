'use strict';
const $ = id => document.getElementById(id);
const programs = JSON.parse($('program-data').textContent);
const numbers = new Intl.NumberFormat('en-US');
let program = programs[0].id;
let runId = null, stream = null, state = 'idle', generation = 0, starting = false;
let cycles = 0, fetched = 0, lookups = 0, requests = 0, elapsed = 0, elapsedAt = performance.now();
let terminal = false, pendingRows = [], frame = 0, requestNode = null, requestBody = null;
let samples = [], lastSample = performance.now(), lastCycles = 0, hz = 0;
let inputTokens = 0, inputCost = 0, wave = [], waveHover = -1;
let edited = false;
const expressions = { NOT: '!A', AND: 'AB', OR: 'A+B', XOR: 'A^B', MUX: 'S?B:A' };

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function hex(value) { return value.toString(16).padStart(8, '0'); }
function error(message) { $('error').textContent = message; $('error').hidden = false; }
async function post(path, data) {
  const response = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) });
  if (!response.ok) {
    let reason = `Request failed (${response.status})`;
    try { const body = await response.json(); if (body.error) reason = body.error; } catch { /* Non-JSON error */ }
    throw new Error(reason);
  }
  return response.json();
}
function updateControls() {
  const running = ['running', 'classifying', 'compiling'].includes(state);
  $('run').textContent = running ? 'Pause' : edited && (!runId || terminal) ? 'Compile & run' : 'Run';
  $('run').disabled = starting;
  $('step').disabled = starting || running;
  $('gate-source').disabled = !!runId && !terminal;
  $('source').readOnly = !!runId && !terminal;
  $('restore').disabled = !!runId && !terminal;
  $('status').textContent = state.charAt(0).toUpperCase() + state.slice(1);
  $('status').dataset.state = state;
}
function currentElapsed() {
  return elapsed + (['running', 'classifying', 'compiling'].includes(state) ? (performance.now() - elapsedAt) / 1000 : 0);
}
function drawClock() {
  let value = hz, unit = 'Hz';
  if (value >= 1000000) { value /= 1000000; unit = 'MHz'; }
  else if (value >= 1000) { value /= 1000; unit = 'kHz'; }
  $('hz').textContent = value === 0 ? '0' : value.toFixed(2);
  $('hz-unit').textContent = unit;
  const canvas = $('speed-chart'), ctx = canvas.getContext('2d');
  const width = canvas.width, height = canvas.height;
  ctx.clearRect(0, 0, width, height);
  ctx.strokeStyle = '#333'; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(0, height - 1); ctx.lineTo(width, height - 1); ctx.stroke();
  if (!samples.length) return;
  const max = Math.max(...samples, 1);
  ctx.strokeStyle = '#eee'; ctx.beginPath();
  samples.forEach((sample, index) => {
    const x = index / 95 * width, y = height - 2 - sample / max * (height - 5);
    if (index === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  });
  ctx.stroke();
}
function render() {
  frame = 0;
  $('cycles').textContent = numbers.format(cycles);
  $('fetched').textContent = numbers.format(fetched);
  $('lookups').textContent = lookups >= 1000000 ? `${(lookups / 1000000).toFixed(2)}M` : numbers.format(lookups);
  $('lookups').title = numbers.format(lookups);
  $('requests').textContent = numbers.format(requests);
  $('input-tokens').textContent = numbers.format(inputTokens);
  $('input-cost').textContent = inputCost ? `$${inputCost.toFixed(6)}` : '$0';
  $('input-cost').title = `$${inputCost.toFixed(9)} estimated input cost`;
  $('elapsed').textContent = `${currentElapsed().toFixed(3)} s`;
  $('step-count').textContent = `${numbers.format(fetched)} instructions${fetched > 300 ? ' · last 300' : ''}`;
  if (pendingRows.length) {
    const fragment = document.createDocumentFragment();
    for (const row of pendingRows.slice(-300)) {
      const tr = element('tr');
      for (const value of [row.fetched, hex(row.pc), row.assembly, row.cycle]) tr.append(element('td', String(value)));
      fragment.append(tr);
    }
    const latest = pendingRows[pendingRows.length - 1];
    $('pc').textContent = `0x${hex(latest.pc)}`;
    pendingRows = [];
    $('steps').append(fragment);
    while ($('steps').children.length > 300) $('steps').firstElementChild.remove();
    if ($('follow').checked) $('execution-scroll').scrollTop = $('execution-scroll').scrollHeight;
  }
  drawClock();
  drawTiming();
  updateControls();
}
function scheduleRender() { if (!frame) frame = requestAnimationFrame(render); }
function details(label, value) {
  const node = element('details', undefined, 'request-detail');
  node.append(element('summary', label), element('pre', JSON.stringify(value, null, 2)));
  return node;
}
function logRequest(event) {
  requestBody = event.request;
  const entry = element('div');
  requestNode = element('div', undefined, 'request-summary');
  requestNode.append(element('span', event.source === 'api' ? 'POST /v1/systemone' : 'Cached classifications'), element('span', 'Pending'));
  entry.append(requestNode);
  $('request-log').replaceChildren(entry);
  if (event.source === 'api') requests++;
}
function logResponse(event) {
  const data = event.response;
  $('model').textContent = data.model;
  inputTokens += event.input_tokens || 0;
  inputCost += event.input_cost_usd || 0;
  requestNode.lastChild.textContent = event.source === 'api' ? `${Math.round(event.duration * 1000)} ms · 22 answers` : '22 answers · no API call';
  const entry = requestNode.parentNode;
  const answers = element('details', undefined, 'request-detail');
  answers.open = true;
  answers.append(element('summary', 'Classifications'));
  const grid = element('div', undefined, 'classifications');
  for (const [name, answer] of Object.entries(data.answers)) {
    const [gate, bits] = name.split('_');
    const row = element('div', undefined, 'classification');
    row.title = `Confidence: ${(answer.confidence * 100).toFixed(1)}%`;
    row.append(element('span', `${expressions[gate]},${bits}`), element('span', answer.choice));
    grid.append(row);
  }
  answers.append(grid);
  entry.append(answers, details(event.source === 'api' ? 'Request' : 'Original request', requestBody), details(event.source === 'api' ? 'Response' : 'Cached response', data));
}
function accept(events) {
  for (const event of events) {
    elapsed = event.elapsed; elapsedAt = performance.now();
    if (event.cycle !== undefined) cycles = event.cycle;
    if (event.gates !== undefined) lookups = event.gates;
    if (event.fetched !== undefined) fetched = event.fetched;
    if (event.wave) wave = event.wave;
    switch (event.type) {
      case 'status': state = event.state; break;
      case 'compiled': $('compile-status').textContent = `${numbers.format(event.bytes)} bytes · RV32I`; break;
      case 'request': logRequest(event); break;
      case 'response': logResponse(event); break;
      case 'fetch': pendingRows.push(event); break;
      case 'output': $('output').textContent += String.fromCharCode(event.byte); $('output').scrollTop = $('output').scrollHeight; break;
      case 'halt': $('exit-code').textContent = `exit ${event.exit_code}`; break;
      case 'error': error(event.message); if (requestNode) requestNode.lastChild.textContent = 'Failed'; break;
      case 'done':
        state = event.state; terminal = true; hz = elapsed ? cycles / elapsed : 0;
        samples.push(hz); if (stream) { stream.close(); stream = null; }
        break;
    }
  }
  scheduleRender();
}
async function reset() {
  generation++;
  const oldRun = runId;
  runId = null;
  if (stream) { stream.close(); stream = null; }
  state = 'idle'; terminal = false; starting = false;
  cycles = fetched = lookups = requests = elapsed = hz = 0;
  inputTokens = inputCost = 0; wave = []; waveHover = -1;
  elapsedAt = lastSample = performance.now(); lastCycles = 0;
  pendingRows = []; samples = []; requestNode = null;
  for (const id of ['steps', 'output', 'request-log', 'exit-code']) $(id).replaceChildren();
  $('error').hidden = true; $('model').textContent = '—'; $('pc').textContent = '0x00000000';
  render();
  if (oldRun) await post(`/api/runs/${oldRun}/control`, { action: 'stop' });
}
async function start(mode) {
  if (terminal) await reset();
  if (runId) {
    await post(`/api/runs/${runId}/control`, { action: mode });
    return;
  }
  const token = generation;
  starting = true; state = edited ? 'compiling' : 'classifying'; terminal = false; elapsedAt = performance.now(); updateControls();
  try {
    const options = { program, fresh: $('gate-source').value === 'fresh', mode };
    if (edited) options.source = $('source').value;
    const result = await post('/api/runs', options);
    if (token !== generation) { await post(`/api/runs/${result.id}/control`, { action: 'stop' }); return; }
    runId = result.id;
    stream = new EventSource(`/api/runs/${runId}/events`);
    stream.onmessage = event => { if (token === generation) accept(JSON.parse(event.data)); };
    stream.onerror = () => { if (!terminal && token === generation) $('status').textContent = 'Reconnecting'; };
  } catch (e) { state = 'error'; terminal = true; throw e; }
  finally { starting = false; updateControls(); }
}
function action(fn) { return () => fn().catch(e => error(e.message)); }
$('run').addEventListener('click', action(async () => {
  if (['running', 'classifying', 'compiling'].includes(state) && runId) await post(`/api/runs/${runId}/control`, { action: 'pause' });
  else await start('run');
}));
$('step').addEventListener('click', action(() => start('step')));
$('reset').addEventListener('click', action(reset));
for (const tab of document.querySelectorAll('[data-program]')) {
  tab.addEventListener('click', action(async () => {
    if (program === tab.dataset.program) return;
    await reset(); program = tab.dataset.program;
    for (const button of document.querySelectorAll('[data-program]')) button.setAttribute('aria-selected', String(button === tab));
    $('source').value = programs.find(p => p.id === program).source;
    edited = false; $('compile-status').textContent = 'C → RV32I'; updateControls();
    $('filename').textContent = `${program}.c`;
  }));
  tab.addEventListener('keydown', event => {
    const tabs = [...document.querySelectorAll('[data-program]')];
    if (event.key === 'ArrowRight' || event.key === 'ArrowLeft') {
      event.preventDefault();
      const next = tabs[(tabs.indexOf(tab) + (event.key === 'ArrowRight' ? 1 : tabs.length - 1)) % tabs.length];
      next.focus(); next.click();
    }
  });
}
setInterval(() => {
  const now = performance.now();
  if (['running', 'classifying', 'compiling'].includes(state)) {
    hz = (cycles - lastCycles) / ((now - lastSample) / 1000);
    samples.push(hz); if (samples.length > 96) samples.shift();
    scheduleRender();
  } else if (state === 'paused') { hz = 0; scheduleRender(); }
  lastCycles = cycles; lastSample = now;
}, 250);
for (let bit = 0; bit < 32; ++bit) {
  const node = element('span', String(bit)); node.title = `Bit ${bit}`; $('bit-strip').append(node);
}
function drawTiming() {
  const canvas = $('timing');
  const box = canvas.getBoundingClientRect();
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const width = box.width, height = 222;
  if (canvas.width !== Math.round(width * dpr) || canvas.height !== height * dpr) {
    canvas.width = Math.round(width * dpr); canvas.height = height * dpr;
  }
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, width, height);
  const left = width < 500 ? 58 : 80, right = 16;
  const columns = width < 500 ? 32 : 64;
  const visible = wave.slice(-columns), cell = (width - left - right) / columns;
  const labels = ['CLK', 'FETCH', 'DATA', 'SERIAL', 'RS1', 'RD'];
  ctx.font = '10px Berkeley, monospace';
  for (let column = 0; column <= columns; column += 4) {
    ctx.strokeStyle = column % 16 === 0 ? '#333' : '#1d1d1d';
    ctx.beginPath(); ctx.moveTo(left + column * cell, 10); ctx.lineTo(left + column * cell, height - 12); ctx.stroke();
  }
  for (let row = 0; row < labels.length; ++row) {
    const y = 18 + row * 33;
    ctx.fillStyle = '#aaa'; ctx.fillText(labels[row], 15, y + 11);
    ctx.strokeStyle = '#252525'; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(left, y + 18); ctx.lineTo(width - right, y + 18); ctx.stroke();
    if (!visible.length) continue;
    ctx.strokeStyle = row === 0 ? '#888' : '#eee'; ctx.lineWidth = 1.25; ctx.beginPath();
    let previous = null;
    for (let index = 0; index < visible.length; ++index) {
      const x = left + index * cell;
      if (row === 0) {
        if (index === 0) ctx.moveTo(x, y + 18); else ctx.lineTo(x, y + 18);
        ctx.lineTo(x + cell / 2, y + 18); ctx.lineTo(x + cell / 2, y + 2); ctx.lineTo(x + cell, y + 2);
      } else {
        const level = y + (visible[index][row] ? 2 : 18);
        if (previous === null) ctx.moveTo(x, level);
        else { ctx.lineTo(x, previous); ctx.lineTo(x, level); }
        ctx.lineTo(x + cell, level); previous = level;
      }
    }
    ctx.stroke();
  }
  const chosen = visible.length ? Math.min(waveHover >= 0 ? waveHover : visible.length - 1, visible.length - 1) : -1;
  if (chosen >= 0) {
    const sample = visible[chosen], x = left + (chosen + .5) * cell;
    ctx.fillStyle = '#ffffff0c'; ctx.fillRect(left + chosen * cell, 10, cell, height - 22);
    ctx.strokeStyle = '#777'; ctx.setLineDash([2, 3]); ctx.beginPath(); ctx.moveTo(x, 10); ctx.lineTo(x, height - 12); ctx.stroke(); ctx.setLineDash([]);
    $('wave-range').textContent = `${numbers.format(visible[0][0])}–${numbers.format(visible[visible.length - 1][0])}`;
    $('wave-cursor').textContent = `Cycle ${numbers.format(sample[0])} · ${sample[6] < 0 ? 'idle' : `bit ${sample[6]}`}`;
    for (let bit = 0; bit < 32; ++bit) $('bit-strip').children[bit].classList.toggle('active', bit === sample[6]);
  } else {
    $('wave-range').textContent = `${columns} cycles`; $('wave-cursor').textContent = '—';
    for (const node of $('bit-strip').children) node.classList.remove('active');
  }
  canvas.setAttribute('aria-label', visible.length ? `SERV timing, cycles ${visible[0][0]} through ${visible[visible.length - 1][0]}. Clock, fetch, data, serial active, RS1 and RD signals.` : 'SERV timing: no samples yet');
}
$('timing').addEventListener('pointermove', event => {
  const width = $('timing').getBoundingClientRect().width, left = width < 500 ? 58 : 80, columns = width < 500 ? 32 : 64;
  waveHover = Math.max(0, Math.min(columns - 1, Math.floor((event.offsetX - left) / ((width - left - 16) / columns))));
  scheduleRender();
});
$('timing').addEventListener('pointerleave', () => { waveHover = -1; scheduleRender(); });
new ResizeObserver(scheduleRender).observe($('timing'));
$('source').addEventListener('input', () => { edited = $('source').value !== programs.find(p => p.id === program).source; $('compile-status').textContent = edited ? 'C → RV32I · edited' : 'C → RV32I'; updateControls(); });
$('source').addEventListener('keydown', event => {
  if (event.key === 'Tab' && !$('source').readOnly) {
    event.preventDefault(); const editor = $('source');
    editor.setRangeText('    ', editor.selectionStart, editor.selectionEnd, 'end'); editor.dispatchEvent(new Event('input'));
  }
  if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) { event.preventDefault(); $('run').click(); }
});
$('restore').addEventListener('click', action(async () => {
  await reset(); $('source').value = programs.find(p => p.id === program).source;
  edited = false; $('compile-status').textContent = 'C → RV32I'; updateControls();
}));
$('share').addEventListener('click', action(async () => {
  const payload = JSON.stringify({ program, source: $('source').value });
  const bytes = new TextEncoder().encode(payload);
  const encoded = btoa(String.fromCharCode(...bytes)).replaceAll('+', '-').replaceAll('/', '_').replaceAll('=', '');
  const url = `${location.origin}/#code=${encoded}`;
  if (navigator.share) await navigator.share({ title: 'RISC-jeV', url });
  else if (navigator.clipboard) { await navigator.clipboard.writeText(url); $('share').textContent = 'Link copied'; setTimeout(() => $('share').textContent = 'Share program', 2000); }
  else { location.hash = `code=${encoded}`; $('share').textContent = 'Link in address bar'; }
}));
if (location.hash.startsWith('#code=')) {
  try {
    const encoded = location.hash.slice(6);
    if (encoded.length > 24000) throw new Error('Shared program is too large');
    const bytes = Uint8Array.from(atob(encoded.replaceAll('-', '+').replaceAll('_', '/')), c => c.charCodeAt(0));
    const data = JSON.parse(new TextDecoder().decode(bytes));
    if (!programs.some(p => p.id === data.program) || typeof data.source !== 'string' || new TextEncoder().encode(data.source).length > 12000) throw new Error('Invalid shared program');
    program = data.program; $('source').value = data.source; edited = true;
    $('filename').textContent = 'shared.c'; $('compile-status').textContent = 'C → RV32I · shared';
    for (const tab of document.querySelectorAll('[data-program]')) tab.setAttribute('aria-selected', String(tab.dataset.program === program));
  } catch (e) { error(e.message); }
}
render();

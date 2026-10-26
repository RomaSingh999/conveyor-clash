/* Conveyor Clash client.
   The server is the single source of truth: every message is a full snapshot of
   the room, and the page re-renders from it. The only local state is which
   packages you have tapped before locking in. */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const COLORS = ['#2f6fde', '#d9480f', '#2b8a3e', '#9c36b5', '#c2255c', '#0b7285'];
  const EMOTES = ['😈', '😂', '🔥', '😱', '👏', '🤔'];
  const PING_MS = 25000;
  const MAX_RETRIES = 15;

  // ---------------------------------------------------------------- storage
  const store = {
    get(key) { try { return localStorage.getItem(key); } catch (e) { return null; } },
    set(key, value) { try { localStorage.setItem(key, value); } catch (e) { /* private mode */ } },
    del(key) { try { localStorage.removeItem(key); } catch (e) { /* ignore */ } },
  };

  // ------------------------------------------------------------------ state
  let ws = null;
  let code = null;
  let myName = '';
  let me = null;
  let state = null;
  let stateAt = 0;
  let sel = [];
  let forklift = false;
  let lastRound = 0;
  let revealAnimatedFor = 0;
  let intentionalClose = false;
  let fatal = false;
  let retry = 0;
  let retryTimer = null;
  let pingTimer = null;
  let toastTimer = null;
  let sentPick = false;

  // ---------------------------------------------------------------- helpers
  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function signed(n) {
    if (n > 0) return '+' + n;
    if (n < 0) return '−' + Math.abs(n);
    return '0';
  }

  function valueText(p) { return p.value < 0 ? '−' + Math.abs(p.value) : String(p.value); }

  function playerColor(id) {
    if (!state) return '#888';
    const i = state.players.findIndex((p) => p.id === id);
    return COLORS[(i < 0 ? 0 : i) % COLORS.length];
  }

  function dot(id) {
    const d = el('span', 'dot');
    d.style.background = playerColor(id);
    return d;
  }

  function toast(message) {
    const t = $('toast');
    t.textContent = message;
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, 3800);
  }

  function showBanner(message) {
    const b = $('banner');
    if (message) { b.textContent = message; b.hidden = false; } else { b.hidden = true; }
  }

  function showView(name) {
    for (const v of ['home', 'lobby', 'game']) $('view-' + v).hidden = v !== name;
    $('emote-bar').hidden = name === 'home';
  }

  // ------------------------------------------------------------- navigation
  function goHome(message) {
    intentionalClose = true;
    clearTimeout(retryTimer);
    stopPing();
    if (ws) { try { ws.close(); } catch (e) { /* ignore */ } }
    ws = null; state = null; me = null; sel = []; forklift = false; lastRound = 0;
    store.del('cc_session');
    showBanner('');
    showView('home');
    history.replaceState(null, '', '/');
    $('invite-note').hidden = true;
    showHomeError(message || '');
  }

  function showHomeError(message) {
    const e = $('home-error');
    e.textContent = message;
    e.hidden = !message;
  }

  // -------------------------------------------------------------- websocket
  function connect(roomCode, name) {
    code = roomCode.toUpperCase();
    myName = name;
    intentionalClose = false;
    fatal = false;
    retry = 0;
    openSocket();
  }

  function openSocket() {
    clearTimeout(retryTimer);
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const sock = new WebSocket(`${proto}://${location.host}/ws/${code}`);
    ws = sock;
    sock.onopen = () => {
      retry = 0;
      sock.send(JSON.stringify({ type: 'hello', name: myName, token: store.get('cc_token_' + code) }));
      startPing();
    };
    sock.onmessage = (event) => {
      let msg;
      try { msg = JSON.parse(event.data); } catch (e) { return; }
      onMessage(msg);
    };
    sock.onclose = (event) => {
      if (ws !== sock) return;
      stopPing();
      if (intentionalClose || fatal) return;
      if (event.code === 4000) { goHome('This game is open in another tab. Close that tab or reload to take over.'); return; }
      scheduleReconnect();
    };
    sock.onerror = () => { /* onclose handles it */ };
  }

  function scheduleReconnect() {
    if (retry >= MAX_RETRIES) { goHome('Lost the connection to the game. Join again with the room code.'); return; }
    showBanner('Connection lost. Reconnecting…');
    const delay = Math.min(500 * Math.pow(2, retry), 5000);
    retry += 1;
    retryTimer = setTimeout(openSocket, delay);
  }

  function startPing() {
    stopPing();
    pingTimer = setInterval(() => send({ type: 'ping' }), PING_MS);
  }
  function stopPing() { clearInterval(pingTimer); pingTimer = null; }

  function send(message) {
    if (ws && ws.readyState === WebSocket.OPEN) { ws.send(JSON.stringify(message)); return true; }
    return false;
  }

  function onMessage(msg) {
    switch (msg.type) {
      case 'welcome':
        me = msg.player_id;
        code = msg.code;
        store.set('cc_token_' + code, msg.token);
        store.set('cc_session', JSON.stringify({ code, name: myName }));
        store.set('cc_name', myName);
        showBanner('');
        history.replaceState(null, '', '/?room=' + code);
        break;
      case 'state':
        onState(msg);
        break;
      case 'emote':
        floatEmote(msg);
        break;
      case 'error':
        if (msg.fatal) {
          fatal = true;
          if (msg.code === 'room_not_found' || msg.code === 'in_progress') store.del('cc_token_' + code);
          goHome(msg.message);
        } else {
          sentPick = false;
          toast(msg.message);
          render();
        }
        break;
      default:
        break;
    }
  }

  function onState(s) {
    if (s.phase === 'picking' && s.round !== lastRound) { sel = []; forklift = false; sentPick = false; }
    if (s.phase === 'lobby') { sel = []; forklift = false; sentPick = false; lastRound = 0; } else { lastRound = s.round; }
    if (s.you.locked && s.you.pick) { sel = s.you.pick.slice(); forklift = s.you.pick.length === 2; }
    state = s;
    stateAt = performance.now();
    render();
  }

  // ----------------------------------------------------------------- render
  function render() {
    if (!state) return;
    showBanner('');
    if (state.phase === 'lobby') { showView('lobby'); renderLobby(); } else { showView('game'); renderGame(); }
    tick();
  }

  function renderLobby() {
    const s = state;
    $('lobby-code').textContent = s.code;
    const roster = $('lobby-players');
    roster.replaceChildren();
    for (const p of s.players) {
      const li = el('li', p.id === me ? 'you' : '');
      li.append(dot(p.id), el('span', 'pname', p.name));
      if (p.id === me) li.append(el('span', 'pill', 'You'));
      if (p.is_bot) li.append(el('span', 'pill', 'Bot'));
      li.append(el('span', 'spacer'));
      if (p.is_host) li.append(el('span', 'pill warn', 'Host'));
      if (!p.connected) li.append(el('span', 'pill off', 'Offline'));
      if (p.is_bot && s.you.is_host) {
        const rm = el('button', 'mini-btn', 'Remove');
        rm.type = 'button';
        rm.setAttribute('aria-label', 'Remove bot ' + p.name);
        rm.addEventListener('click', () => send({ type: 'remove_bot', id: p.id }));
        li.append(rm);
      }
      roster.append(li);
    }
    const online = s.players.filter((p) => p.connected).length;
    $('lobby-count').textContent = `Players (${s.players.length} of ${s.max_players})`;
    const start = $('btn-start');
    start.hidden = !s.you.is_host;
    start.disabled = online < s.min_players;
    const addBot = $('btn-add-bot');
    addBot.hidden = !s.you.is_host;
    addBot.disabled = s.players.length >= s.max_players;
    $('lobby-hint').textContent = s.you.is_host
      ? (online < s.min_players ? 'Playing alone? Add a bot player. Or share the code with friends.' : 'Everyone in? Start when you are ready.')
      : 'Waiting for the host to start the game.';
  }

  function canPick() { return state && state.phase === 'picking' && !state.you.locked; }

  function renderGame() {
    const s = state;
    const finished = s.phase === 'finished';
    $('game-code').textContent = s.code;
    $('final-panel').hidden = !finished;
    $('play-area').hidden = finished;
    if (finished) renderFinal();
    else renderPlay();
    renderScoreboard();
    renderHistory();
  }

  function renderPlay() {
    const s = state;
    $('round-label').textContent = `Round ${s.round} of ${s.total_rounds}`;
    $('double-badge').hidden = !(s.belt && s.belt.multiplier === 2);
    $('result-panel').hidden = s.phase !== 'reveal';
    renderStatus();
    renderBelt();
    renderActions();
    if (s.phase === 'reveal') renderResults();
  }

  function renderStatus() {
    const s = state;
    const st = $('status');
    if (s.phase === 'picking') {
      if (s.you.locked) {
        const waiting = s.players.filter((p) => p.connected && !p.locked).length;
        st.textContent = waiting > 0
          ? `Locked in. Waiting for ${waiting} more ${waiting === 1 ? 'player' : 'players'}…`
          : 'Everyone is locked in. Revealing…';
      } else if (forklift) {
        st.textContent = sel.length < 2
          ? 'Forklift on: tap two packages. It only pays if neither one clashes.'
          : 'Two packages picked. Lock in when you are ready.';
      } else {
        st.textContent = 'Pick one package. Nobody sees your pick until everyone has locked in.';
      }
    } else if (s.phase === 'reveal') {
      st.textContent = `Round ${s.round} result`;
    }
  }

  function renderBelt() {
    const s = state;
    const beltEl = $('belt');
    beltEl.replaceChildren();
    const picking = s.phase === 'picking';
    const result = s.phase === 'reveal' ? s.result : null;
    const mult = s.belt.multiplier;
    const rows = result ? result.rows : [];
    const animate = result && revealAnimatedFor !== s.round;
    beltEl.classList.toggle('reveal-anim', !!animate);
    if (result) revealAnimatedFor = s.round;

    s.belt.packages.forEach((p, index) => {
      const b = el('button', 'pkg');
      b.type = 'button';
      b.dataset.id = String(p.id);
      b.disabled = !canPick();
      if (p.fragile) b.classList.add('fragile');

      b.append(el('span', 'pkg-key', String(index + 1)));
      b.append(el('span', 'pkg-label', p.label));
      b.append(el('span', 'pkg-value', valueText(p)));
      if (p.fragile) b.append(el('span', 'pkg-stamp', 'Fragile'));

      let description = `${p.label}, ${p.value} ${Math.abs(p.value) === 1 ? 'point' : 'points'}${p.fragile ? ', fragile' : ''}`;

      if (picking) {
        const chosen = sel.includes(p.id);
        if (chosen) {
          b.classList.add(state.you.locked ? 'mine' : 'selected');
          b.append(el('span', 'pkg-tag you', state.you.locked ? 'Locked' : 'Picked'));
          description += ', picked';
        }
        b.setAttribute('aria-pressed', chosen ? 'true' : 'false');
      } else if (result) {
        const pickers = el('span', 'pkg-pickers');
        const here = rows.filter((r) => r.picks.includes(p.id));
        here.forEach((r, i) => {
          const chip = el('span', r.id === me ? 'chip me' : 'chip');
          chip.style.animationDelay = (i * 0.12) + 's';
          chip.append(dot(r.id), el('span', 'cn', r.name));
          pickers.append(chip);
        });
        b.append(pickers);
        if (here.length > 1) {
          b.classList.add('clash');
          b.append(el('span', 'pkg-tag clash', 'Clash'));
          description += ', clash';
        } else if (here.length === 1) {
          const r = here[0];
          if (r.scored.includes(p.id)) {
            const pts = p.value * mult;
            b.classList.add('sole');
            b.append(el('span', pts < 0 ? 'pkg-tag loss' : 'pkg-tag gain', signed(pts)));
            description += `, ${r.name} scored ${pts}`;
          } else if (r.forklift_failed) {
            b.classList.add('jam');
            b.append(el('span', 'pkg-tag jam', 'Jammed'));
          }
        } else {
          b.classList.add('dim');
        }
      }
      b.setAttribute('aria-label', description);
      beltEl.append(b);
    });
  }

  function renderActions() {
    const s = state;
    const picking = s.phase === 'picking';
    $('actions').hidden = !picking;
    if (!picking) return;
    const locked = s.you.locked;
    const fork = $('btn-forklift');
    fork.setAttribute('aria-pressed', forklift ? 'true' : 'false');
    fork.disabled = locked || !s.you.forklift_available;
    fork.textContent = !s.you.forklift_available
      ? 'Forklift used'
      : forklift ? 'Forklift on: pick two' : 'Use forklift (once per game)';
    const lock = $('btn-lock');
    const ready = forklift ? sel.length === 2 : sel.length === 1;
    lock.disabled = locked || sentPick || !ready;
    lock.textContent = locked ? 'Locked in' : 'Lock in';
  }

  function describeRow(r, packages, mult) {
    const byId = (id) => packages.find((p) => p.id === id);
    const name = (id) => { const p = byId(id); return `${p.label} (${valueText(p)})`; };
    if (r.idle && r.stumbled) return ['Ran out of time and stumbled into the Fragile package', r.clashed.length ? 'Someone else was on it too, so no points lost.' : ''];
    if (r.idle) return ['Ran out of time', 'No package picked.'];
    if (r.forklift && r.forklift_failed) return [`Forklift jammed: ${r.picks.map(name).join(' + ')}`, 'A clash on one package cancelled the whole forklift.'];
    if (r.forklift) return [`Forklift: ${r.picks.map(name).join(' + ')}`, 'Both packages were safe.'];
    const id = r.picks[0];
    if (r.clashed.length) return [`Picked ${name(id)}`, 'Clashed with another player.'];
    return [`Picked ${name(id)}`, mult === 2 ? 'Sole pick, double points.' : 'Sole pick.'];
  }

  function renderResults() {
    const result = state.result;
    const list = $('result-list');
    list.replaceChildren();
    const rows = result.rows.slice().sort((a, b) => b.gain - a.gain);
    for (const r of rows) {
      const li = el('li');
      li.append(dot(r.id));
      const what = el('div', 'what');
      const [headline, detail] = describeRow(r, result.packages, result.multiplier);
      const who = el('span', 'pname', (r.id === me ? 'You' : r.name) + ': ');
      what.append(who, document.createTextNode(headline));
      if (detail) what.append(el('small', '', detail));
      li.append(what);
      li.append(el('span', 'delta ' + (r.gain > 0 ? 'gain' : r.gain < 0 ? 'loss' : 'zero'), signed(r.gain)));
      list.append(li);
    }
  }

  function renderScoreboard() {
    const s = state;
    const board = $('scoreboard');
    board.replaceChildren();
    const gains = {};
    if (s.phase === 'reveal' && s.result) for (const r of s.result.rows) gains[r.id] = r.gain;
    const ranked = s.players.slice().sort((a, b) => b.score - a.score);
    let rank = 0; let prev = null;
    ranked.forEach((p, i) => {
      if (p.score !== prev) { rank = i + 1; prev = p.score; }
      const li = el('li', p.id === me ? 'you' : '');
      li.append(el('span', 'rank', String(rank)), dot(p.id), el('span', 'pname', p.id === me ? `${p.name} (you)` : p.name));
      li.append(el('span', 'spacer'));
      if (s.phase === 'picking') {
        if (!p.connected) li.append(el('span', 'pill off', p.left ? 'Left' : 'Offline'));
        else li.append(el('span', p.locked ? 'pill ok' : 'pill warn', p.locked ? 'Locked in' : 'Thinking'));
      }
      if (s.phase !== 'finished' && p.forklift_available) li.append(el('span', 'pill fork', 'Forklift'));
      if (p.id in gains) li.append(el('span', 'gain-note ' + (gains[p.id] > 0 ? 'gain' : gains[p.id] < 0 ? 'loss' : ''), signed(gains[p.id])));
      li.append(el('span', 'score', String(p.score)));
      board.append(li);
    });
  }

  function renderFinal() {
    const s = state;
    const winners = s.players.filter((p) => s.winners.includes(p.id));
    let title = '';
    if (winners.length === 1) title = winners[0].id === me ? 'You win!' : `${winners[0].name} wins!`;
    else title = `Tie: ${winners.map((p) => p.name).join(' and ')}`;
    $('final-title').textContent = title;

    const board = $('final-board');
    board.replaceChildren();
    let rank = 0; let prev = null;
    s.players.slice().sort((a, b) => b.score - a.score).forEach((p, i) => {
      if (p.score !== prev) { rank = i + 1; prev = p.score; }
      const li = el('li', p.id === me ? 'you' : '');
      li.append(el('span', 'rank', String(rank)), dot(p.id), el('span', 'pname', p.id === me ? `${p.name} (you)` : p.name), el('span', 'spacer'), el('span', 'score', String(p.score)));
      board.append(li);
    });
    $('btn-again').hidden = !s.you.is_host;
    $('final-hint').textContent = s.you.is_host ? 'Start a rematch with the same players.' : 'Waiting for the host to start a rematch.';
  }

  function renderHistory() {
    const s = state;
    const host = $('history-table');
    host.replaceChildren();
    if (!s.history.length) { host.append(el('p', 'muted', 'Nothing yet. Rounds show up here after each reveal.')); return; }
    const table = el('table', 'history');
    const head = el('tr');
    head.append(el('th', '', 'Round'));
    for (const p of s.players) head.append(el('th', '', p.name));
    table.append(head);
    for (const round of s.history) {
      const tr = el('tr');
      tr.append(el('td', '', round.multiplier === 2 ? `${round.round} (x2)` : String(round.round)));
      for (const p of s.players) {
        const r = round.rows.find((row) => row.id === p.id);
        if (!r) { tr.append(el('td', '', '-')); continue; }
        const values = r.picks.map((id) => {
          const pk = round.packages.find((x) => x.id === id);
          return pk ? valueText(pk) : '?';
        });
        let text = r.idle && !r.stumbled ? 'No pick' : values.join(' + ');
        if (r.stumbled) text += ' (stumbled)';
        if (r.forklift) text += r.forklift_failed ? ' (jam)' : ' (fork)';
        text += ` → ${signed(r.gain)}`;
        const td = el('td', r.gain > 0 ? 'win' : (r.clashed.length || r.forklift_failed) ? 'clash' : '', text);
        tr.append(td);
      }
      table.append(tr);
    }
    host.append(table);
  }

  // ------------------------------------------------------------------ timer
  function tick() {
    if (!state) return;
    const timer = $('timer');
    const left = state.remaining_ms == null ? null : Math.max(0, state.remaining_ms - (performance.now() - stateAt));
    const secs = left == null ? 0 : Math.ceil(left / 1000);
    if (state.phase === 'picking' && left != null) {
      timer.classList.remove('idle');
      timer.classList.toggle('low', secs <= 5);
      $('timer-fill').style.width = (state.phase_ms ? (left / state.phase_ms) * 100 : 0) + '%';
      $('timer-text').textContent = secs + 's';
    } else {
      timer.classList.add('idle');
    }
    if (state.phase === 'reveal' && left != null) {
      const last = state.round === state.total_rounds;
      $('next-note').textContent = last ? `Final scores in ${secs}s` : `Next round in ${secs}s`;
    }
  }

  // ------------------------------------------------------------------ input
  function togglePick(id) {
    if (!canPick()) return;
    if (forklift) {
      if (sel.includes(id)) sel = sel.filter((x) => x !== id);
      else if (sel.length < 2) sel = sel.concat(id);
      else sel = [sel[1], id];
    } else {
      sel = [id];
    }
    render();
  }

  function toggleForklift() {
    if (!canPick() || !state.you.forklift_available) return;
    forklift = !forklift;
    if (!forklift && sel.length > 1) sel = sel.slice(0, 1);
    render();
  }

  function lockIn() {
    if (!canPick() || sentPick) return;
    if (!(forklift ? sel.length === 2 : sel.length === 1)) return;
    if (send({ type: 'pick', packages: sel })) { sentPick = true; renderActions(); }
    else toast('Not connected. Trying to reconnect…');
  }

  async function copyInvite() {
    const url = `${location.origin}/?room=${code}`;
    try {
      if (navigator.share && /Mobi|Android/i.test(navigator.userAgent)) {
        await navigator.share({ title: 'Conveyor Clash', text: `Join my Conveyor Clash room ${code}`, url });
        return;
      }
      await navigator.clipboard.writeText(url);
      toast('Invite link copied');
    } catch (e) {
      if (e && e.name === 'AbortError') return;
      window.prompt('Copy this invite link:', url);
    }
  }

  function leaveRoom() {
    send({ type: 'leave' });
    if (code) store.del('cc_token_' + code);
    goHome('');
  }

  function floatEmote(msg) {
    if (!EMOTES.includes(msg.emoji)) return;
    const f = el('div', 'floater');
    f.style.left = (8 + Math.random() * 76) + '%';
    f.append(el('span', '', msg.emoji), el('small', '', msg.from === me ? 'You' : msg.name));
    $('emote-layer').append(f);
    setTimeout(() => f.remove(), 2300);
  }

  // ------------------------------------------------------------- home / init
  function submitHome(event) {
    event.preventDefault();
    if (document.activeElement === $('in-code')) joinRoom(); else createRoom();
  }

  function readName() {
    const name = $('in-name').value.replace(/\s+/g, ' ').trim();
    if (!name) { showHomeError('Enter your name first.'); $('in-name').focus(); return null; }
    return name;
  }

  async function createRoom() {
    showHomeError('');
    const name = readName();
    if (!name) return;
    const btn = $('btn-create');
    btn.disabled = true;
    try {
      const res = await fetch('/api/rooms', { method: 'POST' });
      const data = await res.json();
      if (!res.ok) { showHomeError(data.error || 'Could not create a room. Try again.'); return; }
      connect(data.code, name);
    } catch (e) {
      showHomeError('Could not reach the server. Check your connection and try again.');
    } finally {
      btn.disabled = false;
    }
  }

  function joinRoom() {
    showHomeError('');
    const name = readName();
    if (!name) return;
    const roomCode = $('in-code').value.trim().toUpperCase();
    if (!/^[A-Z]{4}$/.test(roomCode)) { showHomeError('Room codes are 4 letters. Check the code and try again.'); $('in-code').focus(); return; }
    connect(roomCode, name);
  }

  function init() {
    const rules = $('tpl-rules').content;
    $('how-rules').append(rules.cloneNode(true));
    $('lobby-rules').append(rules.cloneNode(true));

    const bar = $('emote-bar');
    for (const emoji of EMOTES) {
      const b = el('button', '', emoji);
      b.type = 'button';
      b.setAttribute('aria-label', 'Send ' + emoji);
      b.addEventListener('click', () => send({ type: 'emote', emoji }));
      bar.append(b);
    }

    $('form-home').addEventListener('submit', submitHome);
    $('btn-join').addEventListener('click', joinRoom);
    $('in-code').addEventListener('input', (e) => { e.target.value = e.target.value.replace(/[^a-zA-Z]/g, '').toUpperCase(); });
    $('btn-how').addEventListener('click', () => $('how-dialog').showModal());
    $('btn-how-close').addEventListener('click', () => $('how-dialog').close());
    $('btn-start').addEventListener('click', () => send({ type: 'start' }));
    $('btn-add-bot').addEventListener('click', () => send({ type: 'add_bot' }));
    $('btn-again').addEventListener('click', () => send({ type: 'play_again' }));
    $('btn-copy').addEventListener('click', copyInvite);
    $('btn-forklift').addEventListener('click', toggleForklift);
    $('btn-lock').addEventListener('click', lockIn);
    for (const id of ['btn-leave-lobby', 'btn-leave-game', 'btn-leave-final']) $(id).addEventListener('click', leaveRoom);
    $('belt').addEventListener('click', (e) => {
      const card = e.target.closest('.pkg');
      if (card && !card.disabled) togglePick(Number(card.dataset.id));
    });

    document.addEventListener('keydown', (e) => {
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      if (document.activeElement && document.activeElement.tagName === 'INPUT') return;
      if (!state || state.phase !== 'picking') return;
      if (/^[1-9]$/.test(e.key)) {
        const p = state.belt.packages[Number(e.key) - 1];
        if (p) togglePick(p.id);
      } else if (e.key === 'f' || e.key === 'F') {
        toggleForklift();
      } else if (e.key === 'Enter' && document.activeElement === document.body) {
        lockIn();
      }
    });

    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible' && code && !intentionalClose && !fatal && (!ws || ws.readyState > 1)) {
        retry = 0;
        openSocket();
      }
    });

    setInterval(tick, 100);

    $('in-name').value = store.get('cc_name') || '';
    const params = new URLSearchParams(location.search);
    const roomParam = (params.get('room') || '').toUpperCase();
    let saved = null;
    try { saved = JSON.parse(store.get('cc_session') || 'null'); } catch (e) { saved = null; }
    const target = /^[A-Z]{4}$/.test(roomParam) ? roomParam : (saved && saved.code);

    showView('home');
    if (target && store.get('cc_token_' + target)) {
      showBanner('Rejoining your game…');
      connect(target, (saved && saved.name) || store.get('cc_name') || 'Player');
    } else if (/^[A-Z]{4}$/.test(roomParam)) {
      $('in-code').value = roomParam;
      const note = $('invite-note');
      note.textContent = `You are invited to room ${roomParam}. Enter your name and join.`;
      note.hidden = false;
      $('in-name').focus();
    }
  }

  init();
})();

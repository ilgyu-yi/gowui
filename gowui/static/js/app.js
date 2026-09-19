/* Wires the board, the control panel and the server's WebSocket together. */
(function () {
  'use strict';

  var $ = function (id) { return document.getElementById(id); };
  var t = i18n.t;

  var state = {
    game: null,
    engine: { connected: false },
    settings: {},
    analysis: null,
    analysisCursor: -1,
    thinking: false
  };

  var socket = null;
  var reconnectDelay = 500;
  // Set by a 4401 / 4403 close: the page stops reconnecting and keeps saying why.
  var closedFor = null;
  // The engine host/port/protocol inputs belong to the user once they touch
  // them; state broadcasts must not type over what someone is filling in.
  var engineFormDirty = false;
  var wasConnected = null;
  // The page's own engine defaults, replaced by /api/health's typed defaults.
  var formDefaults = { protocol: 'gtp', host: '127.0.0.1', port: 6363 };
  var PORTS = { gtp: 6363, analysis: 6364, handol: 11985 };

  var board = new GoBoard($('board'), {
    onClick: function (vertex) {
      if (!state.game) return;
      send({ type: 'play', color: state.game.toPlay, vertex: vertex });
    }
  });

  /* -- transport --------------------------------------------------------- */
  function connect() {
    var scheme = location.protocol === 'https:' ? 'wss' : 'ws';
    socket = new WebSocket(scheme + '://' + location.host + '/ws');

    socket.onopen = function () {
      reconnectDelay = 500;
      setStatus('');
    };
    socket.onclose = function (event) {
      if (event.code === 4401 || event.code === 4403) {
        // Not signed in, or refused by the Host and Origin rules: retrying cannot help.
        closedFor = event.code === 4401 ? 'status.notSignedIn' : 'status.refused';
        showConnectionProblem();
        return;
      }
      showConnectionProblem();
      setTimeout(connect, reconnectDelay);
      reconnectDelay = Math.min(8000, reconnectDelay * 2);
    };
    socket.onmessage = function (event) {
      var message;
      try { message = JSON.parse(event.data); } catch (err) { return; }
      handle(message);
    };
  }

  // Every frame goes out here. A frame sent while the socket is not open is
  // dropped, and the status says the action did not go through.
  function send(message) {
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify(message));
      return;
    }
    showConnectionProblem();
  }

  function showConnectionProblem() {
    if (closedFor === 'status.notSignedIn') setStatus(t('status.notSignedIn'), true, true);
    else if (closedFor === 'status.refused') setStatus(t('status.refused'), true, true);
    else setStatus(t('status.lost'), false, true);
  }

  // The engine reports which player it searched for. Trust that over the live
  // board, which can already have moved on by the time a report is drawn.
  function searchedFor(message) {
    var reported = message.analysis && message.analysis.currentPlayer;
    if (reported === 'B') return 'black';
    if (reported === 'W') return 'white';
    return message.toPlay;
  }

  function handle(message) {
    switch (message.type) {
      case 'state':
        applyState(message);
        break;
      case 'analysis':
        if (state.game && message.cursor !== state.game.cursor) return;
        state.analysis = message.analysis;
        state.analysisCursor = message.cursor;
        window.__lastAnalysis = message.analysis;   // handy when debugging in the console
        state.searchedFor = searchedFor(message);
        board.setAnalysis(viewOf(message.analysis), state.searchedFor);
        renderEvaluation();
        renderBoards();
        break;
      case 'log':
        appendLog(message.line);
        break;
      case 'log_history':
        $('log').replaceChildren();
        (message.lines || []).forEach(appendLog);
        break;
      case 'error':
        setStatus(message.message, true);
        break;
    }
  }

  /* -- state rendering --------------------------------------------------- */
  function positionKey(game) {
    // Everything that changes which position an analysis belongs to. Cursor
    // alone is not enough -- Start with a handicap also lands on cursor 0 --
    // and neither is the board size.
    return [game.size, game.moveCount, game.cursor, game.komi, game.rules,
            game.handicap, (game.setupStones || []).map(function (stone) {
              return stone.color[0] + stone.vertex;
            }).join('')].join(':');
  }

  function applyState(message) {
    var previousKey = state.game ? positionKey(state.game) : null;
    state.game = message.game;
    state.engine = message.engine;
    state.settings = message.settings;
    state.thinking = message.thinking;

    var analysisOff = state.settings && !state.settings.analysisEnabled;
    if (positionKey(message.game) !== previousKey || analysisOff) {
      state.analysis = null;
      board.clearAnalysis();
    }
    if (message.status) setStatus(message.status);

    state.boards = message.boards || [];
    state.activeBoard = message.activeBoard;
    board.setState(message.game);
    renderBoards();
    renderEngine();
    renderControls();
    renderMoveList();
    renderEvaluation();
  }

  function renderEngine() {
    var badge = $('engine-state');
    if (state.thinking) {
      badge.textContent = t('thinking');
      badge.className = 'badge busy';
    } else if (state.engine.connected) {
      badge.textContent = (state.engine.name || t('engine')) + ' ' + (state.engine.version || '');
      badge.className = 'badge on';
    } else {
      badge.textContent = t('disconnected');
      badge.className = 'badge off';
    }
    $('connect').textContent = state.engine.connected ? t('disconnect') : t('connect');
    if (state.engine.connected !== wasConnected) {
      // Connecting or disconnecting settles the form: show what actually happened.
      wasConnected = state.engine.connected;
      engineFormDirty = false;
    }
    if (!engineFormDirty) fillEngineForm();

    // Capabilities come from the data, never from a protocol or mode name.
    $('genmove').disabled = !(state.engine.connected && state.engine.supportsGenmove);
    $('final-score').disabled = !state.engine.supportsFinalScore;
    $('raw').disabled = !state.engine.console;
  }

  // The last accepted engine request, else /api/health's defaults, else the page's own.
  function fillEngineForm() {
    var request = state.engine && state.engine.request;
    var shown = request && request.protocol ? request : formDefaults;
    $('protocol').value = shown.protocol || formDefaults.protocol;
    $('host').value = shown.host || formDefaults.host;
    $('port').value = shown.port || formDefaults.port;
  }

  function renderControls() {
    var game = state.game;
    $('move-counter').textContent = game.cursor + ' / ' + game.moveCount;
    $('to-play').textContent = t(game.toPlay);
    $('captures-black').textContent = game.captures.black;
    $('captures-white').textContent = game.captures.white;
    $('analysis-on').checked = !!state.settings.analysisEnabled;
    $('black-engine').checked = !!state.settings.blackIsEngine;
    $('white-engine').checked = !!state.settings.whiteIsEngine;
    $('black-style').value = state.settings.blackStyle || 'human';
    $('white-style').value = state.settings.whiteStyle || 'human';
    if (document.activeElement !== $('max-visits')) $('max-visits').value = state.settings.maxVisits;
    if (document.activeElement !== $('interval')) $('interval').value = state.settings.reportInterval;
    if (document.activeElement !== $('human-profile')) $('human-profile').value = state.settings.humanProfile || '';
    tupleEditor.set(state.settings.humanPolicy || {}, state.settings.humanCompare);
    if (document.activeElement !== $('eval-visits') && state.settings.evalVisits != null) {
      $('eval-visits').value = state.settings.evalVisits;
    }
    showHumanControls(state.engine.protocol === 'handol' || $('protocol').value === 'handol');
    // The server owns this one: it decides whether ownership is even requested.
    $('show-ownership').checked = !!state.settings.includeOwnership;
    board.setOptions({ showOwnership: !!state.settings.includeOwnership });
    ['first', 'prev', 'prev10'].forEach(function (id) { $(id).disabled = game.cursor === 0; });
    ['next', 'next10', 'last'].forEach(function (id) {
      $(id).disabled = game.cursor >= game.moveCount;
    });
    $('undo').disabled = game.cursor === 0;
  }

  var moveListSignature = null;
  function renderMoveList() {
    var list = $('move-list');
    var game = state.game;
    var signature = game.cursor + ':' + game.moves.map(function (m) {
      return m.color[0] + m.vertex;
    }).join(',');
    if (signature === moveListSignature) return;
    moveListSignature = signature;
    list.replaceChildren();
    game.moves.forEach(function (move, index) {
      var item = document.createElement('li');
      item.textContent = (move.color === 'black' ? '● ' : '○ ') + move.vertex;
      item.value = index + 1;
      if (index + 1 === game.cursor) item.className = 'current';
      item.onclick = function () { send({ type: 'navigate', index: index + 1 }); };
      list.appendChild(item);
    });
    var current = list.querySelector('.current');
    if (current) current.scrollIntoView({ block: 'nearest' });
  }

  function renderEvaluation() {
    var analysis = state.analysis;
    var root = analysis && analysis.rootInfo;
    var bar = $('winbar-black');
    var label = $('winbar-label');

    if (!root || root.winrate == null) {
      bar.style.width = '50%';
      label.textContent = state.engine.connected ? '--' : t('noEngine');
      $('score-lead').textContent = t('score.none');
      $('visit-count').textContent = t('visits.count', { n: 0 });
    } else {
      var blackWinrate = root.winrate;    // already Black's point of view
      bar.style.width = (blackWinrate * 100).toFixed(1) + '%';
      label.textContent = t('black') + ' ' + (blackWinrate * 100).toFixed(1) + '%  /  ' + t('white') + ' ' +
        ((1 - blackWinrate) * 100).toFixed(1) + '%';
      var lead = root.scoreLead;
      $('score-lead').textContent = lead == null ? t('score.none')
        : (lead >= 0 ? 'B+' : 'W+') + Math.abs(lead).toFixed(1);
      $('visit-count').textContent = t('visits.count', { n: goboardUtils.abbreviate(root.visits || 0) });
    }
    renderCandidates();
  }

  /* -- thumbnail strip ---------------------------------------------------- */
  // Each board is drawn small with its stones and the heatmap of its last
  // analysis; the active one uses the live analysis instead of the stored one.
  var thumbSignatures = {};

  function thumbHeat(entry) {
    if (entry.id === state.activeBoard && state.analysis && state.game &&
        state.analysisCursor === state.game.cursor) {
      return (state.analysis.policy || []).slice(0, entry.size * entry.size);
    }
    return entry.heat || [];
  }

  function drawThumb(canvas, entry) {
    var px = 2 * 120;
    canvas.width = px;
    canvas.height = px;
    var ctx = canvas.getContext('2d');
    var n = entry.size;
    var margin = px / (n + 1);
    var cell = (px - 2 * margin) / (n - 1);
    ctx.fillStyle = '#dcb06a';
    ctx.fillRect(0, 0, px, px);
    ctx.strokeStyle = 'rgba(40, 28, 10, 0.55)';
    ctx.lineWidth = 1;
    for (var i = 0; i < n; i++) {
      var at = margin + i * cell;
      ctx.beginPath(); ctx.moveTo(margin, at); ctx.lineTo(px - margin, at); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(at, margin); ctx.lineTo(at, px - margin); ctx.stroke();
    }
    var heat = thumbHeat(entry);
    var best = 0;
    heat.forEach(function (v) { best = Math.max(best, v); });
    for (var k = 0; k < n * n; k++) {
      var x = k % n, y = Math.floor(k / n);
      var cx = margin + x * cell, cy = margin + y * cell;
      var p = heat[k] || 0;
      if (best > 0 && p > 0.002) {
        ctx.fillStyle = 'hsla(260, 85%, 42%, ' + (0.1 + 0.7 * Math.pow(p / best, 0.45)).toFixed(3) + ')';
        ctx.fillRect(cx - cell * 0.45, cy - cell * 0.45, cell * 0.9, cell * 0.9);
      }
      var stone = entry.stones[k];
      if (stone) {
        ctx.beginPath();
        ctx.arc(cx, cy, cell * 0.47, 0, Math.PI * 2);
        ctx.fillStyle = stone === 1 ? '#15171b' : '#f2f2ec';
        ctx.fill();
      }
    }
    var last = entry.lastMove && goboardUtils.vertexToPoint(entry.lastMove, n);
    if (last) {
      ctx.beginPath();
      ctx.arc(margin + last.x * cell, margin + last.y * cell, cell * 0.2, 0, Math.PI * 2);
      ctx.fillStyle = '#e2574c';
      ctx.fill();
    }
  }

  function thumbMeta(entry) {
    var parts = [t('boards.move', { n: entry.cursor, total: entry.moveCount })];
    if (entry.winrate != null) parts.push(t('black') + ' ' + (entry.winrate * 100).toFixed(0) + '%');
    return parts.join(' · ');
  }

  function tupleSummary(entry) {
    var keys = Object.keys(entry.policy || {});
    var text = keys.length ? keys.map(function (k) { return k.replace(/_.*/, '') + ' ' + entry.policy[k]; }).join(', ')
                           : t('boards.identity');
    return entry.profile + ' · ' + text + (entry.compare ? ' · A/B' : '');
  }

  function renderBoards() {
    var list = $('board-list');
    var entries = state.boards || [];
    var ids = entries.map(function (e) { return String(e.id); });
    // Drop tiles for boards that no longer exist.
    Array.prototype.slice.call(list.children).forEach(function (node) {
      if (ids.indexOf(node.dataset.id) < 0) { list.removeChild(node); delete thumbSignatures[node.dataset.id]; }
    });
    entries.forEach(function (entry, index) {
      var node = list.querySelector('.thumb[data-id="' + entry.id + '"]');
      if (!node) {
        node = document.createElement('div');
        node.className = 'thumb';
        node.dataset.id = String(entry.id);
        buildThumb(node);
        node.onclick = function () { send({ type: 'board_select', id: entry.id }); };
        var startRename = function (event) {
          event.stopPropagation();
          renameInPlace(node, entry.id);
        };
        node.querySelector('.thumb-name').ondblclick = startRename;
        node.querySelector('.thumb-edit').onclick = startRename;
        node.querySelector('.thumb-close').onclick = function (event) {
          event.stopPropagation();
          if (window.confirm(t('boards.confirmDelete', { name: displayName(node.dataset.name) }))) {
            send({ type: 'board_delete', id: entry.id });
          }
        };
      }
      if (list.children[index] !== node) list.insertBefore(node, list.children[index] || null);
      node.classList.toggle('active', entry.id === state.activeBoard);
      node.querySelector('.thumb-close').hidden = entries.length < 2;
      node.querySelector('.thumb-close').title = t('boards.delete');
      if (!node.querySelector('.thumb-rename')) {
        node.querySelector('.thumb-name').textContent = displayName(entry.name);
      }
      node.dataset.name = entry.name;
      node.querySelector('.thumb-name').title = t('boards.renameHint');
      node.querySelector('.thumb-edit').title = t('boards.rename');
      node.querySelector('.thumb-meta').textContent = thumbMeta(entry);
      node.querySelector('.thumb-tuple').textContent = tupleSummary(entry);
      node.querySelector('.thumb-tuple').title = tupleSummary(entry);
      var heat = thumbHeat(entry);
      var signature = entry.stones.join('') + '|' + entry.lastMove + '|' + heat.length + ':' +
        heat.reduce(function (acc, v, i) { return acc + v * (i + 1); }, 0).toFixed(4);
      if (thumbSignatures[entry.id] !== signature) {
        thumbSignatures[entry.id] = signature;
        drawThumb(node.querySelector('canvas'), entry);
      }
    });
  }

  function element(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text) node.textContent = text;
    return node;
  }

  function buildThumb(node) {
    var title = element('div', 'thumb-title');
    title.append(element('span', 'thumb-name'), element('button', 'thumb-edit', '✎'));
    title.lastChild.type = 'button';
    var close = element('button', 'thumb-close', '×');
    close.type = 'button';
    node.append(element('canvas'), title, element('div', 'thumb-meta'),
                element('div', 'thumb-meta thumb-tuple'), close);
  }

  // The server's default name is exactly "Board N"; only that one is localised.
  var DEFAULT_NAME = /^Board (\d+)$/;
  function isDefaultName(name) { return DEFAULT_NAME.test(name || ''); }
  function displayName(name) {
    var match = DEFAULT_NAME.exec(name || '');
    return match ? t('boards.default', { n: match[1] }) : name;
  }

  // Swap the name for an input in place: Enter saves, Escape or an empty
  // name leaves it as it was.
  function renameInPlace(node, boardId) {
    if (node.querySelector('.thumb-rename')) return;
    var label = node.querySelector('.thumb-name');
    var field = document.createElement('input');
    field.type = 'text';
    field.className = 'thumb-rename';
    field.maxLength = 40;
    field.value = isDefaultName(node.dataset.name) ? '' : node.dataset.name;
    field.placeholder = displayName(node.dataset.name);
    label.hidden = true;
    label.parentNode.insertBefore(field, label);
    field.focus();
    field.select();
    var done = false;
    var finish = function (save) {
      if (done) return;
      done = true;
      var name = field.value.trim();
      field.remove();
      label.hidden = false;
      if (save && name && name !== node.dataset.name) {
        label.textContent = name;
        send({ type: 'board_rename', id: boardId, name: name });
      }
    };
    field.onclick = function (event) { event.stopPropagation(); };
    field.onkeydown = function (event) {
      event.stopPropagation();  // keep [ ] and the board shortcuts out of the name
      if (event.key === 'Enter') finish(true);
      else if (event.key === 'Escape') finish(false);
    };
    field.onblur = function () { finish(true); };
  }

  $('board-duplicate').onclick = function () { send({ type: 'board_duplicate' }); };

  function stepBoard(delta) {
    var entries = state.boards || [];
    var index = entries.findIndex(function (e) { return e.id === state.activeBoard; });
    var next = entries[index + delta];
    if (next) send({ type: 'board_select', id: next.id });
  }

  /* -- handol-mux comparison views --------------------------------------- */
  // The analysis as the board should draw it: tuple A (the default), tuple B,
  // or B − A. Winrates exist only for A's candidates' moves, so B's reuse them.
  function compareView() {
    var analysis = state.analysis;
    return analysis && analysis.compare ? $('compare-view').value : 'A';
  }

  function viewOf(analysis) {
    if (!analysis || !analysis.compare) return analysis;
    var view = $('compare-view').value;
    if (view === 'A') return analysis;
    var evalByMove = {};
    analysis.moveInfos.forEach(function (info) { evalByMove[info.move] = info; });
    var withEval = function (info) {
      var known = evalByMove[info.move] || {};
      return Object.assign({}, info, {
        winrate: known.winrate, scoreLead: known.scoreLead, pv: known.pv || []
      });
    };
    if (view === 'B') {
      return Object.assign({}, analysis, {
        policy: analysis.compare.policy,
        moveInfos: analysis.compare.moveInfos.map(withEval)
      });
    }
    var a = analysis.policy, b = analysis.compare.policy;
    var diff = b.map(function (v, i) { return v - (a[i] || 0); });
    var size = state.game ? state.game.size : 19;
    var moves = [];
    for (var i = 0; i < diff.length; i++) {
      if (Math.abs(diff[i]) < 0.001) continue;
      var vertex = i === size * size ? 'pass'
        : goboardUtils.pointToVertex(i % size, Math.floor(i / size), size);
      moves.push(withEval({ move: vertex, prior: diff[i], visits: 0, order: 0 }));
    }
    moves.sort(function (x, y) { return Math.abs(y.prior) - Math.abs(x.prior); });
    return Object.assign({}, analysis, { policy: diff, moveInfos: moves.slice(0, 20), diffView: true });
  }

  function probabilityOf(policy, move) {
    var size = state.game ? state.game.size : 19;
    if (move === 'pass') return policy[size * size] || 0;
    var point = goboardUtils.vertexToPoint(move, size);
    return point ? policy[point.y * size + point.x] || 0 : 0;
  }

  var DEFAULT_HEAD = ['col.move', 'col.win', 'col.score', 'col.visits', 'col.policy'];
  function setTableHead(keys) {
    var row = document.querySelector('table.candidates thead tr');
    keys = keys || DEFAULT_HEAD;
    var mode = keys.join(',');
    if (row.dataset.mode === mode) return;
    row.dataset.mode = mode;
    row.replaceChildren();
    keys.forEach(function (key) {
      var th = document.createElement('th');
      th.setAttribute('data-i18n', key);
      th.textContent = t(key);
      row.appendChild(th);
    });
  }

  function percent(v, signed) {
    if (v == null) return '-';
    var text = (v * 100).toFixed(1) + '%';
    return signed && v > 0 ? '+' + text : text;
  }

  function renderCandidates() {
    var body = $('candidates');
    body.replaceChildren();
    var handol = state.analysis && state.analysis.source === 'handol';
    var comparing = handol && !!state.analysis.compare;
    setTableHead(!handol ? null : comparing
      ? ['col.move', 'col.win', 'col.score', 'col.a', 'col.b', 'col.delta']
      : ['col.move', 'col.win', 'col.score', 'col.prob']);
    var shown = viewOf(state.analysis);
    var infos = (shown && shown.moveInfos) || [];

    var toPlay = state.game ? state.game.toPlay : 'black';
    var reported = state.analysis && state.analysis.currentPlayer;
    var searched = reported === 'B' ? 'black' : (reported === 'W' ? 'white' : toPlay);
    infos.slice(0, 10).forEach(function (info, index) {
      var row = document.createElement('tr');
      if (index === 0) row.className = 'best';
      var winrate = info.winrate == null ? null
        : (searched === 'black' ? info.winrate : 1 - info.winrate);
      var score = info.scoreLead == null ? '-' : (info.scoreLead >= 0 ? '+' : '') + info.scoreLead.toFixed(1);
      var cells;
      if (comparing) {
        var pa = probabilityOf(state.analysis.policy, info.move);
        var pb = probabilityOf(state.analysis.compare.policy, info.move);
        cells = [info.move, percent(winrate), score, percent(pa), percent(pb), percent(pb - pa, true)];
      } else if (handol) {
        cells = [info.move, percent(winrate), score, percent(info.prior)];
      } else {
        cells = [
          info.move,
          winrate == null ? '-' : (winrate * 100).toFixed(1) + '%',
          score,
          goboardUtils.abbreviate(info.visits),
          info.prior == null ? '-' : (info.prior * 100).toFixed(1) + '%'
        ];
      }
      cells.forEach(function (text) {
        var cell = document.createElement('td');
        cell.textContent = text;
        row.appendChild(cell);
      });
      row.onmouseenter = function () { board.setPreview(info.move); };
      row.onmouseleave = function () { board.setPreview(null); };
      row.onclick = function () {
        board.setPreview(null);
        send({ type: 'play', color: toPlay, vertex: info.move });
      };
      body.appendChild(row);
    });
  }

  function appendLog(line) {
    var log = $('log');
    var entry = document.createElement('div');
    entry.className = line.direction;
    entry.textContent = (line.direction === 'send' ? '▸ ' : '  ') + line.text;
    log.appendChild(entry);
    while (log.childElementCount > 400) log.removeChild(log.firstChild);
    log.scrollTop = log.scrollHeight;
  }

  // Messages clear after 8 s; a sticky one (the connection is down) stays.
  var statusTimer = null;
  function setStatus(text, isError, sticky) {
    var node = $('status');
    node.textContent = text || '';
    node.classList.toggle('error', !!isError);
    if (statusTimer) clearTimeout(statusTimer);
    statusTimer = null;
    if (text && !sticky) statusTimer = setTimeout(function () { node.textContent = ''; }, 8000);
  }

  /* -- controls ---------------------------------------------------------- */
  $('connect').onclick = function () {
    if (state.engine.connected) {
      send({ type: 'disconnect' });
    } else {
      send({
        type: 'connect',
        protocol: $('protocol').value,
        host: $('host').value.trim(),
        port: parseInt($('port').value, 10)
      });
    }
  };

  ['protocol', 'host', 'port'].forEach(function (id) {
    $(id).addEventListener('input', function () { engineFormDirty = true; });
  });

  $('protocol').onchange = function () {
    // The two interfaces conventionally listen on different ports.
    engineFormDirty = true;
    $('port').value = PORTS[this.value] || 6363;
    showHumanControls(this.value === 'handol' || state.engine.protocol === 'handol');
  };

  $('pass').onclick = function () { send({ type: 'pass' }); };
  $('resign').onclick = function () { send({ type: 'resign' }); };
  $('undo').onclick = function () { send({ type: 'undo' }); };
  $('genmove').onclick = function () { send({ type: 'genmove' }); };
  $('final-score').onclick = function () { send({ type: 'final_score' }); };

  function navigate(index) { send({ type: 'navigate', index: index }); }
  $('first').onclick = function () { navigate(0); };
  $('prev').onclick = function () { navigate(state.game.cursor - 1); };
  $('prev10').onclick = function () { navigate(state.game.cursor - 10); };
  $('next').onclick = function () { navigate(state.game.cursor + 1); };
  $('next10').onclick = function () { navigate(state.game.cursor + 10); };
  $('last').onclick = function () { navigate(state.game.moveCount); };

  $('analysis-on').onchange = function () { send({ type: 'analysis', enabled: this.checked }); };
  $('black-engine').onchange = function () { send({ type: 'players', blackIsEngine: this.checked }); };
  $('white-engine').onchange = function () { send({ type: 'players', whiteIsEngine: this.checked }); };
  $('black-style').onchange = function () { send({ type: 'players', blackStyle: this.value }); };
  $('white-style').onchange = function () { send({ type: 'players', whiteStyle: this.value }); };

  function pushEngineParams() {
    send({
      type: 'engine_params',
      maxVisits: parseInt($('max-visits').value, 10) || 500,
      reportInterval: parseFloat($('interval').value) || 0.4,
      includeOwnership: $('show-ownership').checked
    });
  }
  $('max-visits').onchange = function () { tupleEditor.recheck(); pushEngineParams(); };
  $('interval').onchange = pushEngineParams;
  $('show-ownership').onchange = function () {
    board.setOptions({ showOwnership: this.checked });
    pushEngineParams();
  };

  // The human surface reports probabilities only, so the first time it is
  // picked the board switches to the views that have something to show.
  var humanViewsSet = false;
  function showHumanControls(human) {
    document.querySelectorAll('.human-only').forEach(function (node) { node.hidden = !human; });
    if (!human || humanViewsSet) return;
    humanViewsSet = true;
    $('label-mode').value = 'prior';
    $('show-policy').checked = true;
    board.setOptions({ labelMode: 'prior', showPolicy: true });
  }

  function currentVisits() { return parseInt($('max-visits').value, 10) || 0; }

  var tupleEditor = humanTuple.mount({
    visits: currentVisits,
    onChange: function (pair) {
      send({ type: 'human_params', policy: pair.policy, compare: pair.compare });
    },
    onCompareToggle: function (on) {
      if (on) $('compare-view').value = 'diff';
      redrawView();
    },
    notify: function (text, isError) { setStatus(text, isError); }
  });

  function redrawView() {
    if (!state.analysis) return;
    board.setAnalysis(viewOf(state.analysis), state.searchedFor);
    renderCandidates();
  }
  $('compare-view').onchange = redrawView;

  $('eval-visits').onchange = function () {
    var visits = parseInt(this.value, 10);
    if (isNaN(visits) || visits < 0) return;
    send({ type: 'human_params', evalVisits: visits });
  };

  humanTuple.PROFILES.forEach(function (name) {
    var option = document.createElement('option');
    option.value = name;
    $('profile-list').appendChild(option);
  });
  profileHelp.mount($('profile-help'), $('human-profile'));
  $('human-profile').onchange = function () {
    var profile = this.value.trim();
    if (profile) send({ type: 'human_params', profile: profile });
  };

  $('label-mode').onchange = function () { board.setOptions({ labelMode: this.value }); };
  $('show-policy').onchange = function () { board.setOptions({ showPolicy: this.checked }); };
  $('show-numbers').onchange = function () { board.setOptions({ showNumbers: this.checked }); };

  // One table, so changing the board size cannot quietly disagree with the
  // rule set about the default komi.
  // The komi defaults belong to the rules, which live on the server. The page
  // only displays them; what it sends is null unless the user typed a number,
  // so the server stays the one place that decides.
  var ruleDefaults = {};
  var handicapKomi = 0.5;
  var komiTouched = false;

  fetch('/api/health').then(function (r) { return r.json(); }).then(function (info) {
    ruleDefaults = info.ruleDefaults || {};
    if (typeof info.handicapKomi === 'number') handicapKomi = info.handicapKomi;
    if (!komiTouched) resetKomi();
    var address = info.engineAddress || {};
    if (address.kind === 'typed' && address.defaults) {
      formDefaults = {
        protocol: address.defaults.protocol || formDefaults.protocol,
        host: address.defaults.host || formDefaults.host,
        port: address.defaults.port || formDefaults.port
      };
      if (!engineFormDirty) fillEngineForm();
    }
    // Without a console for any engine the section has nothing to offer.
    if (info.console === false) $('console-section').hidden = true;
  }).catch(function () { /* the fields keep the page's defaults; the console stays */ });

  function resetKomi() {
    if ((parseInt($('new-handicap').value, 10) || 0) >= 2) {
      $('new-komi').value = handicapKomi;
      return;
    }
    var komi = ruleDefaults[$('new-rules').value];
    if (komi !== undefined) $('new-komi').value = komi;
  }
  $('new-komi').addEventListener('input', function () { komiTouched = true; });
  $('new-size').onchange = resetKomi;
  $('new-rules').onchange = function () { komiTouched = false; resetKomi(); };
  $('new-handicap').onchange = function () { komiTouched = false; resetKomi(); };
  $('new-game').onclick = function () {
    send({
      type: 'new_game',
      size: parseInt($('new-size').value, 10),
      komi: komiTouched ? parseFloat($('new-komi').value) : null,
      rules: $('new-rules').value,
      handicap: parseInt($('new-handicap').value, 10) || 0
    });
    komiTouched = false;
  };

  // The name defaults to the board and the time; the user picks the rest.
  function defaultSgfName() {
    var now = new Date();
    var pad = function (n) { return (n < 10 ? '0' : '') + n; };
    var stamp = now.getFullYear() + pad(now.getMonth() + 1) + pad(now.getDate()) + '-' +
      pad(now.getHours()) + pad(now.getMinutes());
    var active = (state.boards || []).filter(function (b) { return b.id === state.activeBoard; })[0];
    var board = active ? '-' + active.name.replace(/[\\/:*?"<>|\s]+/g, '_') : '';
    return 'gowui' + board + '-' + stamp + '.sgf';
  }

  function withSgfExtension(name) {
    name = name.trim();
    return /\.sgf$/i.test(name) ? name : name + '.sgf';
  }

  $('save-sgf').onclick = function () {
    fetch('/api/sgf').then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.text();
    }).then(function (text) {
      if (window.showSaveFilePicker) {
        // The system save dialog: the user chooses the name and the folder.
        return window.showSaveFilePicker({
          suggestedName: defaultSgfName(),
          types: [{ description: 'SGF', accept: { 'application/x-go-sgf': ['.sgf'] } }]
        }).then(function (handle) {
          return handle.createWritable().then(function (out) {
            return out.write(text).then(function () { return out.close(); });
          }).then(function () { setStatus(t('sgf.saved', { name: handle.name })); });
        });
      }
      var name = window.prompt(t('sgf.namePrompt'), defaultSgfName());
      if (name === null || !name.trim()) return;
      var link = document.createElement('a');
      link.href = URL.createObjectURL(new Blob([text], { type: 'application/x-go-sgf' }));
      link.download = withSgfExtension(name);
      link.click();
      setTimeout(function () { URL.revokeObjectURL(link.href); }, 1000);
    }).catch(function (err) {
      if (err && err.name === 'AbortError') return;  // the user closed the dialog
      setStatus(t('sgf.failed', { error: err && err.message ? err.message : String(err) }), true);
    });
  };
  $('load-sgf').onclick = function () { $('sgf-file').click(); };
  var SGF_LIMIT = 1048576;
  // The text goes to POST /api/sgf; the new state then arrives on the socket.
  $('sgf-file').onchange = function () {
    var file = this.files && this.files[0];
    this.value = '';
    if (!file) return;
    if (file.size > SGF_LIMIT) { setStatus(t('sgf.tooLarge'), true); return; }
    file.text().then(function (text) {
      return fetch('/api/sgf', { method: 'POST', body: text });
    }).then(function (response) {
      if (response.ok) return null;
      return response.json().catch(function () { return {}; }).then(function (body) {
        if (body && typeof body.error === 'string') setStatus(body.error, true);
        else setStatus(t('sgf.loadFailed', { status: response.status }), true);
      });
    }).catch(function () {
      setStatus(t('sgf.loadFailed', { status: '-' }), true);
    });
  };

  $('raw-form').onsubmit = function (event) {
    event.preventDefault();
    var command = $('raw').value.trim();
    if (!command) return;
    send({ type: 'raw', command: command });
    $('raw').value = '';
  };

  document.addEventListener('keydown', function (event) {
    if (/^(INPUT|SELECT|TEXTAREA)$/.test(event.target.tagName)) return;
    if (!state.game) return;
    var handlers = {
      ArrowLeft: function () { navigate(state.game.cursor - 1); },
      ArrowRight: function () { navigate(state.game.cursor + 1); },
      Home: function () { navigate(0); },
      End: function () { navigate(state.game.moveCount); },
      p: function () { send({ type: 'pass' }); },
      '[': function () { stepBoard(-1); },
      ']': function () { stepBoard(1); },
      u: function () { send({ type: 'undo' }); },
      g: function () { send({ type: 'genmove' }); },
      a: function () { $('analysis-on').checked = !$('analysis-on').checked;
                       send({ type: 'analysis', enabled: $('analysis-on').checked }); }
    };
    var handler = handlers[event.key];
    if (handler) { event.preventDefault(); handler(); }
  });

  $('lang').value = i18n.lang();
  $('lang').onchange = function () { i18n.setLang(this.value); };
  i18n.onChange(function () {
    if (!state.game) return;
    renderBoards();
    renderEngine();
    renderControls();
    renderEvaluation();
  });
  i18n.apply();

  connect();
  board.resize();
})();

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
  // Set by a 4401 / 4403 / 4429 close: the page stops reconnecting and keeps saying why.
  var closedFor = null;
  // Set while the page is already leaving (the log-out form was sent): a 4401 then must not
  // start a second navigation that would cancel the first.
  var leaving = false;
  // The engine host/port/protocol inputs belong to the user once they touch
  // them; state broadcasts must not type over what someone is filling in.
  var engineFormDirty = false;
  var wasConnected = null;
  // The page's own engine defaults, replaced by /api/health's typed defaults.
  var formDefaults = { protocol: 'gtp', host: '127.0.0.1', port: 6363 };
  // The engine catalog from /api/health when the server offers one instead of typed
  // addresses (engineAddress.kind === 'catalog'); null for typed addresses.
  var catalog = null;
  // The account's preferences while the server keeps them (state.preferences, SPEC §4.2); null
  // while it does not, and the language and the presets then live in this browser (§8.5).
  var preferences = null;
  // The JSON of the preferences last applied, so a state that repeats them rebuilds nothing.
  var preferencesShown = null;
  // Set while a `preferences` this page sent has had no answering `state`: an `error` then means
  // the account holds none of it (§3.8 "Preferences").
  var preferencesSent = false;
  var PORTS = { gtp: 6363, analysis: 6364, handol: 11985 };

  // The move of the candidate under the pointer, from a board circle or a table row, or null.
  var pointedAt = null;

  var board = new GoBoard($('board'), {
    onClick: function (vertex) {
      if (!state.game) return;
      send({ type: 'play', color: state.game.toPlay, vertex: vertex });
    },
    // The board knows where the pointer is; the readout line is what says it (§3.8).
    onPointer: function (info) {
      var move = info ? info.move : null;
      if (move === pointedAt) return;
      pointedAt = move;
      renderReadout();
    }
  });

  /* -- transport --------------------------------------------------------- */
  $('logout-form').addEventListener('submit', function () { leaving = true; });

  function connect() {
    var scheme = location.protocol === 'https:' ? 'wss' : 'ws';
    socket = new WebSocket(scheme + '://' + location.host + '/ws');

    socket.onopen = function () {
      reconnectDelay = 500;
      setStatus('');
    };
    socket.onclose = function (event) {
      if (event.code === 4401) {
        // Not signed in: the guard at / picks the sign-in page or the 401 page.
        closedFor = 'status.notSignedIn';
        showConnectionProblem();
        if (!leaving) location.assign('/');
        return;
      }
      if (event.code === 4403) {
        // Refused by the Host and Origin rules: retrying cannot help.
        closedFor = 'status.refused';
        showConnectionProblem();
        return;
      }
      if (event.code === 4429) {
        // This identity already holds the most sockets it may (SPEC 4.3, 7.6): the cap is
        // still reached on the next try, so say so instead of reconnecting for ever.
        closedFor = 'status.tooManySockets';
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
    else if (closedFor === 'status.tooManySockets') setStatus(t('status.tooManySockets'), true, true);
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
        // The server sends the next state only once this one is applied (SPEC §4.3), so a slow
        // page gets the newest state instead of falling behind a backlog of stale ones.
        try { applyState(message); } finally { send({ type: 'ack' }); }
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
        reconcilePreferences();
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

  // The account's language and presets are the source of truth while the server keeps them
  // (§3.8 "Preferences"); this browser then stores neither (§8.5).
  function applyPreferences(kept) {
    preferences = kept && typeof kept === 'object' ? kept : null;
    if (!preferences) {
      // This browser keeps them: the menu reads the stored list now, not at mount (§3.8, §8.5).
      tupleEditor.useBrowserPresets();
      return;
    }
    var text = JSON.stringify(preferences);
    if (text === preferencesShown) return;
    preferencesShown = text;
    preferencesSent = false;
    i18n.useAccountLang(preferences.lang);
    tupleEditor.usePresets(preferences.presets);
  }

  // A `preferences` (§4.1) is refused whole, and a refusal brings no new `state`: the page would
  // otherwise go on showing an entry the account does not hold. So an `error` after one was sent
  // puts the language and the menu back to what the last `state` carried (§3.8 "Preferences").
  function reconcilePreferences() {
    if (!preferences || !preferencesSent) return;
    preferencesSent = false;
    preferencesShown = null;
    applyPreferences(preferences);
  }

  // The two `preferences` sends of §4.1. Each marks the send unanswered, so the `error` a
  // refusal brings can put the page back to what the account holds.
  function sendLang(lang) {
    preferencesSent = true;
    send({ type: 'preferences', lang: lang });
  }

  function sendPresets(list) {
    preferencesSent = true;
    send({ type: 'preferences', presets: list });
  }

  function applyState(message) {
    var previousKey = state.game ? positionKey(state.game) : null;
    applyPreferences(message.preferences);
    state.game = message.game;
    state.engine = message.engine;
    state.settings = message.settings;
    state.thinking = message.thinking;

    var analysisOff = state.settings && !state.settings.analysisEnabled;
    if (positionKey(message.game) !== previousKey || analysisOff) {
      state.analysis = null;
      board.clearAnalysis();
    }
    if (message.status && message.status !== state.lastStatus) setStatus(message.status);
    state.lastStatus = message.status;

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
    if (catalog) {
      if (request && typeof request.engineId === 'string' &&
          catalog.some(function (entry) { return entry.id === request.engineId; })) {
        $('engine-pick').value = request.engineId;
      }
      return;
    }
    var shown = request && request.protocol ? request : formDefaults;
    $('protocol').value = shown.protocol || formDefaults.protocol;
    $('host').value = shown.host || formDefaults.host;
    $('port').value = shown.port || formDefaults.port;
  }

  // The protocol the form would connect with: the picked catalog entry's, or the typed one.
  function formProtocol() {
    if (!catalog) return $('protocol').value;
    var picked = $('engine-pick').value;
    var entry = catalog.filter(function (e) { return e.id === picked; })[0];
    return entry ? entry.protocol : '';
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
    showHumanControls(state.engine.protocol === 'handol' || formProtocol() === 'handol');
    // The server owns this one: it decides whether ownership is even requested.
    $('show-ownership').checked = !!state.settings.includeOwnership;
    board.setOptions({ showOwnership: !!state.settings.includeOwnership });
    ['first', 'prev', 'prev10'].forEach(function (id) { $(id).disabled = game.cursor === 0; });
    ['next', 'next10', 'last'].forEach(function (id) {
      $(id).disabled = game.cursor >= game.moveCount;
    });
    $('undo').disabled = game.cursor === 0;
  }

  // The items on screen, one "b<vertex>" / "w<vertex>" key each, and the marked cursor. A new
  // state only replaces the items after the first move that differs and moves the mark, so a
  // long game does not rebuild (and lay out) its whole list on every move.
  var shownMoves = [];
  var shownCursor = -1;
  function renderMoveList() {
    var list = $('move-list');
    var game = state.game;
    var keys = game.moves.map(function (m) { return m.color[0] + m.vertex; });
    var same = 0;
    while (same < keys.length && same < shownMoves.length && keys[same] === shownMoves[same]) same++;
    if (same === keys.length && same === shownMoves.length && game.cursor === shownCursor) return;
    while (list.children.length > same) list.removeChild(list.lastChild);
    game.moves.slice(same).forEach(function (move, offset) {
      var index = same + offset;
      var item = document.createElement('li');
      item.textContent = (move.color === 'black' ? '● ' : '○ ') + move.vertex;
      item.value = index + 1;
      item.onclick = function () { send({ type: 'navigate', index: index + 1 }); };
      list.appendChild(item);
    });
    shownMoves = keys;
    shownCursor = game.cursor;
    Array.prototype.forEach.call(list.querySelectorAll('li.current'), function (item) {
      item.className = '';
    });
    if (game.cursor > 0 && list.children[game.cursor - 1]) {
      list.children[game.cursor - 1].className = 'current';
    }
    scrollLater('moves');
  }

  // Scrolling reads the layout, so it runs once per animation frame, not once per frame from
  // the server: a fast engine-vs-engine game would otherwise lay the page out on every move.
  var scrollsDue = {};
  function scrollLater(what) {
    if (scrollsDue.moves || scrollsDue.log) { scrollsDue[what] = true; return; }
    scrollsDue[what] = true;
    requestAnimationFrame(function () {
      var due = scrollsDue;
      scrollsDue = {};
      if (due.moves) {
        var current = $('move-list').querySelector('.current');
        if (current) current.scrollIntoView({ block: 'nearest' });
      }
      if (due.log) $('log').scrollTop = $('log').scrollHeight;
    });
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

  // Moving or removing a tile blurs an open rename field inside it. That blur is the page's own
  // doing, not the user leaving the field, so it saves nothing (§3.8 "Rename in place").
  var rearranging = 0;
  function rearrange(change) {
    rearranging += 1;
    try { change(); } finally { rearranging -= 1; }
  }

  // Put the tile at its board's position, giving an open rename field its focus and caret back.
  function moveTile(list, node, index) {
    var field = node.querySelector('.thumb-rename');
    var focused = !!field && document.activeElement === field;
    var caret = focused ? [field.selectionStart, field.selectionEnd] : null;
    rearrange(function () { list.insertBefore(node, list.children[index] || null); });
    if (!focused) return;
    field.focus();
    field.setSelectionRange(caret[0], caret[1]);
  }

  function renderBoards() {
    var list = $('board-list');
    var entries = state.boards || [];
    var ids = entries.map(function (e) { return String(e.id); });
    // Drop tiles for boards that no longer exist.
    rearrange(function () {
      Array.prototype.slice.call(list.children).forEach(function (node) {
        if (ids.indexOf(node.dataset.id) < 0) { list.removeChild(node); delete thumbSignatures[node.dataset.id]; }
      });
    });
    entries.forEach(function (entry, index) {
      var node = list.querySelector('.thumb[data-id="' + entry.id + '"]');
      if (!node) {
        node = document.createElement('div');
        node.className = 'thumb';
        node.dataset.id = String(entry.id);
        buildThumb(node);
        node.onclick = function () {
          if (takeSuppressedClick()) return;  // a drag never also selects (§3.8 "Reordering")
          send({ type: 'board_select', id: entry.id });
        };
        var startRename = function (event) {
          event.stopPropagation();
          renameInPlace(node, entry.id);
        };
        node.querySelector('.thumb-name').ondblclick = startRename;
        node.querySelector('.thumb-edit').onclick = startRename;
        // ⧉ names *that* tile's board: `board_duplicate` has no "the active one" meaning (§4.1).
        node.querySelector('.thumb-duplicate').onclick = function (event) {
          event.stopPropagation();
          send({ type: 'board_duplicate', id: entry.id });
        };
        node.querySelector('.thumb-close').onclick = function (event) {
          event.stopPropagation();
          // On the last tile the × asks a different question, because it will not remove the
          // tile: a dialog that said "delete" and then did not would be a lie (§3.8, §3.3).
          var last = (state.boards || []).length < 2;
          var question = t(last ? 'boards.confirmReset' : 'boards.confirmDelete',
                           { name: displayName(node.dataset.name) });
          if (window.confirm(question)) send({ type: 'board_delete', id: entry.id });
        };
        node.onkeydown = function (event) { tileKey(event, node, entry.id); };
        node.onpointerdown = function (event) { tilePointerDown(event, node, entry.id); };
        node.onpointermove = function (event) { tilePointerMove(event); };
        node.onpointerup = function (event) { tilePointerUp(event); };
        node.onpointercancel = function () { cancelDrag(); };
      }
      if (list.children[index] !== node) moveTile(list, node, index);
      node.classList.toggle('active', entry.id === state.activeBoard);
      // The × is shown on every tile, the last one included (§3.3: there it resets rather
      // than removes).
      node.querySelector('.thumb-close').title = t(entries.length < 2 ? 'boards.reset' : 'boards.delete');
      node.querySelector('.thumb-duplicate').title = t('boards.duplicate');
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
    node.tabIndex = 0;  // a tile takes focus (§3.8 "Reordering by keyboard")
    var title = element('div', 'thumb-title');
    var edit = element('button', 'thumb-edit', '✎');
    // ⧉ sits in the title row after ✎, not beside ×: the copy it makes appears below, and the
    // one button that destroys something keeps its corner to itself (§3.8).
    var duplicate = element('button', 'thumb-duplicate', '⧉');
    edit.type = 'button';
    duplicate.type = 'button';
    title.append(element('span', 'thumb-name'), edit, duplicate);
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
    node.classList.add('renaming');  // the field fills the row; no hover button is shown (§3.8)
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
      node.classList.remove('renaming');
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
    // Leaving the field saves -- unless the page moved or dropped the tile under it, which is
    // not the user leaving anything (§3.8).
    field.onblur = function () { if (!rearranging) finish(true); };
  }

  // The button under the strip appends a fresh board; where it sits is where its board lands.
  $('board-new').onclick = function () { send({ type: 'board_new' }); };

  /* -- reordering the strip (§3.8 "Reordering") ---------------------------- */
  // The server owns the order: a drop and an Alt+arrow both only *send* `board_move`, and the
  // tile moves when the server's `state` comes back. Rearranging first would need an undo for a
  // refused move. The anchor is the board the tile lands **after**, or null for the head (§4.1).

  //: a press picks nothing up until the pointer has moved this far, so a click, a double-click on
  //: the name and a press inside the rename field are all still themselves.
  var DRAG_THRESHOLD = 5;
  //: how near the strip's leading or trailing edge a drag scrolls it, and by how much a tick.
  var EDGE_BAND = 36, EDGE_STEP = 12;

  var drag = null;          // { node, id, pointerId, x, y, started, anchor }
  var edgeTimer = 0;
  var suppressClick = false;

  function takeSuppressedClick() {
    if (!suppressClick) return false;
    suppressClick = false;
    return true;
  }

  // The strip lies down its column when the layout is wide and sideways when it is narrow, so
  // the drag reads the axis the tiles are actually laid out on rather than a breakpoint.
  function stripIsHorizontal(list) {
    return window.getComputedStyle(list).flexDirection === 'row';
  }

  // The element that actually scrolls the strip on that axis: the list itself when narrow, the
  // column around it when wide.
  function stripScroller(list, horizontal) {
    for (var node = list; node && node !== document.body; node = node.parentNode) {
      var over = horizontal ? node.scrollWidth - node.clientWidth
                            : node.scrollHeight - node.clientHeight;
      if (over > 1) return node;
    }
    return null;
  }

  // The last tile whose midpoint the pointer has passed, skipping the dragged one; null before
  // the first, which is the head.
  function anchorAt(clientX, clientY) {
    var list = $('board-list');
    var horizontal = stripIsHorizontal(list);
    var at = horizontal ? clientX : clientY;
    var anchor = null;
    Array.prototype.forEach.call(list.children, function (node) {
      if (drag && node === drag.node) return;
      var box = node.getBoundingClientRect();
      var middle = horizontal ? box.left + box.width / 2 : box.top + box.height / 2;
      if (at > middle) anchor = node.dataset.id;
    });
    return anchor;
  }

  // The drop line is a class on an existing tile, never an inserted node: renderBoards indexes
  // list.children, and an extra node would corrupt its arithmetic.
  function clearDropMark() {
    Array.prototype.forEach.call($('board-list').children, function (node) {
      node.classList.remove('drop-before', 'drop-after');
    });
  }

  function showDropMark() {
    clearDropMark();
    var others = Array.prototype.filter.call($('board-list').children,
                                             function (n) { return n !== drag.node; });
    if (!others.length) return;
    if (drag.anchor == null) { others[0].classList.add('drop-before'); return; }
    others.forEach(function (node) {
      if (node.dataset.id === drag.anchor) node.classList.add('drop-after');
    });
  }

  function stopEdgeScroll() {
    if (edgeTimer) { window.clearInterval(edgeTimer); edgeTimer = 0; }
  }

  // Dragging near the strip's leading or trailing edge scrolls it, so a tile can be moved past
  // the ones that fit on screen.
  function edgeScroll(clientX, clientY) {
    stopEdgeScroll();
    var list = $('board-list');
    var horizontal = stripIsHorizontal(list);
    var box = stripScroller(list, horizontal);
    if (!box) return;
    var rect = box.getBoundingClientRect();
    var at = horizontal ? clientX : clientY;
    var lead = (horizontal ? rect.left : rect.top) + EDGE_BAND;
    var trail = (horizontal ? rect.right : rect.bottom) - EDGE_BAND;
    var step = at < lead ? -EDGE_STEP : (at > trail ? EDGE_STEP : 0);
    if (!step) return;
    edgeTimer = window.setInterval(function () {
      if (!drag || !drag.started) { stopEdgeScroll(); return; }
      if (horizontal) box.scrollLeft += step; else box.scrollTop += step;
      // The pointer has not moved, but the tiles under it have — recompute, or the drop lands
      // where it would have with no scrolling at all and the scroll serves nothing (§3.8
      // "Reordering").
      drag.anchor = anchorAt(clientX, clientY);
      showDropMark();
    }, 16);
  }

  function endDrag() {
    if (!drag) return;
    var node = drag.node;
    var pointerId = drag.pointerId;
    drag = null;
    stopEdgeScroll();
    clearDropMark();
    node.classList.remove('dragging');
    $('board-list').classList.remove('dragging');
    if (node.hasPointerCapture && node.hasPointerCapture(pointerId)) {
      node.releasePointerCapture(pointerId);
    }
  }

  // Escape or a cancelled pointer ends the drag and sends nothing; the click the press still
  // produces is swallowed all the same, so a cancelled drag does not select either.
  function cancelDrag() {
    if (!drag) return;
    if (drag.started) suppressClick = true;
    endDrag();
  }

  function tilePointerDown(event, node, boardId) {
    suppressClick = false;
    if (event.button !== 0) return;
    if (event.target.closest && event.target.closest('button, .thumb-rename')) return;
    endDrag();
    drag = { node: node, id: boardId, pointerId: event.pointerId,
             x: event.clientX, y: event.clientY, started: false, anchor: null };
    // The capture is taken at the threshold (below), not here: capturing on pointerdown retargets
    // the compatibility click and dblclick to this node, and the name's own dblclick handler —
    // rename in place, §3.8 — would never run.
  }

  function tilePointerMove(event) {
    if (!drag || drag.pointerId !== event.pointerId) return;
    if (!drag.started) {
      if (Math.abs(event.clientX - drag.x) < DRAG_THRESHOLD &&
          Math.abs(event.clientY - drag.y) < DRAG_THRESHOLD) return;
      drag.started = true;
      if (drag.node.setPointerCapture) drag.node.setPointerCapture(drag.pointerId);
      drag.node.classList.add('dragging');
      $('board-list').classList.add('dragging');
    }
    // The strip does not rearrange under the pointer: only the dimming and the line move.
    drag.anchor = anchorAt(event.clientX, event.clientY);
    showDropMark();
    edgeScroll(event.clientX, event.clientY);
  }

  function tilePointerUp(event) {
    if (!drag || drag.pointerId !== event.pointerId) return;
    var dropped = drag;
    endDrag();
    if (!dropped.started) return;   // a press that never became a drag is still a click
    suppressClick = true;
    var after = dropped.anchor == null ? null : Number(dropped.anchor);
    var entries = state.boards || [];
    var index = entries.findIndex(function (e) { return e.id === dropped.id; });
    var before = index > 0 ? entries[index - 1].id : null;
    if (after === before) return;   // it landed where it started
    send({ type: 'board_move', id: dropped.id, after: after });
  }

  // Alt with the up or down arrow moves a focused tile one place earlier or later; at the ends
  // it does nothing. Alt with the left or right arrow is deliberately not used: it is Back and
  // Forward in the major browsers. The document handler returns early while Alt is held, so this
  // one never has to fight it.
  function moveTileBy(boardId, delta) {
    var entries = state.boards || [];
    var index = entries.findIndex(function (e) { return e.id === boardId; });
    if (index < 0) return;
    var to = index + delta;
    if (to < 0 || to >= entries.length) return;
    // Computed from this tab's own view, which is safe precisely because the frame carries an
    // anchor: a stale view is refused, not misapplied (§3.8, §4.1).
    var after = delta < 0 ? (to > 0 ? entries[to - 1].id : null) : entries[to].id;
    send({ type: 'board_move', id: boardId, after: after });
  }

  function tileKey(event, node, boardId) {
    if (event.target !== node) return;   // a button or the rename field answers for itself
    if (event.altKey && (event.key === 'ArrowUp' || event.key === 'ArrowDown')) {
      event.preventDefault();
      moveTileBy(boardId, event.key === 'ArrowUp' ? -1 : 1);
      return;
    }
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      send({ type: 'board_select', id: boardId });
    }
  }

  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape') cancelDrag();
  });

  function stepBoard(delta) {
    var entries = state.boards || [];
    var index = entries.findIndex(function (e) { return e.id === state.activeBoard; });
    var next = entries[index + delta];
    if (next) send({ type: 'board_select', id: next.id });
  }

  /* -- handol-mux comparison views --------------------------------------- */
  // The analysis as the board should draw it: tuple A (the default), tuple B,
  // or B − A. The search was run on the position, not on a view of the position, so every view
  // carries the eval it produced: winrates, scores, visits and utilities exist for A's
  // candidates' moves, and B's and the difference's reuse them.
  function viewOf(analysis) {
    if (!analysis || !analysis.compare) return analysis;
    var view = $('compare-view').value;
    if (view === 'A') return analysis;
    var evalByMove = {};
    analysis.moveInfos.forEach(function (info) { evalByMove[info.move] = info; });
    // A move the search never saw takes `undefined` for each of these, which every field renders
    // as `-`: a value the engine did not report is never a zero (§3.8 "Candidate readout").
    var withEval = function (info) {
      var known = evalByMove[info.move] || {};
      return Object.assign({}, info, {
        winrate: known.winrate, scoreLead: known.scoreLead, pv: known.pv || [],
        visits: known.visits, utility: known.utility, utilityLcb: known.utilityLcb
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
      // No visit count here: the difference is a difference of two policies, and what the search
      // spent on this move — if it spent anything — is what `withEval` carries in.
      moves.push(withEval({ move: vertex, prior: diff[i], order: 0 }));
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

  // The three column sets of §3.8 "Candidate table". Visits and Value are in every one of them,
  // and the readout line takes its fields from the same three arrays - one rule, so the line and
  // the table cannot come to disagree (§3.8 "Candidate readout").
  var DEFAULT_HEAD = ['col.move', 'col.win', 'col.score', 'col.visits', 'col.policy', 'col.value'];
  var HANDOL_HEAD = ['col.move', 'col.win', 'col.score', 'col.visits', 'col.prob', 'col.value'];
  var COMPARE_HEAD = ['col.move', 'col.win', 'col.score', 'col.visits', 'col.a', 'col.b',
                      'col.delta', 'col.value'];

  function headKeys() {
    if (!state.analysis || state.analysis.source !== 'handol') return DEFAULT_HEAD;
    return state.analysis.compare ? COMPARE_HEAD : HANDOL_HEAD;
  }

  function setTableHead(keys) {
    var row = document.querySelector('table.candidates thead tr');
    var mode = keys.join(',');
    // No mode class: the columns take their contents' widths in every mode, and the eight
    // comparing ones are served by the box's sideways scrolling rather than by a narrower rule
    // the page would have to switch on (§3.8 "Candidate table", §7.5).
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

  function percent(v, plus) {
    if (v == null) return '-';
    var text = (v * 100).toFixed(1) + '%';
    return plus && v > 0 ? '+' + text : text;
  }

  // A number the engine did not report is '-', never a zero: §2.2 turns a non-finite number into
  // null, so every field can take that path, `visits` included (§3.8 "Candidate readout").
  function counted(n) {
    return n == null ? '-' : goboardUtils.abbreviate(n);
  }

  function withSign(v, digits) {
    return v == null ? '-' : (v >= 0 ? '+' : '') + v.toFixed(digits);
  }

  // Black's view in the B+ / W+ form the score line already uses. The readout writes its
  // perspective-bearing fields this way, so the line cannot contradict the circle it describes
  // (§3.8 "Candidate readout"); the table's own sign sits under a column header instead.
  function blacksView(v, digits) {
    return v == null ? '-' : (v >= 0 ? 'B+' : 'W+') + Math.abs(v).toFixed(digits);
  }

  // The side the engine searched for (§0): the winrate is shown from it, as the circle's label is.
  function searchedSide() {
    var reported = state.analysis && state.analysis.currentPlayer;
    if (reported === 'B') return 'black';
    if (reported === 'W') return 'white';
    return state.game ? state.game.toPlay : 'black';
  }

  // One candidate's fields, by the column key that names each (without its `col.` prefix).
  // `named` asks for the readout's forms, where a field with a perspective names its side.
  function fieldsOf(info, searched, named) {
    var winrate = info.winrate == null ? null
      : (searched === 'black' ? info.winrate : 1 - info.winrate);
    var fields = {
      move: info.move,
      win: percent(winrate),
      score: named ? blacksView(info.scoreLead, 1) : withSign(info.scoreLead, 1),
      visits: counted(info.visits),
      policy: percent(info.prior),
      prob: percent(info.prior),
      value: named ? blacksView(info.utility, 2) : withSign(info.utility, 2)
    };
    if (state.analysis && state.analysis.compare) {
      var pa = probabilityOf(state.analysis.policy, info.move);
      var pb = probabilityOf(state.analysis.compare.policy, info.move);
      fields.a = percent(pa);
      fields.b = percent(pb);
      fields.delta = percent(pb - pa, true);
    }
    return fields;
  }

  function renderCandidates() {
    var body = $('candidates');
    body.replaceChildren();
    var keys = headKeys();
    setTableHead(keys);
    var shown = viewOf(state.analysis);
    var infos = (shown && shown.moveInfos) || [];

    var toPlay = state.game ? state.game.toPlay : 'black';
    var searched = searchedSide();
    infos.slice(0, 10).forEach(function (info, index) {
      var row = document.createElement('tr');
      if (index === 0) row.className = 'best';
      var fields = fieldsOf(info, searched, false);
      keys.forEach(function (column) {
        var cell = document.createElement('td');
        cell.textContent = fields[column.slice(4)];
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
    renderReadout();
  }

  /* -- the candidate readout (§3.8 "Candidate readout") ------------------- */
  // One line under the board, always present so the controls below it do not move as it fills.
  function piece(className, text, field) {
    var node = document.createElement('span');
    node.className = className;
    node.textContent = text;
    if (field) node.setAttribute('data-field', field);
    return node;
  }

  function renderReadout() {
    var line = $('candidate-readout');
    var shown = viewOf(state.analysis);
    var infos = (shown && shown.moveInfos) || [];
    var info = null;
    for (var i = 0; pointedAt && i < infos.length; i++) {
      if (infos[i].move === pointedAt) info = infos[i];
    }
    // With no pointer on a circle or a row the line shows the best candidate, marked as such so
    // it is not mistaken for something hovered.
    var best = !info;
    if (!info) info = infos[0] || null;
    line.replaceChildren();
    line.classList.toggle('empty', !info);
    if (!info) {
      // No analysis, or none with candidates.
      line.appendChild(piece('mark', t('readout.none')));
      return;
    }
    if (best) line.appendChild(piece('mark', t('readout.best')));
    var searched = searchedSide();
    var fields = fieldsOf(info, searched, true);
    headKeys().forEach(function (key) {
      var name = key.slice(4);
      var label = t(key);
      // The winrate is the side the engine searched for, as the circle's label is; the score and
      // the Value name Black in their own text (§3.8).
      if (name === 'win') label += ' (' + t(searched === 'black' ? 'black' : 'white') + ')';
      var field = document.createElement('span');
      field.className = 'field';
      field.appendChild(piece('label', label));
      field.appendChild(piece('num', fields[name], name));
      // The bound sits beside the Value it bounds, in the same view.
      if (name === 'value') {
        field.appendChild(piece('lcb', blacksView(info.utilityLcb, 2), 'valueLcb'));
      }
      line.appendChild(field);
    });
    fitLater();
  }

  /* -- what each surface can hold at this window (§3.8 "Candidate table", "Candidate readout") -- */
  // Neither surface is guessed at: the table's box says which of its edges clip, and the readout
  // drops the fields it cannot show whole. Both read the layout, so they run once per animation
  // frame and not once per analysis frame - a fast engine would otherwise lay the page out on
  // every frame it sends, the same cost `scrollLater` above avoids for the same reason.
  var fitDue = false;
  function fitLater() {
    if (fitDue) return;
    fitDue = true;
    requestAnimationFrame(function () {
      fitDue = false;
      markTableEdges();
      trimReadout();
    });
  }

  // The box's surplus width is reached by scrolling the box, and on a platform that draws overlay
  // scrollbars nothing says it is there: no track is reserved, so a column clipped at the box's
  // edge is indistinguishable from a column the table does not have. `data-more` names the edges
  // that have content past them; the fade over them is CSS (§3.8 "Candidate table").
  function markTableEdges() {
    var box = document.querySelector('.candidates-box');
    var slack = box.scrollWidth - box.clientWidth;
    var start = box.scrollLeft > 1;
    var end = slack - box.scrollLeft > 1;
    var edges = slack < 1 ? '' : (start ? (end ? 'both' : 'start') : 'end');
    if (edges) box.dataset.more = edges;
    else delete box.dataset.more;
  }

  // The line loses whole fields off its end rather than cutting one in half: a field the line's
  // edge crosses is hidden, so a number is never shown short of its last digits - a truncated
  // signed decimal still reads as a number and is off by an order of magnitude (§3.8 "Candidate
  // readout"). Every field is shown before anything is measured, so a window that grew gives its
  // fields back; the tail is hidden after every read, so hiding one moves nothing before it.
  function trimReadout() {
    var line = $('candidate-readout');
    var fields = line.querySelectorAll('.field');
    var over = [];
    var i;
    for (i = 0; i < fields.length; i++) fields[i].hidden = false;
    var edge = line.getBoundingClientRect().right;
    for (i = 0; i < fields.length; i++) {
      over.push(fields[i].getBoundingClientRect().right > edge + 0.5);
    }
    for (i = 0; i < fields.length; i++) fields[i].hidden = over[i];
  }

  // A resize changes what both surfaces hold and re-renders neither; scrolling the box changes
  // which of its edges has content past it.
  window.addEventListener('resize', fitLater);
  document.querySelector('.candidates-box').addEventListener('scroll', fitLater);

  function appendLog(line) {
    var log = $('log');
    var entry = document.createElement('div');
    entry.className = line.direction;
    entry.textContent = (line.direction === 'send' ? '▸ ' : '  ') + line.text;
    log.appendChild(entry);
    while (log.childElementCount > 400) log.removeChild(log.firstChild);
    scrollLater('log');
  }

  // Messages clear after 8 s; a sticky one (the connection is down) stays. Once a 4401 / 4403 /
  // 4429 close has said why, that reason holds the line until the page is reloaded (§3.8): a later
  // message neither replaces it nor starts a timer over it.
  var statusTimer = null;
  function setStatus(text, isError, sticky) {
    if (closedFor && !sticky) return;
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
    } else if (catalog) {
      if ($('engine-pick').value) send({ type: 'connect', engineId: $('engine-pick').value });
    } else {
      send({
        type: 'connect',
        protocol: $('protocol').value,
        host: $('host').value.trim(),
        port: parseInt($('port').value, 10)
      });
    }
  };

  $('engine-pick').onchange = function () {
    engineFormDirty = true;
    showHumanControls(formProtocol() === 'handol' || state.engine.protocol === 'handol');
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
  $('genmove').onclick = function () { send({ type: 'genmove', color: state.game.toPlay }); };
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

  // The Visits field as a setting: a whole number of at least 1, else 0 for "no usable number".
  // Read as a whole, never truncated: a typed 1.9 names no setting rather than 1, a number the
  // user did not type (§3.8).
  function typedVisits() {
    var visits = Number($('max-visits').value);
    return isFinite(visits) && visits >= 1 && visits === Math.floor(visits) ? visits : 0;
  }

  // Tuple validation needs the setting the engine will use, not merely the setting this field
  // names. An unusable edit names no change, so the last state remains in force (§3.8).
  function effectiveVisits() {
    return typedVisits() || state.settings.maxVisits || 0;
  }

  // The Every field as a setting: a number above 0, else 0. A usable number out of range is sent
  // and clamped by the settings (§3.4); only an unusable one is left out (§3.8).
  function typedInterval() {
    var interval = Number($('interval').value);
    return isFinite(interval) && interval > 0 ? interval : 0;
  }

  function pushEngineParams() {
    // A field holding no usable number names no setting. Undefined is left out of the JSON, so
    // the frame carries neither key (each field is optional, §4.1): the server keeps the value it
    // has and the next state fills the field again (§3.8).
    send({
      type: 'engine_params',
      maxVisits: typedVisits() || undefined,
      reportInterval: typedInterval() || undefined,
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

  var tupleEditor = humanTuple.mount({
    visits: effectiveVisits,
    onChange: function (pair) {
      send({ type: 'human_params', policy: pair.policy, compare: pair.compare });
    },
    onCompareToggle: function (on) {
      if (on) $('compare-view').value = 'diff';
      redrawView();
    },
    // The account keeps the presets: they are saved by being sent (§4.1, §8.5).
    onPresets: function (list) { sendPresets(list); },
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
    } else if (address.kind === 'catalog') {
      showCatalog(Array.isArray(address.engines) ? address.engines : []);
    }
    showIdentity(info.me || {});
    // Without a console for any engine the section has nothing to offer.
    if (info.console === false) $('console-section').hidden = true;
  }).catch(function () { /* the fields keep the page's defaults; the console stays */ });

  // Server mode: a picker of catalog entries replaces the protocol, host and port fields.
  function showCatalog(engines) {
    catalog = engines;
    ['protocol', 'host', 'port'].forEach(function (id) { $(id).hidden = true; });
    var pick = $('engine-pick');
    while (pick.firstChild) pick.removeChild(pick.firstChild);
    engines.forEach(function (entry) {
      var option = document.createElement('option');
      option.value = entry.id;
      option.textContent = entry.label;
      pick.appendChild(option);
    });
    if (!engines.length) {
      // The attribute keeps the hint right after a language change (i18n.apply).
      pick.setAttribute('data-i18n-title', 'picker.empty');
      pick.title = t('picker.empty');
      $('connect').disabled = true;
    }
    pick.hidden = false;
    if (!engineFormDirty) fillEngineForm();
    if (state.game) renderControls();
  }

  // The signed-in name and the log-out control follow /api/health's `me`.
  function showIdentity(me) {
    var name = $('me-name');
    if (me.name) {
      name.textContent = me.name;
      if (me.source === 'sso') {
        name.setAttribute('data-i18n-title', 'me.sso');
        name.title = t('me.sso');
      } else {
        name.setAttribute('data-i18n-title', 'me.local');
        name.title = t('me.local');
      }
      name.hidden = false;
    }
    // The log-out kind (§5): a form post, the SSO log-out URL, or nothing.
    switch (me.logout) {
      case 'local':
        $('logout-form').hidden = false;
        break;
      case 'sso':
        if (me.logoutUrl) {
          $('logout-link').href = me.logoutUrl;
          $('logout-link').hidden = false;
        }
        break;
      default:
        break;
    }
  }

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
      setStatus(t('sgf.saved', { name: link.download }));
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
    if (event.ctrlKey || event.metaKey || event.altKey) return;  // the browser's own (§3.7)
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
      g: function () { send({ type: 'genmove', color: state.game.toPlay }); },
      a: function () { $('analysis-on').checked = !$('analysis-on').checked;
                       send({ type: 'analysis', enabled: $('analysis-on').checked }); }
    };
    var handler = handlers[event.key];
    if (handler) { event.preventDefault(); handler(); }
  });

  $('lang').value = i18n.lang();
  $('lang').onchange = function () {
    i18n.setLang(this.value);
    // Saved in this browser, or by being sent when the account keeps it (§3.8, §8.5).
    if (preferences) sendLang(i18n.lang());
  };
  // A language the account carries arrives with a state, so the select follows it too.
  i18n.onChange(function () { $('lang').value = i18n.lang(); });
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

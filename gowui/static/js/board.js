/* Canvas rendering of the goban and KataGo's analysis on top of it. */
(function (global) {
  'use strict';

  var GTP_COLUMNS = 'ABCDEFGHJKLMNOPQRSTUVWXYZ';
  // At most this many moveInfos are drawn as candidates (SPEC §3.8); a later entry is nowhere on
  // the board, so the pointer never finds it.
  var DRAWN = 12;
  // The strength the position is drawn at while a preview is up (SPEC §3.8 "The position recedes
  // while a preview is up"). The same faintness the board already uses for the "you could play
  // here" ghost stone: enough to read what is underneath, far enough below 1 that the variation
  // stands apart - and, being below 1, it drops the shadow `_stone` gives only an opaque stone.
  var POSITION_DIM = 0.35;

  function vertexToPoint(vertex, size) {
    if (!vertex || vertex.toLowerCase() === 'pass' || vertex.toLowerCase() === 'resign') return null;
    var column = GTP_COLUMNS.indexOf(vertex[0].toUpperCase());
    var row = parseInt(vertex.slice(1), 10);
    if (column < 0 || isNaN(row)) return null;
    return { x: column, y: size - row };
  }

  function pointToVertex(x, y, size) {
    return GTP_COLUMNS[x] + (size - y);
  }

  function starPoints(size) {
    if (size < 7) return [];
    var edge = size >= 13 ? 3 : 2;
    var middle = (size - 1) / 2;
    var coords = [edge, size - 1 - edge];
    var points = [];
    coords.forEach(function (x) {
      coords.forEach(function (y) { points.push([x, y]); });
    });
    if (size % 2 === 1 && size >= 9) {
      points.push([middle, middle]);
      if (size >= 13) {
        points.push([edge, middle], [size - 1 - edge, middle], [middle, edge], [middle, size - 1 - edge]);
      }
    }
    return points;
  }

  /* Red for a move the search barely looked at, green for the one it likes. */
  function candidateColor(ratio, alpha) {
    var hue = 8 + 122 * Math.pow(Math.max(0, Math.min(1, ratio)), 0.45);
    return 'hsla(' + hue.toFixed(0) + ', 62%, 42%, ' + alpha + ')';
  }

  function abbreviate(n) {
    if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
    if (n >= 10000) return Math.round(n / 1000) + 'k';
    if (n >= 1000) return (n / 1000).toFixed(1) + 'k';
    return String(n);
  }

  function GoBoard(canvas, handlers) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.handlers = handlers || {};
    this.state = null;
    this.analysis = null;
    this.analysisToPlay = 'black';
    this.options = {
      labelMode: 'winrate',
      showOwnership: false,
      showPolicy: false,
      showNumbers: false
    };
    this.hover = null;        // intersection under the pointer
    this.pinnedPv = null;
    this.draws = 0;     // candidate whose PV is being previewed

    var self = this;
    canvas.addEventListener('mousemove', function (event) { self._onMove(event); });
    canvas.addEventListener('mouseleave', function () {
      self.hover = null;
      self.draw();
    });
    canvas.addEventListener('click', function (event) { self._onClick(event); });
    window.addEventListener('resize', function () { self.resize(); });
  }

  GoBoard.prototype.setState = function (state) {
    var sizeChanged = !this.state || this.state.size !== state.size;
    this.state = state;
    if (sizeChanged) this.resize();
    this.draw();
  };

  GoBoard.prototype.setAnalysis = function (analysis, toPlay) {
    this.analysis = analysis;
    this.analysisToPlay = toPlay || 'black';
    this.draw();
  };

  GoBoard.prototype.clearAnalysis = function () {
    this.analysis = null;
    this.draw();
  };

  GoBoard.prototype.setOptions = function (options) {
    Object.assign(this.options, options);
    this.draw();
  };

  GoBoard.prototype.setPreview = function (vertex) {
    this.pinnedPv = vertex;
    this.draw();
  };

  /* -- geometry ---------------------------------------------------------- */
  GoBoard.prototype.resize = function () {
    var wrap = this.canvas.parentElement;
    var css = Math.max(240, Math.floor(wrap.clientWidth));
    var ratio = global.devicePixelRatio || 1;
    this.canvas.width = Math.floor(css * ratio);
    this.canvas.height = Math.floor(css * ratio);
    // Pin both axes: pointAt derives one scale factor from the width, so a
    // non-square element would map clicks to the wrong row.
    this.canvas.style.width = css + 'px';
    this.canvas.style.height = css + 'px';
    this.pixelRatio = ratio;
    this.draw();
  };

  GoBoard.prototype.metrics = function () {
    var size = (this.state && this.state.size) || 19;
    var extent = this.canvas.width;
    var margin = extent / (size + 1.6);
    var cell = (extent - 2 * margin) / (size - 1);
    return { size: size, margin: margin, cell: cell, extent: extent };
  };

  GoBoard.prototype.pointAt = function (clientX, clientY) {
    var rect = this.canvas.getBoundingClientRect();
    var m = this.metrics();
    var scale = this.canvas.width / rect.width;
    var x = Math.round(((clientX - rect.left) * scale - m.margin) / m.cell);
    var y = Math.round(((clientY - rect.top) * scale - m.margin) / m.cell);
    if (x < 0 || y < 0 || x >= m.size || y >= m.size) return null;
    return { x: x, y: y };
  };

  GoBoard.prototype._onMove = function (event) {
    var point = this.pointAt(event.clientX, event.clientY);
    var changed = (point && this.hover) ? (point.x !== this.hover.x || point.y !== this.hover.y)
                                        : (point !== this.hover);
    this.hover = point;
    if (changed) this.draw();
  };

  GoBoard.prototype._onClick = function (event) {
    var point = this.pointAt(event.clientX, event.clientY);
    if (point && this.handlers.onClick && this.state) {
      this.handlers.onClick(pointToVertex(point.x, point.y, this.state.size), point);
    }
  };

  /* -- drawing ----------------------------------------------------------- */
  GoBoard.prototype.draw = function () {
    if (!this.state) return;
    var ctx = this.ctx;
    var m = this.metrics();
    ctx.clearRect(0, 0, m.extent, m.extent);
    this.analysisHoverHit = this._hoverIsCandidate(m);
    var preview = this._previewCandidate();
    // SPEC §3.8 "The position recedes while a preview is up": the position and the things that
    // belong to its stones recede; the wood, the grid, the coordinates, the heatmap and the
    // ownership squares do not.
    var dim = preview ? POSITION_DIM : 1;
    this._drawWood(m);
    this._drawGrid(m);
    var ownership = this.options.showOwnership ? this._drawOwnership(m) : false;
    var heatmap = this.options.showPolicy ? this._drawPolicy(m) : 'off';
    var positionDim = this._drawStones(m, dim);
    // SPEC §3.8 "Board overlays": the ring goes over the stones and under the numbers, so the
    // two no longer take turns — the ring keeps to the stone's edge, the number to its centre.
    var ring = this._drawLastMove(m, dim);
    var numbers = this.options.showNumbers ? this._drawMoveNumbers(m, dim) : false;
    var candidates = 0;
    var previewStones = 0;
    if (preview) previewStones = this._drawPrincipalVariation(m, preview);
    else candidates = this._drawCandidates(m);
    // What this draw put on the canvas, for the browser tests (SPEC §3.8 "Test
    // observability"); the page's own logic never reads it.
    this.draws += 1;
    var record = this.canvas.dataset;
    record.draws = String(this.draws);
    record.labelMode = this.options.labelMode;
    record.candidates = String(candidates);
    record.preview = preview ? preview.move : '';
    record.previewStones = String(previewStones);
    record.heatmap = heatmap;
    record.ownership = ownership ? 'on' : 'off';
    record.numbers = numbers ? 'on' : 'off';
    // Taken from the draw, not from this.state.lastMove: the record has to name the ring that
    // reached the canvas, or it would report one the board never drew.
    record.lastMoveRing = ring;
    // The alpha the position's stones were painted with, read back off the canvas rather than
    // repeated from the argument: a record that only echoed what this draw asked for could not
    // see the paint, and would go on reporting the dim after a change stopped applying it
    // (SPEC §3.8 "Test observability").
    record.positionDim = String(positionDim);
    // The readout line reads the pointer from here: the board is what knows whether the pointer
    // is on a candidate's circle, and `setPreview` is what carries a table row's move in.
    if (this.handlers.onPointer) this.handlers.onPointer(this.hoveredCandidate());
  };

  GoBoard.prototype._drawWood = function (m) {
    var ctx = this.ctx;
    var gradient = ctx.createLinearGradient(0, 0, m.extent, m.extent);
    gradient.addColorStop(0, '#e6bd77');
    gradient.addColorStop(1, '#d4a25c');
    ctx.fillStyle = gradient;
    ctx.fillRect(0, 0, m.extent, m.extent);
  };

  GoBoard.prototype._drawGrid = function (m) {
    var ctx = this.ctx;
    var last = m.margin + (m.size - 1) * m.cell;
    ctx.strokeStyle = 'rgba(40, 26, 10, 0.75)';
    ctx.lineWidth = Math.max(1, m.cell * 0.025);
    ctx.beginPath();
    for (var i = 0; i < m.size; i++) {
      var at = m.margin + i * m.cell;
      ctx.moveTo(m.margin, at); ctx.lineTo(last, at);
      ctx.moveTo(at, m.margin); ctx.lineTo(at, last);
    }
    ctx.stroke();

    ctx.fillStyle = 'rgba(40, 26, 10, 0.85)';
    starPoints(m.size).forEach(function (p) {
      ctx.beginPath();
      ctx.arc(m.margin + p[0] * m.cell, m.margin + p[1] * m.cell, m.cell * 0.09, 0, Math.PI * 2);
      ctx.fill();
    });

    ctx.fillStyle = 'rgba(60, 40, 16, 0.8)';
    ctx.font = (m.cell * 0.38).toFixed(0) + 'px system-ui, sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    for (var j = 0; j < m.size; j++) {
      var pos = m.margin + j * m.cell;
      ctx.fillText(GTP_COLUMNS[j], pos, m.margin * 0.5);
      ctx.fillText(GTP_COLUMNS[j], pos, m.extent - m.margin * 0.5);
      ctx.fillText(String(m.size - j), m.margin * 0.5, pos);
      ctx.fillText(String(m.size - j), m.extent - m.margin * 0.5, pos);
    }
  };

  // Draws the position at `dim` and returns the alpha the stones were painted with, as the canvas
  // reports it. A board with no stones on it paints nothing to observe, and answers `dim`.
  GoBoard.prototype._drawStones = function (m, dim) {
    var stones = this.state.stones;
    var painted = null;
    for (var y = 0; y < m.size; y++) {
      for (var x = 0; x < m.size; x++) {
        var value = stones[y * m.size + x];
        if (value) painted = this._stone(m, x, y, value === 1 ? 'black' : 'white', dim);
      }
    }
    // Ghost stone under the pointer on an empty intersection. It recedes with the position: it
    // says "you could play here" about a position that is not the subject while a preview is up.
    if (this.hover && !this.analysisHoverHit && !stones[this.hover.y * m.size + this.hover.x]) {
      this._stone(m, this.hover.x, this.hover.y, this.state.toPlay, 0.35 * dim);
    }
    // The ghost is not the position and is drawn fainter still, so it is not what is reported.
    return painted == null ? dim : painted;
  };

  // Paints one stone and returns the alpha the canvas painted it with, read back off the context
  // rather than handed back: an answer that repeated the argument would say a draw dimmed the
  // position whether or not it did (SPEC §3.8 "Test observability").
  GoBoard.prototype._stone = function (m, x, y, color, alpha) {
    var ctx = this.ctx;
    var cx = m.margin + x * m.cell;
    var cy = m.margin + y * m.cell;
    var radius = m.cell * 0.475;
    ctx.save();
    ctx.globalAlpha = alpha;
    if (alpha === 1) {
      ctx.beginPath();
      ctx.arc(cx + radius * 0.12, cy + radius * 0.14, radius, 0, Math.PI * 2);
      ctx.fillStyle = 'rgba(0, 0, 0, 0.22)';
      ctx.fill();
    }
    var gradient = ctx.createRadialGradient(
      cx - radius * 0.35, cy - radius * 0.4, radius * 0.1, cx, cy, radius);
    if (color === 'black') {
      gradient.addColorStop(0, '#5a5f68');
      gradient.addColorStop(1, '#0d0f12');
    } else {
      gradient.addColorStop(0, '#ffffff');
      gradient.addColorStop(1, '#cfcfc6');
    }
    ctx.beginPath();
    ctx.arc(cx, cy, radius, 0, Math.PI * 2);
    ctx.fillStyle = gradient;
    ctx.fill();
    var painted = ctx.globalAlpha;   // what the fill above went on at, before the state is popped
    ctx.restore();
    return painted;
  };

  // Rings the stone just played and returns the vertex it ringed, or '' when it ringed nothing
  // (no move yet, or the last move was a pass and left no stone to ring).
  GoBoard.prototype._drawLastMove = function (m, dim) {
    var vertex = this.state.lastMove;
    var point = vertexToPoint(vertex, m.size);
    if (!point) return '';
    var stone = this.state.stones[point.y * m.size + point.x];
    if (!stone) return '';
    var ctx = this.ctx;
    // A fixed share of the stone, floored and capped so it neither vanishes on a 240px board nor
    // bloats on a 900px one. The share needs no scaling - m.cell is already in device pixels - but
    // the floor and the cap are sizes a person sees, so they are CSS pixels: left in device pixels
    // they would halve the ring's weight on a display that packs two device pixels to one CSS.
    // The ring's outer edge sits on the stone's, so it stays clear of the number's centre and
    // reads against both stone colours in the same red as the old dot.
    var ratio = this.pixelRatio || 1;
    var width = Math.min(3.5 * ratio, Math.max(1.25 * ratio, m.cell * 0.085));
    ctx.save();
    ctx.globalAlpha = dim;
    ctx.lineWidth = width;
    ctx.strokeStyle = stone === 1 ? '#ff6b5e' : '#d63b2c';
    ctx.beginPath();
    ctx.arc(m.margin + point.x * m.cell, m.margin + point.y * m.cell,
      Math.max(width, m.cell * 0.475 - width / 2), 0, Math.PI * 2);
    ctx.stroke();
    ctx.restore();
    return vertex;
  };

  GoBoard.prototype._drawMoveNumbers = function (m, dim) {
    var numbers = this.state.moveNumbers || {};
    var ctx = this.ctx;
    ctx.save();
    ctx.globalAlpha = dim;
    ctx.font = 'bold ' + (m.cell * 0.36).toFixed(0) + 'px system-ui, sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    var self = this;
    var drawn = 0;
    Object.keys(numbers).forEach(function (vertex) {
      var point = vertexToPoint(vertex, m.size);
      if (!point) return;
      drawn += 1;
      var stone = self.state.stones[point.y * m.size + point.x];
      ctx.fillStyle = stone === 1 ? '#f0f0ea' : '#16181d';
      ctx.fillText(String(numbers[vertex]),
        m.margin + point.x * m.cell, m.margin + point.y * m.cell);
    });
    ctx.restore();
    return drawn > 0;
  };

  GoBoard.prototype._drawOwnership = function (m) {
    var ownership = this.analysis && this.analysis.ownership;
    if (!ownership || ownership.length < m.size * m.size) return false;
    var ctx = this.ctx;
    var side = m.cell * 0.82;
    var drawn = 0;
    for (var y = 0; y < m.size; y++) {
      for (var x = 0; x < m.size; x++) {
        var value = ownership[y * m.size + x];
        if (Math.abs(value) < 0.06) continue;
        drawn += 1;
        ctx.fillStyle = value > 0
          ? 'rgba(10, 12, 16, ' + (Math.abs(value) * 0.55).toFixed(3) + ')'
          : 'rgba(248, 248, 244, ' + (Math.abs(value) * 0.55).toFixed(3) + ')';
        ctx.fillRect(m.margin + x * m.cell - side / 2, m.margin + y * m.cell - side / 2, side, side);
      }
    }
    return drawn > 0;
  };

  /* The raw policy: how likely the network is to play each point before search. */
  GoBoard.prototype._drawPolicy = function (m) {
    var policy = this.analysis && this.analysis.policy;
    if (!policy || policy.length < m.size * m.size) return 'off';
    if (this.analysis.diffView) return this._drawPolicyDiff(m, policy) ? 'diff' : 'off';
    var best = 0;
    for (var i = 0; i < m.size * m.size; i++) best = Math.max(best, policy[i]);
    if (best <= 0) return 'off';
    var drawn = 0;
    var ctx = this.ctx;
    var side = m.cell * 0.9;
    for (var y = 0; y < m.size; y++) {
      for (var x = 0; x < m.size; x++) {
        var p = policy[y * m.size + x];
        if (p <= 0.0005) continue;
        drawn += 1;
        var ratio = Math.pow(p / best, 0.45);
        ctx.fillStyle = 'hsla(260, 85%, 42%, ' + (0.08 + ratio * 0.62).toFixed(3) + ')';
        ctx.fillRect(m.margin + x * m.cell - side / 2, m.margin + y * m.cell - side / 2, side, side);
      }
    }
    return drawn > 0 ? 'policy' : 'off';
  };

  /* B − A: warm where the second tuple plays a point more, cool where less. */
  GoBoard.prototype._drawPolicyDiff = function (m, diff) {
    var worst = 0;
    for (var i = 0; i < m.size * m.size; i++) worst = Math.max(worst, Math.abs(diff[i]));
    if (worst <= 0) return false;
    var ctx = this.ctx;
    var side = m.cell * 0.9;
    var drawn = 0;
    for (var y = 0; y < m.size; y++) {
      for (var x = 0; x < m.size; x++) {
        var d = diff[y * m.size + x];
        if (Math.abs(d) <= 0.0005) continue;
        drawn += 1;
        var ratio = Math.pow(Math.abs(d) / worst, 0.5);
        ctx.fillStyle = (d > 0 ? 'hsla(8, 85%, 48%, ' : 'hsla(212, 85%, 48%, ') +
          (0.1 + ratio * 0.6).toFixed(3) + ')';
        ctx.fillRect(m.margin + x * m.cell - side / 2, m.margin + y * m.cell - side / 2, side, side);
      }
    }
    return drawn > 0;
  };

  GoBoard.prototype._candidateLabel = function (info) {
    if (this.analysis && this.analysis.diffView && this.options.labelMode === 'prior') {
      var delta = (info.prior || 0) * 100;
      return (delta > 0 ? '+' : '') + delta.toFixed(1);
    }
    switch (this.options.labelMode) {
      // A count the engine did not report is '-', as it is in the table and the readout line:
      // `abbreviate` is written for a number and would put the word `null` on the board.
      case 'visits': return info.visits == null ? '-' : abbreviate(info.visits);
      case 'prior': return info.prior == null ? '-' : (info.prior * 100).toFixed(1);
      case 'score': return info.scoreLead == null ? '-' : info.scoreLead.toFixed(1);
      default:
        if (info.winrate == null) return '-';
        // Always shown for the player to move, the way every Go GUI does it.
        var rate = this.analysisToPlay === 'black' ? info.winrate : 1 - info.winrate;
        return (rate * 100).toFixed(1);
    }
  };

  GoBoard.prototype._drawCandidates = function (m) {
    var infos = (this.analysis && this.analysis.moveInfos) || [];
    if (!infos.length) return 0;
    // A human-policy distribution has no visits; shade by probability instead.
    // Some candidates of a compared view carry a count and some carry none (§3.8 "Candidate
    // table"): a missing one weighs nothing rather than turning the shade into a NaN.
    var byVisits = infos.some(function (info) { return info.visits > 0; });
    var weight = function (info) {
      return byVisits ? (info.visits || 0) : Math.abs(info.prior || 0);
    };
    var diffView = !!this.analysis.diffView;
    var best = infos.reduce(function (acc, info) { return Math.max(acc, weight(info)); }, byVisits ? 1 : 1e-9);
    var ctx = this.ctx;
    var self = this;
    var drawn = 0;

    infos.slice(0, DRAWN).forEach(function (info, index) {
      var point = vertexToPoint(info.move, m.size);
      if (!point) return;   // a pass candidate has nowhere to draw
      drawn += 1;
      var cx = m.margin + point.x * m.cell;
      var cy = m.margin + point.y * m.cell;
      var radius = m.cell * 0.46;
      var ratio = weight(info) / best;

      ctx.beginPath();
      ctx.arc(cx, cy, radius, 0, Math.PI * 2);
      ctx.fillStyle = diffView
        ? ((info.prior || 0) > 0 ? 'hsla(8, 70%, 42%, 0.85)' : 'hsla(212, 70%, 42%, 0.85)')
        : candidateColor(ratio, index === 0 ? 0.92 : 0.8);
      ctx.fill();
      if (index === 0) {
        ctx.lineWidth = Math.max(1.5, m.cell * 0.06);
        ctx.strokeStyle = 'rgba(255, 255, 255, 0.9)';
        ctx.stroke();
      }

      ctx.fillStyle = '#ffffff';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.font = 'bold ' + (m.cell * 0.32).toFixed(0) + 'px system-ui, sans-serif';
      ctx.fillText(self._candidateLabel(info), cx, cy - m.cell * 0.11);
      ctx.font = (m.cell * 0.26).toFixed(0) + 'px system-ui, sans-serif';
      ctx.fillStyle = 'rgba(255, 255, 255, 0.85)';
      // The second line is the visits, so a candidate without them is left with the first alone.
      if (byVisits && info.visits != null) {
        ctx.fillText(abbreviate(info.visits), cx, cy + m.cell * 0.19);
      }
    });
    return drawn;
  };

  /* Is the pointer resting on a move the engine is suggesting? */
  GoBoard.prototype._hoverIsCandidate = function (m) {
    if (!this.hover) return false;
    var infos = (this.analysis && this.analysis.moveInfos) || [];
    var vertex = pointToVertex(this.hover.x, this.hover.y, m.size);
    return infos.slice(0, DRAWN).some(function (info) { return info.move === vertex; });
  };

  /* The candidate under the pointer, whether the pointer is on its circle or on its table row
     (§3.8 "Candidate readout"). Unlike a preview it asks for no `pv`: the readout describes the
     candidate, and a candidate without a variation still has every other field. */
  GoBoard.prototype.hoveredCandidate = function () {
    var infos = (this.analysis && this.analysis.moveInfos) || [];
    if (!infos.length) return null;
    var wanted = this.pinnedPv;
    var searched = infos;
    if (!wanted && this.hover && this.state) {
      wanted = pointToVertex(this.hover.x, this.hover.y, this.state.size);
      searched = infos.slice(0, DRAWN);
    }
    if (!wanted) return null;
    for (var i = 0; i < searched.length; i++) {
      if (searched[i].move === wanted) return searched[i];
    }
    return null;
  };

  GoBoard.prototype._previewCandidate = function () {
    var infos = (this.analysis && this.analysis.moveInfos) || [];
    if (!infos.length) return null;
    var wanted = this.pinnedPv;
    // A table row names its own move; the pointer on the board only finds a drawn candidate,
    // so a point whose entry was not drawn previews nothing (§3.8 "PV preview").
    var searched = infos;
    if (!wanted && this.hover && this.state) {
      wanted = pointToVertex(this.hover.x, this.hover.y, this.state.size);
      searched = infos.slice(0, DRAWN);
    }
    if (!wanted) return null;
    for (var i = 0; i < searched.length; i++) {
      if (searched[i].move === wanted && searched[i].pv && searched[i].pv.length) return searched[i];
    }
    return null;
  };

  /* Walk the principal variation out as numbered ghost stones. */
  GoBoard.prototype._drawPrincipalVariation = function (m, info) {
    var ctx = this.ctx;
    var color = this.analysisToPlay === 'black' ? 'black' : 'white';
    var occupied = {};
    var self = this;
    // Numbered on from the position shown, like the move numbers on the board,
    // so the two never repeat each other.
    var first = (this.state.cursor || 0) + 1;
    var drawn = 0;
    info.pv.slice(0, 20).forEach(function (vertex, index) {
      var point = vertexToPoint(vertex, m.size);
      var current = color;
      color = color === 'black' ? 'white' : 'black';
      if (!point) return;   // a pass in the variation
      var key = point.x + ',' + point.y;
      if (occupied[key]) return;
      occupied[key] = true;
      drawn += 1;
      // Full strength: the variation is the subject, and an opaque stone carries the shadow the
      // dimmed position loses (§3.8 "The position recedes while a preview is up").
      self._stone(m, point.x, point.y, current, 1);
      ctx.fillStyle = current === 'black' ? '#f0f0ea' : '#16181d';
      var number = String(first + index);
      var scale = number.length > 2 ? 0.3 : 0.38;
      ctx.font = 'bold ' + (m.cell * scale).toFixed(0) + 'px system-ui, sans-serif';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText(number, m.margin + point.x * m.cell, m.margin + point.y * m.cell);
    });
    // No caption: what the board would have said about this candidate is in the readout line
    // (§3.8 "PV preview", "Candidate readout"), which is the only place a candidate's full set is
    // written. With it goes the one text board.js took from the i18n tables.
    return drawn;
  };

  global.GoBoard = GoBoard;
  global.goboardUtils = {
    vertexToPoint: vertexToPoint,
    pointToVertex: pointToVertex,
    candidateColor: candidateColor,
    abbreviate: abbreviate,
    starPoints: starPoints
  };
})(window);

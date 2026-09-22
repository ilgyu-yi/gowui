/* The handol-mux policy tuple editor.

   A tuple can be shaped three ways that stay in step: four knobs named for
   what they do (value pull, locality, variety, tail cut), the raw per-axis
   fields, and the JSON text. Presets -- built in or saved in this browser --
   fill all three. With "compare" on there are two tuples, A and B, sent in
   the same query; the tabs pick which one the controls edit.

   Values are checked against the ranges the mux applies (selection.py
   HumanScoreParams / HumanPolicy) before anything is sent. */
(function (global) {
  'use strict';

  // An empty field means "leave the key out", which is the default shown.
  var FIELDS = [
    { key: 'lambda_utility', step: 0.05, def: null },
    { key: 'trust_mu', step: 0.01, def: null },
    { key: 'fill_kappa', step: 0.5, def: null },
    { key: 'min_p', step: 0.01, def: 0 },
    { key: 'distance_slope', step: 0.05, def: 0 },
    { key: 'distance_floor', step: 0.05, def: 0.1 },
    { key: 'distance_peak', step: 0.1, def: 1.5 },
    { key: 'temperature', step: 0.1, def: 1 }
  ];

  var PRESETS = [
    { id: 'identity', tuple: {} },
    { id: '483B', tuple: { lambda_utility: 0.15, trust_mu: 0.05, fill_kappa: 1, min_p: 0.05 } },
    { id: 'lambdaLight', tuple: { lambda_utility: 0.5, trust_mu: 0.05, fill_kappa: 1 } },
    { id: 'lambdaStrong', tuple: { lambda_utility: 0.05, trust_mu: 0.05, fill_kappa: 1 } },
    { id: 'minP', tuple: { min_p: 0.05 } },
    { id: 'local', tuple: { distance_slope: 0.25 } },
    { id: 'localStrong', tuple: { distance_slope: 1 } },
    { id: 'sharp', tuple: { temperature: 0.5 } },
    { id: 'flat', tuple: { temperature: 1.5 } }
  ];

  // Profiles the engine is known to accept; the field also takes any other name.
  var PROFILES = [];
  ['20k', '15k', '10k', '5k', '3k', '1k', '1d', '3d', '5d', '7d', '9d'].forEach(function (rank) {
    PROFILES.push('preaz_' + rank);
  });
  ['20k', '10k', '5k', '1k', '1d', '5d', '9d'].forEach(function (rank) {
    PROFILES.push('rank_' + rank);
  });
  [1800, 1900, 1950, 1980, 2000, 2010, 2020, 2023].forEach(function (year) {
    PROFILES.push('proyear_' + year);
  });

  function round3(v) { return Number(Number(v).toPrecision(3)); }
  function clamp(v, lo, hi) { return Math.min(hi, Math.max(lo, v)); }
  function has(tuple, key) { return tuple[key] !== undefined && tuple[key] !== null; }

  /* Knobs: a slider position <-> the raw keys it owns. Each scale puts the
     useful range in the middle of the travel. λ is a temperature on the value
     term (score × exp((û − û_best)/λ)), so pulling harder means a *smaller* λ:
     up the knob, λ falls from 2 to 0.01 on a log scale. Locality and tail cut
     are quadratic so the small values that matter (slope 0.25, min p 0.05)
     get room; temperature is 4^x, 0.25 to 4 around 1. Position 0 means "off":
     the keys are removed, not set to a neutral value. */
  var LAMBDA_WEAK = 2, LAMBDA_STRONG = 0.01;
  var LAMBDA_SPAN = Math.log(LAMBDA_STRONG / LAMBDA_WEAK);
  var SLOPE_MAX = 3, MIN_P_MAX = 0.5;
  var KNOBS = [
    {
      id: 'strength', min: 0, max: 100,
      apply: function (tuple, v) {
        if (v <= 0) {
          delete tuple.lambda_utility; delete tuple.trust_mu; delete tuple.fill_kappa;
          return;
        }
        tuple.lambda_utility = round3(LAMBDA_WEAK * Math.exp(LAMBDA_SPAN * v / 100));
        if (!has(tuple, 'trust_mu')) tuple.trust_mu = 0.05;
        if (!has(tuple, 'fill_kappa')) tuple.fill_kappa = 1;
      },
      read: function (tuple) {
        if (!has(tuple, 'lambda_utility') || !(tuple.lambda_utility > 0)) return 0;
        return clamp(Math.round(100 * Math.log(tuple.lambda_utility / LAMBDA_WEAK) / LAMBDA_SPAN), 1, 100);
      },
      show: function (tuple) { return has(tuple, 'lambda_utility') ? 'λ ' + tuple.lambda_utility : null; }
    },
    {
      id: 'locality', min: 0, max: 100,
      apply: function (tuple, v) {
        if (v <= 0) delete tuple.distance_slope;
        else tuple.distance_slope = round3(SLOPE_MAX * Math.pow(v / 100, 2));
      },
      read: function (tuple) {
        return has(tuple, 'distance_slope') && tuple.distance_slope > 0
          ? clamp(Math.round(100 * Math.sqrt(tuple.distance_slope / SLOPE_MAX)), 1, 100) : 0;
      },
      show: function (tuple) { return has(tuple, 'distance_slope') ? 'slope ' + tuple.distance_slope : null; }
    },
    {
      id: 'variety', min: -100, max: 100,
      apply: function (tuple, v) {
        if (v === 0) delete tuple.temperature;
        else tuple.temperature = round3(Math.pow(4, v / 100));
      },
      read: function (tuple) {
        return has(tuple, 'temperature') && tuple.temperature > 0
          ? clamp(Math.round(100 * Math.log(tuple.temperature) / Math.log(4)), -100, 100) : 0;
      },
      show: function (tuple) { return has(tuple, 'temperature') ? 'T ' + tuple.temperature : null; }
    },
    {
      id: 'tail', min: 0, max: 100,
      apply: function (tuple, v) {
        if (v <= 0) delete tuple.min_p;
        else tuple.min_p = round3(MIN_P_MAX * Math.pow(v / 100, 2));
      },
      read: function (tuple) {
        return has(tuple, 'min_p') && tuple.min_p > 0
          ? clamp(Math.round(100 * Math.sqrt(tuple.min_p / MIN_P_MAX)), 1, 100) : 0;
      },
      show: function (tuple) { return has(tuple, 'min_p') ? 'min p ' + tuple.min_p : null; }
    }
  ];

  function label(key) { return global.i18n.t('field.' + key); }

  /** First problem with ``tuple`` as the mux would judge it, or null. */
  function problem(tuple, visits) {
    var t = global.i18n.t;
    if (tuple === null || typeof tuple !== 'object' || Array.isArray(tuple)) return t('tuple.json.invalid');
    var known = FIELDS.map(function (f) { return f.key; });
    for (var name in tuple) {
      if (known.indexOf(name) < 0) return t('err.unknown', { field: name });
    }
    for (var i = 0; i < FIELDS.length; i++) {
      var key = FIELDS[i].key;
      var v = tuple[key];
      if (v === undefined || (v === null && key === 'lambda_utility')) continue;
      if (typeof v !== 'number' || !isFinite(v)) return t('err.number', { field: label(key) });
    }
    var h = function (k) { return has(tuple, k); };
    var gt = function (k, v) { return h(k) && !(tuple[k] > v) ? t('err.gt', { field: label(k), v: v }) : null; };
    var ge = function (k, v) { return h(k) && !(tuple[k] >= v) ? t('err.ge', { field: label(k), v: v }) : null; };
    var found =
      gt('lambda_utility', 0) || gt('trust_mu', 0) || ge('fill_kappa', 0) ||
      (h('min_p') && !(tuple.min_p >= 0 && tuple.min_p <= 1)
        ? t('err.range', { field: label('min_p'), a: 0, b: 1 }) : null) ||
      ge('distance_slope', 0) || gt('distance_floor', 0) || gt('distance_peak', 0) ||
      gt('temperature', 0.0001);
    if (found) return found;
    if (h('lambda_utility') && !(h('trust_mu') && h('fill_kappa'))) return t('err.lambdaSubs');
    if (!h('lambda_utility') && (h('trust_mu') || h('fill_kappa'))) return t('err.subsWithoutLambda');
    var floor = h('distance_floor') ? tuple.distance_floor : 0.1;
    var peak = h('distance_peak') ? tuple.distance_peak : 1.5;
    if (!(floor < peak)) return t('err.floorPeak');
    if (h('lambda_utility') && !(visits > 1)) return t('tuple.lambdaNeedsSearch');
    return null;
  }

  function copy(tuple) { return JSON.parse(JSON.stringify(tuple || {})); }

  function sameTuple(a, b) {
    var ka = Object.keys(a).sort(), kb = Object.keys(b).sort();
    if (ka.join() !== kb.join()) return false;
    return ka.every(function (k) { return a[k] === b[k]; });
  }

  /* -- presets saved in this browser ----------------------------------- */
  var STORE = 'gowui.userPresets';

  /* The caps of §7.6 and the name rules of §4.1, held here as well as on the server: a
     `preferences` message is refused whole, so one name the server would refuse would block
     every later save of the list too (§3.8 "Preferences"). */
  var MAX_PRESETS = 64;
  var MAX_PRESET_NAME = 40;
  // Whitespace other than a plain space, and a C0 or C1 control: a newline would let a name fake
  // a line of the confirmation dialogs below.
  var BAD_NAME_CHAR = /[ --]|[^\S ]/;

  // Code points, as the server counts them: a name of 40 emoji is 40 characters, not 80 units.
  function nameLength(name) { return Array.from(name).length; }

  // A surrogate that is no half of a pair -- text no non-ASCII serialiser can write (§4.1).
  function loneSurrogate(name) {
    for (var i = 0; i < name.length; i++) {
      var unit = name.charCodeAt(i);
      if (unit >= 0xD800 && unit <= 0xDBFF) {
        var next = name.charCodeAt(i + 1);
        if (!(next >= 0xDC00 && next <= 0xDFFF)) return true;
        i += 1;
      } else if (unit >= 0xDC00 && unit <= 0xDFFF) return true;
    }
    return false;
  }

  /** Why the account would refuse ``name`` as a preset name (§4.1), as text, or null. */
  function nameProblem(name) {
    var t = global.i18n.t;
    if (nameLength(name) > MAX_PRESET_NAME) {
      return t('preset.nameLong', { max: MAX_PRESET_NAME });
    }
    if (BAD_NAME_CHAR.test(name) || loneSurrogate(name)) return t('preset.nameChars');
    return null;
  }

  // A stored entry is read with the rules a sent one must pass (§8.5): what the panel offers is
  // always something the mux would take. Checked as if searching, as Import is, so a λ preset is
  // not dropped over Visits.
  function usablePreset(p) {
    if (!p || typeof p.name !== 'string' || !p.name.trim()) return false;
    return nameProblem(p.name.trim()) === null && problem(p.tuple, 2) === null;
  }

  function loadUserPresets() {
    try {
      var raw = JSON.parse(global.localStorage.getItem(STORE) || '[]');
      return Array.isArray(raw) ? raw.filter(usablePreset) : [];
    } catch (err) {
      return [];
    }
  }

  function storeUserPresets(list) {
    try { global.localStorage.setItem(STORE, JSON.stringify(list)); } catch (err) { /* ignore */ }
  }

  function clearUserPresets() {
    try { global.localStorage.removeItem(STORE); } catch (err) { /* ignore */ }
  }

  /**
   * Build the editor. ``opts.onChange({policy, compare})`` fires with a valid
   * pair (compare is null when comparison is off); ``opts.visits()`` gives the
   * Visits setting in force, which decides whether λ is usable.
   *
   * ``opts.onPresets(list)`` fires instead of a write to browser storage once
   * ``usePresets`` has said the account keeps the presets (§8.5); the caller
   * sends them (only app.js sends frames, §3.8).
   *
   * The menu offers the built-in presets alone until ``usePresets`` or
   * ``useBrowserPresets`` says whose own presets go under them, which the first
   * ``state`` decides: nothing of this browser's is shown to a page whose
   * account keeps them, not even for the moment before that frame (§3.8).
   */
  function mount(opts) {
    var $ = function (id) { return document.getElementById(id); };
    var t = global.i18n.t;
    var tuples = { A: {}, B: {} };
    var comparing = false;
    var active = 'A';
    var userPresets = [];
    // Set by usePresets(): the account keeps the presets, so this browser stores none (§8.5).
    var accountPresets = false;
    // Set by either: whose presets these are is settled, so the menu is no longer built-ins only.
    var presetsSettled = false;
    var timer = null;
    var fieldInputs = {};
    var knobParts = {};

    ['preset-save', 'preset-delete', 'preset-export', 'preset-import'].forEach(function (id) {
      $(id).disabled = true;
    });

    /* -- build controls ------------------------------------------------ */
    FIELDS.forEach(function (field) {
      var wrap = document.createElement('label');
      wrap.className = 'tuple-field';
      var name = document.createElement('span');
      var input = document.createElement('input');
      input.type = 'number';
      input.step = String(field.step);
      wrap.appendChild(name);
      wrap.appendChild(input);
      $('tuple-fields').appendChild(wrap);
      fieldInputs[field.key] = { input: input, name: name, wrap: wrap };
      input.addEventListener('input', function () { fromFields(); schedule(); });
      input.addEventListener('change', commitNow);
    });

    KNOBS.forEach(function (knob) {
      var row = document.createElement('div');
      row.className = 'knob';
      var head = document.createElement('div');
      head.className = 'knob-head';
      var name = document.createElement('span');
      name.className = 'knob-name';
      var value = document.createElement('span');
      value.className = 'knob-value';
      head.appendChild(name);
      head.appendChild(value);
      var slider = document.createElement('input');
      slider.type = 'range';
      slider.min = String(knob.min);
      slider.max = String(knob.max);
      slider.step = '1';
      var ends = document.createElement('div');
      ends.className = 'knob-ends';
      var lo = document.createElement('span');
      var hi = document.createElement('span');
      ends.appendChild(lo);
      ends.appendChild(hi);
      row.appendChild(head);
      row.appendChild(slider);
      row.appendChild(ends);
      $('tuple-knobs').appendChild(row);
      knobParts[knob.id] = { row: row, name: name, value: value, slider: slider, lo: lo, hi: hi };
      slider.addEventListener('input', function () {
        var tuple = tuples[active];
        knob.apply(tuple, Number(slider.value));
        render({ keepKnob: knob.id });
        if ($('live-apply').checked) schedule();
      });
      slider.addEventListener('change', commitNow);
      slider.addEventListener('dblclick', function () {
        // Double-click puts a knob back to its neutral position.
        knob.apply(tuples[active], 0);
        render();
        commitNow();
      });
    });

    /* -- hover card: what a field or knob is, shown at once ------------- */
    var card = document.createElement('div');
    card.className = 'hover-card';
    card.hidden = true;
    document.body.appendChild(card);

    function cardLine(text, className) {
      var line = document.createElement('div');
      if (className) line.className = className;
      line.textContent = text;
      card.appendChild(line);
    }

    function showCard(anchor, fill) {
      card.replaceChildren();
      fill();
      card.hidden = false;
      // Beside the side panel, level with the control; kept on screen.
      var box = anchor.getBoundingClientRect();
      var width = card.offsetWidth, height = card.offsetHeight;
      var left = box.left - width - 12;
      if (left < 8) left = Math.min(window.innerWidth - width - 8, box.right + 12);
      var top = Math.max(8, Math.min(window.innerHeight - height - 8, box.top - 4));
      card.style.left = left + 'px';
      card.style.top = top + 'px';
    }

    function hideCard() { card.hidden = true; }

    FIELDS.forEach(function (field) {
      var wrap = fieldInputs[field.key].wrap;
      wrap.addEventListener('mouseenter', function () {
        showCard(wrap, function () {
          cardLine(label(field.key), 'hover-title');
          cardLine(t('field.key') + ': ' + field.key, 'hover-key');
          cardLine(t('field.' + field.key + '.help'));
          cardLine(t('field.range') + ': ' + t('field.' + field.key + '.range'), 'hover-meta');
          cardLine(t('field.example') + ': ' + t('field.' + field.key + '.example'), 'hover-meta');
        });
      });
      wrap.addEventListener('mouseleave', hideCard);
    });

    var KNOB_KEYS = {
      strength: ['lambda_utility', 'trust_mu', 'fill_kappa'],
      locality: ['distance_slope'],
      variety: ['temperature'],
      tail: ['min_p']
    };
    KNOBS.forEach(function (knob) {
      var row = knobParts[knob.id].row;
      row.addEventListener('mouseenter', function () {
        showCard(row, function () {
          cardLine(t('knob.' + knob.id), 'hover-title');
          cardLine(t('knob.' + knob.id + '.help'));
          var tuple = tuples[active];
          var owned = KNOB_KEYS[knob.id].map(function (key) {
            return key + ' = ' + (has(tuple, key) ? tuple[key] : t('field.off'));
          });
          cardLine(owned.join(' · '), 'hover-key');
        });
      });
      row.addEventListener('mouseleave', hideCard);
    });

    /* -- labels (and again on a language switch) ----------------------- */
    function relabel() {
      FIELDS.forEach(function (field) {
        var parts = fieldInputs[field.key];
        parts.name.textContent = label(field.key);
        // Just the default value: a longer hint is cut off in the narrow field.
        parts.input.placeholder = field.def === null ? t('field.off') : String(field.def);
      });
      KNOBS.forEach(function (knob) {
        var parts = knobParts[knob.id];
        parts.name.textContent = t('knob.' + knob.id);
        parts.lo.textContent = t('knob.' + knob.id + '.lo');
        parts.hi.textContent = t('knob.' + knob.id + '.hi');
      });
      fillPresetMenu();
      render();
    }

    // Where a saved list goes: to the account through the caller, or to browser storage (§8.5).
    // While whose presets these are is unsettled — before the first `state` — the menu has read
    // no stored list, so nothing is written over one.
    function saveUserPresets(list) {
      if (!accountPresets) {
        if (presetsSettled) storeUserPresets(list);
        return;
      }
      if (opts.onPresets) opts.onPresets(list.map(function (p) {
        return { name: p.name, tuple: copy(p.tuple) };
      }));
    }

    function fillPresetMenu() {
      var menu = $('human-preset');
      menu.replaceChildren();
      var custom = document.createElement('option');
      custom.value = '';
      custom.textContent = t('human.preset.custom');
      menu.appendChild(custom);
      var builtIn = document.createElement('optgroup');
      builtIn.label = t('preset.group.builtin');
      PRESETS.forEach(function (p) {
        var option = document.createElement('option');
        option.value = 'builtin:' + p.id;
        option.textContent = t('preset.' + p.id);
        builtIn.appendChild(option);
      });
      menu.appendChild(builtIn);
      if (userPresets.length) {
        var mine = document.createElement('optgroup');
        mine.label = t('preset.group.mine');
        userPresets.forEach(function (p) {
          var option = document.createElement('option');
          option.value = 'user:' + p.name;
          option.textContent = p.name + '  ' + JSON.stringify(p.tuple);
          mine.appendChild(option);
        });
        menu.appendChild(mine);
      }
      matchPreset();
    }

    function matchPreset() {
      var tuple = tuples[active];
      var mine = userPresets.filter(function (p) { return sameTuple(p.tuple, tuple); })[0];
      var builtIn = PRESETS.filter(function (p) { return sameTuple(p.tuple, tuple); })[0];
      $('human-preset').value = mine ? 'user:' + mine.name : builtIn ? 'builtin:' + builtIn.id : '';
      $('preset-delete').disabled = !presetsSettled || !mine;
    }

    /* -- state -> controls --------------------------------------------- */
    function render(options) {
      options = options || {};
      var tuple = tuples[active];
      FIELDS.forEach(function (field) {
        var input = fieldInputs[field.key].input;
        var v = tuple[field.key];
        if (document.activeElement !== input) input.value = v === undefined || v === null ? '' : v;
      });
      KNOBS.forEach(function (knob) {
        var parts = knobParts[knob.id];
        if (options.keepKnob !== knob.id) parts.slider.value = String(knob.read(tuple));
        parts.value.textContent = knob.show(tuple) || t('field.off');
      });
      if (document.activeElement !== $('human-policy')) $('human-policy').value = JSON.stringify(tuple);
      $('compare-on').checked = comparing;
      $('tuple-tabs').hidden = !comparing;
      $('compare-view-wrap').hidden = !comparing;
      document.querySelectorAll('#tuple-tabs .tab').forEach(function (tab) {
        tab.classList.toggle('active', tab.dataset.slot === active);
      });
      matchPreset();
      check();
    }

    function check() {
      var visits = opts.visits();
      var found = problem(tuples.A, visits);
      if (found && comparing) found = 'A: ' + found;
      if (!found && comparing) {
        var other = problem(tuples.B, visits);
        if (other) found = 'B: ' + other;
      }
      $('tuple-problem').hidden = !found;
      $('tuple-problem').textContent = found || '';
      $('human-policy').classList.toggle('invalid', !!problem(tuples[active], visits));
      return found;
    }

    function fromFields() {
      var next = {};
      FIELDS.forEach(function (field) {
        var raw = fieldInputs[field.key].input.value.trim();
        if (raw !== '') next[field.key] = Number(raw);
      });
      tuples[active] = next;
      render();
    }

    /* -- controls -> server -------------------------------------------- */
    function schedule() {
      if (timer) clearTimeout(timer);
      timer = setTimeout(commitNow, 300);
    }

    function commitNow() {
      if (timer) { clearTimeout(timer); timer = null; }
      if (check()) return;
      opts.onChange({ policy: copy(tuples.A), compare: comparing ? copy(tuples.B) : null });
    }

    $('human-policy').addEventListener('input', function () {
      var parsed;
      try {
        parsed = JSON.parse(this.value || '{}');
      } catch (err) {
        $('tuple-problem').hidden = false;
        $('tuple-problem').textContent = t('tuple.json.invalid');
        this.classList.add('invalid');
        return;
      }
      tuples[active] = parsed;
      render();
    });
    $('human-policy').addEventListener('change', commitNow);

    $('human-preset').addEventListener('change', function () {
      var value = this.value;
      var tuple = null;
      if (value.indexOf('builtin:') === 0) {
        var id = value.slice(8);
        tuple = PRESETS.filter(function (p) { return p.id === id; })[0].tuple;
      } else if (value.indexOf('user:') === 0) {
        var name = value.slice(5);
        tuple = userPresets.filter(function (p) { return p.name === name; })[0].tuple;
      }
      if (!tuple) return;
      tuples[active] = copy(tuple);
      render();
      commitNow();
    });

    $('compare-on').addEventListener('change', function () {
      comparing = this.checked;
      // B starts as a copy of A, so a comparison begins from "no difference".
      if (comparing) { tuples.B = copy(tuples.A); active = 'B'; }
      else active = 'A';
      render();
      commitNow();
      if (opts.onCompareToggle) opts.onCompareToggle(comparing);
    });

    document.querySelectorAll('#tuple-tabs .tab').forEach(function (tab) {
      tab.addEventListener('click', function () {
        active = tab.dataset.slot;
        render();
      });
    });

    /* -- saving, deleting, exporting, importing ------------------------ */
    $('preset-save').addEventListener('click', function () {
      if (check()) return;
      var current = $('human-preset').value;
      var suggestion = current.indexOf('user:') === 0 ? current.slice(5) : '';
      var name = global.prompt(t('preset.namePrompt'), suggestion);
      if (name === null) return;
      name = name.trim();
      if (!name) return;
      // Refused here, before anything is sent, so one bad name never wedges the next save (§3.8).
      var refusal = nameProblem(name);
      var known = userPresets.filter(function (p) { return p.name === name; })[0];
      if (!refusal && !known && userPresets.length >= MAX_PRESETS) {
        refusal = t('preset.full', { max: MAX_PRESETS });
      }
      if (refusal) {
        if (opts.notify) opts.notify(refusal, true);
        return;
      }
      var tuple = copy(tuples[active]);
      var existing = known;
      if (existing) {
        if (!global.confirm(t('preset.overwrite', { name: name }))) return;
        existing.tuple = tuple;
      } else {
        userPresets.push({ name: name, tuple: tuple });
      }
      saveUserPresets(userPresets);
      fillPresetMenu();
      if (opts.notify) opts.notify(t('preset.saved', { name: name }));
    });

    $('preset-delete').addEventListener('click', function () {
      var value = $('human-preset').value;
      if (value.indexOf('user:') !== 0) return;
      var name = value.slice(5);
      if (!global.confirm(t('preset.confirmDelete', { name: name }))) return;
      userPresets = userPresets.filter(function (p) { return p.name !== name; });
      saveUserPresets(userPresets);
      fillPresetMenu();
    });

    $('preset-export').addEventListener('click', function () {
      var blob = new Blob([JSON.stringify({ gowuiPresets: 1, presets: userPresets }, null, 2)],
                          { type: 'application/json' });
      var link = document.createElement('a');
      link.href = URL.createObjectURL(blob);
      link.download = 'gowui-presets.json';
      link.click();
      setTimeout(function () { URL.revokeObjectURL(link.href); }, 1000);
    });

    $('preset-import').addEventListener('click', function () { $('preset-file').click(); });
    $('preset-file').addEventListener('change', function () {
      var file = this.files && this.files[0];
      this.value = '';
      if (!file) return;
      file.text().then(function (text) {
        var data;
        try { data = JSON.parse(text); } catch (err) { data = null; }
        var list = data && Array.isArray(data.presets) ? data.presets : Array.isArray(data) ? data : null;
        if (!list) {
          if (opts.notify) opts.notify(t('preset.importBad'), true);
          return;
        }
        var added = 0, skipped = 0;
        list.forEach(function (p) {
          // An entry the account would refuse, and one past the 64 presets of §7.6, are skipped
          // rather than sent: the whole list would otherwise be refused with them (§3.8).
          if (!usablePreset(p)) {
            skipped += 1;
            return;
          }
          var name = p.name.trim();
          var without = userPresets.filter(function (q) { return q.name !== name; });
          if (without.length >= MAX_PRESETS) {
            skipped += 1;
            return;
          }
          userPresets = without;
          userPresets.push({ name: name, tuple: copy(p.tuple) });
          added += 1;
        });
        saveUserPresets(userPresets);
        fillPresetMenu();
        if (opts.notify) opts.notify(t('preset.imported', { added: added, skipped: skipped }), skipped > 0);
      });
    });

    relabel();
    global.i18n.onChange(relabel);

    return {
      /** Show what the server holds, unless the user is mid-edit. */
      set: function (policy, compare) {
        if (timer) return;
        var focus = document.activeElement;
        if (focus && (focus.closest('#tuple-knobs') || focus.closest('#tuple-fields') ||
                      focus.id === 'human-policy')) return;
        tuples.A = copy(policy);
        comparing = compare !== null && compare !== undefined;
        if (comparing) tuples.B = copy(compare);
        if (!comparing) active = 'A';
        render();
      },
      /**
       * The account keeps the presets (§4.2 `preferences`): show these instead of this
       * browser's, and store none here from now on (§8.5).
       */
      usePresets: function (list) {
        accountPresets = true;
        presetsSettled = true;
        $('preset-save').disabled = false;
        $('preset-export').disabled = false;
        $('preset-import').disabled = false;
        // Not merely "stores none": the key goes, so a list saved here before lingers for nobody
        // (§8.5).
        clearUserPresets();
        userPresets = (Array.isArray(list) ? list : []).filter(usablePreset).map(function (p) {
          return { name: p.name.trim(), tuple: copy(p.tuple) };
        });
        fillPresetMenu();
      },
      /**
       * This browser keeps the presets (`state.preferences` is null, §8.5): read the stored
       * ones now. Read here and not at mount, so a page whose account keeps them never shows
       * this browser's, not even for the moment before the first `state` (§3.8).
       */
      useBrowserPresets: function () {
        if (presetsSettled) return;
        presetsSettled = true;
        $('preset-save').disabled = false;
        $('preset-export').disabled = false;
        $('preset-import').disabled = false;
        userPresets = loadUserPresets();
        fillPresetMenu();
      },
      /** Re-check after something the verdict depends on (Visits) changed. */
      recheck: check,
      comparing: function () { return comparing; }
    };
  }

  global.humanTuple = {
    FIELDS: FIELDS, KNOBS: KNOBS, PRESETS: PRESETS, PROFILES: PROFILES,
    problem: problem, mount: mount
  };
})(window);

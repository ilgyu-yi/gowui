/* What a humanSL profile imitates, shown from the "?" beside the profile field.

   The explanations come from KataGo itself: cpp/neuralnet/sgfmetadata.cpp
   (SGFMetadata::getProfile) turns a profile into the game metadata the human
   SL model is conditioned on, and cpp/configs/gtp_human5k_example.cfg
   describes the options. They say only what those two files say. */
(function (global) {
  'use strict';

  var RANK = /^(rank|preaz)_(\d+)([kd])(?:_(\d+)([kd]))?$/;
  var YEAR = /^proyear_(\d+)$/;

  function t(key, vars) { return global.i18n.t(key, vars); }

  function rankLabel(n, kd) { return t(kd === 'k' ? 'profile.kyu' : 'profile.dan', { n: n }); }

  /** The short label a list entry shows ("5급", "2020"). */
  function label(name) {
    var m = RANK.exec(name);
    if (m) {
      var own = rankLabel(m[2], m[3]);
      return m[4] ? t('profile.pair', { b: own, w: rankLabel(m[4], m[5]) }) : own;
    }
    m = YEAR.exec(name);
    return m ? m[1] : name;
  }

  /** What a profile imitates: a title and a few lines, or null if unknown. */
  function describe(name) {
    var m = RANK.exec(name);
    if (m) {
      var modern = m[1] === 'rank';
      var lines = [
        t(modern ? 'profile.desc.rank' : 'profile.desc.preaz'),
        t('profile.desc.kgs')
      ];
      if (m[4]) lines.push(t('profile.desc.pair'));
      var dans = [[m[2], m[3]], [m[4], m[5]]]
        .filter(function (r) { return r[1] === 'd'; })
        .map(function (r) { return +r[0]; });
      if (Math.max.apply(null, [0].concat(dans)) >= 5) lines.push(t('profile.desc.strength'));
      return {
        title: t(modern ? 'profile.title.rank' : 'profile.title.preaz', { rank: label(name) }),
        lines: lines
      };
    }
    m = YEAR.exec(name);
    if (m) {
      var year = +m[1];
      return {
        title: t('profile.title.proyear', { year: year }),
        lines: [
          t(year <= 2020 ? 'profile.desc.gogod' : 'profile.desc.go4go', { year: year }),
          t('profile.desc.strength')
        ]
      };
    }
    return null;
  }

  /**
   * The "?" beside the profile field: hovering or focusing it explains the
   * profile currently typed there.
   */
  function mountHelp(help, input) {
    var pop = document.createElement('div');
    pop.className = 'help-pop';
    pop.hidden = true;
    help.parentNode.appendChild(pop);

    function fill() {
      var name = input.value.trim();
      var info = describe(name);
      pop.replaceChildren();
      var add = function (text, className) {
        var line = document.createElement('div');
        if (className) line.className = className;
        line.textContent = text;
        pop.appendChild(line);
      };
      if (info) {
        add(info.title + '  ·  ' + name, 'help-title');
        info.lines.forEach(function (text) { add(text); });
      } else if (name) {
        add(t('profile.desc.unknown', { name: name }));
      }
      // What the three families are, whatever is chosen.
      add(t('profile.families'), 'help-title help-gap');
      ['rank', 'preaz', 'proyear'].forEach(function (id) {
        add(t('profile.group.' + id) + ' — ' + t('profile.groupdesc.' + id));
      });
      add(t('profile.pairHint'));
      add(t('profile.desc.source'), 'help-source');
    }

    function show() { fill(); pop.hidden = false; }
    function hide() { pop.hidden = true; }
    help.addEventListener('mouseenter', show);
    help.addEventListener('mouseleave', hide);
    help.addEventListener('focus', show);
    help.addEventListener('blur', hide);
  }

  global.profileHelp = { mount: mountHelp, describe: describe, label: label };
})(window);

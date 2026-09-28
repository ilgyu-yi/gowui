/* UI strings in Korean and English. Static text is tagged in the HTML with
   data-i18n (text) / data-i18n-title (tooltip); scripts call i18n.t(key, vars).
   Messages the server sends (engine errors) are shown as they arrive. */
(function (global) {
  'use strict';

  var STRINGS = {
    en: {
      'protocol.title': 'KataGo interface',
      'protocol.handol': 'handol-mux (human)',
      'host.title': 'Engine host',
      'port.title': 'Engine port',
      'connect': 'Connect',
      'disconnect': 'Disconnect',
      'disconnected': 'disconnected',
      'thinking': 'thinking',
      'engine': 'engine',
      'nav.first': 'Go to the start',
      'nav.prev10': 'Back 10 moves',
      'nav.prev': 'Back one move',
      'nav.next': 'Forward one move',
      'nav.next10': 'Forward 10 moves',
      'nav.last': 'Go to the end',
      'pass': 'Pass',
      'undo': 'Undo',
      'resign': 'Resign',
      'winbar.title': "Black's winrate",
      'toPlay': 'To play:',
      'black': 'black',
      'white': 'white',
      'score.none': 'score --',
      'visits.count': '{n} visits',
      'noEngine': 'no engine',
      'human.section': 'Human policy',
      'human.profile': 'Profile',
      'human.profile.title': 'humanSL profile: preaz_20k…preaz_9d, rank_20k…rank_9d, proyear_1800…proyear_2023',
      'human.preset': 'Preset',
      'human.preset.custom': '(custom)',
      'human.json': 'JSON',
      'human.json.title': 'The policy tuple as JSON; kept in step with the fields above',
      'human.hint': 'Visits 1 = policy only (no search). Labels and the heatmap show the selection probability.',
      'human.evalVisits': 'Winrate visits',
      'human.evalVisits.title': 'Visits for a plain KataGo query run alongside, for winrate and score (the human response has neither). 0 = off.',
      'human.live': 'Apply while dragging',
      'human.advanced': 'Raw values',
      'compare.on': 'Compare two tuples',
      'compare.view': 'Show',
      'compare.view.A': 'A',
      'compare.view.B': 'B',
      'compare.view.diff': 'Difference B − A',
      'col.a': 'A',
      'col.b': 'B',
      'col.delta': 'Δ',
      'col.prob': 'Prob.',
      'knob.strength': 'Value pull',
      'knob.strength.help': 'How hard the search pulls the human distribution toward the moves it rates best. Up = smaller λ = stronger pull (λ 2 → 0.01, log scale). Needs Visits ≥ 2. Double-click to reset.',
      'knob.strength.lo': 'off (human only)',
      'knob.strength.hi': 'follow the search',
      'knob.locality': 'Locality',
      'knob.locality.help': 'Favour moves near the last move (distance slope 0–3; the ACG value 0.25 sits near the start). Double-click to reset.',
      'knob.locality.lo': 'anywhere',
      'knob.locality.hi': 'answer locally',
      'knob.variety': 'Variety',
      'knob.variety.help': 'Temperature 0.25–4: left plays the likeliest moves, right spreads out. Double-click to reset.',
      'knob.variety.lo': 'predictable',
      'knob.variety.hi': 'varied',
      'knob.tail': 'Tail cut',
      'knob.tail.help': 'Drop moves the human model itself finds unlikely: below min p × the probability of the human model\'s top move (0–0.5). Decided on the raw human probabilities, before search value (λ), distance or temperature. Double-click to reset.',
      'knob.tail.lo': 'keep all',
      'knob.tail.hi': 'only common moves',
      'preset.group.builtin': 'Built in',
      'preset.group.mine': 'Mine',
      'preset.save': 'Save as…',
      'preset.delete': 'Delete',
      'preset.export': 'Export',
      'preset.import': 'Import',
      'preset.namePrompt': 'Name for this preset',
      'preset.overwrite': 'Replace the preset "{name}"?',
      'preset.confirmDelete': 'Delete the preset "{name}"?',
      'preset.saved': 'Saved preset "{name}"',
      'preset.imported': 'Imported {added} preset(s), skipped {skipped}',
      'preset.importBad': 'That file holds no presets',
      'preset.nameLong': 'A preset name is at most {max} characters',
      'preset.nameChars': 'A preset name holds no line break and no control character',
      'preset.full': 'No more than {max} presets are kept; delete one first',
      'analysis.section': 'Analysis',
      'analysis.continuous': 'Continuous analysis',
      'analysis.visits': 'Visits',
      'analysis.every': 'Every',
      'analysis.seconds': 's',
      'analysis.label': 'Label',
      'analysis.ownership': 'Ownership',
      'analysis.heatmap': 'Raw policy heatmap',
      'analysis.numbers': 'Move numbers',
      'label.winrate': 'Winrate',
      'label.visits': 'Visits',
      'label.prior': 'Policy %',
      'label.score': 'Score lead',
      'col.move': 'Move',
      'col.win': 'Win',
      'col.score': 'Score',
      'col.visits': 'Visits',
      'col.policy': 'Policy',
      'col.value': 'Value',
      'analysis.candidates': 'Candidate moves',
      'help.label.winrate': 'The winning chance of the side the engine searched for, as a percentage.',
      'help.label.visits': 'How many visits the search spent on the move, abbreviated.',
      'help.label.prior': "The move's raw policy probability, as a percentage. While comparing on Difference B − A, the signed difference in percentage points.",
      'help.label.score': "The move's score lead in Black's view — positive while Black leads — to one decimal.",
      'help.ownership': "Squares on the board: dark where Black owns the point, light where White does, stronger the larger the value. Ownership is the engine's own value for each point; ticking this asks the engine to report it.",
      'help.heatmap': "A purple square on each point the raw policy gives weight to, stronger the closer it is to the largest. The raw policy is the engine's own probability for each move — what the candidate table shows under Policy, under Prob. for a handol-mux analysis, and while comparing under A or B for the tuple Show draws; gowui does not compute it. While comparing on Difference B − A the squares are warm red where B plays a point more and cool blue where it plays it less.",
      'help.numbers': 'Every stone shows the number of the move that played it. The ring on the last move is there whether or not this is ticked.',
      'help.compare.tuple': 'A tuple is the set of eight human-policy settings under Raw values; the distribution the engine answers with depends on it. Ticking this sends a second tuple, B, with the first, A.',
      'help.compare.views': 'The A / B tabs choose which tuple the controls edit; Show chooses which one is drawn.',
      'help.compare.diff': 'Under Difference B − A each candidate is a filled disc — red where B plays the move more than A, blue where less — and the largest difference is outlined in white, unless that move is a pass, which has no point on the board.',
      'help.col.move': 'The candidate move.',
      'help.col.a': "The move's probability under tuple A.",
      'help.col.b': "The move's probability under tuple B.",
      'help.col.delta': 'B − A, signed.',
      'readout.best': 'best',
      'readout.none': 'no candidate',
      // §3.8 "Candidate readout": the button that opens the help panel while no candidate shows.
      'help.start': 'How to start',
      // The help panel (SPEC §3.8 "Help panel"). Every legend line below carries the source it
      // comes from on the line above it, so a reviewer checks a claim against a named source
      // instead of deriving it again. None of them says more than its source supports.
      'help.open': 'Help',
      'help.open.title': 'What this page does, what its numbers mean, and every key it binds',
      'help.title': 'Using gowui',
      'help.close': 'Close',
      'help.close.title': 'Close the help panel',
      'help.flow.title': 'How to use this page',
      // §3.8 "Engine form": the protocol select, host, port, the Connect button and the badge.
      'help.flow.connect': 'Pick the engine interface, type the address it listens on and press Connect. The badge beside it names the engine that answered, or says none is connected.',
      // §3.8 "Engine picker": with engineAddress.kind catalog a select of entries replaces the
      // three fields, and Connect sends the picked entry.
      'help.flow.connect.catalog': 'Pick an engine in the list beside Connect and press Connect. The badge beside it names the engine that answered, or says none is connected.',
      // §3.8 "Engine picker": an empty catalog disables Connect and shows a note beside the picker.
      'help.flow.connect.empty': 'This server has no engine configured, so there is nothing to connect: Connect is disabled, and the note beside it says so.',
      // §3.8 "Layout" (the board pane) and "Controls" (Navigation: a click on a point sends play).
      'help.flow.play': 'Click a point on the board to play there. Under the board are first, −10, −1, the position counter, +1, +10 and last, then Pass, Undo and Resign.',
      // §3.8 "Controls" (Analysis section) and "Board overlays" (where the candidates are drawn).
      'help.flow.analyse': 'Tick Continuous analysis and the engine reads the position on screen: its candidates are drawn on the board and listed in the table of the Analysis section.',
      // §3.8 "Board strip": one tile per board, "+ New board", a click selects, ✎ renames.
      'help.flow.boards': 'Every board in the strip on the left is a game of its own. "+ New board" adds one, clicking a tile switches to it, and ✎ renames it.',
      // §3.8 "Controls" (Players, New game and Engine console sections; index.html leaves the last
      // two closed); §3.5 (the console sends raw commands to a GTP engine, where allowed).
      'help.flow.more': 'Further down the side panel: Players, with KataGo plays Black or White, Engine move now and Final score; New game, closed until opened, which starts a game and holds Save SGF and Load SGF; and Engine console, also closed, with the lines exchanged with the engine and a field that sends a GTP engine a command of your own where that is allowed.',
      // §3.8 "Help panel": this panel points at the hover cards and the profile "?" for what an
      // individual control is (§3.8 "Human policy panel", "Scrolling") rather than restating them.
      'help.flow.controls': 'What one control is, you read where it is: hover a field or a knob for its card, and the "?" beside the profile field for what that profile imitates. This panel does not repeat them.',
      // §3.8 "Top candidates" (board.js _drawCandidates): the ring, its two arcs from six
      // o'clock, the track; no cobalt arc without visits; the smaller visits line under the label.
      'help.glyph.title': 'What is drawn on a candidate',
      'help.glyph.ring': "Each candidate is a ring over a faint track. From the bottom, the vermilion left half rises with the move's raw policy probability, and the blue right half with the move's share of the search.",
      'help.glyph.visits': 'A move the search did not reach has no blue arc. The smaller blue number is its visits; the number above it is the one the Label select chooses, and the ? beside Label says which.',
      // §3.8 "Top candidates": the white outer ring, and a pass has no point (board.js:16, 453);
      // the other candidates keep their rings, so only the white one is absent.
      'help.glyph.best': 'The first candidate, the one the table marks as best, has a white outer ring — unless it is a pass, which has no point on the board, and then no candidate has the white ring.',
      // §3.8 "Compare views": the filled disc is the Compare card's (help.compare.diff), not repeated.
      'help.glyph.diff': 'While comparing on Difference B − A a candidate is drawn another way: the ? beside Compare two tuples says how.',
      // §3.8 "Help panel" part 3: one line per switch with a ? card, which each line points at.
      'help.task.title': 'To see a thing, what to turn on',
      'help.task.label': 'To choose the number written in each candidate:',
      'help.task.ownership': 'To see which side owns each point:',
      // §3.8 "Raw policy heatmap": a square only where the entry is above 0.0005 (board.js:387).
      'help.task.heatmap': 'To see the raw policy as a square on each point above 0.05%, not only on the candidates:',
      'help.task.numbers': 'To read the order the stones were played in:',
      'help.task.compare': 'To see how two human-policy tuples differ, with handol-mux:',
      'help.task.see': '— the ? beside it says what it shows.',
      'help.numbers.title': 'What the numbers mean',
      // §0 "Perspective" makes the winrate the one exception to Black's view; §3.8 "Candidate
      // table" shows Win from the side searched for and the score lead as Black's. Said in one
      // breath because the asymmetry is inside a single row.
      'help.num.winScore': "The two on one line are read from different sides. Win is the winning chance of the side the engine searched for; Score is the point lead and stays Black's. The winrate is the one place this page leaves Black's view.",
      // §3.8 "Candidate table".
      'help.num.visits': 'How much of the search went to that move.',
      // §2.2 `prior`; §3.8 "Label modes" and "Candidate table" (Prob. for a handol-mux analysis).
      'help.num.policy': 'The raw policy probability of the move. A handol-mux analysis shows the same column as Prob.',
      // §2.2 (`utility`: signed, centred on zero, not a probability, Black's view); §3.8
      // "Candidate table" (two decimals); §3.8 "Candidate readout" (recentred per position).
      'help.num.value': "The engine's utility: one signed number centred on zero, not a probability, in Black's view like the score, to two decimals. Compare it between the candidates of one position and not between positions — its score part is recentred on that position's own expected score.",
      // §3.8 "What the numbers mean": the legend points at the table's ? for these three.
      'help.num.inTable': 'These, and every other column of the candidate table, are explained by the ? beside Candidate moves.',
      // §2.2 `utilityLcb`; §3.8 "Candidate readout" (the parentheses are style.css
      // `.readout .lcb::before` / `::after`).
      'help.num.lcb': "The number in parentheses beside the Value under the board is utilityLcb, that Value's lower confidence bound, in the same view.",
      // §3.8 "Human policy panel" and the strings field.lambda_utility.help and
      // field.trust_mu.help, where û is the value μ and κ have shrunk toward the fill value;
      // field.fill_kappa.help defines that fill value and does not name û, so its card is not
      // cited. Named as the reader sees them: the Value pull knob sets λ, μ and κ
      // (tuple.js:66-75), and field.lambda_utility and field.trust_mu are the fields' labels.
      'help.num.collision': "One word, two quantities. The Value of the candidate table is the engine's raw utility. The û behind the Value pull knob — in the cards of Utility temperature λ and Trust μ under Raw values — is a different number: a move's search value after Trust and Pessimism have shrunk it toward the fill value.",
      // §3.8 "What the numbers mean": no formula and no conversion factor between the two is
      // derivable from anything this repo holds, so the panel carries neither.
      'help.num.noConversion': 'Different quantities in different units, and gowui defines no conversion between them. There is no formula here that turns a winrate into a Value, or a Value into a winrate.',
      // The list below is rendered from the table of §3.7; these name its four surfaces and the
      // five groups within them.
      'help.keys.title': 'Keys',
      'help.scope.nav': 'The page, outside a text field',
      'help.scope.tile': 'A board tile with the focus',
      'help.scope.drag': 'While a board tile is being dragged',
      'help.scope.rename': 'In the board name field',
      'help.key.prev': 'One move back',
      'help.key.next': 'One move forward',
      'help.key.first': 'The first position',
      'help.key.last': 'The last position',
      'help.key.pass': 'Pass',
      'help.key.undo': 'Undo the last move',
      'help.key.genmove': 'Let the engine play the side to move',
      'help.key.analysis': 'Turn continuous analysis on or off',
      'help.key.prevBoard': 'The previous board',
      'help.key.nextBoard': 'The next board',
      'help.key.tileMove': 'Move the tile one place earlier or later',
      'help.key.tileSelect': 'Switch to that board',
      'help.key.cancelDrag': 'Cancel the drag',
      'help.key.renameSave': 'Save the name',
      'help.key.renameCancel': 'Abandon the edit',
      'players.section': 'Players',
      'players.black': 'KataGo plays Black',
      'players.white': 'KataGo plays White',
      'players.genmove': 'Engine move now',
      'players.finalScore': 'Final score',
      'captures.black': 'Black captured',
      'captures.white': 'White captured',
      'newGame.section': 'New game',
      'newGame.size': 'Size',
      'newGame.handicap': 'Handicap',
      'newGame.komi': 'Komi',
      'newGame.rules': 'Rules',
      'newGame.start': 'Start',
      'newGame.save': 'Save SGF',
      'newGame.load': 'Load SGF',
      'rules.japanese': 'Japanese',
      'rules.korean': 'Korean',
      'rules.chinese': 'Chinese',
      'rules.newZealand': 'New Zealand',
      'moves.section': 'Moves',
      'console.section': 'Engine console',
      'console.send': 'Send',
      'profile.kyu': '{n} kyu',
      'profile.dan': '{n} dan',
      'profile.pair': 'Black {b} vs White {w}',
      'profile.families': 'Profile families',
      'profile.group.rank': 'rank_20k…rank_9d',
      'profile.group.preaz': 'preaz_20k…preaz_9d',
      'profile.group.proyear': 'proyear_1800…proyear_2023',
      'profile.groupdesc.rank': 'amateurs of that rank, openings after AlphaZero changed them',
      'profile.groupdesc.preaz': 'amateurs of that rank, openings from before AlphaZero',
      'profile.groupdesc.proyear': 'pros and strong insei, from game records of that year and the years around it',
      'profile.pairHint': 'Black and White can be given different ranks: rank_5k_1d, preaz_10k_3k.',
      'profile.title.rank': 'Amateur {rank}, modern openings',
      'profile.title.preaz': 'Amateur {rank}, pre-AlphaZero openings',
      'profile.title.proyear': 'Professionals, {year}',
      'profile.desc.rank': 'Conditioned on a game dated March 2020, after human openings changed because of AlphaZero.',
      'profile.desc.preaz': 'Conditioned on a game dated September 2016, before the AlphaZero opening style caught on.',
      'profile.desc.kgs': 'Treated as a KGS game (its ranks are the reference) at 20 min + 5 × 30 s byo-yomi.',
      'profile.desc.pair': 'Each side knows the other is stronger or weaker. More than 9 ranks apart, or at odds with the handicap, is outside the training data and may misbehave.',
      'profile.desc.gogod': 'GoGoD, the historical pro game collection, conditioned on June {year}.',
      'profile.desc.go4go': 'Go4Go recent pro game records, conditioned on June {year}.',
      'profile.desc.strength': 'The raw model does no search, so it is not expected to reach the strength of high dans or pros; Value pull (λ) and visits add that.',
      'profile.desc.unknown': '"{name}" is not a known profile name; the engine decides whether it accepts it.',
      'profile.desc.source': 'Source: KataGo sgfmetadata.cpp, gtp_human5k_example.cfg',
      'sgf.namePrompt': 'File name for the SGF',
      'sgf.saved': 'Saved {name}',
      'sgf.failed': 'Could not save the SGF: {error}',
      'style.black': 'Black moves',
      'style.white': 'White moves',
      'style.human': 'human distribution',
      'style.katago': 'KataGo\'s best',
      'style.title': 'How the engine picks a move: sample the human distribution (with the tuple), or play KataGo\'s own first choice from a plain search of Visits.',
      'boards.new': '+ New board',
      'boards.new.title': 'Add an empty board at this board\'s size and rules and switch to it',
      'boards.duplicate': 'Copy this board (position and human settings) and switch to the copy',
      'boards.move': 'move {n}/{total}',
      'boards.identity': 'identity',
      'boards.delete': 'Delete this board',
      'boards.reset': 'Empty this board',
      'boards.confirmDelete': 'Delete board "{name}"?',
      'boards.confirmReset': 'Empty board "{name}"? It is the only board, so it is emptied, not removed.',
      'boards.renamePrompt': 'Board name',
      'boards.renameHint': 'Double-click or ✎ to rename · [ and ] switch boards · drag a tile, or Alt with an arrow, to reorder',
      'boards.rename': 'Rename',
      'boards.default': 'Board {n}',
      'status.lost': 'Lost the connection to gowui; reconnecting...',
      'status.notSignedIn': 'Not signed in: reload the page after signing in.',
      'status.refused': 'The connection was refused by the Host and Origin rules.',
      'status.tooManySockets': 'Too many gowui tabs are open on this account. Close one, then reload this page.',
      'picker.title': 'Engine',
      'picker.empty': 'No engine is configured on this server.',
      'me.local': 'Signed in with a password',
      'me.sso': 'Signed in through single sign-on',
      'logout': 'Log out',
      'sgf.tooLarge': 'The SGF file is larger than 1 MiB.',
      'sgf.loadFailed': 'Could not load the SGF (HTTP {status}).',
      'tuple.json.invalid': 'The tuple must be a JSON object, e.g. {"min_p": 0.05}',
      'tuple.fix': 'Fix the tuple before it is sent: {problem}',
      'tuple.lambdaNeedsSearch': 'lambda_utility needs a search: set Visits above 1',
      // tuple fields
      'field.lambda_utility': 'Utility temperature λ',
      'field.lambda_utility.help': 'Each move is multiplied by exp((û − û_best) / λ), where û is its search value (shrunk toward the fill value by μ, κ). Smaller λ pulls harder toward the best-valued moves; larger λ fades the effect. Empty = off.',
      'field.trust_mu': 'Trust μ',
      'field.trust_mu.help': 'How far a move\'s own value is trusted: c = Wᵢ / (Wᵢ + μ·ΣW), û = c·uᵢ + (1 − c)·fill. A move that got a μ share of all search weight is weighed half its own value, half the fill. Smaller μ trusts thinly read moves sooner.',
      'field.fill_kappa': 'Pessimism κ',
      'field.fill_kappa.help': 'The fill value for thinly read moves: mean − κ·spread of the read moves\' values (weighted). Larger κ assumes unread moves are worse.',
      'field.min_p': 'Min p (relative)',
      'field.min_p.help': 'Cuts (to 0) every move whose human-model probability is below min p × the human model\'s top probability. Both sides of that are the raw human policy -- how likely a human of this profile plays the move -- not KataGo\'s search value, and the cut comes before λ, distance and temperature. Relative: 0.05 keeps moves at least 5% as likely as the human favourite.',
      'field.distance_slope': 'Distance slope',
      'field.distance_slope.help': 'Weight by Euclidean distance d from the last move: w(d) = clamp(peak − slope·(d − 2), floor, peak). Within d = 2 a move gets the peak; farther ones fall off by the slope until the floor. 0 = off.',
      'field.distance_floor': 'Distance floor',
      'field.distance_floor.help': 'The lowest weight w(d) reaches, for moves far from the last one.',
      'field.distance_peak': 'Distance peak',
      'field.distance_peak.help': 'The weight of moves within distance 2 of the last one (the top of w(d)).',
      'field.temperature': 'Temperature',
      'field.temperature.help': 'Selection temperature, the way KataGo picks moves: probabilities are raised to 1/T and renormalised. Below 1 concentrates on the likeliest moves, above 1 flattens.',
      'field.key': 'JSON key',
      'field.range': 'Range',
      'field.example': 'Examples',
      'field.lambda_utility.range': '> 0 · needs Visits ≥ 2 and both μ, κ',
      'field.lambda_utility.example': '0.05 strong · 0.15 (#483 vertex B) · 0.5 weak',
      'field.trust_mu.range': '> 0 · required with λ',
      'field.trust_mu.example': '0.02 · 0.05',
      'field.fill_kappa.range': '≥ 0 · required with λ',
      'field.fill_kappa.example': '0 (mean) · 1 · 2',
      'field.min_p.range': '0–1 · default 0 (keep all)',
      'field.min_p.example': '0.02 · 0.05 · 0.2',
      'field.distance_slope.range': '≥ 0 · default 0 (off)',
      'field.distance_slope.example': '0.25 (ACG, floor from d ≈ 7.6) · 1 (very local)',
      'field.distance_floor.range': '> 0 and below the peak · default 0.1',
      'field.distance_floor.example': '0.1 (ACG)',
      'field.distance_peak.range': '> floor · default 1.5',
      'field.distance_peak.example': '1.5 (ACG)',
      'field.temperature.range': '> 0.0001 · default 1',
      'field.temperature.example': '0.5 · 1 · 1.5',
      'field.default': 'default {v}',
      'field.off': 'off',
      // validation
      'err.number': '{field} must be a number',
      'err.unknown': 'unknown key {field}',
      'err.gt': '{field} must be > {v}',
      'err.ge': '{field} must be ≥ {v}',
      'err.range': '{field} must be between {a} and {b}',
      'err.lambdaSubs': 'λ needs both μ and κ',
      'err.subsWithoutLambda': 'μ and κ only apply when λ is set',
      'err.floorPeak': 'distance floor must be below distance peak',
      // presets
      'preset.identity': 'Identity — the human policy as is',
      'preset.483B': '#483 vertex B — λ 0.15, μ 0.05, κ 1, min p 0.05',
      'preset.lambdaLight': 'Weak utility pull — λ 0.5, μ 0.05, κ 1',
      'preset.lambdaStrong': 'Strong utility pull — λ 0.05, μ 0.05, κ 1',
      'preset.minP': 'Cut the tail — min p 0.05',
      'preset.local': 'Play locally — distance slope 0.25',
      'preset.localStrong': 'Play very locally — distance slope 1',
      'preset.sharp': 'Sharper — temperature 0.5',
      'preset.flat': 'More varied — temperature 1.5'
    },
    ko: {
      'protocol.title': 'KataGo 인터페이스',
      'protocol.handol': 'handol-mux (휴먼)',
      'host.title': '엔진 호스트',
      'port.title': '엔진 포트',
      'connect': '연결',
      'disconnect': '연결 끊기',
      'disconnected': '연결 안 됨',
      'thinking': '생각 중',
      'engine': '엔진',
      'nav.first': '처음으로',
      'nav.prev10': '10수 뒤로',
      'nav.prev': '한 수 뒤로',
      'nav.next': '한 수 앞으로',
      'nav.next10': '10수 앞으로',
      'nav.last': '끝으로',
      'pass': '패스',
      'undo': '무르기',
      'resign': '기권',
      'winbar.title': '흑 승률',
      'toPlay': '둘 차례:',
      'black': '흑',
      'white': '백',
      'score.none': '집 차이 --',
      'visits.count': '방문 {n}',
      'noEngine': '엔진 없음',
      'human.section': '휴먼 정책',
      'human.profile': '프로파일',
      'human.profile.title': '휴먼 SL 프로파일: preaz_20k…preaz_9d, rank_20k…rank_9d, proyear_1800…proyear_2023',
      'human.preset': '프리셋',
      'human.preset.custom': '(직접 입력)',
      'human.json': 'JSON',
      'human.json.title': '정책 튜플 JSON — 위 입력칸과 서로 맞춰진다',
      'human.hint': '방문 1 = 탐색 없이 정책만. 라벨과 히트맵은 선택 확률을 보여준다.',
      'human.evalVisits': '승률 계산 방문',
      'human.evalVisits.title': '승률·집 차이를 얻으려고 함께 보내는 일반 KataGo 질의의 방문수(휴먼 응답에는 둘 다 없다). 0 = 끔.',
      'human.live': '움직이는 동안 바로 적용',
      'human.advanced': '원시 값',
      'compare.on': '두 튜플 비교',
      'compare.view': '보기',
      'compare.view.A': 'A',
      'compare.view.B': 'B',
      'compare.view.diff': '차이 B − A',
      'col.a': 'A',
      'col.b': 'B',
      'col.delta': 'Δ',
      'col.prob': '확률',
      'knob.strength': '가치 끌림',
      'knob.strength.help': '탐색이 좋게 보는 수 쪽으로 휴먼 분포를 끌어당기는 세기. 올릴수록 λ가 작아져 더 강하게 끈다(λ 2 → 0.01, 로그 척도). 방문 2 이상 필요. 더블클릭하면 초기화.',
      'knob.strength.lo': '꺼짐(휴먼만)',
      'knob.strength.hi': '탐색을 강하게 따름',
      'knob.locality': '국지성',
      'knob.locality.help': '직전 수 근처의 수를 선호(거리 기울기 0–3, ACG 값 0.25는 앞쪽에 있다). 더블클릭하면 초기화.',
      'knob.locality.lo': '어디든',
      'knob.locality.hi': '국지적으로 응수',
      'knob.variety': '다양성',
      'knob.variety.help': '온도 0.25–4: 왼쪽은 가장 그럴듯한 수 위주, 오른쪽은 고르게 퍼진다. 더블클릭하면 초기화.',
      'knob.variety.lo': '예측 가능',
      'knob.variety.hi': '다양하게',
      'knob.tail': '꼬리 자르기',
      'knob.tail.help': '휴먼 모델이 보기에 가능성이 낮은 수를 뺀다: 휴먼 모델 확률 1위 수의 확률 × 최소 확률(0–0.5)보다 낮은 수. 탐색 가치(λ)·거리·온도를 적용하기 전의 원래 휴먼 확률로 정한다. 더블클릭하면 초기화.',
      'knob.tail.lo': '모두 둠',
      'knob.tail.hi': '흔한 수만',
      'preset.group.builtin': '기본',
      'preset.group.mine': '내 프리셋',
      'preset.save': '저장…',
      'preset.delete': '삭제',
      'preset.export': '내보내기',
      'preset.import': '가져오기',
      'preset.namePrompt': '프리셋 이름',
      'preset.overwrite': '"{name}" 프리셋을 덮어쓸까?',
      'preset.confirmDelete': '"{name}" 프리셋을 지울까?',
      'preset.saved': '"{name}" 프리셋을 저장했다',
      'preset.imported': '프리셋 {added}개를 가져왔다 (건너뜀 {skipped})',
      'preset.importBad': '이 파일에는 프리셋이 없다',
      'preset.nameLong': '프리셋 이름은 {max}자까지다',
      'preset.nameChars': '프리셋 이름에는 줄바꿈이나 제어 문자를 넣을 수 없다',
      'preset.full': '프리셋은 {max}개까지 보관한다. 하나를 지우고 저장하라',
      'analysis.section': '분석',
      'analysis.continuous': '계속 분석',
      'analysis.visits': '방문',
      'analysis.every': '갱신',
      'analysis.seconds': '초',
      'analysis.label': '라벨',
      'analysis.ownership': '영역',
      'analysis.heatmap': '정책 히트맵',
      'analysis.numbers': '수순 번호',
      'label.winrate': '승률',
      'label.visits': '방문수',
      'label.prior': '정책 %',
      'label.score': '집 차이',
      'col.move': '수',
      'col.win': '승률',
      'col.score': '집',
      'col.visits': '방문',
      'col.policy': '정책',
      'col.value': '가치',
      'analysis.candidates': '후보 수',
      'help.label.winrate': '엔진이 탐색한 쪽이 이길 확률(%).',
      'help.label.visits': '탐색이 그 수에 쓴 방문 수(줄여 씀).',
      'help.label.prior': '그 수의 날 정책 확률(%). 차이 B − A를 볼 때는 부호 있는 차이(%p).',
      'help.label.score': '그 수의 집 차이. 흑 기준이라 흑이 앞서면 +이고, 소수 한 자리까지 나온다.',
      'help.ownership': '판 위의 네모: 흑이 차지하는 자리는 어둡게, 백이 차지하는 자리는 밝게, 값이 클수록 진하게 칠한다. 영역은 엔진이 자리마다 내는 값이고, 이것을 켜면 엔진에게 그 값을 보내 달라고 한다.',
      'help.heatmap': '날 정책이 무게를 두는 자리마다 보라색 네모를 칠하고, 가장 큰 값에 가까울수록 진하다. 날 정책은 엔진이 수마다 내는 확률이다. 후보 표에서는 정책 칸에 나오는 값이고, handol-mux 분석에서는 확률 칸, 비교할 때는 보기가 그리는 튜플의 A 또는 B 칸에 나오는 값이다. gowui가 계산하지 않는다. 차이 B − A를 볼 때는 B가 더 두는 자리는 따뜻한 빨강, 덜 두는 자리는 차가운 파랑이다.',
      'help.numbers': '판 위의 돌마다 그 돌을 둔 수순 번호를 보여 준다. 마지막 수의 고리는 이것을 켜든 끄든 있다.',
      'help.compare.tuple': '튜플은 원시 값 아래 여덟 가지 휴먼 정책 설정의 묶음이고, 엔진이 돌려주는 분포는 튜플에 따라 달라진다. 이것을 켜면 첫 튜플 A와 함께 두 번째 튜플 B를 보낸다.',
      'help.compare.views': 'A / B 탭은 조작이 어느 튜플을 고칠지 고르고, 보기는 어느 쪽을 그릴지 고른다.',
      'help.compare.diff': '차이 B − A에서는 후보마다 채운 원이 된다. B가 A보다 더 두는 수는 빨강, 덜 두는 수는 파랑이고, 차이가 가장 큰 수에는 흰 테두리를 두른다. 다만 그 수가 패스면 판 위에 자리가 없어 두르지 않는다.',
      'help.col.move': '후보 수.',
      'help.col.a': '튜플 A에서 그 수의 확률.',
      'help.col.b': '튜플 B에서 그 수의 확률.',
      'help.col.delta': 'B − A, 부호 있음.',
      'readout.best': '최선',
      'readout.none': '후보 없음',
      // §3.8 "Candidate readout": 후보가 없는 동안 도움말을 여는 단추.
      'help.start': '시작하는 법',
      // 도움말 패널(SPEC §3.8 "Help panel"). 설명 한 줄마다 바로 위에 근거를 적어 둔다.
      // 읽는 사람이 다시 따져 보는 대신 적힌 자리에서 확인하면 된다. 근거가 받쳐 주지
      // 않는 말은 넣지 않았다.
      'help.open': '도움말',
      'help.open.title': '이 페이지로 무엇을 하는지, 숫자가 무슨 뜻인지, 어떤 키가 있는지',
      'help.title': 'gowui 쓰는 법',
      'help.close': '닫기',
      'help.close.title': '도움말 닫기',
      'help.flow.title': '이 페이지 쓰는 차례',
      // §3.8 "Engine form": 프로토콜 선택, 호스트, 포트, 연결 단추와 배지.
      'help.flow.connect': '엔진 방식을 고르고 엔진이 열려 있는 주소를 넣은 다음 연결을 누른다. 옆 배지가 대답한 엔진의 이름을 보여 주고, 붙은 엔진이 없으면 없다고 말한다.',
      // §3.8 "Engine picker": engineAddress.kind가 catalog면 세 칸 대신 목록이 나온다.
      'help.flow.connect.catalog': '연결 옆 목록에서 엔진을 고르고 연결을 누른다. 옆 배지가 대답한 엔진의 이름을 보여 주고, 붙은 엔진이 없으면 없다고 말한다.',
      // §3.8 "Engine picker": 목록이 비면 연결이 꺼지고 목록 옆에 안내가 나온다.
      'help.flow.connect.empty': '이 서버에는 설정된 엔진이 없어서 연결할 것이 없다. 연결 단추는 꺼져 있고, 옆의 안내가 그렇게 말한다.',
      // §3.8 "Layout"(판 영역)과 "Controls"(내비게이션 — 판 위를 누르면 play를 보낸다).
      'help.flow.play': '판 위의 자리를 누르면 그 자리에 둔다. 판 아래에는 처음, −10, −1, 국면 세기, +1, +10, 끝이 있고 그다음에 패스·무르기·기권이 있다.',
      // §3.8 "Controls"(분석 항목)과 "Board overlays"(후보를 판 위에 어떻게 그리는지).
      'help.flow.analyse': '계속 분석을 켜면 엔진이 화면에 떠 있는 국면을 읽는다. 후보 수는 판 위에 그려지고 분석 항목의 표에 줄지어 나온다.',
      // §3.8 "Board strip": 보드마다 타일 하나, "+ 새 보드", 누르면 선택, ✎로 이름 변경.
      'help.flow.boards': '왼쪽 띠에 있는 보드는 저마다 딴 대국이다. "+ 새 보드"로 하나 더하고, 타일을 누르면 그 보드로 넘어가고, ✎로 이름을 바꾼다.',
      // §3.8 "Controls"(대국자, 새 대국, 엔진 콘솔 항목. 뒤의 둘은 index.html에서 닫혀 있다);
      // §3.5(콘솔은 허용된 곳에서 GTP 엔진에만 명령을 보낸다).
      'help.flow.more': '옆 패널 아래쪽에는 대국자(KataGo가 흑·백, 지금 엔진 착수, 계가)가 있고, 열어야 보이는 새 대국은 대국을 시작하고 SGF 저장과 SGF 열기를 담고 있으며, 역시 닫혀 있는 엔진 콘솔에는 엔진과 주고받은 줄과, 허용된 곳에서 GTP 엔진에 명령을 직접 보내는 칸이 있다.',
      // §3.8 "Help panel": 조작 하나하나는 호버 카드와 프로파일 "?"가 맡는다
      // (§3.8 "Human policy panel", "Scrolling"). 이 패널은 그것을 다시 옮겨 적지 않는다.
      'help.flow.controls': '조작 하나가 무엇인지는 그 조작이 있는 자리에서 읽는다. 입력칸이나 손잡이에 마우스를 올리면 설명 카드가 뜨고, 프로파일 칸 옆 "?"는 그 프로파일이 무엇을 흉내 내는지 알려 준다. 이 패널은 그것을 되풀이하지 않는다.',
      // §3.8 "Top candidates"(board.js _drawCandidates): 고리, 여섯 시에서 오르는 두 호, 바탕 고리;
      // 방문이 없으면 파란 호가 없다; 라벨 아래 작은 방문 수.
      'help.glyph.title': '후보 위에 그려지는 것',
      'help.glyph.ring': '후보마다 옅은 바탕 고리 위에 고리를 그린다. 아래에서부터 왼쪽 주홍 반쪽은 그 수의 날 정책 확률만큼, 오른쪽 파란 반쪽은 탐색 가운데 그 수에 쓴 몫만큼 올라간다.',
      'help.glyph.visits': '탐색이 닿지 않은 수에는 파란 호가 없다. 작은 파란 숫자는 그 수의 방문 수이고, 그 위의 숫자는 라벨 선택이 고른 값이다. 라벨 옆 ?가 무엇인지 알려 준다.',
      // §3.8 "Top candidates": 흰 바깥 고리, 패스는 자리가 없다(board.js:16, 453).
      // 다른 후보의 고리는 그대로이니 없는 것은 흰 고리 하나다.
      'help.glyph.best': '첫 후보, 곧 표가 최선으로 표시하는 수에는 흰 바깥 고리가 있다. 다만 그 수가 패스면 판 위에 자리가 없어서 흰 바깥 고리를 두른 후보가 없다.',
      // §3.8 "Compare views": 채운 원은 비교 카드(help.compare.diff)가 맡고, 여기서 되풀이하지 않는다.
      'help.glyph.diff': '차이 B − A를 볼 때는 후보를 다르게 그린다. 두 튜플 비교 옆 ?가 어떻게 그리는지 알려 준다.',
      // §3.8 "Help panel" 셋째 부분: ? 카드가 있는 스위치마다 한 줄, 그 ?를 가리킨다.
      'help.task.title': '무엇을 보려면 무엇을 켜나',
      'help.task.label': '후보마다 적히는 숫자를 고르려면:',
      'help.task.ownership': '자리마다 어느 쪽 차지인지 보려면:',
      // §3.8 "Raw policy heatmap": 값이 0.0005를 넘는 자리에만 네모를 그린다(board.js:387).
      'help.task.heatmap': '후보 자리만이 아니라 날 정책이 0.05%를 넘는 자리마다 네모로 보려면:',
      'help.task.numbers': '돌이 놓인 차례를 읽으려면:',
      'help.task.compare': '두 휴먼 정책 튜플이 어떻게 다른지 보려면(handol-mux):',
      'help.task.see': '— 옆의 ?가 무엇을 보여 주는지 알려 준다.',
      'help.numbers.title': '숫자가 뜻하는 것',
      // §0 "Perspective"에서 승률만이 흑 기준의 예외이고, §3.8 "Candidate table"에서
      // 승률은 탐색한 쪽 기준, 집 차이는 흑 기준이다. 어긋남이 한 줄 안에 있으니 한 호흡에 적는다.
      'help.num.winScore': '한 줄에 있는 둘은 서로 다른 쪽에서 읽은 값이다. 승률은 엔진이 탐색한 쪽이 이길 확률이고, 집은 흑 기준의 집 차이 그대로다. 이 페이지에서 흑 기준을 벗어나는 것은 승률 하나뿐이다.',
      // §3.8 "Candidate table".
      'help.num.visits': '그 수에 탐색을 얼마나 썼는지.',
      // §2.2 `prior`; §3.8 "Label modes", "Candidate table"(handol-mux 분석에서는 확률).
      'help.num.policy': '그 수의 날 정책 확률. handol-mux 분석에서는 같은 칸이 확률로 나온다.',
      // §2.2(`utility` — 부호 있는 값, 0이 가운데, 확률이 아니고 흑 기준); §3.8
      // "Candidate table"(소수 두 자리); §3.8 "Candidate readout"(국면마다 기준을 다시 맞춘다).
      // §3.8 "What the numbers mean": 이 셋은 범례가 표의 ?를 가리킨다.
      'help.num.inTable': '이 셋과 후보 표의 다른 칸은 후보 수 옆 ?가 설명한다.',
      'help.num.value': '엔진의 utility 값. 0을 가운데 둔 부호 있는 수 하나이고 확률이 아니며, 집처럼 흑 기준으로 소수 두 자리까지 나온다. 한 국면 안의 후보끼리 견주는 값이지 국면끼리 견주는 값이 아니다. 집에 해당하는 부분이 그 국면의 기대 집 수를 기준으로 다시 맞춰지기 때문이다.',
      // §2.2 `utilityLcb`; §3.8 "Candidate readout"(괄호는 style.css
      // `.readout .lcb::before` / `::after`).
      'help.num.lcb': '판 아래 줄의 가치 옆 괄호 안 숫자는 utilityLcb, 같은 기준으로 본 그 가치의 신뢰 하한이다.',
      // §3.8 "Human policy panel"과 field.lambda_utility.help, field.trust_mu.help — 거기서 û는
      // μ와 κ가 채움값 쪽으로 당긴 뒤의 값이다. field.fill_kappa.help는 그 채움값을 정할 뿐 û를
      // 말하지 않으니 그 카드는 들지 않는다. 읽는 이가 보는 이름으로 적는다: 가치 끌림 손잡이가
      // λ·μ·κ를 정하고(tuple.js:66-75), field.lambda_utility, field.trust_mu가 그 칸들의 이름이다.
      'help.num.collision': '같은 말이 둘을 가리킨다. 후보 표의 가치는 엔진의 날 utility다. 가치 끌림 손잡이 뒤의 û — 원시 값 아래 가치 온도 λ·신뢰 μ 카드에 나오는 û — 는 다른 수다. 그 수의 탐색 가치를 신뢰와 비관도가 채움값 쪽으로 당기고 난 값이다.',
      // §3.8 "What the numbers mean": 둘 사이의 식도 환산 계수도 이 저장소에 있는
      // 어떤 것에서도 끌어낼 수 없으니, 패널은 둘 다 싣지 않는다.
      'help.num.noConversion': '단위가 다른 서로 다른 값이고, gowui는 둘 사이의 환산을 정해 두지 않았다. 승률을 가치로, 가치를 승률로 바꾸는 식은 여기에 없다.',
      // 아래 목록은 §3.7의 표에서 그려 낸다. 여기 있는 것은 그 네 자리와 그 안의 다섯 묶음 이름이다.
      'help.keys.title': '단축키',
      'help.scope.nav': '글자 입력칸 밖, 페이지 전체',
      'help.scope.tile': '보드 타일에 초점이 있을 때',
      'help.scope.drag': '보드 타일을 끄는 동안',
      'help.scope.rename': '보드 이름 칸에서',
      'help.key.prev': '한 수 뒤로',
      'help.key.next': '한 수 앞으로',
      'help.key.first': '맨 처음 국면',
      'help.key.last': '맨 끝 국면',
      'help.key.pass': '패스',
      'help.key.undo': '마지막 수 무르기',
      'help.key.genmove': '둘 차례인 쪽을 엔진이 두게 한다',
      'help.key.analysis': '계속 분석 켜고 끄기',
      'help.key.prevBoard': '앞 보드로',
      'help.key.nextBoard': '뒤 보드로',
      'help.key.tileMove': '타일을 한 자리 앞이나 뒤로 옮긴다',
      'help.key.tileSelect': '그 보드로 넘어간다',
      'help.key.cancelDrag': '끌던 것을 무른다',
      'help.key.renameSave': '이름을 저장한다',
      'help.key.renameCancel': '고치던 것을 버린다',
      'players.section': '대국자',
      'players.black': 'KataGo가 흑',
      'players.white': 'KataGo가 백',
      'players.genmove': '지금 엔진 착수',
      'players.finalScore': '계가',
      'captures.black': '흑이 딴 돌',
      'captures.white': '백이 딴 돌',
      'newGame.section': '새 대국',
      'newGame.size': '크기',
      'newGame.handicap': '접바둑',
      'newGame.komi': '덤',
      'newGame.rules': '규칙',
      'newGame.start': '시작',
      'newGame.save': 'SGF 저장',
      'newGame.load': 'SGF 열기',
      'rules.japanese': '일본',
      'rules.korean': '한국',
      'rules.chinese': '중국',
      'rules.newZealand': '뉴질랜드',
      'moves.section': '기보',
      'console.section': '엔진 콘솔',
      'console.send': '보내기',
      'profile.kyu': '{n}급',
      'profile.dan': '{n}단',
      'profile.pair': '흑 {b} · 백 {w}',
      'profile.families': '프로파일 종류',
      'profile.group.rank': 'rank_20k…rank_9d',
      'profile.group.preaz': 'preaz_20k…preaz_9d',
      'profile.group.proyear': 'proyear_1800…proyear_2023',
      'profile.groupdesc.rank': '그 급수의 아마추어, AlphaZero 이후 바뀐 포석',
      'profile.groupdesc.preaz': '그 급수의 아마추어, AlphaZero 이전 포석',
      'profile.groupdesc.proyear': '프로·강한 연구생, 그 해와 앞뒤 해의 기보',
      'profile.pairHint': '흑·백 급수를 따로 줄 수도 있다: rank_5k_1d, preaz_10k_3k.',
      'profile.title.rank': '아마추어 {rank}, 현대 포석',
      'profile.title.preaz': '아마추어 {rank}, AlphaZero 이전 포석',
      'profile.title.proyear': '프로 기사, {year}년',
      'profile.desc.rank': '대국 날짜를 2020년 3월로 두고 조건화 — AlphaZero 때문에 사람 포석이 바뀐 뒤.',
      'profile.desc.preaz': '대국 날짜를 2016년 9월로 두고 조건화 — AlphaZero 포석이 퍼지기 전.',
      'profile.desc.kgs': 'KGS 대국(급수 기준이 KGS)으로 가정, 20분 + 30초 초읽기 5회.',
      'profile.desc.pair': '서로 상대가 더 세거나 약하다는 걸 아는 대국. 9급 넘게 차이 나거나 치석과 맞지 않으면 학습 데이터 밖이라 이상하게 둘 수 있다.',
      'profile.desc.gogod': 'GoGoD(역사 프로 기보 모음), {year}년 6월로 조건화.',
      'profile.desc.go4go': 'Go4Go 최근 프로 기보, {year}년 6월로 조건화.',
      'profile.desc.strength': '원모델은 탐색을 하지 않아 고단·프로의 실제 기력에는 못 미친다 — 가치 끌림(λ)과 방문수가 그걸 보탠다.',
      'profile.desc.unknown': '"{name}"은(는) 알려진 프로파일 이름이 아니다 — 받아들일지는 엔진이 정한다.',
      'profile.desc.source': '출처: KataGo sgfmetadata.cpp, gtp_human5k_example.cfg',
      'sgf.namePrompt': 'SGF 파일 이름',
      'sgf.saved': '{name} 저장함',
      'sgf.failed': 'SGF를 저장하지 못했다: {error}',
      'style.black': '흑 착수',
      'style.white': '백 착수',
      'style.human': '휴먼 분포',
      'style.katago': 'KataGo 최선수',
      'style.title': '엔진이 수를 고르는 방식: 휴먼 분포(튜플 적용)에서 확률대로 뽑기, 또는 방문 수만큼 일반 탐색한 KataGo 1순위 수.',
      'boards.new': '+ 새 보드',
      'boards.new.title': '이 보드의 크기와 규칙으로 빈 보드를 더하고 그 보드로 넘어간다',
      'boards.duplicate': '이 보드(국면과 휴먼 설정)를 복사해 그 복사본으로 넘어간다',
      'boards.move': '{n}/{total}수',
      'boards.identity': '항등',
      'boards.delete': '이 보드 지우기',
      'boards.reset': '이 보드 비우기',
      'boards.confirmDelete': '"{name}" 보드를 지울까?',
      'boards.confirmReset': '"{name}" 보드를 비울까? 하나뿐인 보드라 지우지 않고 비운다.',
      'boards.renamePrompt': '보드 이름',
      'boards.renameHint': '더블클릭하거나 ✎로 이름 변경 · [ ] 키로 보드 전환 · 타일을 끌거나 Alt+화살표로 순서 변경',
      'boards.rename': '이름 바꾸기',
      'boards.default': '보드 {n}',
      'status.lost': 'gowui 연결이 끊겼다 — 다시 연결하는 중...',
      'status.notSignedIn': '로그인되어 있지 않다: 로그인한 뒤 페이지를 새로 고친다.',
      'status.refused': 'Host 및 Origin 규칙에 따라 연결이 거부되었다.',
      'status.tooManySockets': '이 계정으로 연 gowui 탭이 너무 많다. 하나를 닫은 뒤 이 페이지를 새로 고친다.',
      'picker.title': '엔진',
      'picker.empty': '이 서버에 설정된 엔진이 없다.',
      'me.local': '비밀번호로 로그인함',
      'me.sso': '통합 로그인(SSO)으로 로그인함',
      'logout': '로그아웃',
      'sgf.tooLarge': 'SGF 파일이 1 MiB보다 크다.',
      'sgf.loadFailed': 'SGF를 불러오지 못했다 (HTTP {status}).',
      'tuple.json.invalid': '튜플은 JSON 객체여야 한다 (예: {"min_p": 0.05})',
      'tuple.fix': '보내기 전에 튜플을 고쳐야 한다: {problem}',
      'tuple.lambdaNeedsSearch': 'λ는 탐색이 필요하다 — 방문을 2 이상으로',
      'field.lambda_utility': '가치 온도 λ',
      'field.lambda_utility.help': '각 수에 exp((û − û_최고) / λ)를 곱한다. û는 그 수의 탐색 가치(μ·κ로 채움값 쪽으로 당긴 것). λ가 작을수록 가치가 가장 좋은 수로 강하게 쏠리고, 클수록 효과가 흐려진다. 비우면 꺼짐.',
      'field.trust_mu': '신뢰 μ',
      'field.trust_mu.help': '그 수의 가치를 얼마나 믿을지: c = Wᵢ / (Wᵢ + μ·ΣW), û = c·uᵢ + (1 − c)·채움값. 전체 탐색 가중의 μ만큼 받은 수는 자기 가치와 채움값을 반반 섞는다. 작을수록 적게 읽은 수도 빨리 믿는다.',
      'field.fill_kappa': '비관도 κ',
      'field.fill_kappa.help': '덜 읽은 수를 채우는 값: 읽은 수들 가치의 (가중) 평균 − κ·퍼짐. 클수록 안 읽은 수를 더 나쁘게 본다.',
      'field.min_p': '최소 확률 (상대)',
      'field.min_p.help': '휴먼 모델 확률이 최소 확률 × 휴먼 모델 1위 수의 확률보다 낮은 수를 잘라낸다(0으로). 여기서 확률은 양쪽 다 휴먼 모델의 원래 확률(이 프로파일의 사람이 그 수를 둘 가능성)이고 KataGo 탐색 가치가 아니다. 자르기는 λ·거리·온도보다 먼저 한다. 상대값이라 0.05면 사람 1순위 수의 5% 이상인 수만 남는다.',
      'field.distance_slope': '거리 기울기',
      'field.distance_slope.help': '직전 수와의 유클리드 거리 d로 가중: w(d) = clamp(정점 − 기울기·(d − 2), 바닥, 정점). 거리 2 안은 정점, 그 밖은 기울기만큼 줄다가 바닥에서 멈춘다. 0이면 꺼짐.',
      'field.distance_floor': '거리 바닥',
      'field.distance_floor.help': 'w(d)가 내려가는 최저값 — 직전 수에서 먼 수의 가중.',
      'field.distance_peak': '거리 정점',
      'field.distance_peak.help': '직전 수에서 거리 2 이내인 수의 가중(w(d)의 최고값).',
      'field.temperature': '온도',
      'field.temperature.help': 'KataGo가 수를 고르는 방식과 같은 선택 온도: 확률을 1/T 제곱한 뒤 다시 정규화한다. 1보다 작으면 유력한 수로 쏠리고, 크면 평평해진다.',
      'field.key': 'JSON 키',
      'field.range': '값역',
      'field.example': '예',
      'field.lambda_utility.range': '> 0 · 방문 2 이상, μ·κ 둘 다 필요',
      'field.lambda_utility.example': '0.05 강하게 · 0.15 (#483 꼭짓점 B) · 0.5 약하게',
      'field.trust_mu.range': '> 0 · λ가 있으면 필수',
      'field.trust_mu.example': '0.02 · 0.05',
      'field.fill_kappa.range': '≥ 0 · λ가 있으면 필수',
      'field.fill_kappa.example': '0 (평균) · 1 · 2',
      'field.min_p.range': '0–1 · 기본 0 (모두 둠)',
      'field.min_p.example': '0.02 · 0.05 · 0.2',
      'field.distance_slope.range': '≥ 0 · 기본 0 (꺼짐)',
      'field.distance_slope.example': '0.25 (ACG, d ≈ 7.6부터 바닥) · 1 (아주 국지적)',
      'field.distance_floor.range': '> 0, 정점보다 작게 · 기본 0.1',
      'field.distance_floor.example': '0.1 (ACG)',
      'field.distance_peak.range': '> 바닥 · 기본 1.5',
      'field.distance_peak.example': '1.5 (ACG)',
      'field.temperature.range': '> 0.0001 · 기본 1',
      'field.temperature.example': '0.5 · 1 · 1.5',
      'field.default': '기본 {v}',
      'field.off': '꺼짐',
      'err.number': '{field}은(는) 숫자여야 한다',
      'err.unknown': '알 수 없는 키 {field}',
      'err.gt': '{field}은(는) {v}보다 커야 한다',
      'err.ge': '{field}은(는) {v} 이상이어야 한다',
      'err.range': '{field}은(는) {a}–{b} 사이여야 한다',
      'err.lambdaSubs': 'λ를 쓰면 μ와 κ가 둘 다 필요하다',
      'err.subsWithoutLambda': 'μ·κ는 λ가 있을 때만 쓴다',
      'err.floorPeak': '거리 바닥은 거리 정점보다 작아야 한다',
      'preset.identity': '항등 — 휴먼 정책 그대로',
      'preset.483B': '#483 꼭짓점 B — λ 0.15, μ 0.05, κ 1, 최소 확률 0.05',
      'preset.lambdaLight': '가치 약하게 — λ 0.5, μ 0.05, κ 1',
      'preset.lambdaStrong': '가치 강하게 — λ 0.05, μ 0.05, κ 1',
      'preset.minP': '꼬리 자르기 — 최소 확률 0.05',
      'preset.local': '국지적으로 — 거리 기울기 0.25',
      'preset.localStrong': '아주 국지적으로 — 거리 기울기 1',
      'preset.sharp': '날카롭게 — 온도 0.5',
      'preset.flat': '다양하게 — 온도 1.5'
    }
  };

  var STORAGE_KEY = 'gowui.lang';
  var listeners = [];

  // Own entries only: a saved language or a key such as "constructor" or "toString" names an
  // inherited property, never a table or a text (§8.5).
  function own(table, name) {
    return table && Object.prototype.hasOwnProperty.call(table, name) ? table[name] : null;
  }

  function initial() {
    try {
      var saved = global.localStorage.getItem(STORAGE_KEY);
      if (saved && own(STRINGS, saved)) return saved;
    } catch (err) { /* storage can be unavailable */ }
    var nav = (global.navigator && global.navigator.language) || '';
    return nav.toLowerCase().indexOf('en') === 0 ? 'en' : 'ko';
  }

  var lang = initial();

  function t(key, vars) {
    var text = own(own(STRINGS, lang), key) || own(STRINGS.en, key) || key;
    if (vars) {
      text = text.replace(/\{(\w+)\}/g, function (m, name) {
        return vars[name] === undefined ? m : String(vars[name]);
      });
    }
    return text;
  }

  function apply(root) {
    root = root || global.document;
    root.querySelectorAll('[data-i18n]').forEach(function (node) {
      node.textContent = t(node.getAttribute('data-i18n'));
    });
    root.querySelectorAll('[data-i18n-title]').forEach(function (node) {
      node.title = t(node.getAttribute('data-i18n-title'));
    });
    root.querySelectorAll('[data-i18n-placeholder]').forEach(function (node) {
      node.placeholder = t(node.getAttribute('data-i18n-placeholder'));
    });
    global.document.documentElement.lang = lang;
  }

  // Written until the server says it keeps the language for the account (§8.5).
  var inBrowser = true;

  function switchTo(next) {
    if (!own(STRINGS, next) || next === lang) return false;
    lang = next;
    apply();
    listeners.forEach(function (fn) { fn(lang); });
    return true;
  }

  function setLang(next) {
    if (!switchTo(next)) return;
    if (!inBrowser) return;
    try { global.localStorage.setItem(STORAGE_KEY, lang); } catch (err) { /* ignore */ }
  }

  // The account keeps the language (§4.2 `preferences`): this browser stops storing it, and the
  // account's choice — when it names a table — replaces the page's initial one (§3.8).
  function useAccountLang(saved) {
    inBrowser = false;
    // Not merely "stops storing": the key goes, so a language saved here before — in local mode,
    // or under another account on a shared machine — lingers for nobody (§8.5).
    try { global.localStorage.removeItem(STORAGE_KEY); } catch (err) { /* ignore */ }
    switchTo(saved);
  }

  global.i18n = {
    t: t,
    apply: apply,
    setLang: setLang,
    useAccountLang: useAccountLang,
    keptInBrowser: function () { return inBrowser; },
    lang: function () { return lang; },
    onChange: function (fn) { listeners.push(fn); }
  };
})(window);

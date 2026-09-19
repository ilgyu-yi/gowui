"""The server-mode page additions and the sign-in page speak Korean and English (SPEC §3.8
"Server-mode additions", §7.11; issue #9 AC5).

The page's own tables are checked for every key in general by tests/test_frontend_i18n.py; this
file pins that the server-mode strings exist in both tables and are used, that the elements they
label are in ``index.html``, and that the sign-in page's two tables hold the same keys.
"""

from __future__ import annotations

import re

import pytest

from frontend_helpers import read_static
from test_frontend_i18n import table, used_keys

SERVER_KEYS = ["picker.title", "picker.empty", "me.local", "me.sso", "logout"]


@pytest.mark.parametrize("lang", ["en", "ko"])
def test_every_server_mode_key_is_in_the_table(lang):
    assert sorted(set(SERVER_KEYS) - set(table(lang))) == []


def test_every_server_mode_key_is_used_by_the_page():
    assert sorted(set(SERVER_KEYS) - used_keys()) == []


def test_the_page_has_the_picker_and_a_hidden_log_out_form():
    html = read_static("index.html")
    assert re.search(r'<select id="engine-pick"[^>]*\bhidden\b', html)
    assert re.search(r'<form id="logout-form" method="post" action="/logout"[^>]*\bhidden\b', html)
    assert re.search(r'<a id="logout-link"[^>]*\bhidden\b', html)
    assert 'id="me-name"' in html


def test_the_sign_in_tables_hold_the_same_keys():
    from gowui.server_mode import SIGN_IN_TEXT

    assert sorted(SIGN_IN_TEXT) == ["en", "ko"]
    assert set(SIGN_IN_TEXT["en"]) == set(SIGN_IN_TEXT["ko"])
    assert all(v.strip() for lang in SIGN_IN_TEXT.values() for v in lang.values())
    assert {"title", "name", "password", "submit", "error.wrong", "error.throttled"} <= \
        set(SIGN_IN_TEXT["en"])

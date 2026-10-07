"""Streamlit script used by test_streamlit.py: the real UI over in-memory services."""

from __future__ import annotations

import streamlit as st

from auditable_sql.interfaces.streamlit_app import run
from tests.fakes import result
from tests.unit.interfaces.test_cli import World, answered

if "world" not in st.session_state:
    st.session_state["world"] = World(
        answered("SELECT tax_id FROM clients"),
        answered(),
        results=(result(["aum"], [42]), result(["aum"], [42])),
    )
run(st.session_state["world"].services())

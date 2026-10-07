"""`streamlit run src/auditable_sql/bootstrap/streamlit_main.py`"""

from __future__ import annotations

import streamlit as st

from auditable_sql.bootstrap.container import Container
from auditable_sql.interfaces.services import Services
from auditable_sql.interfaces.streamlit_app import run


@st.cache_resource
def services() -> Services:
    return Container().services()


run(services())

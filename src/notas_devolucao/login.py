from __future__ import annotations

import hashlib
import hmac

import streamlit as st

from .config import Configuracao


def _hash(senha: str) -> str:
    return hashlib.sha256(senha.encode("utf-8")).hexdigest()


def exigir_login(config: Configuracao) -> None:
    """Trava o acesso ao app até o usuário informar usuário/senha corretos. Chamar no topo do
    script, antes de renderizar qualquer dado - `st.stop()` interrompe a execução do restante da
    página enquanto não autenticado."""
    if st.session_state.get("autenticado"):
        return

    st.markdown("### 🔒 Acesso restrito")
    with st.form("form_login"):
        usuario = st.text_input("Usuário")
        senha = st.text_input("Senha", type="password")
        entrar = st.form_submit_button("Entrar")

    if entrar:
        usuario_ok = hmac.compare_digest(usuario.strip().lower(), config.auth_usuario.strip().lower())
        senha_ok = hmac.compare_digest(_hash(senha), config.auth_senha_hash)
        if usuario_ok and senha_ok:
            st.session_state["autenticado"] = True
            st.rerun()
        else:
            st.error("Usuário ou senha incorretos.")

    st.stop()

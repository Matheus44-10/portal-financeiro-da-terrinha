from __future__ import annotations

import re


def eh_intercompany(nome: str, termos: list[str]) -> bool:
    """Compara por palavra inteira (case-insensitive), não substring - "MASTER" não deve casar com
    "MASTERSENSE" (fornecedor externo real), mas deve casar com "MASTER LTDA" ou "GRUPO MASTER"."""
    return any(re.search(rf"\b{re.escape(termo)}\b", nome, re.IGNORECASE) for termo in termos)

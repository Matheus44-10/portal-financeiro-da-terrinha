from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import pandas as pd


@dataclass
class Antecipacao:
    duplicata_key: str
    fornecedor: str
    loja_pagadora: int
    data_vencimento: date
    valor_liquido: float
    desconto_financeiro: float
    ganho_percentual: float


def carregar_antecipacoes(caminho: Path) -> list[Antecipacao]:
    """Lê a aba "report" da planilha externa de antecipação de fornecedores (mantida manualmente
    pelo financeiro, fora deste projeto) - colunas fixas: L.P, Duplicata, Favorecido, Vencimento,
    Mês, Valor líquido, Desconto Financeiro, Ganha por %.

    Lê por posição de coluna, não pelo nome do cabeçalho - os cabeçalhos da planilha de origem têm
    os acentos corrompidos (ex.: "M?s" em vez de "Mês", confirmado tanto via pandas quanto via
    openpyxl), então comparar pelo texto do cabeçalho não é confiável."""
    df = pd.read_excel(caminho, sheet_name="report")
    df = df.iloc[:, :8]
    df.columns = [
        "loja_pagadora",
        "duplicata_key",
        "fornecedor",
        "data_vencimento",
        "_mes",
        "valor_liquido",
        "desconto_financeiro",
        "ganho_percentual",
    ]
    df = df.dropna(subset=["duplicata_key"])

    return [
        Antecipacao(
            duplicata_key=str(int(linha.duplicata_key)),
            fornecedor=str(linha.fornecedor).strip(),
            loja_pagadora=int(linha.loja_pagadora),
            data_vencimento=linha.data_vencimento.date(),
            valor_liquido=float(linha.valor_liquido),
            desconto_financeiro=float(linha.desconto_financeiro),
            ganho_percentual=float(linha.ganho_percentual),
        )
        for linha in df.itertuples()
    ]


def data_modificacao(caminho: Path) -> datetime:
    """Quando a planilha foi modificada pela última vez pelo financeiro.

    Na nuvem o `caminho` é um retrato versionado no git, cujo mtime é a hora em que o Streamlit
    Community Cloud clonou o repositório - nunca a hora em que alguém mexeu na planilha de verdade.
    Por isso `publicacao.publicar_dados` grava a data real num `antecipacao_meta.json` ao lado, e é
    ela que vale quando existe. Localmente esse arquivo não existe e o mtime do próprio arquivo já
    é a resposta certa."""
    meta = caminho.with_name("antecipacao_meta.json")
    if meta.exists():
        try:
            return datetime.fromisoformat(json.loads(meta.read_text(encoding="utf-8"))["modificada_em"])
        except (ValueError, KeyError, OSError):
            pass
    return datetime.fromtimestamp(caminho.stat().st_mtime)

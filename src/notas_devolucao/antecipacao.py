from __future__ import annotations

from dataclasses import dataclass
from datetime import date
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

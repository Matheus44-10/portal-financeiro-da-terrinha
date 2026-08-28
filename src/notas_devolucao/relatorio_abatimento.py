"""Suporte para notas de devolução JÁ QUITADAS - conceito diferente do restante do app (que por
padrão só olha notas em aberto). Usado tanto pelo relatório mensal do financeiro
(Report_Abatimento_Dinamico.xlsx) quanto pelo app Streamlit (métrica de valor quitado no período)."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime

from playwright.sync_api import Frame, Page

from . import navigation

logger = logging.getLogger("notas_devolucao.relatorio_abatimento")


@dataclass
class NotaQuitada:
    duplicata_key: str
    fornecedor: str
    fornecedor_cnpj: str
    loja: int
    loja_nome: str
    loja_recebedora: int
    valor_liquido: float
    data_emissao: date
    data_criacao: date
    data_quitacao: date
    descritivo: str
    numero_documento: str
    tipo: str
    conta_contabil_dlp: str
    conta_contabil_destino: str


@dataclass
class AbatimentoDetalhe:
    duplicata_cobranca_key: str
    loja_origem: int
    loja_recebedora: int
    valor_liquido_abatido: float
    numero_documento: str


_JS_EXTRAIR_QUITADAS = """
() => {
    const linhas = document.querySelectorAll('tr[ng-repeat="cobranca in cobrancasSintetica"]');
    return Array.from(linhas).map(el => {
        const c = angular.element(el).scope().cobranca;
        return {
            duplicataKey: c.duplicataKey,
            sacadoNome: c.sacadoNome,
            sacadoCpfCnpj: c.sacadoCpfCnpj,
            lojaKey: c.lojaKey,
            lojaNome: c.lojaNome,
            lojaRecebedoraKey: c.lojaRecebedoraKey,
            valorLiquido: c.valorLiquido,
            dataEmissao: c.dataEmissao,
            dataCriacao: c.dataCriacao,
            dataLiquidacao: c.dataLiquidacao,
            descritivo: c.descritivo,
            numeroDocumento: c.numeroDocumento,
            tipoDuplicataDescricao: c.tipoDuplicataDescricao,
            contaContabilDescricao: c.contaContabilDescricao,
            contaContabilDestinoDescricao: c.contaContabilDestinoDescricao,
        };
    });
}
"""


def _parse_data(texto: str | None):
    if not texto:
        return None
    return datetime.strptime(texto.strip(), "%d/%m/%Y").date()


def extrair_notas_quitadas(frame: Frame) -> list[NotaQuitada]:
    linhas = frame.evaluate(_JS_EXTRAIR_QUITADAS)
    notas: list[NotaQuitada] = []
    for linha in linhas:
        try:
            data_quitacao = _parse_data(linha["dataLiquidacao"])
            if data_quitacao is None:
                continue
            notas.append(
                NotaQuitada(
                    duplicata_key=str(linha["duplicataKey"]),
                    fornecedor=linha["sacadoNome"].strip(),
                    fornecedor_cnpj=(linha["sacadoCpfCnpj"] or "").strip(),
                    loja=int(linha["lojaKey"]),
                    loja_nome=(linha["lojaNome"] or "").strip(),
                    loja_recebedora=int(linha["lojaRecebedoraKey"]) if linha.get("lojaRecebedoraKey") is not None else int(linha["lojaKey"]),
                    valor_liquido=float(linha["valorLiquido"]),
                    data_emissao=_parse_data(linha["dataEmissao"]),
                    data_criacao=_parse_data(linha["dataCriacao"]) or _parse_data(linha["dataEmissao"]),
                    data_quitacao=data_quitacao,
                    descritivo=(linha["descritivo"] or "").strip(),
                    numero_documento=(linha["numeroDocumento"] or "").strip(),
                    tipo=linha["tipoDuplicataDescricao"] or "",
                    conta_contabil_dlp=(linha.get("contaContabilDescricao") or "").strip(),
                    conta_contabil_destino=(linha.get("contaContabilDestinoDescricao") or "").strip(),
                )
            )
        except (TypeError, ValueError, KeyError) as exc:
            logger.warning("Falha ao extrair nota quitada %s: %s", linha.get("duplicataKey"), exc)

    logger.info("%s notas quitadas extraídas.", len(notas))
    return notas


def obter_abatimentos(page: Page, duplicata_pagamento_key: str) -> tuple[str | None, list[AbatimentoDetalhe]]:
    """Retorna (nome da loja pagadora, lista de abatimentos) da duplicata de pagamento informada."""
    dados = navigation.obter_abatimentos_duplicata_pagamento(page, duplicata_pagamento_key)
    detalhes = []
    for linha in dados["linhas"]:
        try:
            detalhes.append(
                AbatimentoDetalhe(
                    duplicata_cobranca_key=str(linha["duplicataKey"]),
                    loja_origem=int(linha["lojaOrigemKey"]),
                    loja_recebedora=int(linha["lojaRecebedoraKey"]),
                    valor_liquido_abatido=float(linha["valorLiquido"]),
                    numero_documento=str(linha.get("numeroDocumento", "")),
                )
            )
        except (TypeError, ValueError, KeyError) as exc:
            logger.warning("Falha ao extrair linha de abatimento de %s: %s", duplicata_pagamento_key, exc)
    return dados["loja_pagadora_nome"], detalhes

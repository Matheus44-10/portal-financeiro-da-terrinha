from __future__ import annotations

import logging
from datetime import datetime

from playwright.sync_api import Frame

from .models import ContaPagar, NotaDevolucao

logger = logging.getLogger("notas_devolucao.extraction")

# Lê direto do model do AngularJS em vez de parsear texto/ícones da tabela renderizada - mesma
# técnica validada no projeto de agrupamento (ver docs/mapeamento_ui.md).
_JS_EXTRAIR_DEVOLUCOES = """
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
            valorNominal: c.valorNominal,
            valorLiquido: c.valorLiquido,
            dataEmissao: c.dataEmissao,
            dataVencimento: c.dataVencimento,
            descritivo: c.descritivo,
            numeroDocumento: c.numeroDocumento,
            tipoDuplicataDescricao: c.tipoDuplicataDescricao,
            quantidadeDiasDeAtraso: c.quantidadeDiasDeAtraso,
            liquidada: c.liquidada,
        };
    });
}
"""

_JS_EXTRAIR_CONTAS_A_PAGAR = """
() => {
    const linhas = document.querySelectorAll('tr[ng-repeat="duplicata in vm.pagamentosAnalitica"]');
    return Array.from(linhas).map(el => {
        const d = angular.element(el).scope().duplicata;
        return {
            duplicataKey: d.duplicataKey,
            favorecidoNome: d.favorecidoNome,
            favorecidoCpfCnpj: d.favorecidoCpfCnpj,
            lojaKey: d.sacadoKey,
            lojaPagadoraKey: d.lojaPagadoraKey,
            valorLiquido: d.valorLiquido,
            dataVencimento: d.dataVencimento,
            dataEmissao: d.dataEmissao,
            descritivo: d.descritivo,
            notaFiscal: d.numerosDeNotasFiscaisVinculadosComAFatura,
        };
    });
}
"""


def _parse_data(texto: str | None):
    if not texto:
        return None
    return datetime.strptime(texto.strip(), "%d/%m/%Y").date()


def extrair_notas_devolucao(frame: Frame) -> list[NotaDevolucao]:
    linhas = frame.evaluate(_JS_EXTRAIR_DEVOLUCOES)
    notas: list[NotaDevolucao] = []

    for linha in linhas:
        try:
            data_emissao = _parse_data(linha["dataEmissao"])
            data_vencimento = _parse_data(linha["dataVencimento"])
            if data_emissao is None or data_vencimento is None:
                raise ValueError(f"data ausente: emissao={linha['dataEmissao']!r} vencimento={linha['dataVencimento']!r}")

            notas.append(
                NotaDevolucao(
                    duplicata_key=str(linha["duplicataKey"]),
                    fornecedor=linha["sacadoNome"].strip(),
                    fornecedor_cnpj=(linha["sacadoCpfCnpj"] or "").strip(),
                    loja=int(linha["lojaKey"]),
                    loja_nome=(linha["lojaNome"] or "").strip(),
                    valor_nominal=float(linha["valorNominal"]),
                    valor_liquido=float(linha["valorLiquido"]),
                    data_emissao=data_emissao,
                    data_vencimento=data_vencimento,
                    descritivo=(linha["descritivo"] or "").strip(),
                    numero_documento=(linha["numeroDocumento"] or "").strip(),
                    tipo=linha["tipoDuplicataDescricao"] or "",
                    dias_de_atraso=int(linha["quantidadeDiasDeAtraso"] or 0),
                    liquidada=bool(linha["liquidada"]),
                )
            )
        except (TypeError, ValueError, KeyError) as exc:
            logger.warning("Falha ao extrair nota de devolução %s: %s", linha.get("duplicataKey"), exc)

    logger.info("%s notas de devolução extraídas.", len(notas))
    return notas


def extrair_contas_a_pagar(frame: Frame) -> list[ContaPagar]:
    linhas = frame.evaluate(_JS_EXTRAIR_CONTAS_A_PAGAR)
    contas: list[ContaPagar] = []

    for linha in linhas:
        try:
            data_emissao = _parse_data(linha["dataEmissao"])
            data_vencimento = _parse_data(linha["dataVencimento"])
            if data_emissao is None or data_vencimento is None:
                raise ValueError(f"data ausente: emissao={linha['dataEmissao']!r} vencimento={linha['dataVencimento']!r}")

            contas.append(
                ContaPagar(
                    duplicata_key=str(linha["duplicataKey"]),
                    favorecido=linha["favorecidoNome"].strip(),
                    favorecido_cnpj=(linha["favorecidoCpfCnpj"] or "").strip(),
                    loja=int(linha["lojaKey"]),
                    loja_pagadora=int(linha["lojaPagadoraKey"]),
                    valor=float(linha["valorLiquido"]),
                    data_emissao=data_emissao,
                    data_vencimento=data_vencimento,
                    descritivo=(linha["descritivo"] or "").strip(),
                    nota_fiscal=(linha["notaFiscal"] or "").strip(),
                )
            )
        except (TypeError, ValueError, KeyError) as exc:
            logger.warning("Falha ao extrair conta a pagar %s: %s", linha.get("duplicataKey"), exc)

    logger.info("%s contas a pagar extraídas.", len(contas))
    return contas

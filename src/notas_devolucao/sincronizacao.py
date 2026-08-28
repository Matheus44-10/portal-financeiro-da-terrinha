from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Callable

from . import auth, extraction, factoring, filtros, navigation, storage
from .config import Configuracao
from .models import BoletoFactoring, ContaPagar, NotaDevolucao
from .relatorio_abatimento import NotaQuitada, extrair_notas_quitadas

logger = logging.getLogger("notas_devolucao.sincronizacao")


def _checar_total_informado(frame, extraidos: int, rotulo: str, avisos: list[str]) -> None:
    """Compara o total que o próprio Bluesoft informa no rodapé da grade com o que foi realmente
    extraído - acumula um aviso legível em `avisos` se não bater (ver navigation.obter_total_informado
    pro contexto de por que essa checagem existe)."""
    total_informado = navigation.obter_total_informado(frame)
    if total_informado is not None and total_informado != extraidos:
        aviso = (
            f"{rotulo}: o Bluesoft informa {total_informado} duplicata(s), mas só {extraidos} "
            f"foram extraídas - a busca pode ter ficado incompleta."
        )
        logger.warning(aviso)
        avisos.append(aviso)


def atualizar_dados(
    config: Configuracao, headless: bool = False
) -> tuple[list[NotaDevolucao], list[ContaPagar], list[NotaQuitada], list[str]]:
    """Faz login (manual assistido, reaproveitando sessão salva quando possível), extrai as notas
    de devolução pendentes e, para cada fornecedor envolvido, as contas a pagar em aberto -
    salvando tudo no cache local ao final. Retorna os dados extraídos e uma lista de avisos (não
    vazia se algum total extraído não bateu com o que o Bluesoft informa)."""
    data_de = config.data_inicial
    data_ate = config.data_final
    avisos: list[str] = []

    playwright, browser = auth.abrir_navegador(headless=headless)
    contexto = auth.criar_contexto(browser)
    page = contexto.new_page()

    try:
        auth.garantir_login(contexto, page)

        frame = navigation.abrir_consulta_contas_a_receber(page)
        navigation.aplicar_filtros_devolucao(frame, data_de, data_ate)
        navigation.executar_consulta_sintetica_devolucao(frame)
        navigation.carregar_todas_paginas(frame, metodo="nextPageSintetica")
        notas = extraction.extrair_notas_devolucao(frame)
        _checar_total_informado(frame, len(notas), "Notas de devolução (período)", avisos)
        notas_pendentes = [
            n
            for n in notas
            if not n.liquidada and not filtros.eh_intercompany(n.fornecedor, config.intercompany_nomes)
        ]
        logger.info(
            "%s notas de devolução pendentes de fornecedor externo (de %s extraídas no período, "
            "descontando quitadas e intercompany).",
            len(notas_pendentes),
            len(notas),
        )

        logger.info("Checando vínculo de abatimento e produto de cada nota (uma tela por nota)...")
        produtos_por_fatura: dict[str, list[str]] = {}
        for indice, nota in enumerate(notas_pendentes, start=1):
            nota.duplicata_pagamento_vinculada = navigation.verificar_vinculo_pagamento(page, nota.duplicata_key)
            fatura_key = navigation.obter_fatura_key(page)
            if fatura_key:
                if fatura_key not in produtos_por_fatura:
                    produtos_por_fatura[fatura_key] = navigation.obter_produtos_da_fatura(page, fatura_key)
                nota.produtos = "; ".join(produtos_por_fatura[fatura_key]) or None
            if indice % 10 == 0 or indice == len(notas_pendentes):
                logger.info("Vínculo/produto checado: %s/%s notas.", indice, len(notas_pendentes))
        ja_vinculadas = sum(1 for n in notas_pendentes if n.duplicata_pagamento_vinculada)
        logger.info("%s notas já têm abatimento vinculado no Bluesoft.", ja_vinculadas)

        fornecedores = sorted({n.fornecedor for n in notas_pendentes})
        contas: list[ContaPagar] = []
        for fornecedor in fornecedores:
            frame = navigation.abrir_consulta_contas_a_pagar(page)
            navigation.aplicar_filtros_contas_a_pagar(frame, data_de, data_ate, favorecido=fornecedor)
            navigation.executar_consulta_analitica_contas_a_pagar(frame)
            navigation.carregar_todas_paginas(frame, metodo="nextPageAnalitica")
            contas_fornecedor = extraction.extrair_contas_a_pagar(frame)
            _checar_total_informado(frame, len(contas_fornecedor), f"Contas a pagar de {fornecedor}", avisos)
            contas.extend(contas_fornecedor)

        logger.info("%s contas a pagar em aberto extraídas para %s fornecedores.", len(contas), len(fornecedores))

        logger.info("Buscando notas de devolução já quitadas no período (por data de quitação)...")
        frame = navigation.abrir_consulta_contas_a_receber(page)
        navigation.aplicar_filtros_devolucao_quitadas(frame, data_de, data_ate)
        navigation.executar_consulta_sintetica_devolucao(frame)
        navigation.carregar_todas_paginas(frame, metodo="nextPageSintetica")
        quitadas_brutas = extrair_notas_quitadas(frame)
        _checar_total_informado(frame, len(quitadas_brutas), "Notas de devolução quitadas (período)", avisos)
        quitadas = [
            n for n in quitadas_brutas if not filtros.eh_intercompany(n.fornecedor, config.intercompany_nomes)
        ]
        logger.info("%s notas de devolução quitadas no período (excluindo intercompany).", len(quitadas))

        storage.salvar_cache(notas_pendentes, contas, quitadas)
        storage.salvar_ultima_atualizacao(datetime.now())
        return notas_pendentes, contas, quitadas, avisos
    finally:
        contexto.close()
        browser.close()
        playwright.stop()


def verificar_boletos_factoring(
    data_de: date,
    data_ate: date,
    headless: bool = False,
    progress_callback: Callable[[int, int], None] | None = None,
) -> tuple[list[BoletoFactoring], list[str]]:
    """Verifica, para todas as contas a pagar já QUITADAS entre `data_de` e `data_ate` (todas as
    lojas pagadoras, não só Matriz/Filial - diferente do resto do app), se o boleto foi pago de fato
    ao fornecedor cadastrado ou a uma factoring/financeira (lendo o anexo de cada duplicata, ver
    `factoring.py`). Duplicatas já verificadas em execuções anteriores são puladas - essa checagem
    é bem mais lenta que o sync normal (uma tela + download de anexo, às vezes OCR, por duplicata),
    então só o que for novo desde a última vez é processado. Pensado para ser rodado mês a mês (ou
    período curto) em vez de num intervalo longo de uma vez só - mais rápido de conferir aos poucos.
    Retorna também uma lista de avisos (não vazia se algum total extraído não bateu com o Bluesoft)."""
    janelas = factoring.dividir_em_janelas(data_de, data_ate)
    avisos: list[str] = []

    playwright, browser = auth.abrir_navegador(headless=headless)
    contexto = auth.criar_contexto(browser)
    page = contexto.new_page()

    try:
        auth.garantir_login(contexto, page)

        contas_por_chave: dict[str, ContaPagar] = {}
        for i, (janela_de, janela_ate) in enumerate(janelas, start=1):
            logger.info(
                "Buscando contas a pagar quitadas: janela %s/%s (%s a %s)...",
                i, len(janelas), janela_de, janela_ate,
            )
            frame = navigation.abrir_consulta_contas_a_pagar(page)
            navigation.aplicar_filtros_contas_a_pagar(
                frame,
                janela_de,
                janela_ate,
                quitado="Pagos",
                tipo_data="DATA_QUITACAO",
                forma_pagamento="Boleto Bancário",
                tipo_duplicata=["Recebimento de Mercadorias", "Recebimento de Mercadorias de Uso e Consumo"],
            )
            navigation.executar_consulta_analitica_contas_a_pagar(frame)
            navigation.carregar_todas_paginas(frame, metodo="nextPageAnalitica")
            contas_janela = extraction.extrair_contas_a_pagar(frame)
            _checar_total_informado(
                frame, len(contas_janela), f"Boletos quitados {janela_de} a {janela_ate}", avisos
            )
            for conta in contas_janela:
                contas_por_chave[conta.duplicata_key] = conta

        logger.info("%s contas a pagar quitadas encontradas no período (todas as lojas).", len(contas_por_chave))

        ja_verificadas = storage.chaves_ja_verificadas_factoring()
        pendentes = [c for chave, c in contas_por_chave.items() if chave not in ja_verificadas]
        logger.info(
            "%s já verificadas anteriormente, %s novas para checar.",
            len(contas_por_chave) - len(pendentes), len(pendentes),
        )

        resultados: list[BoletoFactoring] = []
        for indice, conta in enumerate(pendentes, start=1):
            resultado = factoring.verificar_duplicata(page, conta)
            storage.salvar_resultado_factoring(resultado)
            resultados.append(resultado)
            if progress_callback:
                progress_callback(indice, len(pendentes))
            if indice % 10 == 0 or indice == len(pendentes):
                logger.info("Boleto verificado: %s/%s.", indice, len(pendentes))

        return resultados, avisos
    finally:
        contexto.close()
        browser.close()
        playwright.stop()

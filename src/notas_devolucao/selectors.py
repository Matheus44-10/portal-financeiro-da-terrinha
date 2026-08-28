"""Locators centralizados para as telas do Bluesoft usadas por esta automação (somente leitura).

Consulta Contas a Pagar: mesma tela já validada no projeto irmão "Automação agrupamento" - reaproveitada
aqui apenas para leitura (sem manutenção/agrupamento).

Consulta Contas a Receber: validada ao vivo em sessão supervisionada para este projeto, ver
docs/mapeamento_ui.md.
"""

from __future__ import annotations

from playwright.sync_api import Frame

# --- Consulta Contas a Pagar (financeiro/pagamento/consultaContasAPagar/index.action) ---


def cap_campo_favorecido(frame: Frame):
    return frame.locator("#favorecidoNome")


def cap_campo_quitado(frame: Frame):
    return frame.locator("#quitado")


def cap_campo_periodo_de(frame: Frame):
    return frame.locator("#dataInicial")


def cap_campo_periodo_ate(frame: Frame):
    return frame.locator("#dataFinal")


def cap_botao_analitica(frame: Frame):
    return frame.locator("#buscaAnalitica")


# --- Consulta Contas a Receber (financeiro/cobranca/consultaContasAReceber/index.action) ---


def car_link_exibir_filtros_avancados(frame: Frame):
    # O campo "Tipo de duplicata" vive dentro deste painel recolhido por padrão - sem expandir
    # primeiro, interações com #tipoDuplicataCobrancaKeys não têm efeito real na busca mesmo
    # aparentando funcionar no DOM (confirmado ao vivo, ver docs/mapeamento_ui.md).
    return frame.get_by_text("Exibir/Esconder Filtros Avançados", exact=False)


def car_campo_pagos(frame: Frame):
    return frame.locator("#pagos")


def car_container_tipo_duplicata(frame: Frame):
    # Widget select2 (multi) - interagir via clique/digitação, não via select_option() direto no
    # <select> escondido por trás (confirmado ao vivo que select_option não gera resultados reais
    # de busca aqui, mesmo com force=True e o valor aparentando selecionado no DOM).
    return frame.locator("#s2id_tipoDuplicataCobrancaKeys")


def car_campo_busca_tipo_duplicata(frame: Frame):
    return frame.locator("#s2id_tipoDuplicataCobrancaKeys .select2-input")


def car_primeiro_resultado_select2(frame: Frame):
    return frame.locator("#select2-drop li.select2-result-selectable").first


def car_campo_periodo_de(frame: Frame):
    return frame.locator("#dataInicial")


def car_campo_periodo_ate(frame: Frame):
    return frame.locator("#dataFinal")


def car_botao_sintetica(frame: Frame):
    # A automação usa Sintética, não Analítica - nos testes ao vivo Analítica retornou 0 linhas
    # mesmo com filtros corretos e dados reais existentes (ver docs/mapeamento_ui.md).
    return frame.locator("#btnSintetica")

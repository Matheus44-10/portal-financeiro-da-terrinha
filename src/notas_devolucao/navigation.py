from __future__ import annotations

import logging
import re
from datetime import date

from playwright.sync_api import Frame, Page

from . import selectors
from .config import BLUESOFT_BASE_URL

logger = logging.getLogger("notas_devolucao.navigation")

URL_MENU_CENTRAL = f"{BLUESOFT_BASE_URL}/erp-app/areas/core/menu-central/menu-central.index.jsp"


def _aguardar_overlay(page: Page) -> None:
    page.get_by_text("AGUARDE...").wait_for(state="hidden", timeout=30_000)


def _abrir_consulta(page: Page, texto_link: str, trecho_url_frame: str) -> Frame:
    """Abre (ou reusa) uma aba do menu central e retorna o iframe onde a consulta vive - mesmo
    padrão de retry usado no projeto de agrupamento para a UI intermitentemente lenta do Bluesoft."""
    for tentativa in range(3):
        page.goto(URL_MENU_CENTRAL)
        _aguardar_overlay(page)
        try:
            page.get_by_text(texto_link, exact=False).first.click(timeout=30_000)
            break
        except Exception:
            if tentativa == 2:
                raise
            logger.warning("Falha ao clicar em '%s' (tentativa %s) - tentando de novo.", texto_link, tentativa + 1)
    _aguardar_overlay(page)
    page.wait_for_timeout(1000)

    frame = next(f for f in page.frames if trecho_url_frame in f.url)
    return frame


def abrir_consulta_contas_a_pagar(page: Page) -> Frame:
    return _abrir_consulta(page, "Consultar Contas a Pagar", "consultaContasAPagar")


def abrir_consulta_contas_a_receber(page: Page) -> Frame:
    return _abrir_consulta(page, "Consultar Contas a Receber", "consultaContasAReceber")


def aplicar_filtros_contas_a_pagar(
    frame: Frame,
    data_vencimento_de: date,
    data_vencimento_ate: date,
    favorecido: str | None = None,
    quitado: str = "Não Pagos",
    tipo_data: str | None = None,
    forma_pagamento: str | None = None,
    tipo_duplicata: list[str] | None = None,
) -> None:
    """O Bluesoft rejeita períodos de vencimento maiores que 31 dias, a menos que um favorecido seja
    informado (confirmado no projeto de agrupamento) - por isso um favorecido é sempre passado
    quando o período configurado for maior que isso (exceto na checagem de factoring, que quebra o
    período em janelas de até 31 dias pra poder deixar o favorecido em branco).

    `tipo_data` troca o que o período de datas realmente filtra (campo `#tipoDataDuplicata`, dentro
    de "Exibir/Esconder Filtros Avançados" - mesmo campo já usado em Contas a Receber). Por padrão
    o período filtra "Data de vencimento" (`None` = deixa o padrão do Bluesoft). Confirmado ao vivo
    que isso é necessário pra achar duplicatas já quitadas: várias não têm data de vencimento
    preenchida, então um filtro de período por vencimento nunca as acha - é preciso usar
    `tipo_data="DATA_QUITACAO"` pra filtrar pela data em que foram pagas de fato.

    `forma_pagamento` filtra por `#tipoFormaPagamentoKeys` (ex.: "Boleto Bancário") - importante na
    checagem de factoring: sem favorecido, Contas a Pagar inclui TODO tipo de pagamento da empresa
    (impostos, folha, financiamentos, etc.), não só boletos de fornecedor - confirmado ao vivo que
    isso inflava o resultado de umas centenas para quase 6 mil linhas num período de 25 dias.

    `tipo_duplicata` filtra por `#tipoDuplicataPagamentoKeys` (ex.: ["Recebimento de Mercadorias",
    "Recebimento de Mercadorias de Uso e Consumo"]) - reduz ainda mais o ruído de guias/impostos
    (GARE, GNRE, GPS, DARF etc.) que também são pagos via boleto mas nunca são objeto de factoring.
    Mesmo tipo de duplicata já usado como filtro de compra de fornecedor no projeto de Lançamento
    de notas. Confirmado ao vivo: reduziu de 972 para 274 linhas no mesmo período de teste."""
    selectors.cap_campo_quitado(frame).select_option(label=quitado)
    if favorecido:
        selectors.cap_campo_favorecido(frame).fill(favorecido)
        selectors.cap_campo_favorecido(frame).press("Tab")

    if tipo_data:
        link_avancado = frame.get_by_text("Exibir/Esconder Filtros Avançados", exact=False)
        if link_avancado.count() > 0 and link_avancado.first.is_visible():
            link_avancado.first.click()
            frame.wait_for_timeout(500)
        frame.locator("#tipoDataDuplicata").select_option(value=tipo_data, force=True)

    if forma_pagamento:
        frame.locator("#tipoFormaPagamentoKeys").select_option(label=forma_pagamento, force=True)

    if tipo_duplicata:
        frame.locator("#tipoDuplicataPagamentoKeys").select_option(label=tipo_duplicata, force=True)

    selectors.cap_campo_periodo_de(frame).fill(data_vencimento_de.strftime("%d/%m/%Y"))
    selectors.cap_campo_periodo_de(frame).press("Tab")
    selectors.cap_campo_periodo_ate(frame).fill(data_vencimento_ate.strftime("%d/%m/%Y"))
    selectors.cap_campo_periodo_ate(frame).press("Tab")


def executar_consulta_analitica_contas_a_pagar(frame: Frame) -> None:
    selectors.cap_botao_analitica(frame).click()
    frame.wait_for_load_state("networkidle")


def _selecionar_tipo_duplicata_devolucao(frame: Frame) -> None:
    container = selectors.car_container_tipo_duplicata(frame)
    campo_busca = selectors.car_campo_busca_tipo_duplicata(frame)
    for termo in ("Devolução de Compra", "Devolução de Trocas"):
        container.click()
        frame.wait_for_timeout(500)
        campo_busca.fill(termo)
        frame.wait_for_timeout(800)
        selectors.car_primeiro_resultado_select2(frame).click()
        frame.wait_for_timeout(500)


def aplicar_filtros_devolucao(frame: Frame, data_vencimento_de: date, data_vencimento_ate: date) -> None:
    link_avancado = selectors.car_link_exibir_filtros_avancados(frame)
    if link_avancado.count() > 0 and link_avancado.first.is_visible():
        link_avancado.first.click()
        frame.wait_for_timeout(500)

    selectors.car_campo_pagos(frame).select_option(label="Não Pagos")
    _selecionar_tipo_duplicata_devolucao(frame)
    selectors.car_campo_periodo_de(frame).fill(data_vencimento_de.strftime("%d/%m/%Y"))
    selectors.car_campo_periodo_de(frame).press("Tab")
    selectors.car_campo_periodo_ate(frame).fill(data_vencimento_ate.strftime("%d/%m/%Y"))
    selectors.car_campo_periodo_ate(frame).press("Tab")


def aplicar_filtros_devolucao_quitadas(frame: Frame, data_de: date, data_ate: date) -> None:
    """Variante de aplicar_filtros_devolucao para buscar notas já QUITADAS (usada no relatório
    mensal, não no app de pendências) - filtra pela Data de Quitação em vez de Vencimento."""
    link_avancado = selectors.car_link_exibir_filtros_avancados(frame)
    if link_avancado.count() > 0 and link_avancado.first.is_visible():
        link_avancado.first.click()
        frame.wait_for_timeout(500)

    selectors.car_campo_pagos(frame).select_option(label="Pagos")
    _selecionar_tipo_duplicata_devolucao(frame)
    frame.locator("#tipoDataDuplicata").select_option(value="DATA_QUITACAO", force=True)
    selectors.car_campo_periodo_de(frame).fill(data_de.strftime("%d/%m/%Y"))
    selectors.car_campo_periodo_de(frame).press("Tab")
    selectors.car_campo_periodo_ate(frame).fill(data_ate.strftime("%d/%m/%Y"))
    selectors.car_campo_periodo_ate(frame).press("Tab")


def executar_consulta_sintetica_devolucao(frame: Frame) -> None:
    selectors.car_botao_sintetica(frame).click()
    # Esperas generosas antes E depois de checar "networkidle" - confirmado ao vivo que sem essa
    # folga o carregamento intermitentemente não vence a tempo, mesmo com filtros corretos (a
    # extração então lê a tabela ainda vazia sem lançar nenhum erro).
    frame.wait_for_timeout(4000)
    try:
        frame.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:
        pass
    frame.wait_for_timeout(2000)


def fechar_modais_pendentes(page: Page) -> None:
    """Fecha qualquer modal que tenha ficado aberto (ex.: "Preview do Arquivo" de um anexo que é
    imagem em vez de PDF - o preview abre um modal em vez de disparar a API de URL assinada que
    `factoring.visualizar_e_capturar_anexo` espera).

    Necessário em dois pontos da checagem de factoring: (1) entre uma duplicata e outra - a tela é
    uma SPA com roteamento por hash (`#/financeiro/duplicata/{key}`), então trocar de duplicata na
    mesma aba não recarrega o documento de verdade e overlays globais não fecham sozinhos; (2)
    entre uma tentativa de ícone e outra na mesma duplicata, quando o anexo é imagem - o clique já
    abre o modal de preview e bloqueia o clique do próximo seletor candidato."""
    for _ in range(3):
        modal = page.locator(".modal.in").first
        if modal.count() == 0:
            return
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)


_PADRAO_ABATIMENTO = re.compile(r"abatimento da duplicata de pagamento\D*(\d+)")


def verificar_vinculo_pagamento(page: Page, duplicata_key: str) -> str | None:
    """Abre a tela de detalhe da duplicata de cobrança e verifica se o Bluesoft já a marca como
    abatimento de uma duplicata de pagamento - só aparece nesse detalhe, não na lista em massa
    (confirmado ao vivo: é caso a caso, não depende do tipo Compra/Trocas)."""
    url = f"{BLUESOFT_BASE_URL}/financeiro/cobranca/duplicataCobranca/editar.action?duplicataKey={duplicata_key}"
    page.goto(url)
    page.wait_for_timeout(1500)
    texto = page.evaluate("() => document.body.innerText")
    resultado = _PADRAO_ABATIMENTO.search(texto)
    return resultado.group(1) if resultado else None


_PADRAO_FATURA_KEY = re.compile(r"financeiro/fatura/index\.action\?faturaKey=(\d+)")
_PADRAO_NF_KEY = re.compile(r"consultaNotaFiscal/visualizar\.action\?nfKey=(\d+)")


def obter_fatura_key(page: Page) -> str | None:
    """Lê o número da fatura vinculada à duplicata de cobrança atualmente aberta na página (a
    mesma tela já carregada por `verificar_vinculo_pagamento` - não precisa de navegação extra)."""
    resultado = _PADRAO_FATURA_KEY.search(page.content())
    return resultado.group(1) if resultado else None


def obter_produtos_da_fatura(page: Page, fatura_key: str) -> list[str]:
    """Abre a fatura e cada nota fiscal vinculada a ela para ler a descrição dos itens (produtos)
    na tabela `#itens` ("Itens da nota fiscal") - HTML estático, sem precisar de scope Angular.
    Uma fatura pode ter mais de uma nota fiscal, e cada nota fiscal mais de um item."""
    page.goto(f"{BLUESOFT_BASE_URL}/financeiro/fatura/index.action?faturaKey={fatura_key}")
    page.wait_for_timeout(1000)
    nf_keys = dict.fromkeys(_PADRAO_NF_KEY.findall(page.content()))

    produtos: list[str] = []
    for nf_key in nf_keys:
        page.goto(f"{BLUESOFT_BASE_URL}/comercial/consultaNotaFiscal/visualizar.action?nfKey={nf_key}")
        page.wait_for_timeout(1000)
        for celula in page.locator("table#itens tbody td.truncate").all():
            descricao = (celula.get_attribute("title") or "").strip()
            if descricao and descricao not in produtos:
                produtos.append(descricao)
    return produtos


_PADRAO_LOJA_PAGADORA = re.compile(r"Loja pagadora\s*\n(.+?)\n")


def obter_abatimentos_duplicata_pagamento(page: Page, duplicata_pagamento_key: str) -> dict:
    """Abre a tela (Angular moderno, não a legada AngularJS/.action) de edição da duplicata de
    pagamento e lê a aba "Abatimentos" - lista cada duplicata de cobrança (nota de devolução) usada
    nesse abatimento, com o valor líquido exato abatido (pode ser parcial) e a loja recebedora.
    Continua sendo AngularJS por baixo (window.angular existe) mas a grade usa ui-grid virtualizado
    (ng-repeat sobre rowContainer.renderedRows, dado real em scope.row.entity). Também devolve o
    texto da "Loja pagadora" (aba Geral, carregada antes de trocar de aba) de graça, sem visita extra."""
    url = f"{BLUESOFT_BASE_URL}/erp-app/areas/financeiro/duplicata/index.action#/financeiro/duplicata/{duplicata_pagamento_key}"
    page.goto(url)
    page.wait_for_timeout(3000)

    texto_geral = page.evaluate("() => document.body.innerText")
    loja_pagadora_match = _PADRAO_LOJA_PAGADORA.search(texto_geral)
    loja_pagadora = loja_pagadora_match.group(1).strip() if loja_pagadora_match else None

    page.get_by_text("Abatimentos", exact=True).first.click()
    page.wait_for_timeout(2500)

    linhas = page.evaluate(
        """
        () => {
            const linhas = document.querySelectorAll('[ng-repeat*="rowContainer.renderedRows"]');
            const resultados = [];
            for (const el of linhas) {
                try {
                    const scope = angular.element(el).scope();
                    if (scope && scope.row && scope.row.entity && scope.row.entity.duplicataKey) {
                        resultados.push(scope.row.entity);
                    }
                } catch (e) {}
            }
            return resultados;
        }
        """
    )
    return {"loja_pagadora_nome": loja_pagadora, "linhas": linhas}


def carregar_todas_paginas(frame: Frame, max_paginas: int = 60, metodo: str | None = None) -> None:
    """Tanto a Analítica de Contas a Pagar quanto a Sintética de Contas a Receber paginam via
    infinite-scroll do AngularJS (100 em 100). O nome exato do método de "próxima página" no
    controller varia por tela - por padrão este helper descobre o método certo em vez de fixar um
    nome, e procura tanto em `scope.vm` (Contas a Pagar) quanto direto em `scope` (Contas a
    Receber - `ConsultaContasAReceber` não expõe `vm` nenhum, `nextPageSintetica` e
    `todosResultadosCarregados` ficam direto no scope).

    **Bug real confirmado ao vivo (2026-08-14):** antes de checar `scope` como fallback, esta
    função só olhava `scope.vm` - como esse controller não tem `vm`, a checagem `!scope.vm`
    batia na primeira iteração e a função devolvia "já carregado" sem paginar nada, silenciosamente
    truncando a extração de Devolução (pendente e quitada) na primeira leva de ~100-124 linhas
    renderizadas. Só não tinha sido percebido porque a quantidade de notas pendentes sempre ficou
    abaixo desse limite; a busca de quitadas com 215 duplicatas no período expôs o problema (só
    100 eram extraídas, sempre as mesmas, cortando qualquer coisa mais recente que ~junho/2026).

    `metodo`, se informado, força qual método usar. Necessário porque a tela de Contas a Pagar
    Analítica com resultado grande expõe MAIS DE UM candidato `nextPage*` no mesmo `vm`
    (`nextPageSintetica`, `nextPageAnalitica`, `nextPageSinteticaAgrupada` - confirmado ao vivo) -
    "pegar o primeiro que bater" escolhia o método errado (da Sintética, não da Analítica), o que
    não paginava de verdade e ainda zerava os resultados já carregados. Passe `metodo="nextPageAnalitica"`
    explicitamente sempre que a tela puder ter mais de 100 resultados na Analítica."""
    for _ in range(max_paginas):
        estado = frame.evaluate(
            """
            (metodoForcado) => {
                const el = document.querySelector('[ng-controller]');
                if (!el) return {carregado: true, metodo: null};
                const scope = angular.element(el).scope();
                if (!scope) return {carregado: true, metodo: null};
                const alvo = scope.vm || scope;
                if (alvo.todosResultadosCarregados) return {carregado: true, metodo: null};
                if (metodoForcado && typeof alvo[metodoForcado] === 'function') {
                    return {carregado: false, metodo: metodoForcado};
                }
                const metodo = Object.keys(alvo).find(k => /^nextPage/.test(k) && typeof alvo[k] === 'function');
                return {carregado: false, metodo: metodo || null};
            }
            """,
            metodo,
        )
        if estado["carregado"] or not estado["metodo"]:
            return
        frame.evaluate(
            """
            (metodo) => {
                const el = document.querySelector('[ng-controller]');
                const scope = angular.element(el).scope();
                const alvo = scope.vm || scope;
                scope.$apply(function () { alvo[metodo](); });
            }
            """,
            estado["metodo"],
        )
        frame.wait_for_timeout(1000)
        frame.wait_for_load_state("networkidle")


_PADRAO_TOTAL_INFORMADO = re.compile(r"Total\s+([\d.]+)\s+duplicatas?", re.IGNORECASE)


def obter_total_informado(frame: Frame) -> int | None:
    """Lê o total de duplicatas que o próprio Bluesoft informa no rodapé da grade (linha "Total X
    duplicatas ..."), pra comparar com o que a extração realmente trouxe. Existe pra detectar
    automaticamente o mesmo tipo de bug de paginação incompleta já confirmado ao vivo em 2026-08-14
    (extração parava em ~100-124 linhas mesmo quando o Bluesoft tinha 215) - sem essa checagem,
    esse tipo de corte silencioso só aparece se alguém notar um número "baixo demais" por conta
    própria. Retorna None se o rodapé não tiver esse texto (algumas telas não mostram)."""
    texto = frame.evaluate("() => document.body.innerText")
    encontrado = _PADRAO_TOTAL_INFORMADO.search(texto)
    if not encontrado:
        return None
    return int(encontrado.group(1).replace(".", ""))

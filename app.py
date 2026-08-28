from __future__ import annotations

import base64
import io
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

import altair as alt
import pandas as pd
import streamlit as st
from st_aggrid import AgGrid, ColumnsAutoSizeMode, DataReturnMode, GridOptionsBuilder, GridUpdateMode
from st_aggrid.shared import JsCode

from notas_devolucao import storage
from notas_devolucao.antecipacao import Antecipacao, carregar_antecipacoes
from notas_devolucao.config import carregar_configuracao
from notas_devolucao.email_cobranca import abrir_no_outlook
from notas_devolucao.login import exigir_login
from notas_devolucao.models import BoletoFactoring, ContaPagar, NotaDevolucao, Vinculo
from notas_devolucao.relatorio_abatimento import NotaQuitada
from notas_devolucao.sincronizacao import atualizar_dados, verificar_boletos_factoring

LOGO_PATH = Path(__file__).resolve().parent / "assets" / "logo_daterrinha.png"

TITULO_APP = "Portal Financeiro Da Terrinha"

st.set_page_config(page_title=TITULO_APP, page_icon=str(LOGO_PATH), layout="wide")
storage.inicializar()
st.logo(str(LOGO_PATH), size="large")


def _agrupar_por_fornecedor(
    notas: list[NotaDevolucao], contas: list[ContaPagar], quitadas: list[NotaQuitada]
) -> dict[str, dict]:
    grupos: dict[str, dict] = {}

    for nota in notas:
        cnpj = nota.fornecedor_cnpj
        grupo = grupos.setdefault(cnpj, {"nome": nota.fornecedor, "notas": [], "contas": [], "quitadas": []})
        grupo["notas"].append(nota)

    for conta in contas:
        cnpj = conta.favorecido_cnpj
        grupo = grupos.setdefault(cnpj, {"nome": conta.favorecido, "notas": [], "contas": [], "quitadas": []})
        grupo["contas"].append(conta)

    for quitada in quitadas:
        cnpj = quitada.fornecedor_cnpj
        grupo = grupos.setdefault(cnpj, {"nome": quitada.fornecedor, "notas": [], "contas": [], "quitadas": []})
        grupo["quitadas"].append(quitada)

    return grupos


def _tabela_notas(notas: list[NotaDevolucao], vinculos_por_nota: dict[str, list[Vinculo]]):
    """Consolida por Nº do documento (NF) - o Bluesoft desmembra uma nota de devolução em várias
    duplicatas/parcelas conforme vai abatendo parcialmente, então sem isso a mesma devolução
    aparecia várias vezes na tabela e dava a falsa impressão de mais notas em aberto do que
    realmente há."""
    grupos_por_nf: dict[str, list[NotaDevolucao]] = {}
    for n in notas:
        grupos_por_nf.setdefault(n.numero_documento, []).append(n)

    linhas = [
        {
            "Nº NF": numero_documento,
            "Tipo": parcelas[0].tipo,
            "Valor líquido": sum(p.valor_liquido for p in parcelas),
            "Vencimento mais próximo": min(p.data_vencimento for p in parcelas).strftime("%d/%m/%Y"),
            "Parcelas": len(parcelas),
            "Produto": parcelas[0].produtos or "",
            "Descritivo": parcelas[0].descritivo,
            "Vínculos": sum(len(vinculos_por_nota.get(p.duplicata_key, [])) for p in parcelas),
        }
        for numero_documento, parcelas in grupos_por_nf.items()
    ]
    df = pd.DataFrame(sorted(linhas, key=lambda l: l["Valor líquido"], reverse=True))
    return df.style.format({"Valor líquido": "R$ {:,.2f}"})


def _formatar_moeda_br(valor: float) -> str:
    """`f"{valor:,.2f}"` usa separador americano (39,680.00) - troca pra separador de milhar com
    ponto e decimal com vírgula (39.680,00), formato que o financeiro brasileiro espera."""
    texto = f"{valor:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
    return f"R$ {texto}"


def _resumo_por_produto(notas: list[NotaDevolucao]) -> pd.DataFrame:
    """Agrupa as notas pendentes por produto (uma nota com mais de um item conta em cada um) -
    dá pra ver de cara quanto está pendente por produto, independente do fornecedor. "Qtde. de
    notas" conta por NF (fornecedor + nº do documento), não por parcela - mesmo motivo da
    consolidação em `_tabela_notas` (o Bluesoft desmembra a mesma nota em várias duplicatas).
    Retorna DataFrame puro (sem Styler) porque a tabela usa seleção de linha (`on_select`), que
    exige acesso à linha por posição."""
    resumo: dict[str, dict] = {}
    for n in notas:
        produtos = [p.strip() for p in (n.produtos or "").split("; ") if p.strip()] or ["Sem produto identificado"]
        for produto in produtos:
            item = resumo.setdefault(
                produto, {"Produto": produto, "notas_unicas": set(), "Valor pendente": 0.0}
            )
            item["notas_unicas"].add((n.fornecedor_cnpj, n.numero_documento))
            item["Valor pendente"] += n.valor_liquido
    linhas = sorted(
        (
            {"Produto": item["Produto"], "Qtde. de notas": len(item["notas_unicas"]), "Valor pendente": item["Valor pendente"]}
            for item in resumo.values()
        ),
        key=lambda r: r["Valor pendente"],
        reverse=True,
    )
    for linha in linhas:
        linha["Valor pendente"] = _formatar_moeda_br(linha["Valor pendente"])
    return pd.DataFrame(linhas)


def _fornecedores_do_produto(notas: list[NotaDevolucao], produto: str) -> list[str]:
    return sorted(
        {
            n.fornecedor.strip()
            for n in notas
            if n.produtos and produto in {p.strip() for p in n.produtos.split("; ")}
        }
    )


def _produtos_recorrentes(notas: list[NotaDevolucao]) -> list[tuple[str, int, int]]:
    """Produtos que aparecem em mais de um mês distinto entre as notas do fornecedor - sinal de
    possível problema recorrente (não só atraso de cobrança pontual). Retorna
    (produto, qtde de notas, qtde de meses distintos), ordenado pelo mais recorrente."""
    meses_por_produto: dict[str, set[tuple[int, int]]] = {}
    qtd_por_produto: dict[str, int] = {}
    for n in notas:
        if not n.produtos:
            continue
        for produto in {p.strip() for p in n.produtos.split("; ") if p.strip()}:
            meses_por_produto.setdefault(produto, set()).add((n.data_emissao.year, n.data_emissao.month))
            qtd_por_produto[produto] = qtd_por_produto.get(produto, 0) + 1
    return sorted(
        (
            (produto, qtd_por_produto[produto], len(meses))
            for produto, meses in meses_por_produto.items()
            if len(meses) >= 2
        ),
        key=lambda item: item[2],
        reverse=True,
    )


def _tabela_contas(contas: list[ContaPagar]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Nº duplicata": c.duplicata_key,
                "Valor": c.valor,
                "Vencimento": c.data_vencimento.strftime("%d/%m/%Y"),
                "Nota fiscal": c.nota_fiscal,
                "Descritivo": c.descritivo,
            }
            for c in contas
        ]
    )


def _tabela_notas_vinculadas(notas: list[NotaDevolucao]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Nº duplicata": n.duplicata_key,
                "Valor líquido": n.valor_liquido,
                "Vencimento": n.data_vencimento.strftime("%d/%m/%Y"),
                "Duplicata de pagamento vinculada": n.duplicata_pagamento_vinculada,
            }
            for n in notas
        ]
    )


def _ranking_fornecedores(grupos: dict) -> pd.DataFrame:
    """Um fornecedor 'gerou' uma devolução esteja ela pendente, já vinculada ou já quitada -
    por isso soma as três listas do grupo, não só as pendentes (que é o que as métricas do topo
    e a lista de expanders mostram)."""
    linhas = [
        {
            "Fornecedor": grupo["nome"].strip(),
            "CNPJ": cnpj,
            "Qtde. de notas": len(grupo["notas"]) + len(grupo["quitadas"]),
            "Valor total de devolução": (
                sum(n.valor_liquido for n in grupo["notas"]) + sum(q.valor_liquido for q in grupo["quitadas"])
            ),
        }
        for cnpj, grupo in grupos.items()
        if grupo["notas"] or grupo["quitadas"]
    ]
    return pd.DataFrame(linhas)


_STATUS_FACTORING_LABELS = {
    "factoring": "Factoring",
    "normal": "Normal",
    "nao_verificado": "Não verificado",
    "ignorado": "Ignorado (não é boleto)",
}

# Estilo padrão do número exibido em cima de cada coluna nos gráficos de barra do portal - mesmo
# padrão (tamanho, negrito, cor escura pra contraste no fundo claro) em todos eles.
_ROTULO_VALOR_BARRA = dict(dy=-10, fontSize=14, fontWeight="bold", color="#1A1A1A")


def _cor_status_factoring(status_label: str) -> str:
    if status_label == "Factoring":
        return "background-color: #FDE2D0; color: #9C4300; font-weight: bold"
    if status_label == "Normal":
        return "background-color: #DCEEDC; color: #2E7D32"
    if status_label.startswith("Ignorado"):
        return "background-color: #E8E8E8; color: #888888; font-style: italic"
    return "background-color: #F0F0F0; color: #555555"


def _tabela_boletos(boletos: list[BoletoFactoring]):
    df = pd.DataFrame(
        [
            {
                "Status": _STATUS_FACTORING_LABELS.get(b.status, b.status),
                "Fornecedor cadastrado": b.favorecido.strip(),
                "CNPJ cadastrado": b.favorecido_cnpj,
                "Loja pagadora": b.loja_pagadora,
                "Pago via EDI": "Sim" if b.pago_via_edi else "Não",
                "CNPJ do beneficiário (boleto)": ", ".join(b.cnpjs_encontrados),
                "Trecho do boleto": b.trecho_beneficiario,
                "Valor": b.valor,
                "Quitado em": b.data_quitacao.strftime("%d/%m/%Y") if b.data_quitacao else "?",
                "Vencimento": b.data_vencimento.strftime("%d/%m/%Y"),
                "Nº duplicata": b.duplicata_key,
                "Nota fiscal": b.nota_fiscal,
            }
            for b in boletos
        ]
    )
    return df.style.map(_cor_status_factoring, subset=["Status"]).format({"Valor": "R$ {:,.2f}"})


def _tabela_ranking_fornecedores_factoring(boletos: list[BoletoFactoring]) -> pd.DataFrame:
    """Agrupa boletos de factoring por fornecedor cadastrado - responde "quais fornecedores tiveram
    boletos pagos por factoring", já que a tabela detalhada (_tabela_boletos) repete o fornecedor
    uma vez por duplicata."""
    if not boletos:
        return pd.DataFrame()
    df = pd.DataFrame([{"Fornecedor": b.favorecido.strip(), "Valor": b.valor} for b in boletos])
    return (
        df.groupby("Fornecedor")
        .agg(**{"Qtde. de boletos": ("Valor", "count"), "Valor total": ("Valor", "sum")})
        .reset_index()
        .sort_values("Valor total", ascending=False)
    )


def _tabela_tendencia_factoring(boletos: list[BoletoFactoring]) -> pd.DataFrame:
    if not boletos:
        return pd.DataFrame()
    linhas = [
        {
            "Mês": (b.data_quitacao or b.data_vencimento).strftime("%m/%Y"),
            "_ordem": (b.data_quitacao or b.data_vencimento).replace(day=1),
            "status": b.status,
        }
        for b in boletos
    ]
    df = pd.DataFrame(linhas)
    agrupado = (
        df.groupby(["Mês", "_ordem"])
        .agg(**{
            "Total analisados": ("status", "count"),
            "Factoring": ("status", lambda s: (s == "factoring").sum()),
        })
        .reset_index()
        .sort_values("_ordem")
        .drop(columns="_ordem")
    )
    return agrupado


def _tabela_mensal_antecipacao(itens: list[Antecipacao]) -> pd.DataFrame:
    if not itens:
        return pd.DataFrame()
    linhas = [
        {
            "Mês": i.data_vencimento.strftime("%m/%Y"),
            "_ordem": i.data_vencimento.replace(day=1),
            "Valor líquido": i.valor_liquido,
            "Desconto financeiro": i.desconto_financeiro,
        }
        for i in itens
    ]
    df = pd.DataFrame(linhas)
    agrupado = (
        df.groupby(["Mês", "_ordem"])
        .agg(**{
            "Valor líquido": ("Valor líquido", "sum"),
            "Desconto financeiro": ("Desconto financeiro", "sum"),
        })
        .reset_index()
        .sort_values("_ordem")
        .drop(columns="_ordem")
    )
    return agrupado


def _tabela_ranking_antecipacao(itens: list[Antecipacao]) -> pd.DataFrame:
    if not itens:
        return pd.DataFrame()
    linhas = [
        {
            "Fornecedor": i.fornecedor,
            "Valor líquido": i.valor_liquido,
            "Desconto financeiro": i.desconto_financeiro,
            "% ganho": i.ganho_percentual,
        }
        for i in itens
    ]
    df = pd.DataFrame(linhas)
    agrupado = (
        df.groupby("Fornecedor")
        .agg(**{
            "Qtde. de duplicatas": ("Fornecedor", "count"),
            "Valor líquido": ("Valor líquido", "sum"),
            "Desconto financeiro": ("Desconto financeiro", "sum"),
            "% ganho médio": ("% ganho", "mean"),
        })
        .reset_index()
        .sort_values("Desconto financeiro", ascending=False)
    )
    return agrupado


def _gerar_excel_antecipacao(itens: list[Antecipacao]) -> bytes:
    df = pd.DataFrame(
        [
            {
                "Fornecedor": i.fornecedor,
                "Loja pagadora": i.loja_pagadora,
                "Nº duplicata": i.duplicata_key,
                "Vencimento": i.data_vencimento,
                "Valor líquido": i.valor_liquido,
                "Desconto financeiro": i.desconto_financeiro,
                "% ganho": i.ganho_percentual,
            }
            for i in itens
        ]
    )
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Antecipação de Fornecedores", index=False)
    return buffer.getvalue()


def _gerar_excel_factoring(boletos: list[BoletoFactoring]) -> bytes:
    df = pd.DataFrame(
        [
            {
                "Status": _STATUS_FACTORING_LABELS.get(b.status, b.status),
                "Fornecedor cadastrado": b.favorecido.strip(),
                "CNPJ cadastrado": b.favorecido_cnpj,
                "Loja pagadora": b.loja_pagadora,
                "Pago via EDI": "Sim" if b.pago_via_edi else "Não",
                "CNPJ do beneficiário (boleto)": ", ".join(b.cnpjs_encontrados),
                "Trecho do boleto": b.trecho_beneficiario,
                "Valor": b.valor,
                "Quitado em": b.data_quitacao,
                "Vencimento": b.data_vencimento,
                "Nº duplicata": b.duplicata_key,
                "Nota fiscal": b.nota_fiscal,
            }
            for b in boletos
        ]
    )
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Boletos Factoring", index=False)
    return buffer.getvalue()


def _gerar_excel(notas: list[NotaDevolucao], contas: list[ContaPagar]) -> bytes:
    df_notas = pd.DataFrame(
        [
            {
                "Fornecedor": n.fornecedor.strip(),
                "CNPJ": n.fornecedor_cnpj,
                "Loja": n.loja_nome or n.loja,
                "Nº duplicata": n.duplicata_key,
                "Tipo": n.tipo,
                "Valor líquido": n.valor_liquido,
                "Vencimento": n.data_vencimento,
                "Dias de atraso": n.dias_de_atraso,
                "Produto": n.produtos or "",
                "Descritivo": n.descritivo,
                "Duplicata de pagamento vinculada": n.duplicata_pagamento_vinculada or "",
            }
            for n in notas
        ]
    )
    df_contas = pd.DataFrame(
        [
            {
                "Favorecido": c.favorecido.strip(),
                "CNPJ": c.favorecido_cnpj,
                "Nº duplicata": c.duplicata_key,
                "Valor": c.valor,
                "Vencimento": c.data_vencimento,
                "Nota fiscal": c.nota_fiscal,
                "Descritivo": c.descritivo,
            }
            for c in contas
        ]
    )
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df_notas.to_excel(writer, sheet_name="Notas de Devolução", index=False)
        df_contas.to_excel(writer, sheet_name="Contas a Pagar", index=False)
    return buffer.getvalue()


_LOGO_B64 = base64.b64encode(LOGO_PATH.read_bytes()).decode()

st.markdown(
    f"""
    <style>
    .terrinha-header {{
        background: linear-gradient(135deg, #EE6931 0%, #CA592A 100%);
        padding: 1.2rem 2rem;
        border-radius: 12px;
        display: flex;
        align-items: center;
        gap: 1.2rem;
        margin-bottom: 1.5rem;
        box-shadow: 0 6px 16px rgba(202, 89, 42, 0.25);
    }}
    .terrinha-header img {{
        height: 64px;
    }}
    .terrinha-header h1 {{
        color: #FFFFFF;
        font-size: 1.5rem;
        margin: 0;
        line-height: 1.3;
    }}
    div[data-testid="stExpander"] {{
        border: 1px solid #F0D9CE;
        border-left: 5px solid #EE6931;
        border-radius: 10px;
    }}
    div[data-testid="stMetric"] {{
        background-color: #FFFFFF;
        border-top: 3px solid #EE6931;
        border-radius: 10px;
        padding: 1rem 1.2rem;
        box-shadow: 0 2px 8px rgba(26, 26, 26, 0.08);
    }}
    div[data-testid="stMetricValue"] {{
        font-size: 1.6rem;
        font-weight: 700;
    }}
    hr {{
        border: none;
        border-top: 1px solid #F0D9CE;
        margin: 1.4rem 0;
    }}
    </style>
    <div class="terrinha-header">
        <img src="data:image/png;base64,{_LOGO_B64}" />
        <h1>{TITULO_APP}</h1>
    </div>
    """,
    unsafe_allow_html=True,
)

config = carregar_configuracao()

# A página "Antecipação de Fornecedores" (url_path="antecipacao" no st.navigation lá embaixo) fica
# liberada sem login, a pedido do usuário - só mostra dados de uma planilha, sem nenhuma ação real
# (não mexe no Bluesoft nem grava nada), então dá pra compartilhar o link com alguém da mesma rede
# sem senha. Todas as outras páginas continuam exigindo login normalmente.
_caminho_pagina_atual = urlparse(st.context.url).path.strip("/").lower()
if _caminho_pagina_atual != "antecipacao":
    exigir_login(config)


def _barra_lateral_atualizar_dados():
    """Botão de sincronização com o Bluesoft (notas de devolução + contas a pagar) - usado só
    pelas páginas Fornecedores e Dashboard, que dependem dessa base. Factoring tem seu próprio
    botão de verificação, e a Home é só um resumo, então não mostram isso."""
    with st.sidebar:
        st.header("Dados do Bluesoft")
        ultima_atualizacao = storage.carregar_ultima_atualizacao()
        if ultima_atualizacao:
            st.caption(f"🕓 Última atualização: {ultima_atualizacao.strftime('%d/%m/%Y às %H:%M')}")
        if st.button(
            "Atualizar dados do Bluesoft",
            icon=":material/sync:",
            width='stretch',
            type="primary",
        ):
            with st.spinner(
                "Conectando ao Bluesoft... uma janela do navegador pode abrir pedindo login manual."
            ):
                notas, contas, quitadas, avisos_sync = atualizar_dados(config)
            st.session_state["avisos_sync"] = avisos_sync
            st.session_state["mensagem_sync"] = (
                f"{len(notas)} notas de devolução, {len(contas)} contas a pagar e "
                f"{len(quitadas)} notas quitadas atualizadas."
            )
            st.rerun()

        for aviso in st.session_state.get("avisos_sync", []):
            st.warning(f"⚠️ {aviso}")
        if st.session_state.get("mensagem_sync"):
            st.success(st.session_state.pop("mensagem_sync"))

def _preparar_dados_devolucao():
    """Carrega e filtra (Loja + Vencimento) os dados de devolução/contas a pagar - usado só pelas
    páginas Fornecedores e Dashboard. A página Factoring não chama isso: ela tem escopo e filtros
    próprios (todas as lojas pagadoras, período independente), então não deve mostrar esses widgets."""
    notas = storage.carregar_notas_devolucao()
    contas = storage.carregar_contas_pagar()
    quitadas = storage.carregar_notas_devolucao_quitadas()
    vinculos = storage.carregar_vinculos()
    emails_fornecedor = storage.carregar_emails_fornecedor()

    # Escopo do site restrito às lojas Da Terrinha Matriz e Filial SP - demais lojas do Bluesoft (2JM,
    # Terrafec, Okker etc.) ficam de fora, a pedido do usuário.
    LOJAS_PERMITIDAS = {1, 14}
    notas = [n for n in notas if n.loja in LOJAS_PERMITIDAS]
    contas = [c for c in contas if c.loja in LOJAS_PERMITIDAS]
    quitadas = [q for q in quitadas if q.loja in LOJAS_PERMITIDAS]

    if not notas and not contas:
        st.info("Nenhum dado carregado ainda. Clique em **Atualizar dados do Bluesoft** na barra lateral.")
        st.stop()

    nome_da_loja: dict[int, str] = {n.loja: n.loja_nome for n in notas if n.loja_nome}

    opcoes_loja = {"Todas": None}
    for numero in sorted({n.loja for n in notas} | {c.loja for c in contas} | {q.loja for q in quitadas}):
        rotulo = f"{numero} — {nome_da_loja[numero]}" if numero in nome_da_loja else f"Loja {numero}"
        opcoes_loja[rotulo] = numero

    col_loja, col_produto, col_data_de, col_data_ate = st.columns([2, 2, 1, 1])

    with col_loja:
        loja_escolhida = st.selectbox("Loja", options=list(opcoes_loja.keys()), key="devolucao_loja")
    numero_loja_escolhida = opcoes_loja[loja_escolhida]

    if numero_loja_escolhida is not None:
        notas = [n for n in notas if n.loja == numero_loja_escolhida]
        contas = [c for c in contas if c.loja == numero_loja_escolhida]
        quitadas = [q for q in quitadas if q.loja == numero_loja_escolhida]

    with col_data_de:
        data_de = st.date_input(
            "Vencimento de",
            value=config.data_inicial,
            min_value=config.data_inicial,
            max_value=config.data_final,
            format="DD/MM/YYYY",
            key="devolucao_data_de",
        )
    with col_data_ate:
        data_ate = st.date_input(
            "Vencimento até",
            value=config.data_final,
            min_value=config.data_inicial,
            max_value=config.data_final,
            format="DD/MM/YYYY",
            key="devolucao_data_ate",
        )

    notas = [n for n in notas if data_de <= n.data_vencimento <= data_ate]
    contas = [c for c in contas if data_de <= c.data_vencimento <= data_ate]
    # Quitadas usam a mesma faixa de datas do filtro acima, mas aplicada à data de quitação (não de
    # vencimento) - é o período em que elas já foram buscadas do Bluesoft durante o sync.
    quitadas = [q for q in quitadas if data_de <= q.data_quitacao <= data_ate]

    # Filtro por produto - uma nota pode ter mais de um item ("; " no campo produtos), então as
    # opções e o filtro comparam contra cada item separadamente, não a string inteira.
    produtos_disponiveis = sorted(
        {p.strip() for n in notas if n.produtos for p in n.produtos.split("; ") if p.strip()}
    )
    with col_produto:
        produto_escolhido = st.selectbox(
            "Produto", options=["Todos"] + produtos_disponiveis, key="devolucao_produto"
        )
    if produto_escolhido != "Todos":
        notas = [
            n
            for n in notas
            if n.produtos and produto_escolhido in {p.strip() for p in n.produtos.split("; ")}
        ]

    # Notas que o próprio Bluesoft já marca como abatimento de uma duplicata de pagamento (visto na
    # tela de detalhe de cada uma) não são mais pendência de cobrança de verdade - ficam à parte, só
    # para consulta, em vez de contar no total/quantidade principal.
    notas_pendentes_reais = [n for n in notas if not n.duplicata_pagamento_vinculada]

    vinculos_por_nota: dict[str, list[Vinculo]] = {}
    for v in vinculos:
        vinculos_por_nota.setdefault(v.nota_devolucao_key, []).append(v)

    grupos = _agrupar_por_fornecedor(notas, contas, quitadas)
    # Só mostra fornecedores com alguma devolução pendente de verdade - um fornecedor com apenas
    # contas a pagar (sem devolução em aberto) não é uma pendência de cobrança e fica de fora da lista.
    grupos_com_pendencia = {
        cnpj: grupo
        for cnpj, grupo in grupos.items()
        if any(not n.duplicata_pagamento_vinculada for n in grupo["notas"])
    }
    grupos_ordenados = sorted(
        grupos_com_pendencia.items(),
        key=lambda item: sum(n.valor_liquido for n in item[1]["notas"] if not n.duplicata_pagamento_vinculada),
        reverse=True,
    )

    return {
        "notas": notas,
        "contas": contas,
        "vinculos": vinculos,
        "emails_fornecedor": emails_fornecedor,
        "notas_pendentes_reais": notas_pendentes_reais,
        "vinculos_por_nota": vinculos_por_nota,
        "grupos": grupos,
        "grupos_ordenados": grupos_ordenados,
        "total_pendente": sum(n.valor_liquido for n in notas_pendentes_reais),
        "total_quitado": sum(q.valor_liquido for q in quitadas),
    }


def _resumo_devolucao():
    """Versão sem os widgets de filtro de _preparar_dados_devolucao - usada só na Home pra um
    resumo rápido (lojas Matriz/Filial SP, período completo configurado), sem exigir escolha."""
    notas = storage.carregar_notas_devolucao()
    contas = storage.carregar_contas_pagar()
    quitadas = storage.carregar_notas_devolucao_quitadas()

    LOJAS_PERMITIDAS = {1, 14}
    notas = [n for n in notas if n.loja in LOJAS_PERMITIDAS]
    contas = [c for c in contas if c.loja in LOJAS_PERMITIDAS]
    quitadas = [q for q in quitadas if q.loja in LOJAS_PERMITIDAS]

    notas = [n for n in notas if config.data_inicial <= n.data_vencimento <= config.data_final]
    contas = [c for c in contas if config.data_inicial <= c.data_vencimento <= config.data_final]
    quitadas = [q for q in quitadas if config.data_inicial <= q.data_quitacao <= config.data_final]

    notas_pendentes_reais = [n for n in notas if not n.duplicata_pagamento_vinculada]
    grupos = _agrupar_por_fornecedor(notas, contas, quitadas)

    return {
        "total_pendente": sum(n.valor_liquido for n in notas_pendentes_reais),
        "total_quitado": sum(q.valor_liquido for q in quitadas),
        # Conta por NF (fornecedor + nº do documento), não por parcela - mesmo ajuste feito na
        # página Fornecedores, pra não inflar o número por causa do desmembramento do Bluesoft.
        "qtd_pendentes": len({(n.fornecedor_cnpj, n.numero_documento) for n in notas_pendentes_reais}),
        "grupos": grupos,
    }


def _gerar_excel_resumo_executivo(
    resumo: dict,
    df_ranking: pd.DataFrame,
    boletos_factoring: list[BoletoFactoring],
    itens_antecipacao: list[Antecipacao],
) -> bytes:
    linhas = [
        {"Área": "Devolução", "Indicador": "Pendente de devolução (R$)", "Valor": resumo["total_pendente"]},
        {"Área": "Devolução", "Indicador": "Notas pendentes (qtde.)", "Valor": resumo["qtd_pendentes"]},
        {"Área": "Devolução", "Indicador": "Quitado no período (R$)", "Valor": resumo["total_quitado"]},
    ]
    if not df_ranking.empty:
        top = df_ranking.sort_values("Valor total de devolução", ascending=False).iloc[0]
        linhas.append(
            {
                "Área": "Ranking",
                "Indicador": f"Maior fornecedor em devolução — {top['Fornecedor']} (R$)",
                "Valor": top["Valor total de devolução"],
            }
        )
    if boletos_factoring:
        total_factoring = len([b for b in boletos_factoring if b.status == "factoring"])
        total_nao_confirmados = len([b for b in boletos_factoring if b.status == "nao_verificado"])
        linhas += [
            {"Área": "Factoring", "Indicador": "Boletos analisados (qtde.)", "Valor": len(boletos_factoring)},
            {"Área": "Factoring", "Indicador": "Identificados como factoring (qtde.)", "Valor": total_factoring},
            {"Área": "Factoring", "Indicador": "Não confirmados (qtde.)", "Valor": total_nao_confirmados},
        ]
    if itens_antecipacao:
        total_liquido = sum(i.valor_liquido for i in itens_antecipacao)
        total_desconto = sum(i.desconto_financeiro for i in itens_antecipacao)
        percentual_medio = sum(i.ganho_percentual for i in itens_antecipacao) / len(itens_antecipacao)
        linhas += [
            {"Área": "Antecipação", "Indicador": "Valor líquido antecipado (R$)", "Valor": total_liquido},
            {"Área": "Antecipação", "Indicador": "Desconto financeiro ganho (R$)", "Valor": total_desconto},
            {"Área": "Antecipação", "Indicador": "% ganho médio", "Valor": percentual_medio},
        ]
    df = pd.DataFrame(linhas)
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Resumo Executivo", index=False)
    return buffer.getvalue()


def pagina_home():
    st.subheader(f"Bem-vindo(a) ao {TITULO_APP}")
    st.caption("Resumo rápido de cada área do portal - use o menu ao lado pra ver os detalhes.")

    resumo = _resumo_devolucao()

    st.divider()
    st.markdown("##### 📋 Devolução e Abatimento")
    col_pendente, col_qtd, col_quitado = st.columns(3)
    col_pendente.metric("Pendente de devolução", f"R$ {resumo['total_pendente']:,.2f}")
    col_qtd.metric("Notas pendentes", resumo["qtd_pendentes"])
    col_quitado.metric("Quitado no período", f"R$ {resumo['total_quitado']:,.2f}")

    st.divider()
    st.markdown("##### 🏆 Ranking de fornecedores")
    df_ranking = _ranking_fornecedores(resumo["grupos"])
    if not df_ranking.empty:
        top = df_ranking.sort_values("Valor total de devolução", ascending=False).iloc[0]
        st.metric(
            f"Maior fornecedor em devolução — {top['Fornecedor']}",
            f"R$ {top['Valor total de devolução']:,.2f}",
        )
    else:
        st.caption("Ainda sem dados suficientes pra montar o ranking.")

    st.divider()
    st.markdown("##### 🧾 Factoring")
    boletos_factoring = storage.carregar_boletos_factoring()
    if boletos_factoring:
        total_factoring = len([b for b in boletos_factoring if b.status == "factoring"])
        total_nao_confirmados = len([b for b in boletos_factoring if b.status == "nao_verificado"])
        col_analisados, col_factoring_total, col_nao_confirmados = st.columns(3)
        col_analisados.metric("Boletos já analisados", len(boletos_factoring))
        col_factoring_total.metric("Identificados como factoring", total_factoring)
        col_nao_confirmados.metric("Não foi possível confirmar", total_nao_confirmados)
        ultima_verificacao = storage.obter_ultima_verificacao_factoring()
        if ultima_verificacao:
            st.caption(f"🕓 Última verificação: {ultima_verificacao.strftime('%d/%m/%Y às %H:%M')}")
    else:
        st.caption(
            "Nenhuma verificação de factoring feita ainda - veja a página "
            "**Pagamentos via Boleto / Factoring**."
        )

    st.divider()
    st.markdown("##### 💰 Antecipação de Fornecedores")
    itens_antecipacao: list[Antecipacao] = []
    if config.antecipacao_planilha_path and config.antecipacao_planilha_path.exists():
        try:
            itens_antecipacao = carregar_antecipacoes(config.antecipacao_planilha_path)
        except Exception:
            itens_antecipacao = []
        if itens_antecipacao:
            total_liquido_antecipacao = sum(i.valor_liquido for i in itens_antecipacao)
            total_desconto_antecipacao = sum(i.desconto_financeiro for i in itens_antecipacao)
            percentual_medio_antecipacao = (
                sum(i.ganho_percentual for i in itens_antecipacao) / len(itens_antecipacao)
            )
            col_liquido_home, col_desconto_home, col_percentual_home = st.columns(3)
            col_liquido_home.metric("Valor líquido antecipado", f"R$ {total_liquido_antecipacao:,.2f}")
            col_desconto_home.metric("Desconto financeiro ganho", f"R$ {total_desconto_antecipacao:,.2f}")
            col_percentual_home.metric("% ganho médio", f"{percentual_medio_antecipacao:.2%}")
            dias_desde_modificacao_home = (
                datetime.now() - datetime.fromtimestamp(config.antecipacao_planilha_path.stat().st_mtime)
            ).days
            if dias_desde_modificacao_home > 35:
                st.caption(f"⚠️ Planilha sem atualização há {dias_desde_modificacao_home} dias.")
        else:
            st.caption("Planilha de antecipação sem nenhuma linha lida.")
    else:
        st.caption(
            "Planilha de antecipação não configurada - veja a página **Antecipação de Fornecedores**."
        )

    st.divider()
    st.download_button(
        "Baixar resumo executivo em Excel",
        icon=":material/summarize:",
        data=_gerar_excel_resumo_executivo(resumo, df_ranking, boletos_factoring, itens_antecipacao),
        file_name="resumo_executivo_portal.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def pagina_fornecedores():
    st.subheader("Fornecedores")
    st.caption("Vínculo entre notas de devolução pendentes e as contas a pagar de cada fornecedor.")
    _barra_lateral_atualizar_dados()
    dados = _preparar_dados_devolucao()
    notas = dados["notas"]
    contas = dados["contas"]
    vinculos = dados["vinculos"]
    emails_fornecedor = dados["emails_fornecedor"]
    notas_pendentes_reais = dados["notas_pendentes_reais"]
    vinculos_por_nota = dados["vinculos_por_nota"]
    grupos_ordenados = dados["grupos_ordenados"]
    total_pendente = dados["total_pendente"]
    total_quitado = dados["total_quitado"]

    col_total, col_quitado, col_qtd, col_excel = st.columns([2, 2, 2, 1])
    col_total.metric("Total pendente de devolução", f"R$ {total_pendente:,.2f}")
    col_quitado.metric("Total quitado no período", f"R$ {total_quitado:,.2f}")
    # Conta por NF (fornecedor + nº do documento), não por duplicata - o Bluesoft desmembra a
    # mesma nota em várias parcelas, e contar cada parcela infla artificialmente esse número.
    qtd_notas_reais = len({(n.fornecedor_cnpj, n.numero_documento) for n in notas_pendentes_reais})
    col_qtd.metric("Qtde. de notas de devolução", qtd_notas_reais)
    with col_excel:
        st.write("")
        st.download_button(
            "Baixar em Excel",
            icon=":material/download:",
            data=_gerar_excel(notas, contas),
            file_name="notas_devolucao.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            width="stretch",
        )

    if notas_pendentes_reais:
        st.markdown("**Resumo por produto**")
        st.caption("Clique em uma linha para ver de qual fornecedor é aquele produto.")
        df_resumo_produto = _resumo_por_produto(notas_pendentes_reais)
        evento_produto = st.dataframe(
            df_resumo_produto,
            hide_index=True,
            width="stretch",
            on_select="rerun",
            selection_mode="single-row",
            key="tabela_resumo_produto",
        )
        linhas_selecionadas = evento_produto.selection.rows
        if linhas_selecionadas:
            produto_selecionado = df_resumo_produto.iloc[linhas_selecionadas[0]]["Produto"]
            fornecedores = _fornecedores_do_produto(notas_pendentes_reais, produto_selecionado)
            if fornecedores:
                st.success(f"**{produto_selecionado}** — fornecedor(es): {', '.join(fornecedores)}", icon="🔎")
            else:
                st.info(f"**{produto_selecionado}** não tem fornecedor identificado nas notas atuais.")

    st.divider()

    for cnpj, grupo in grupos_ordenados:
        notas_pendentes_grupo = [n for n in grupo["notas"] if not n.duplicata_pagamento_vinculada]
        notas_vinculadas_grupo = [n for n in grupo["notas"] if n.duplicata_pagamento_vinculada]
        total_devolucao = sum(n.valor_liquido for n in notas_pendentes_grupo)
        total_a_pagar = sum(c.valor for c in grupo["contas"])
        total_quitado_grupo = sum(q.valor_liquido for q in grupo["quitadas"])
        recorrentes = _produtos_recorrentes(grupo["notas"])
        prefixo_titulo = "⚠️ " if recorrentes else ""

        with st.expander(
            f"{prefixo_titulo}{grupo['nome'].strip()} — CNPJ {cnpj} — "
            f"devolução R\\$ {total_devolucao:,.2f} / a pagar R\\$ {total_a_pagar:,.2f} / "
            f"quitado R\\$ {total_quitado_grupo:,.2f}",
            icon=":material/storefront:",
        ):
            if recorrentes:
                detalhe = " · ".join(f"{produto} ({qtd}x em {meses} meses)" for produto, qtd, meses in recorrentes)
                st.warning(f"Recorrência: {detalhe}", icon="⚠️")

            col_notas, col_contas = st.columns(2)

            with col_notas:
                st.subheader("Notas de devolução pendentes")
                if notas_pendentes_grupo:
                    st.dataframe(
                        _tabela_notas(notas_pendentes_grupo, vinculos_por_nota), hide_index=True, width='stretch'
                    )
                else:
                    st.caption("Nenhuma nota de devolução pendente para este fornecedor.")

            with col_contas:
                st.subheader("Contas a pagar em aberto")
                if grupo["contas"]:
                    st.dataframe(_tabela_contas(grupo["contas"]), hide_index=True, width='stretch')
                else:
                    st.caption("Nenhuma conta a pagar em aberto para este fornecedor.")

            if notas_vinculadas_grupo:
                st.markdown("**Notas já vinculadas para abatimento (Bluesoft)**")
                st.dataframe(_tabela_notas_vinculadas(notas_vinculadas_grupo), hide_index=True, width='stretch')

            if notas_pendentes_grupo:
                st.markdown("**E-mail do responsável**")
                col_email, col_email_salvar = st.columns([4, 1])
                email_atual = emails_fornecedor.get(cnpj, "")
                novo_email = col_email.text_input(
                    "E-mail do responsável",
                    value=email_atual,
                    key=f"email_input_{cnpj}",
                    label_visibility="collapsed",
                    placeholder="responsavel@fornecedor.com.br",
                )
                if col_email_salvar.button("Salvar e-mail", key=f"salvar_email_{cnpj}", width="stretch"):
                    if novo_email.strip() and "@" not in novo_email:
                        st.error("Informe um e-mail válido.")
                    else:
                        storage.salvar_email_fornecedor(cnpj, novo_email)
                        st.rerun()

                if st.button("Gerar e-mail de cobrança", icon=":material/mail:", key=f"email_{cnpj}"):
                    try:
                        abrir_no_outlook(
                            grupo["nome"], notas_pendentes_grupo, LOGO_PATH, destinatario=email_atual or None
                        )
                        if email_atual:
                            st.success(f"Rascunho aberto no Outlook já preenchido para {email_atual}.")
                        else:
                            st.success("Rascunho aberto no Outlook. Preencha o destinatário e envie por lá.")
                    except Exception as erro:
                        st.error(f"Não foi possível abrir o Outlook: {erro}")

            if notas_pendentes_grupo and grupo["contas"]:
                st.markdown("**Registrar vínculo (abatimento)**")
                with st.form(key=f"form_vinculo_{cnpj}"):
                    opcoes_nota = {
                        f"{n.duplicata_key} — R$ {n.valor_liquido:,.2f}": n.duplicata_key
                        for n in notas_pendentes_grupo
                    }
                    opcoes_conta = {
                        f"{c.duplicata_key} — R$ {c.valor:,.2f}": c.duplicata_key for c in grupo["contas"]
                    }

                    col1, col2, col3 = st.columns([2, 2, 3])
                    nota_escolhida = col1.selectbox("Nota de devolução", options=list(opcoes_nota.keys()))
                    conta_escolhida = col2.selectbox("Conta a pagar", options=list(opcoes_conta.keys()))
                    observacao = col3.text_input("Observação (opcional)")

                    if st.form_submit_button("Vincular", icon=":material/link:", type="primary"):
                        storage.salvar_vinculo(
                            Vinculo(
                                nota_devolucao_key=opcoes_nota[nota_escolhida],
                                conta_pagar_key=opcoes_conta[conta_escolhida],
                                status="pendente",
                                observacao=observacao,
                            )
                        )
                        st.rerun()

            vinculos_do_grupo = [
                v for v in vinculos if v.nota_devolucao_key in {n.duplicata_key for n in grupo["notas"]}
            ]
            if vinculos_do_grupo:
                st.markdown("**Vínculos registrados**")
                opcoes_status = ["pendente", "abatido"]
                for v in vinculos_do_grupo:
                    col_a, col_b, col_c = st.columns([5, 2, 1])
                    if v.status == "abatido":
                        col_a.badge("Abatido", icon=":material/check_circle:", color="green")
                    else:
                        col_a.badge("Pendente", icon=":material/schedule:", color="orange")
                    col_a.write(
                        f"Devolução `{v.nota_devolucao_key}` ↔ Conta a pagar `{v.conta_pagar_key}`"
                        + (f" — {v.observacao}" if v.observacao else "")
                    )
                    novo_status = col_b.selectbox(
                        "Status",
                        options=opcoes_status,
                        index=opcoes_status.index(v.status) if v.status in opcoes_status else 0,
                        key=f"status_{cnpj}_{v.nota_devolucao_key}_{v.conta_pagar_key}",
                        label_visibility="collapsed",
                    )
                    if novo_status != v.status:
                        storage.salvar_vinculo(
                            Vinculo(v.nota_devolucao_key, v.conta_pagar_key, novo_status, v.observacao)
                        )
                        st.rerun()
                    if col_c.button(
                        "Remover",
                        icon=":material/delete:",
                        key=f"remover_{cnpj}_{v.nota_devolucao_key}_{v.conta_pagar_key}",
                    ):
                        storage.remover_vinculo(v.nota_devolucao_key, v.conta_pagar_key)
                        st.rerun()


def pagina_dashboard():
    st.subheader("Ranking de fornecedores por devolução")
    st.caption("Quem mais gera devolução, pelo valor total ou pela quantidade de notas.")
    _barra_lateral_atualizar_dados()
    dados = _preparar_dados_devolucao()
    df_ranking = _ranking_fornecedores(dados["grupos"])
    if df_ranking.empty:
        st.info("Nenhum dado de devolução para a loja/período selecionados.")
        return

    st.divider()

    formatador_valor = JsCode(
        """
        function(params) {
            if (params.value === null || params.value === undefined) { return ''; }
            return 'R$ ' + Number(params.value).toLocaleString('pt-BR', {
                minimumFractionDigits: 2, maximumFractionDigits: 2
            });
        }
        """
    )

    construtor = GridOptionsBuilder.from_dataframe(df_ranking)
    construtor.configure_default_column(filter=True, sortable=True, resizable=True, floatingFilter=True)
    construtor.configure_column("Qtde. de notas", type=["numericColumn"], filter="agNumberColumnFilter")
    construtor.configure_column(
        "Valor total de devolução",
        type=["numericColumn"],
        filter="agNumberColumnFilter",
        valueFormatter=formatador_valor,
    )

    resultado = AgGrid(
        df_ranking,
        gridOptions=construtor.build(),
        update_mode=GridUpdateMode.MODEL_CHANGED,
        data_return_mode=DataReturnMode.FILTERED_AND_SORTED,
        columns_auto_size_mode=ColumnsAutoSizeMode.FIT_CONTENTS,
        allow_unsafe_jscode=True,
        theme="streamlit",
        height=380,
    )

    df_filtrado = resultado["data"]
    if not isinstance(df_filtrado, pd.DataFrame):
        df_filtrado = pd.DataFrame(df_filtrado)

    if df_filtrado.empty:
        st.caption("Nenhum fornecedor encontrado com esses filtros.")
        return

    st.markdown("**Top 10 do que está filtrado acima**")
    criterio = st.radio(
        "Ordenar gráfico por", ["Valor total", "Quantidade de notas"], horizontal=True
    )
    coluna_ordem = "Valor total de devolução" if criterio == "Valor total" else "Qtde. de notas"
    top10 = df_filtrado.sort_values(coluna_ordem, ascending=False).head(10)
    eixo_x_top10 = alt.X(
        "Fornecedor:N",
        sort=None,
        axis=alt.Axis(labelAngle=0, labelFontSize=13, labelColor="#333333", labelLimit=140),
        title=None,
    )
    selecao_fornecedor = alt.selection_point(fields=["Fornecedor"], name="fornecedor")
    base_top10 = alt.Chart(top10).encode(x=eixo_x_top10, y=alt.Y(f"{coluna_ordem}:Q", title=None))
    barras_top10 = base_top10.mark_bar().encode(
        color=alt.value("#EE6931"),
        opacity=alt.condition(selecao_fornecedor, alt.value(1.0), alt.value(0.55)),
    )
    rotulos_top10 = base_top10.mark_text(**_ROTULO_VALOR_BARRA).encode(
        text=alt.Text(f"{coluna_ordem}:Q", format=",.0f")
    )
    clique_top10 = st.altair_chart(
        (barras_top10 + rotulos_top10).add_params(selecao_fornecedor),
        width="stretch",
        on_select="rerun",
        key="grafico_top10",
    )

    pontos_top10 = clique_top10["selection"].get("fornecedor") or []
    if pontos_top10:
        fornecedor_selecionado = pontos_top10[0]["Fornecedor"]
        linha_selecionada = top10[top10["Fornecedor"] == fornecedor_selecionado]
        if not linha_selecionada.empty:
            linha_selecionada = linha_selecionada.iloc[0]
            col_nome, col_valor, col_qtd = st.columns([2, 1, 1])
            col_nome.metric("🔍 Fornecedor selecionado", fornecedor_selecionado)
            col_valor.metric("Valor total de devolução", f"R$ {linha_selecionada['Valor total de devolução']:,.2f}")
            col_qtd.metric("Qtde. de notas", int(linha_selecionada["Qtde. de notas"]))


def pagina_factoring():
    st.subheader("Pagamentos via Boleto / Factoring")
    st.caption(
        "Verifica quais boletos já pagos (todas as lojas pagadoras) foram na verdade pagos a uma "
        "factoring/financeira em vez do fornecedor cadastrado no Bluesoft - lendo o beneficiário "
        "impresso no boleto anexado a cada duplicata. Duplicatas já verificadas antes não são "
        "checadas de novo. Recomendado rodar mês a mês (mais rápido de conferir aos poucos)."
    )
    ultima_verificacao_factoring = storage.obter_ultima_verificacao_factoring()
    if ultima_verificacao_factoring:
        st.caption(f"🕓 Última verificação: {ultima_verificacao_factoring.strftime('%d/%m/%Y às %H:%M')}")

    hoje = date.today()
    primeiro_dia_mes_atual = hoje.replace(day=1)
    ultimo_dia_mes_passado = primeiro_dia_mes_atual - timedelta(days=1)
    primeiro_dia_mes_passado = ultimo_dia_mes_passado.replace(day=1)

    col_periodo_de, col_periodo_ate = st.columns(2)
    periodo_de = col_periodo_de.date_input("De", value=primeiro_dia_mes_passado, format="DD/MM/YYYY")
    periodo_ate = col_periodo_ate.date_input("Até", value=ultimo_dia_mes_passado, format="DD/MM/YYYY")

    if st.button("Verificar boletos de factoring", icon=":material/search:", type="primary"):
        barra = st.progress(0.0, text="Buscando contas a pagar quitadas no Bluesoft...")

        def _progresso(indice: int, total: int) -> None:
            fracao = indice / total if total else 1.0
            barra.progress(fracao, text=f"Verificando boletos: {indice}/{total}")

        with st.spinner(
            "Conectando ao Bluesoft... uma janela do navegador pode abrir pedindo login manual."
        ):
            novos, avisos_factoring = verificar_boletos_factoring(
                periodo_de, periodo_ate, progress_callback=_progresso
            )
        barra.empty()
        st.session_state["avisos_factoring"] = avisos_factoring
        st.session_state["mensagem_factoring"] = f"{len(novos)} duplicatas novas verificadas nesta rodada."
        st.rerun()

    for aviso in st.session_state.get("avisos_factoring", []):
        st.warning(f"⚠️ {aviso}")
    if st.session_state.get("mensagem_factoring"):
        st.success(st.session_state.pop("mensagem_factoring"))

    boletos_todos = [b for b in storage.carregar_boletos_factoring() if b.status != "ignorado"]
    if not boletos_todos:
        st.info(
            "Nenhum boleto verificado ainda. Escolha o período acima e clique em "
            "**Verificar boletos de factoring** para começar."
        )
        return

    st.markdown("**Tendência mês a mês**")
    df_tendencia = _tabela_tendencia_factoring(boletos_todos)
    if not df_tendencia.empty:
        df_tendencia_longo = df_tendencia.melt(
            id_vars="Mês",
            value_vars=["Total analisados", "Factoring"],
            var_name="Série",
            value_name="Quantidade",
        )
        eixo_x_tendencia = alt.X(
            "Mês:N", axis=alt.Axis(labelAngle=0, labelFontSize=13, labelColor="#333333"), title=None
        )
        cor_tendencia = alt.Color(
            "Série:N",
            scale=alt.Scale(domain=["Total analisados", "Factoring"], range=["#2E7D32", "#EE6931"]),
            legend=alt.Legend(title=None),
        )
        selecao_mes_factoring = alt.selection_point(fields=["Mês"], name="mes")
        base_tendencia = alt.Chart(df_tendencia_longo).encode(
            x=eixo_x_tendencia, y=alt.Y("Quantidade:Q", title=None), xOffset="Série:N"
        )
        barras_tendencia = base_tendencia.mark_bar().encode(
            color=cor_tendencia,
            opacity=alt.condition(selecao_mes_factoring, alt.value(1.0), alt.value(0.55)),
        )
        rotulos_tendencia = base_tendencia.mark_text(**_ROTULO_VALOR_BARRA).encode(text="Quantidade:Q")
        clique_tendencia = st.altair_chart(
            (barras_tendencia + rotulos_tendencia).add_params(selecao_mes_factoring),
            width="stretch",
            on_select="rerun",
            key="grafico_tendencia_factoring",
        )
        pontos_mes = clique_tendencia["selection"].get("mes") or []
        mes_selecionado_factoring = pontos_mes[0]["Mês"] if pontos_mes else None
        if mes_selecionado_factoring:
            st.caption(
                f"🔍 Filtrando por **{mes_selecionado_factoring}** - clique na mesma barra de novo "
                "pra remover o filtro."
            )
    else:
        st.caption("Ainda sem dados suficientes pra mostrar tendência.")
        mes_selecionado_factoring = None

    opcoes_loja_pagadora = {"Todas": None} | {
        f"Loja {n}": n for n in sorted({b.loja_pagadora for b in boletos_todos})
    }
    loja_pagadora_escolhida = st.selectbox("Loja pagadora", options=list(opcoes_loja_pagadora.keys()))
    numero_loja_pagadora = opcoes_loja_pagadora[loja_pagadora_escolhida]
    boletos = (
        [b for b in boletos_todos if b.loja_pagadora == numero_loja_pagadora]
        if numero_loja_pagadora is not None
        else boletos_todos
    )
    if mes_selecionado_factoring:
        boletos = [
            b for b in boletos
            if (b.data_quitacao or b.data_vencimento).strftime("%m/%Y") == mes_selecionado_factoring
        ]

    factoring_encontrados = [b for b in boletos if b.status == "factoring"]
    normais = [b for b in boletos if b.status == "normal"]
    nao_verificados = [b for b in boletos if b.status == "nao_verificado"]

    col_total, col_factoring, col_nao_confirmados = st.columns(3)
    col_total.metric("Total de boletos analisados", len(boletos))
    col_factoring.metric("Identificados como factoring", len(factoring_encontrados))
    col_nao_confirmados.metric("Não foi possível confirmar", len(nao_verificados))

    df_status = pd.DataFrame(
        {
            "Status": ["Factoring", "Normais", "Não confirmados"],
            "Quantidade": [len(factoring_encontrados), len(normais), len(nao_verificados)],
        }
    )
    selecao_status_factoring = alt.selection_point(fields=["Status"], name="status")
    base_status = alt.Chart(df_status).encode(
        x=alt.X(
            "Status:N",
            sort=None,
            axis=alt.Axis(labelAngle=0, labelFontSize=13, labelColor="#333333"),
            title=None,
        ),
        y=alt.Y("Quantidade:Q", title=None),
    )
    grafico_status = base_status.mark_bar().encode(
        color=alt.Color(
            "Status:N",
            scale=alt.Scale(
                domain=["Factoring", "Normais", "Não confirmados"],
                range=["#EE6931", "#2E7D32", "#888888"],
            ),
            legend=None,
        ),
        opacity=alt.condition(selecao_status_factoring, alt.value(1.0), alt.value(0.55)),
    )
    rotulos_status = base_status.mark_text(**_ROTULO_VALOR_BARRA).encode(text="Quantidade:Q")
    clique_status = st.altair_chart(
        (grafico_status + rotulos_status).add_params(selecao_status_factoring),
        width="stretch",
        on_select="rerun",
        key="grafico_status_factoring",
    )
    pontos_status = clique_status["selection"].get("status") or []
    _CHAVE_STATUS_POR_LABEL = {"Factoring": "factoring", "Normais": "normal", "Não confirmados": "nao_verificado"}
    status_selecionado_factoring = (
        _CHAVE_STATUS_POR_LABEL.get(pontos_status[0]["Status"]) if pontos_status else None
    )
    if status_selecionado_factoring:
        st.caption(
            f"🔍 Mostrando só **{pontos_status[0]['Status']}** - clique na mesma barra de novo "
            "pra ver todos de novo."
        )

    st.download_button(
        "Baixar em Excel",
        icon=":material/download:",
        data=_gerar_excel_factoring(boletos),
        file_name="boletos_factoring.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    if status_selecionado_factoring in (None, "factoring"):
        if factoring_encontrados:
            st.markdown("**Fornecedores identificados em factoring**")
            df_ranking_factoring = _tabela_ranking_fornecedores_factoring(factoring_encontrados)
            st.dataframe(
                df_ranking_factoring.style.format({"Valor total": "R$ {:,.2f}"}),
                hide_index=True,
                width="stretch",
            )
            st.dataframe(_tabela_boletos(factoring_encontrados), hide_index=True, width="stretch")
        else:
            st.caption("Nenhum boleto de factoring identificado até agora.")

    if normais and status_selecionado_factoring in (None, "normal"):
        with st.expander(
            f"Normais — bateram com o fornecedor cadastrado ({len(normais)})",
            icon=":material/check_circle:",
            expanded=status_selecionado_factoring == "normal",
        ):
            st.dataframe(_tabela_boletos(normais), hide_index=True, width="stretch")

    if nao_verificados and status_selecionado_factoring in (None, "nao_verificado"):
        with st.expander(
            f"Não foi possível confirmar ({len(nao_verificados)})",
            icon=":material/help:",
            expanded=status_selecionado_factoring == "nao_verificado",
        ):
            st.caption(
                "Não foi possível confirmar automaticamente - veja o motivo na coluna "
                "**'Trecho do boleto'** abaixo (ex.: sem anexo, texto ilegível, CNPJ não "
                "encontrado). Duplicatas sem anexo nenhum geralmente não foram pagas via boleto "
                "de verdade - marque como **'ignorado'** no formulário abaixo, em vez de perder "
                "tempo abrindo no Bluesoft."
            )
            st.download_button(
                "Baixar esta lista em Excel",
                icon=":material/download:",
                data=_gerar_excel_factoring(nao_verificados),
                file_name="boletos_nao_confirmados.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key="baixar_nao_confirmados",
            )
            st.dataframe(_tabela_boletos(nao_verificados), hide_index=True, width="stretch")

    if boletos:
        st.markdown("**Corrigir classificação manualmente**")
        st.caption(
            "Serve pra qualquer boleto listado acima (inclusive já marcados como Factoring ou "
            "Normal) - use pra corrigir um falso positivo/negativo depois de conferir no Bluesoft."
        )
        opcoes_duplicata_manual = {
            f"{b.duplicata_key} — {b.favorecido.strip()} — R$ {b.valor:,.2f} — "
            f"({_STATUS_FACTORING_LABELS.get(b.status, b.status)})": b.duplicata_key
            for b in boletos
        }
        with st.form(key="form_marcar_manual"):
            duplicata_escolhida = st.selectbox(
                "Duplicata", options=list(opcoes_duplicata_manual.keys())
            )
            status_manual = st.radio(
                "Status correto",
                ["normal", "factoring", "ignorado"],
                format_func=lambda s: _STATUS_FACTORING_LABELS.get(s, s),
                horizontal=True,
            )
            observacao_manual = st.text_input(
                "Observação (ex.: nome da financeira encontrada, ou motivo de ignorar - opcional)"
            )
            if st.form_submit_button("Salvar", icon=":material/check:", type="primary"):
                storage.marcar_status_manual_factoring(
                    opcoes_duplicata_manual[duplicata_escolhida], status_manual, observacao_manual
                )
                st.rerun()

        st.markdown("**Marcar vários de uma vez (depois de conferir no Excel)**")
        st.caption(
            "Cole um número de duplicata por linha em cada caixa - vale pra qualquer boleto "
            "listado acima, não só os não confirmados. Na lista de Factoring, você pode escrever "
            "o nome da financeira depois de um traço, ex.: `1490829 - Nome da financeira`."
        )
        with st.form(key="form_marcar_em_lote"):
            col_normais_lote, col_factoring_lote, col_ignoradas_lote = st.columns(3)
            texto_normais_lote = col_normais_lote.text_area(
                "Duplicatas NORMAIS", height=150, placeholder="1445251\n1461950"
            )
            texto_factoring_lote = col_factoring_lote.text_area(
                "Duplicatas FACTORING", height=150, placeholder="1490829 - Nome da financeira\n1490192"
            )
            texto_ignoradas_lote = col_ignoradas_lote.text_area(
                "Duplicatas IGNORADAS (não é boleto)", height=150, placeholder="1236887\n1245765"
            )
            if st.form_submit_button("Salvar tudo", icon=":material/done_all:", type="primary"):
                chaves_validas = {b.duplicata_key for b in boletos}
                atualizados = []
                nao_processados = []
                grupos_lote = (
                    ("normal", texto_normais_lote),
                    ("factoring", texto_factoring_lote),
                    ("ignorado", texto_ignoradas_lote),
                )
                for status_lote, texto_lote in grupos_lote:
                    for linha in texto_lote.splitlines():
                        linha = linha.strip()
                        if not linha:
                            continue
                        chave, _, observacao_lote = linha.partition("-")
                        chave = re.sub(r"\D", "", chave)
                        if not chave:
                            continue
                        if chave in chaves_validas:
                            storage.marcar_status_manual_factoring(chave, status_lote, observacao_lote.strip())
                            atualizados.append(chave)
                        else:
                            nao_processados.append(chave)
                if atualizados:
                    st.success(f"{len(atualizados)} duplicata(s) atualizada(s): {', '.join(atualizados)}")
                    st.rerun()
                if nao_processados:
                    st.warning(
                        "Não encontradas na lista filtrada acima (não processadas): "
                        + ", ".join(nao_processados)
                    )

    st.markdown("**Forçar reverificação de uma duplicata específica**")
    st.caption(
        "Se uma duplicata específica precisa ser checada de novo (ex.: o boleto foi trocado no "
        "Bluesoft), remova o registro dela aqui - da próxima vez que rodar a verificação num "
        "período que a inclua, ela será reprocessada."
    )
    col_dup_reverificar, col_botao_reverificar = st.columns([3, 1])
    duplicata_reverificar = col_dup_reverificar.text_input(
        "Nº duplicata",
        key="duplicata_reverificar",
        label_visibility="collapsed",
        placeholder="Nº duplicata (ex.: 1554727)",
    )
    if col_botao_reverificar.button("Forçar reverificar", icon=":material/refresh:", width="stretch"):
        if duplicata_reverificar.strip():
            storage.remover_verificacao_factoring(duplicata_reverificar.strip())
            st.success(f"Duplicata {duplicata_reverificar.strip()} removida - será reprocessada na próxima rodada.")
            st.rerun()
        else:
            st.error("Informe o número da duplicata.")

    if nao_verificados:
        st.markdown("**Forçar reverificação de todos os \"não confirmados\" listados acima**")
        st.caption(
            f"Remove o registro de todas as {len(nao_verificados)} duplicatas 'não confirmadas' "
            "que estão na tela agora (respeitando os filtros de loja/mês aplicados acima) - elas "
            "serão reprocessadas com a lógica mais recente na próxima vez que você clicar em "
            "'Verificar boletos de factoring' num período que as inclua. Isso só remove os "
            "registros (rápido); o reprocessamento em si acontece depois, no próximo clique em "
            "Verificar, e pode demorar (uma tela + download por duplicata)."
        )
        if st.button(
            f"Forçar reverificar todas as {len(nao_verificados)} não confirmadas",
            icon=":material/refresh:",
        ):
            for boleto in nao_verificados:
                storage.remover_verificacao_factoring(boleto.duplicata_key)
            st.success(
                f"{len(nao_verificados)} duplicata(s) removida(s) - serão reprocessadas na próxima "
                "vez que você rodar a verificação num período que as inclua."
            )
            st.rerun()


def pagina_antecipacao():
    st.subheader("Antecipação de Fornecedores")
    st.caption(
        "Desconto financeiro ganho por antecipar pagamento a fornecedores - lido direto da "
        "planilha mantida pelo financeiro (não vem do Bluesoft nem passa pelo botão de sync)."
    )

    if st.button("🔄 Atualizar dados da planilha", key="atualizar_antecipacao"):
        st.success("Planilha recarregada.")

    if not config.antecipacao_planilha_path:
        st.info(
            "Caminho da planilha não configurado. Adicione `caminho_planilha` na seção "
            "`[antecipacao]` de `config/settings.toml`."
        )
        return

    if not config.antecipacao_planilha_path.exists():
        st.error(f"Planilha não encontrada em: {config.antecipacao_planilha_path}")
        return

    ultima_modificacao_planilha = datetime.fromtimestamp(config.antecipacao_planilha_path.stat().st_mtime)
    dias_desde_modificacao = (datetime.now() - ultima_modificacao_planilha).days
    texto_ultima_modificacao = (
        f"🕓 Planilha modificada pela última vez em "
        f"{ultima_modificacao_planilha.strftime('%d/%m/%Y às %H:%M')}"
    )
    if dias_desde_modificacao > 35:
        st.warning(f"{texto_ultima_modificacao} - já faz {dias_desde_modificacao} dias, pode estar desatualizada.")
    else:
        st.caption(texto_ultima_modificacao)

    try:
        itens = carregar_antecipacoes(config.antecipacao_planilha_path)
    except Exception as erro:
        st.error(f"Não foi possível ler a planilha: {erro}")
        return

    if not itens:
        st.info("Planilha lida, mas sem nenhuma linha de antecipação.")
        return

    st.divider()

    opcoes_loja_pagadora = {"Todas": None} | {
        f"Loja {n}": n for n in sorted({i.loja_pagadora for i in itens})
    }
    loja_pagadora_escolhida = st.selectbox("Loja pagadora", options=list(opcoes_loja_pagadora.keys()))
    numero_loja_pagadora = opcoes_loja_pagadora[loja_pagadora_escolhida]
    itens_filtrados = (
        [i for i in itens if i.loja_pagadora == numero_loja_pagadora]
        if numero_loja_pagadora is not None
        else itens
    )

    st.markdown("**Evolução mensal**")
    df_mensal = _tabela_mensal_antecipacao(itens_filtrados)
    mes_selecionado_antecipacao = None
    if not df_mensal.empty:
        df_mensal_longo = df_mensal.melt(
            id_vars="Mês",
            value_vars=["Valor líquido", "Desconto financeiro"],
            var_name="Série",
            value_name="Valor",
        )
        eixo_x_mensal = alt.X(
            "Mês:N", axis=alt.Axis(labelAngle=0, labelFontSize=13, labelColor="#333333"), title=None
        )
        cor_mensal = alt.Color(
            "Série:N",
            scale=alt.Scale(domain=["Valor líquido", "Desconto financeiro"], range=["#2E7D32", "#EE6931"]),
            legend=alt.Legend(title=None),
        )
        selecao_mes_antecipacao = alt.selection_point(fields=["Mês"], name="mes")
        base_mensal = alt.Chart(df_mensal_longo).encode(
            x=eixo_x_mensal, y=alt.Y("Valor:Q", title=None), xOffset="Série:N"
        )
        barras_mensal = base_mensal.mark_bar().encode(
            color=cor_mensal,
            opacity=alt.condition(selecao_mes_antecipacao, alt.value(1.0), alt.value(0.55)),
        )
        rotulos_mensal = base_mensal.mark_text(**_ROTULO_VALOR_BARRA).encode(
            text=alt.Text("Valor:Q", format=",.0f")
        )
        clique_mensal = st.altair_chart(
            (barras_mensal + rotulos_mensal).add_params(selecao_mes_antecipacao),
            width="stretch",
            on_select="rerun",
            key="grafico_mensal_antecipacao",
        )
        pontos_mes_antecipacao = clique_mensal["selection"].get("mes") or []
        mes_selecionado_antecipacao = pontos_mes_antecipacao[0]["Mês"] if pontos_mes_antecipacao else None
        if mes_selecionado_antecipacao:
            st.caption(
                f"🔍 Filtrando por **{mes_selecionado_antecipacao}** - clique na mesma barra de novo "
                "pra remover o filtro."
            )
    else:
        st.caption("Sem dados suficientes pra mostrar evolução mensal.")

    itens_exibidos = (
        [i for i in itens_filtrados if i.data_vencimento.strftime("%m/%Y") == mes_selecionado_antecipacao]
        if mes_selecionado_antecipacao
        else itens_filtrados
    )

    total_liquido = sum(i.valor_liquido for i in itens_exibidos)
    total_desconto = sum(i.desconto_financeiro for i in itens_exibidos)
    # Média simples do "% ganho" de cada duplicata, não ponderada pelo valor - mesmo cálculo que a
    # planilha de origem usa na linha "Total Geral" (confirmado batendo o número ao vivo).
    percentual_medio = sum(i.ganho_percentual for i in itens_exibidos) / len(itens_exibidos)

    col_liquido, col_desconto, col_percentual = st.columns(3)
    col_liquido.metric("Valor líquido antecipado", f"R$ {total_liquido:,.2f}")
    col_desconto.metric("Desconto financeiro ganho", f"R$ {total_desconto:,.2f}")
    col_percentual.metric("% ganho médio", f"{percentual_medio:.2%}")

    st.download_button(
        "Baixar em Excel",
        icon=":material/download:",
        data=_gerar_excel_antecipacao(itens_exibidos),
        file_name="antecipacao_fornecedores.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    st.markdown("**Ranking por fornecedor**")
    df_ranking = _tabela_ranking_antecipacao(itens_exibidos)

    formatador_valor_antecipacao = JsCode(
        """
        function(params) {
            if (params.value === null || params.value === undefined) { return ''; }
            return 'R$ ' + Number(params.value).toLocaleString('pt-BR', {
                minimumFractionDigits: 2, maximumFractionDigits: 2
            });
        }
        """
    )
    formatador_percentual_antecipacao = JsCode(
        """
        function(params) {
            if (params.value === null || params.value === undefined) { return ''; }
            return (params.value * 100).toLocaleString('pt-BR', { maximumFractionDigits: 2 }) + '%';
        }
        """
    )

    construtor_antecipacao = GridOptionsBuilder.from_dataframe(df_ranking)
    construtor_antecipacao.configure_default_column(
        filter=True, sortable=True, resizable=True, floatingFilter=True
    )
    construtor_antecipacao.configure_column(
        "Qtde. de duplicatas", type=["numericColumn"], filter="agNumberColumnFilter"
    )
    construtor_antecipacao.configure_column(
        "Valor líquido",
        type=["numericColumn"],
        filter="agNumberColumnFilter",
        valueFormatter=formatador_valor_antecipacao,
    )
    construtor_antecipacao.configure_column(
        "Desconto financeiro",
        type=["numericColumn"],
        filter="agNumberColumnFilter",
        valueFormatter=formatador_valor_antecipacao,
    )
    construtor_antecipacao.configure_column(
        "% ganho médio",
        type=["numericColumn"],
        filter="agNumberColumnFilter",
        valueFormatter=formatador_percentual_antecipacao,
    )

    AgGrid(
        df_ranking,
        gridOptions=construtor_antecipacao.build(),
        update_mode=GridUpdateMode.MODEL_CHANGED,
        data_return_mode=DataReturnMode.FILTERED_AND_SORTED,
        columns_auto_size_mode=ColumnsAutoSizeMode.FIT_CONTENTS,
        allow_unsafe_jscode=True,
        theme="streamlit",
        height=380,
    )


# Todas as páginas ficam sempre registradas aqui (o Streamlit não lida bem com a lista de páginas
# mudando de uma execução pra outra - dá "Page not found" na primeira carga de uma URL direta tipo
# /antecipacao). O controle de quem vê o quê é só visual (`visibility="hidden"` esconde do menu
# lateral) - a proteção de verdade já é o `exigir_login` lá em cima, que bloqueia o acesso via URL
# direta a qualquer página que não seja a de Antecipação pra quem não estiver logado.
_visitante_nao_logado = not st.session_state.get("autenticado")
_visibilidade_paginas_privadas = "hidden" if _visitante_nao_logado else "visible"

pg = st.navigation(
    [
        st.Page(
            pagina_home,
            title="Home",
            icon=":material/home:",
            default=True,
            visibility=_visibilidade_paginas_privadas,
        ),
        st.Page(
            pagina_fornecedores,
            title="Fornecedores",
            icon=":material/storefront:",
            visibility=_visibilidade_paginas_privadas,
        ),
        st.Page(
            pagina_dashboard,
            title="Dashboard",
            icon=":material/bar_chart:",
            visibility=_visibilidade_paginas_privadas,
        ),
        st.Page(
            pagina_factoring,
            title="Pagamentos via Boleto / Factoring",
            icon=":material/receipt_long:",
            visibility=_visibilidade_paginas_privadas,
        ),
        st.Page(
            pagina_antecipacao,
            title="Antecipação de Fornecedores",
            icon=":material/percent:",
            url_path="antecipacao",
        ),
    ]
)
pg.run()

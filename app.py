from __future__ import annotations

import base64
import hmac
import html
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
from notas_devolucao.antecipacao import Antecipacao, carregar_antecipacoes, data_modificacao
from notas_devolucao.config import BASE_DIR, MODO_NUVEM, carregar_configuracao
from notas_devolucao.detalhe_devolucao import categoria_motivo, kg_por_produto_materia_prima, limpar_motivo
from notas_devolucao.email_cobranca import abrir_no_outlook
from notas_devolucao.login import exigir_login
from notas_devolucao.models import BoletoFactoring, ContaPagar, NotaDevolucao, Vinculo
from notas_devolucao.publicacao import publicar_dados
from notas_devolucao.relatorio_abatimento import NotaQuitada
from notas_devolucao.sincronizacao import (
    aplicar_beneficiarios_autorizados,
    atualizar_dados,
    reprocessar_nao_verificados,
    verificar_boletos_factoring,
)

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

# Link de "somente visualização" (tipo relatório publicado do Power BI) - qualquer página com
# ?chave=<token> na URL pula o login e entra em modo leitura: continua vendo os dados normalmente,
# mas todo botão/formulário que altera algo (sync com Bluesoft, vínculos, e-mail de cobrança,
# correções de factoring etc.) fica escondido. Configurado em `config/settings.toml [visualizacao]`
# - vazio/ausente desativa esse acesso.
# Uma vez validado, fica só em `session_state` (não reavalia toda hora a partir da URL) porque os
# links do menu lateral do st.navigation trocam de página sem manter o "?chave=" na URL - sem isso,
# o visitante caía de volta na tela de login ao clicar em qualquer página do menu.
_chave_url = st.query_params.get("chave")
_chave_valida = bool(
    config.visualizacao_token and _chave_url and hmac.compare_digest(_chave_url, config.visualizacao_token)
)
st.session_state["modo_leitura"] = MODO_NUVEM or st.session_state.get("modo_leitura", False) or _chave_valida

# No modo nuvem (instância pública no Streamlit Community Cloud) não existe login nenhum - o
# acesso é controlado lá fora, pelo próprio Streamlit Community Cloud (app privado + lista de
# e-mails convidados), não por usuário/senha deste app.
if not MODO_NUVEM and _caminho_pagina_atual != "antecipacao" and not st.session_state["modo_leitura"]:
    exigir_login(config)


def _modo_leitura() -> bool:
    return bool(st.session_state.get("modo_leitura"))


def _barra_lateral_atualizar_dados():
    """Botão de sincronização com o Bluesoft (notas de devolução + contas a pagar) - usado só
    pelas páginas Fornecedores e Dashboard, que dependem dessa base. Factoring tem seu próprio
    botão de verificação, e a Home é só um resumo, então não mostram isso."""
    with st.sidebar:
        st.header("Dados do Bluesoft")
        ultima_atualizacao = storage.carregar_ultima_atualizacao()
        if ultima_atualizacao:
            st.caption(f"🕓 Última atualização: {ultima_atualizacao.strftime('%d/%m/%Y às %H:%M')}")
        if _modo_leitura():
            return
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

        if (BASE_DIR / ".git").exists():
            st.divider()
            st.caption("Envia um retrato dos dados de agora pro portal público na nuvem.")
            if st.button("☁️ Publicar para nuvem", width="stretch"):
                with st.spinner("Publicando..."):
                    try:
                        mensagem = publicar_dados(config.antecipacao_planilha_path)
                        st.success(mensagem)
                    except Exception as erro:
                        st.error(str(erro))

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
    st.markdown("##### 🕘 Histórico de devoluções")
    linhas_historico = _linhas_historico_devolucoes()
    if linhas_historico:
        df_historico = pd.DataFrame(linhas_historico)
        df_historico = df_historico[df_historico["Emissão"].notna()]
        ano_atual = date.today().year
        hoje = date.today()

        def _ate_hoje_no_ano(ano: int) -> pd.DataFrame:
            # Mesmo período (1º de janeiro até o dia de hoje) nos dois anos, pra comparação justa.
            fim = hoje.replace(year=ano) if not (hoje.month == 2 and hoje.day == 29) else date(ano, 2, 28)
            return df_historico[df_historico["Emissão"].apply(lambda d: date(ano, 1, 1) <= d <= fim)]

        atual, anterior = _ate_hoje_no_ano(ano_atual), _ate_hoje_no_ano(ano_atual - 1)

        def _kg(df_ano: pd.DataFrame) -> float:
            return df_ano.dropna(subset=["Matéria-prima (kg)"]).drop_duplicates(subset=["CNPJ", "NF"])["Matéria-prima (kg)"].sum()

        def _delta(valor_atual: float, valor_anterior: float) -> str | None:
            if not valor_anterior:
                return None
            return f"{(valor_atual / valor_anterior - 1):+.0%} vs. {ano_atual - 1}"

        valor_atual, valor_anterior = atual["Valor"].sum(), anterior["Valor"].sum()
        notas_atual = atual[["CNPJ", "NF"]].drop_duplicates().shape[0]
        notas_anterior = anterior[["CNPJ", "NF"]].drop_duplicates().shape[0]
        kg_atual, kg_anterior = _kg(atual), _kg(anterior)
        col_valor_hist, col_notas_hist, col_kg_hist = st.columns(3)
        # Menos devolução é bom - delta_color="inverse" deixa a queda em verde e o aumento em vermelho.
        col_valor_hist.metric(
            f"Valor devolvido em {ano_atual}", _formatar_moeda_br(valor_atual),
            _delta(valor_atual, valor_anterior), delta_color="inverse",
        )
        col_notas_hist.metric(
            f"Notas de devolução em {ano_atual}", notas_atual,
            _delta(notas_atual, notas_anterior), delta_color="inverse",
        )
        col_kg_hist.metric(
            f"Matéria-prima devolvida em {ano_atual}", _formatar_kg(kg_atual),
            _delta(kg_atual, kg_anterior), delta_color="inverse",
        )
        st.caption(
            f"Comparado com o mesmo período de {ano_atual - 1} (1º/jan até {hoje:%d/%m}). "
            "Detalhes na página **Histórico de Devoluções**."
        )
    else:
        st.caption("Sem histórico carregado ainda.")

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
                datetime.now() - data_modificacao(config.antecipacao_planilha_path)
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
                email_atual = emails_fornecedor.get(cnpj, "")
                if _modo_leitura():
                    if email_atual:
                        st.caption(f"📧 E-mail do responsável: {email_atual}")
                else:
                    st.markdown("**E-mail do responsável**")
                    col_email, col_email_salvar = st.columns([4, 1])
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

            if notas_pendentes_grupo and grupo["contas"] and not _modo_leitura():
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
                    if _modo_leitura():
                        continue
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


def _linhas_historico_devolucoes() -> list[dict]:
    """Junta, numa lista só, toda nota de devolução que o portal conhece: em aberto (incluindo as que
    o Bluesoft já marcou como abatimento, mas ainda não quitou) e já quitadas - cada uma com o
    detalhe lido da fatura/nota fiscal (produto, peso, motivo), quando já houver."""
    detalhes = storage.carregar_detalhes_devolucao()
    LOJAS_PERMITIDAS = {1, 14}
    linhas: list[dict] = []

    def _detalhe(chave: str, produtos_fallback: str | None = None) -> dict:
        d = detalhes.get(chave)
        return {
            "Produto(s)": (d.produtos if d and d.produtos else produtos_fallback) or "",
            "Matéria-prima (kg)": d.peso_kg if d else None,
            "Motivo": limpar_motivo(d.motivo if d else None),
            "_mp_por_produto": kg_por_produto_materia_prima(d) if d else {},
        }

    for n in storage.carregar_notas_devolucao():
        if n.loja not in LOJAS_PERMITIDAS:
            continue
        linhas.append(
            {
                "Status": "Vinculada p/ abatimento" if n.duplicata_pagamento_vinculada else "Em aberto",
                "Fornecedor": n.fornecedor.strip(),
                "CNPJ": n.fornecedor_cnpj,
                "NF": n.numero_documento,
                "Tipo": n.tipo,
                "Loja": n.loja_nome or f"Loja {n.loja}",
                "Emissão": n.data_emissao,
                "Vencimento": n.data_vencimento,
                "Quitação": None,
                "Valor": n.valor_liquido,
                "Duplicata": n.duplicata_key,
                **_detalhe(n.duplicata_key, n.produtos),
            }
        )
    for q in storage.carregar_notas_devolucao_quitadas():
        if q.loja not in LOJAS_PERMITIDAS:
            continue
        linhas.append(
            {
                "Status": "Quitada",
                "Fornecedor": q.fornecedor.strip(),
                "CNPJ": q.fornecedor_cnpj,
                "NF": q.numero_documento,
                "Tipo": q.tipo,
                "Loja": q.loja_nome or f"Loja {q.loja}",
                "Emissão": q.data_emissao or q.data_criacao,
                "Vencimento": None,
                "Quitação": q.data_quitacao,
                "Valor": q.valor_liquido,
                "Duplicata": q.duplicata_key,
                **_detalhe(q.duplicata_key),
            }
        )
    return linhas


def _comparativo_anual(df: pd.DataFrame) -> pd.DataFrame:
    """Uma linha por ano de emissão: qtde. de notas (por NF), valor e kg de matéria-prima, com a
    variação do valor contra o ano anterior. `df` = linhas de `_linhas_historico_devolucoes`."""
    if df.empty:
        return pd.DataFrame()
    # Anos antes do início da busca só aparecem por causa de notas antigas quitadas depois - ficariam
    # incompletos e com variação enganosa (ex.: +3000%), então ficam de fora do comparativo.
    df = df[df["Emissão"].apply(lambda d: d.year >= config.data_inicial.year)]
    if df.empty:
        return pd.DataFrame()
    base = df.assign(Ano=df["Emissão"].apply(lambda d: d.year), _nf=df["CNPJ"] + "|" + df["NF"])
    kg_por_ano = (
        base.dropna(subset=["Matéria-prima (kg)"]).drop_duplicates(subset=["CNPJ", "NF"])
        .groupby("Ano")["Matéria-prima (kg)"].sum()
    )
    anual = base.groupby("Ano").agg(**{"Qtde. de notas": ("_nf", "nunique"), "Valor": ("Valor", "sum")})
    # Ano sem nenhum item "MP - " fica em branco, não "0 kg": o prefixo só passou a ser usado no
    # cadastro de produtos do Bluesoft por volta de 2024 - antes disso o dado simplesmente não existe.
    anual["Matéria-prima (kg)"] = kg_por_ano.reindex(anual.index).where(lambda v: v > 0)
    variacao = anual["Valor"].pct_change()
    anual = anual.reset_index().sort_values("Ano", ascending=False)
    anual["Variação do valor"] = [
        "—" if pd.isna(v) else f"{v:+.0%}".replace(".", ",") for v in variacao.reindex(anual["Ano"]).tolist()
    ]
    anual["Valor"] = anual["Valor"].apply(_formatar_moeda_br)
    ano_atual = date.today().year
    anual["Ano"] = anual["Ano"].apply(lambda a: f"{a} (até hoje)" if a == ano_atual else str(a))
    anual["Matéria-prima (kg)"] = anual["Matéria-prima (kg)"].apply(lambda v: "" if pd.isna(v) else _formatar_kg(v))
    return anual[["Ano", "Qtde. de notas", "Valor", "Variação do valor", "Matéria-prima (kg)"]]


def _formatar_kg(valor: float | None) -> str:
    """Kg no formato brasileiro: milhar com ponto, decimal com vírgula. A partir de 100 kg arredonda
    pro inteiro ("28.748 kg"); abaixo disso mostra até 2 casas só quando houver ("4,5 kg", "20 kg").
    Vazio quando não há valor."""
    if valor is None or pd.isna(valor):
        return ""
    texto = f"{valor:,.0f}" if abs(valor) >= 100 else f"{valor:,.2f}"
    texto = texto.replace(",", "_").replace(".", ",").replace("_", ".")
    texto = texto[:-3] if texto.endswith(",00") else texto.rstrip("0") if "," in texto else texto
    return f"{texto} kg"


def pagina_historico():
    st.subheader("Histórico de devoluções")
    st.caption(
        "Todas as notas de devolução de fornecedor (Matriz e Filial SP) dos últimos anos - em aberto, "
        "vinculadas para abatimento e já quitadas, numa visão só."
    )
    _barra_lateral_atualizar_dados()

    st.markdown(
        """
        <style>
        @keyframes pulsoMotivo {
            0%, 100% { box-shadow: 0 0 0 0 rgba(238, 105, 49, 0.55); }
            50% { box-shadow: 0 0 0 8px rgba(238, 105, 49, 0); }
        }
        @keyframes entradaFiltro {
            from { opacity: 0; transform: translateY(-6px); }
            to { opacity: 1; transform: translateY(0); }
        }
        .filtro-motivo-ativo {
            display: inline-block;
            margin: 4px 0 10px;
            padding: 6px 14px;
            border-radius: 999px;
            background: #EE6931;
            color: #FFFFFF;
            font-weight: 600;
            font-size: 0.9rem;
            animation: entradaFiltro 0.35s ease-out, pulsoMotivo 1.6s ease-in-out 3;
        }
        .st-key-hist_mp_filtrado, .st-key-hist_forn_motivo {
            border: 2px solid #EE6931;
            border-radius: 12px;
            padding: 10px 12px;
            animation: entradaFiltro 0.35s ease-out, pulsoMotivo 1.6s ease-in-out 2;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    linhas = _linhas_historico_devolucoes()
    if not linhas:
        st.info("Nenhum dado carregado ainda. Clique em **Atualizar dados do Bluesoft** na barra lateral.")
        return
    df = pd.DataFrame(linhas)

    datas_emissao = [d for d in df["Emissão"] if d is not None]
    menor_data = min(datas_emissao) if datas_emissao else config.data_inicial
    maior_data = max(datas_emissao) if datas_emissao else date.today()

    col_status, col_forn, col_de, col_ate = st.columns([2, 3, 1, 1])
    status_opcoes = ["Em aberto", "Vinculada p/ abatimento", "Quitada"]
    status_escolhidos = col_status.multiselect("Status", status_opcoes, default=status_opcoes, key="hist_status")
    fornecedores_escolhidos = col_forn.multiselect(
        "Fornecedor", sorted(df["Fornecedor"].unique()), placeholder="Todos", key="hist_fornecedor"
    )
    data_de = col_de.date_input(
        "Emissão de", value=menor_data, min_value=menor_data, max_value=maior_data,
        format="DD/MM/YYYY", key="hist_data_de",
    )
    data_ate = col_ate.date_input(
        "Emissão até", value=maior_data, min_value=menor_data, max_value=maior_data,
        format="DD/MM/YYYY", key="hist_data_ate",
    )

    so_materia_prima = st.toggle(
        "Só matéria-prima", key="hist_so_mp",
        help="Mostra só as notas com itens de matéria-prima (\"MP - \" na descrição da nota fiscal).",
    )

    df = df[df["Status"].isin(status_escolhidos)]
    if so_materia_prima:
        df = df[df["Matéria-prima (kg)"].notna()]
    if fornecedores_escolhidos:
        df = df[df["Fornecedor"].isin(fornecedores_escolhidos)]
    df = df[df["Emissão"].apply(lambda d: d is not None and data_de <= d <= data_ate)]

    if df.empty:
        st.info("Nenhuma nota de devolução com esses filtros.")
        return

    # Mesma consolidação do resto do portal: o Bluesoft desmembra a mesma NF em várias duplicatas
    # (parcelas) conforme abate - a contagem é por NF (fornecedor + nº), não por duplicata.
    qtd_notas = df[["CNPJ", "NF"]].drop_duplicates().shape[0]
    total_aberto = df.loc[df["Status"] != "Quitada", "Valor"].sum()
    total_quitado = df.loc[df["Status"] == "Quitada", "Valor"].sum()
    # Kg de matéria-prima é da NF inteira (não da parcela) - soma uma vez por NF.
    df_kg = df.dropna(subset=["Matéria-prima (kg)"]).drop_duplicates(subset=["CNPJ", "NF"])

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Qtde. de notas", qtd_notas)
    col2.metric("Valor em aberto", _formatar_moeda_br(total_aberto))
    col3.metric("Valor quitado", _formatar_moeda_br(total_quitado))
    col4.metric("Matéria-prima devolvida", _formatar_kg(df_kg["Matéria-prima (kg)"].sum()) if not df_kg.empty else "—")

    sem_detalhe = df[df["Produto(s)"] == ""][["CNPJ", "NF"]].drop_duplicates().shape[0]
    if sem_detalhe:
        st.caption(
            f"ℹ️ {sem_detalhe} nota(s) ainda sem produto/kg/motivo lidos do Bluesoft - são preenchidas "
            "aos poucos a cada **Atualizar dados do Bluesoft**."
        )

    # Resumo por motivo vem antes da matéria-prima: clicar numa linha dele filtra as tabelas "Por
    # fornecedor"/"Por produto" logo abaixo. A seleção é lida do session_state antes de desenhar a
    # tabela (o resumo é calculado de forma determinística, então a linha N continua sendo o mesmo motivo).
    # Duas visões: por categoria (motivos parecidos agrupados - "fora do padrão de qualidade",
    # "produto mofado" etc. viram "Qualidade / defeito") ou pelo motivo detalhado, como escrito na nota.
    df_motivo = df.assign(
        Motivo=df["Motivo"].replace("", "Não informado"),
        Categoria=df["Motivo"].apply(categoria_motivo),
        _nf=df["CNPJ"] + "|" + df["NF"],
    )
    st.markdown("**Devoluções por motivo**")
    visao_motivo = st.radio(
        "Ver por", ["Categoria", "Motivo detalhado"], horizontal=True, key="hist_visao_motivo",
        label_visibility="collapsed",
    )
    coluna_motivo = "Categoria" if visao_motivo == "Categoria" else "Motivo"
    chave_selecao = "hist_categoria_sel" if coluna_motivo == "Categoria" else "hist_motivo_sel"
    resumo_motivo = (
        df_motivo.groupby(coluna_motivo)
        .agg(**{"Qtde. de notas": ("_nf", "nunique"), "Valor": ("Valor", "sum")})
        .reset_index()
        .sort_values(["Valor", coluna_motivo], ascending=[False, True])
        .reset_index(drop=True)
    )
    linhas_motivo = st.session_state.get(chave_selecao, {}).get("selection", {}).get("rows", [])
    motivo_escolhido = (
        resumo_motivo.iloc[linhas_motivo[0]][coluna_motivo]
        if linhas_motivo and linhas_motivo[0] < len(resumo_motivo)
        else None
    )

    st.caption(
        "Motivo escrito nas informações adicionais da nota fiscal - nem toda nota tem. "
        "**Clique numa linha** pra ver os fornecedores daquele motivo; clique de novo pra limpar."
    )
    st.dataframe(
        resumo_motivo.assign(Valor=resumo_motivo["Valor"].apply(_formatar_moeda_br)),
        hide_index=True,
        width="stretch",
        on_select="rerun",
        selection_mode="single-row",
        key=chave_selecao,
    )

    if motivo_escolhido:
        st.markdown(
            f'<span class="filtro-motivo-ativo">🔎 Filtrando por {coluna_motivo.lower()}: '
            f"{html.escape(motivo_escolhido)}</span>",
            unsafe_allow_html=True,
        )
        df_do_motivo = df_motivo[df_motivo[coluna_motivo] == motivo_escolhido]
        fornecedores_motivo = (
            df_do_motivo.groupby("Fornecedor")
            .agg(**{"Qtde. de notas": ("_nf", "nunique"), "Valor": ("Valor", "sum")})
            .reset_index()
            .sort_values("Valor", ascending=False)
        )
        with st.container(key="hist_forn_motivo"):
            st.markdown("**Fornecedores com esse motivo**")
            st.dataframe(
                fornecedores_motivo.assign(Valor=fornecedores_motivo["Valor"].apply(_formatar_moeda_br)),
                hide_index=True,
                width="stretch",
            )
        chaves_do_motivo = set(df_do_motivo["_nf"])
        df_kg = df_kg[(df_kg["CNPJ"] + "|" + df_kg["NF"]).isin(chaves_do_motivo)]

    st.markdown("**Matéria-prima devolvida**")
    st.caption("Só itens de matéria-prima (\"MP - \" na descrição da nota fiscal), em kg.")
    if df_kg.empty:
        st.caption(
            "Nenhuma matéria-prima devolvida com esse motivo."
            if motivo_escolhido
            else "Nenhuma matéria-prima identificada nas notas filtradas."
        )
    else:
        resumo_kg = (
            df_kg.groupby("Fornecedor", as_index=False)
            .agg(**{"Matéria-prima (kg)": ("Matéria-prima (kg)", "sum"), "Qtde. de notas": ("NF", "nunique")})
            .sort_values("Matéria-prima (kg)", ascending=False)
        )
        linhas_produto: dict[str, dict] = {}
        for _, linha in df_kg.iterrows():
            for produto, kg in linha["_mp_por_produto"].items():
                item = linhas_produto.setdefault(produto, {"Produto": produto, "Matéria-prima (kg)": 0.0, "notas": set()})
                item["Matéria-prima (kg)"] += kg
                item["notas"].add((linha["CNPJ"], linha["NF"]))
        resumo_produto = pd.DataFrame(
            [
                {"Produto": i["Produto"], "Matéria-prima (kg)": i["Matéria-prima (kg)"], "Qtde. de notas": len(i["notas"])}
                for i in linhas_produto.values()
            ]
        )
        # Com motivo escolhido, o container ganha uma chave diferente - o CSS anima a borda dele
        # (`.st-key-hist_mp_filtrado`), chamando atenção pra parte que mudou.
        with st.container(key="hist_mp_filtrado" if motivo_escolhido else "hist_mp"):
            col_forn_kg, col_prod_kg = st.columns(2)
            with col_forn_kg:
                st.markdown("**Por fornecedor**")
                st.dataframe(
                    resumo_kg.assign(**{"Matéria-prima (kg)": resumo_kg["Matéria-prima (kg)"].apply(_formatar_kg)}),
                    hide_index=True,
                    width="stretch",
                    # Nome com largura limitada pra coluna de kg caber sem rolar a tabela pro lado.
                    column_config={"Fornecedor": st.column_config.TextColumn(width="medium")},
                )
            with col_prod_kg:
                st.markdown("**Por produto**")
                if resumo_produto.empty:
                    st.caption("Nenhum item de matéria-prima com kg identificado.")
                else:
                    resumo_produto = resumo_produto.sort_values("Matéria-prima (kg)", ascending=False)
                    st.dataframe(
                        resumo_produto.assign(
                            **{"Matéria-prima (kg)": resumo_produto["Matéria-prima (kg)"].apply(_formatar_kg)}
                        ),
                        hide_index=True,
                        width="stretch",
                        column_config={"Produto": st.column_config.TextColumn(width="medium")},
                    )

    st.markdown("**Comparativo ano a ano**")
    st.caption("Pelo ano de emissão da nota, respeitando os filtros acima. A variação é contra o ano anterior.")
    st.dataframe(
        _comparativo_anual(df),
        hide_index=True,
        width="stretch",
    )

    st.markdown("**Devoluções por mês de emissão**")
    df_mes = df.assign(Mês=df["Emissão"].apply(lambda d: date(d.year, d.month, 1)))
    grafico = (
        alt.Chart(df_mes.groupby(["Mês", "Status"], as_index=False)["Valor"].sum())
        .mark_bar()
        .encode(
            x=alt.X("yearmonth(Mês):T", title=None, axis=alt.Axis(format="%m/%Y", labelAngle=0)),
            y=alt.Y("Valor:Q", title=None),
            color=alt.Color(
                "Status:N",
                scale=alt.Scale(domain=status_opcoes, range=["#EE6931", "#F5B38F", "#8A8A8A"]),
                legend=alt.Legend(orient="top", title=None, labelLimit=250, columnPadding=24),
            ),
            tooltip=[
                alt.Tooltip("yearmonth(Mês):T", title="Mês", format="%m/%Y"),
                "Status:N",
                alt.Tooltip("Valor:Q", format=",.2f"),
            ],
        )
    )
    st.altair_chart(grafico, width="stretch")

    df_tabela = df.sort_values("Emissão", ascending=False).drop(columns=["CNPJ", "_mp_por_produto"])
    st.dataframe(
        # Na tela o kg vai formatado ("28.748 kg"); o Excel abaixo continua com o número puro.
        df_tabela.assign(**{"Matéria-prima (kg)": df_tabela["Matéria-prima (kg)"].apply(_formatar_kg)}),
        hide_index=True,
        width="stretch",
        height=520,
        column_config={
            "Emissão": st.column_config.DateColumn(format="DD/MM/YYYY"),
            "Vencimento": st.column_config.DateColumn(format="DD/MM/YYYY"),
            "Quitação": st.column_config.DateColumn(format="DD/MM/YYYY"),
            "Valor": st.column_config.NumberColumn(format="R$ %.2f"),
        },
    )

    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df_tabela.to_excel(writer, sheet_name="Histórico de devoluções", index=False)
    st.download_button(
        "Baixar em Excel",
        data=buffer.getvalue(),
        file_name=f"historico_devolucoes_{date.today():%Y-%m-%d}.xlsx",
        icon=":material/download:",
    )


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

    if not _modo_leitura() and st.button("Verificar boletos de factoring", icon=":material/search:", type="primary"):
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

    if not _modo_leitura() and st.button(
        "Reprocessar não confirmados",
        icon=":material/refresh:",
        help="Relê os boletos que ficaram como \"não confirmado\" com a leitura mais recente, usando os "
        "arquivos já baixados (não abre o Bluesoft).",
    ):
        barra = st.progress(0.0, text="Relendo boletos não confirmados...")

        def _progresso_reprocessar(indice: int, total: int) -> None:
            barra.progress(indice / total if total else 1.0, text=f"Relendo boletos: {indice}/{total}")

        reprocessados, decididos = reprocessar_nao_verificados(progress_callback=_progresso_reprocessar)
        barra.empty()
        st.session_state["mensagem_factoring"] = (
            f"{reprocessados} boletos não confirmados relidos - {decididos} agora têm resposta "
            "(normal ou factoring)."
        )
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

    if boletos and not _modo_leitura():
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

    if _modo_leitura():
        return

    st.markdown("**Beneficiários autorizados (mesmo grupo do fornecedor)**")
    st.caption(
        "Empresas que podem aparecer como beneficiário no boleto de um fornecedor sem ser factoring "
        "(outra empresa do mesmo dono, depósito, filial com outro CNPJ). Boletos assim passam a contar "
        "como **Normal** - inclusive os já marcados como Factoring."
    )
    fornecedores_boletos = {
        f"{b.favorecido.strip()} — {b.favorecido_cnpj}": (b.favorecido.strip(), b.favorecido_cnpj)
        for b in sorted(boletos_todos, key=lambda b: b.favorecido.strip())
    }
    with st.form(key="form_beneficiario_autorizado"):
        col_forn_aut, col_cnpj_aut, col_nome_aut = st.columns([3, 2, 2])
        fornecedor_aut = col_forn_aut.selectbox("Fornecedor", options=list(fornecedores_boletos.keys()))
        cnpj_aut = col_cnpj_aut.text_input("CNPJ do beneficiário", placeholder="50.033.764/0001-51")
        nome_aut = col_nome_aut.text_input("Nome do beneficiário", placeholder="R S GROSHEVIS LTDA")
        if st.form_submit_button("Autorizar", icon=":material/verified:", type="primary"):
            digitos_aut = re.sub(r"\D", "", cnpj_aut)
            if len(digitos_aut) < 8:
                st.warning("Informe o CNPJ do beneficiário (pelo menos os 8 primeiros dígitos).")
            else:
                nome_forn_aut, cnpj_forn_aut = fornecedores_boletos[fornecedor_aut]
                storage.salvar_beneficiario_autorizado(
                    re.sub(r"\D", "", cnpj_forn_aut)[:8], digitos_aut[:8], nome_forn_aut, nome_aut.strip()
                )
                alterados_aut = aplicar_beneficiarios_autorizados()
                st.session_state["mensagem_factoring"] = (
                    f"Beneficiário autorizado. {alterados_aut} boleto(s) que estavam como Factoring passaram a Normal."
                )
                st.rerun()

    autorizados_lista = storage.carregar_beneficiarios_autorizados()
    for raiz_forn_aut, raiz_ben_aut, nome_forn_lista, nome_ben_lista in autorizados_lista:
        col_texto_aut, col_remover_aut = st.columns([5, 1])
        col_texto_aut.caption(
            f"✅ **{nome_forn_lista}** pode receber via **{nome_ben_lista or 'CNPJ ' + raiz_ben_aut}** "
            f"(raiz {raiz_ben_aut})"
        )
        if col_remover_aut.button("Remover", key=f"remover_aut_{raiz_forn_aut}_{raiz_ben_aut}"):
            storage.remover_beneficiario_autorizado(raiz_forn_aut, raiz_ben_aut)
            st.rerun()

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

    if not _modo_leitura() and st.button("🔄 Atualizar dados da planilha", key="atualizar_antecipacao"):
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

    ultima_modificacao_planilha = data_modificacao(config.antecipacao_planilha_path)
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
# lateral) - a proteção de verdade já é o `exigir_login`/`chave` lá em cima, que bloqueia o acesso
# via URL direta a qualquer página que não seja a de Antecipação pra quem não estiver logado nem
# tiver o link de visualização. Quem entrou em modo leitura (link `?chave=`) também vê o menu
# completo, senão ficaria preso numa página só, sem conseguir navegar pelo portal.
_pode_ver_menu_paginas_privadas = bool(st.session_state.get("autenticado")) or _modo_leitura()
_visibilidade_paginas_privadas = "visible" if _pode_ver_menu_paginas_privadas else "hidden"

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
            pagina_historico,
            title="Histórico de Devoluções",
            icon=":material/history:",
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

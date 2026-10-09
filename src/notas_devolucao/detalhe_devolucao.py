"""Interpreta o que `navigation.obter_detalhe_da_fatura` leu da nota fiscal de uma devolução:
quanto de matéria-prima (em kg) foi devolvido e qual o motivo da devolução.

Regras confirmadas numa amostra de 25 notas reais (out/2026):
- Matéria-prima é identificada pelo prefixo "MP - " na descrição do item (ex.: "MP - CANJICA
  BRANCA KG", "MP - FLOCAO DE MILHO KG SC 20.0"). Embalagens, EPI, peças etc. não têm esse prefixo.
- Quantidade de item "... KG" já está em kg; "... KG SC 20.0" está em sacos de 20 kg (11 sacos =
  220 kg, batendo com o peso líquido da NF). Sem nenhum dos dois padrões, cai no peso líquido da
  nota (aba Transporte) quando a nota só tem matéria-prima.
- Motivo fica nas "Informações adicionais da nota", misturado com textos automáticos do Bluesoft
  ("Devolução da mercadoria adquirida pela nota fiscal nº ...", "Valor total de IPI = ...", chave
  de acesso etc.) - esses trechos automáticos são descartados e o que sobra é o motivo
  (ex.: "PROBLEMA DE QUALIDADE", "Nota de devolução solicitada pelo Andre da Qualidade em ...")."""

from __future__ import annotations

import json
import re
import unicodedata

from .models import DetalheDevolucao

_PADRAO_SACO = re.compile(r"\bSC\s*(\d+(?:[.,]\d+)?)", re.IGNORECASE)
_PADRAO_KG = re.compile(r"\bKG\b", re.IGNORECASE)

# Trechos automáticos das informações adicionais (comparados sem acento e em minúsculas).
_TRECHOS_AUTOMATICOS = [
    re.compile(
        r"devolucao da mercadoria adquirida pela nota fiscal\s*n\S*\s*[\d-]+\s*(emitida em \d{2}/\d{2}/\d{4})?"
        r"(,?\s*emitida por empresa optante pelo simples nacional\.?)?"
    ),
    re.compile(r"valor total de (ipi|frete|icms[\w -]*|seguro|outras despesas)\s*=\s*r\$\s*[\d.,]+"),
    re.compile(r"chave de acesso da nota fiscal de venda:?[\d ]*"),
    re.compile(r"^nf de venda$"),
]


def _sem_acento(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", texto) if unicodedata.category(c) != "Mn")


def eh_materia_prima(descricao: str) -> bool:
    return descricao.strip().upper().startswith("MP -")


def kg_do_item(item: dict) -> float | None:
    """Kg de um item de matéria-prima pela própria descrição/quantidade - None quando a descrição
    não diz a unidade (aí quem chama decide se usa o peso da nota)."""
    quantidade = item.get("quantidade")
    if quantidade is None:
        return None
    descricao = item.get("descricao") or ""
    saco = _PADRAO_SACO.search(descricao)
    if saco:
        return quantidade * float(saco.group(1).replace(",", "."))
    if _PADRAO_KG.search(descricao):
        return quantidade
    return None


def kg_materia_prima(detalhe_bruto: dict) -> float | None:
    """Total de matéria-prima devolvida na nota, em kg - None quando a nota não tem nenhum item de
    matéria-prima (ex.: devolução de embalagem, peça, EPI)."""
    itens_mp = [i for i in detalhe_bruto.get("itens", []) if eh_materia_prima(i.get("descricao") or "")]
    if not itens_mp:
        return None
    kgs = [kg_do_item(i) for i in itens_mp]
    if all(k is not None for k in kgs):
        return round(sum(kgs), 3)
    so_materia_prima = len(itens_mp) == len(detalhe_bruto.get("itens", []))
    if so_materia_prima and detalhe_bruto.get("peso_liquido"):
        return detalhe_bruto["peso_liquido"]
    conhecidos = [k for k in kgs if k is not None]
    return round(sum(conhecidos), 3) if conhecidos else None


def kg_por_produto_materia_prima(detalhe: DetalheDevolucao) -> dict[str, float]:
    """Kg de cada item de matéria-prima da nota (a partir dos itens guardados em `itens_json`).
    Item sem unidade na descrição só entra quando é o único item de matéria-prima da nota - aí o
    total da nota (`peso_kg`) é dele."""
    if not detalhe.itens_json:
        return {}
    itens_mp = [i for i in json.loads(detalhe.itens_json).get("itens", []) if eh_materia_prima(i.get("descricao") or "")]
    resultado: dict[str, float] = {}
    for item in itens_mp:
        kg = kg_do_item(item)
        if kg is None and len(itens_mp) == 1:
            kg = detalhe.peso_kg
        if kg is not None:
            resultado[item["descricao"]] = resultado.get(item["descricao"], 0.0) + kg
    return resultado


def extrair_motivo(informacoes_adicionais: list[str]) -> str | None:
    partes: list[str] = []
    for bloco in informacoes_adicionais:
        for trecho in bloco.split(" | "):
            limpo = _sem_acento(trecho).strip().lower()
            for padrao in _TRECHOS_AUTOMATICOS:
                limpo = padrao.sub("", limpo).strip(" .,;-|")
            if limpo:
                # Guarda o texto original (com acento) quando nada foi removido; senão o que sobrou.
                original = trecho.strip()
                texto = original if _sem_acento(original).lower().strip(" .,;-|") == limpo else limpo.upper()
                if texto not in partes:
                    partes.append(texto)
    return " / ".join(partes) or None


def montar_detalhe(duplicata_key: str, fatura_key: str | None, detalhe_bruto: dict | None) -> DetalheDevolucao:
    if not detalhe_bruto:
        return DetalheDevolucao(duplicata_key=duplicata_key, fatura_key=fatura_key)
    descricoes = list(dict.fromkeys(i["descricao"] for i in detalhe_bruto["itens"] if i.get("descricao")))
    return DetalheDevolucao(
        duplicata_key=duplicata_key,
        fatura_key=fatura_key,
        produtos="; ".join(descricoes) or None,
        peso_kg=kg_materia_prima(detalhe_bruto),
        motivo=extrair_motivo(detalhe_bruto.get("informacoes_adicionais", [])),
        itens_json=json.dumps(
            {k: detalhe_bruto.get(k) for k in ("itens", "peso_bruto", "peso_liquido", "informacoes_adicionais")},
            ensure_ascii=False,
        ),
    )


# --- Limpeza e categoria do motivo (usados na exibição - valem também pros motivos já gravados) ---

# Textos fiscais/logísticos que alguns fornecedores imprimem nas informações adicionais e que não
# são motivo de devolução - levantados dos ~240 motivos distintos reais (out/2026).
_TRECHOS_SEM_MOTIVO = [
    re.compile(r"imposto recolhido por substituicao.*"),
    re.compile(r"base de calculo.*"),
    re.compile(r"valor de icms.*"),
    re.compile(r"icms de substituicao tributaria.*"),
    re.compile(r"nota fiscal icms diferido.*"),
    re.compile(r"chave de acesso.*"),
    re.compile(r"^nf de venda\s*[\d.\-/ ]*$"),
    re.compile(r"^coletar em .*"),
    re.compile(r"^transportadora .*"),
    re.compile(r"^endereco de coleta.*"),
    re.compile(r"^volumes:.*peso.*"),
    re.compile(r"^descricao do item na nota:.*"),
]
_SIMPLES_NACIONAL = re.compile(r",?\s*emitida por empresa optante pelo simples nacional\.?")
_DATA_NO_TEXTO = re.compile(r"\s*(em|dia)?\s*\d{1,2}[/-]\d{1,2}([/-]\d{2,4})?")


def limpar_motivo(motivo: str | None) -> str:
    """Tira datas, textos fiscais e o aviso de Simples Nacional do motivo, padronizando em
    maiúsculas sem acento - assim o mesmo motivo escrito em notas diferentes vira uma linha só."""
    if not motivo:
        return ""
    partes = []
    for parte in motivo.split(" / "):
        texto = _SIMPLES_NACIONAL.sub("", _sem_acento(parte).lower())
        texto = _DATA_NO_TEXTO.sub("", texto).strip(" .,;-")
        if texto and not any(p.match(texto) for p in _TRECHOS_SEM_MOTIVO):
            partes.append(texto.upper())
    return " / ".join(dict.fromkeys(partes))


# Ordem importa: o primeiro grupo que casar ganha. "Falta" e "Qualidade" vêm antes de "Divergência"
# porque vários motivos começam com o texto genérico "por estar em desacordo com o pedido" e só
# depois dizem o motivo real ("... / produto com defeito").
CATEGORIAS_MOTIVO: list[tuple[str, tuple[str, ...]]] = [
    ("Falta / quantidade menor", (
        "falta", "faltante", "faltou", "falatante", "quantidade menor", "peso (kg) diferente",
        "kg faltante", "nao ter recebido",
    )),
    ("Qualidade / defeito", (
        "qualidade", "padrao", "padroes", "padros", "defeito", "impropri", "mofad", "amarelad",
        "danific", "avaria", "delamin", "mancha", "especificac", "vencimento", "diluindo", "emenda",
        "impressao", "vinco", "descricao incorreta", "fotocelula", "descarte", "embolor", "carunch",
        "sujidade", "estourad", "validade", "selagem", "folga", "sem identificacao", "embalagem incorreta",
    )),
    ("Divergência de pedido", (
        "desacordo", "divergen", "errad", "modelo", "invertido", "alteracao de produto", "troca",
        "pedido alterado", "excedente", "nao utilizamos",
    )),
    ("Excesso de estoque", ("excesso",)),
    ("Ajuste fiscal / nota", (
        "icms", "substituicao de nota", "subtituicao de nf", "nota fiscal", "bonificacao", "cfop",
        "aliquota", "exportacao",
    )),
    ("Só solicitante (sem motivo)", ("solicit", "conforme")),
]


def categoria_motivo(motivo_limpo: str) -> str:
    if not motivo_limpo:
        return "Não informado"
    texto = motivo_limpo.lower()
    for categoria, palavras in CATEGORIAS_MOTIVO:
        if any(p in texto for p in palavras):
            return categoria
    return "Outros"

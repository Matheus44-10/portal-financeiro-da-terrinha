from __future__ import annotations

import logging
import re
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import pdfplumber
from playwright.sync_api import Locator, Page

from . import navigation, ocr
from .config import ANEXOS_DIR, BLUESOFT_BASE_URL
from .models import BoletoFactoring, ContaPagar

logger = logging.getLogger("notas_devolucao.factoring")

_PADRAO_DATA_QUITACAO = re.compile(r"Data de quita[çc][ãa]o\s*\n(.+?)\n")


def _extrair_data_quitacao(texto_pagina: str) -> date | None:
    resultado = _PADRAO_DATA_QUITACAO.search(texto_pagina)
    if not resultado:
        return None
    texto_data = resultado.group(1).strip()
    try:
        return datetime.strptime(texto_data, "%d/%m/%Y").date()
    except ValueError:
        return None


_PADRAO_PAGAMENTO_EDI = re.compile(r"atraves de edi|pagamento por edi")


def verificar_pagamento_via_edi(page: Page, duplicata_key: str) -> bool:
    """Lê a tela de "Ocorrências" da duplicata (URL direta, mesma SPA hash-routing da tela de
    detalhe) e procura por uma ocorrência de pagamento processado via EDI bancário - confirmado ao
    vivo (duplicata 1269853) que esse texto aparece como "Duplicata autorizada para pagamento por
    EDI" e/ou "Ocorrência informada pelo banco através de EDI: ...". Sinal útil pra priorizar
    revisão manual (pagamentos via EDI tendem a ter o beneficiário validado pelo banco, batendo com
    o fornecedor cadastrado) - não decide status sozinho, só informa."""
    url = (
        f"{BLUESOFT_BASE_URL}/erp-app/areas/financeiro/ocorrencia-duplicata/index.action"
        f"?origem=Contasapagarjsp/ocorrencias.jsp#/financeiro/ocorrencia-duplicata/{duplicata_key}"
    )
    page.goto(url)
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1500)
    texto = _normalizar(page.evaluate("() => document.body.innerText"))
    return bool(_PADRAO_PAGAMENTO_EDI.search(texto))


def dividir_em_janelas(data_de: date, data_ate: date, dias: int = 31) -> list[tuple[date, date]]:
    """O Bluesoft rejeita período de vencimento >31 dias em Contas a Pagar sem um favorecido
    informado - como aqui queremos todos os favorecidos, o período é quebrado em janelas menores."""
    janelas: list[tuple[date, date]] = []
    atual = data_de
    while atual <= data_ate:
        fim = min(atual + timedelta(days=dias - 1), data_ate)
        janelas.append((atual, fim))
        atual = fim + timedelta(days=1)
    return janelas


def extrair_texto_pdf(caminho: Path) -> str:
    with pdfplumber.open(caminho) as pdf:
        return "\n".join(pagina.extract_text() or "" for pagina in pdf.pages)


# --- Extração do CNPJ do beneficiário no texto do boleto (portado de bordero_edi/validacao.py) ---

# Separadores OPCIONAIS e de qualquer tipo (`\D?`) - dois casos reais confirmados nos anexos já
# baixados (out/2026): (1) a fonte embutida de alguns PDFs corrompe a pontuação ("79.125.936i0001-07",
# boleto SICOOB da duplicata 1269853); (2) muitos boletos, principalmente Sicredi, imprimem o CNPJ
# como 14 dígitos corridos sem pontuação nenhuma ("... PLANALTINA DO PARANA 62018886000158") - esse
# segundo caso sozinho respondia por ~100 dos "não confirmados". Como aceitar qualquer sequência de
# 14 dígitos abre espaço pra lixo (linha digitável, telefone), só vale como CNPJ o que passa no
# dígito verificador (`_cnpj_valido`).
_PADRAO_CNPJ = re.compile(r"(?<!\d)\d{2}\D?\d{3}\D?\d{3}\D?\d{4}\D?\d{2}(?!\d)")
# Rótulo do beneficiário tolerante a OCR: o Tesseract lê "Beneficiário" como "Beneliciário",
# "Benef1ciario" etc. (confirmado em boletos do Moinho Romariz) - o literal nunca casava.
_ROTULO_BENEFICIARIO = re.compile(r"bene\w{0,3}ci\w{0,2}rio|cedente|favorecido")
# "Beneficiário final" e "Sacador/Avalista" são o fornecedor ORIGINAL num boleto cedido a um fundo -
# nunca podem ser lidos como o beneficiário (daria um "normal" falso justamente no caso de factoring).
_ROTULO_BENEFICIARIO_FINAL = re.compile(r"bene\w{0,3}ci\w{0,2}rio\s*final")
_ROTULO_PAGADOR = re.compile(r"pagador|sacado|sacador|avalista")
_JANELA_LINHAS = 6
# CNPJ das empresas do grupo que pagam os boletos (Da Terrinha, 2JM Amidos) - nunca são o beneficiário.
_RAIZES_PAGADOR = {"15074376", "45210657"}
# Linha digitável do boleto ("34191.09008 02406.410775 ...") - o último bloco dela (fator de
# vencimento + valor) tem 14 dígitos corridos e às vezes passa no dígito verificador de CNPJ por
# coincidência (caso real: duplicata 1361654, "14520001556447"). Linhas assim nunca têm CNPJ.
_PADRAO_LINHA_DIGITAVEL = re.compile(r"\d{5}\D\d{5}\s+\d{5}\D\d{6}")


def _normalizar(texto: str) -> str:
    sem_acento = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii")
    return sem_acento.lower()


def _so_digitos(texto: str) -> str:
    return re.sub(r"\D", "", texto)


def _cnpj_valido(digitos: str) -> bool:
    if len(digitos) != 14 or len(set(digitos)) == 1:
        return False

    def _dv(base: str) -> int:
        pesos = list(range(len(base) - 7, 1, -1)) + list(range(9, 1, -1))
        resto = sum(int(d) * p for d, p in zip(base, pesos)) % 11
        return 0 if resto < 2 else 11 - resto

    return _dv(digitos[:12]) == int(digitos[12]) and _dv(digitos[:13]) == int(digitos[13])


def _raiz_cnpj(cnpj: str) -> str:
    """Os 8 primeiros dígitos do CNPJ identificam a empresa (a "raiz") - os 4 seguintes são só o
    número da filial/matriz. Comparar o CNPJ completo (14 dígitos) é um falso positivo real e
    confirmado: a mesma empresa pode emitir o boleto por uma filial diferente da que está
    cadastrada na duplicata do Bluesoft (ex.: Elastobor cadastrada como .../0002-10, boleto emitido
    por .../0001-39 - mesma empresa, raiz 53.840.542 idêntica, filial diferente)."""
    digitos = _so_digitos(cnpj)
    return digitos[:8]


def _limpar_texto(texto: str) -> str:
    # Alguns PDFs saem do pdfplumber com "(cid:3)" no lugar de espaço (fonte sem mapa de caracteres).
    return texto.replace("(cid:3)", " ")


def extrair_cnpjs_beneficiario(texto_boleto: str) -> tuple[list[str], list[str], str]:
    """Acha o(s) CNPJ(s) do BENEFICIÁRIO/CEDENTE (quem recebe o pagamento) no boleto -
    especificamente, não qualquer CNPJ impresso em qualquer lugar do documento (um CNPJ de
    referência/descrição pode aparecer no texto sem ser o beneficiário de verdade - mesmo cuidado
    já validado no projeto irmão "Criação de borderô EDI titulos", caso Dell/Supplier Flexpay).

    Acha linhas com um rótulo de beneficiário e procura um CNPJ nela mesma ou nas linhas seguintes
    (janela de `_JANELA_LINHAS`) - mas para de procurar se topar com um rótulo de pagador/sacado/
    sacador/avalista/beneficiário final antes, pra não vazar pro CNPJ da seção errada.

    Retorna (CNPJs válidos, CNPJs com dígito verificador inválido - provável erro de OCR -, trecho
    de texto onde foram encontrados, para conferência humana)."""
    linhas = texto_boleto.splitlines()
    linhas_norm = [_normalizar(l) for l in linhas]
    validos: set[str] = set()
    invalidos: set[str] = set()
    trechos: list[str] = []

    for i, linha_norm in enumerate(linhas_norm):
        if _ROTULO_BENEFICIARIO_FINAL.search(linha_norm) or not _ROTULO_BENEFICIARIO.search(linha_norm):
            continue
        for j in range(i, min(i + _JANELA_LINHAS, len(linhas))):
            if j > i and (_ROTULO_PAGADOR.search(linhas_norm[j]) or _ROTULO_BENEFICIARIO_FINAL.search(linhas_norm[j])):
                break
            if _PADRAO_LINHA_DIGITAVEL.search(linhas[j]):
                continue
            encontrados = [_so_digitos(m) for m in _PADRAO_CNPJ.findall(linhas[j])]
            encontrados = [c for c in encontrados if _raiz_cnpj(c) not in _RAIZES_PAGADOR]
            if encontrados:
                for c in encontrados:
                    (validos if _cnpj_valido(c) else invalidos).add(c)
                trechos.append(linhas[j].strip())

    return sorted(validos), sorted(invalidos), " | ".join(dict.fromkeys(trechos))


def _cnpjs_fora_do_bloco_pagador(texto: str) -> list[str]:
    """Todos os CNPJs válidos do documento, menos os do bloco do pagador/sacado/sacador/avalista
    (a linha do rótulo e as 2 seguintes, onde ficam os valores) e o da própria Da Terrinha."""
    linhas = texto.splitlines()
    linhas_norm = [_normalizar(l) for l in linhas]
    bloqueadas: set[int] = set()
    for i, linha_norm in enumerate(linhas_norm):
        if _ROTULO_PAGADOR.search(linha_norm) or _ROTULO_BENEFICIARIO_FINAL.search(linha_norm):
            bloqueadas.update(range(i, i + 3))
    achados: set[str] = set()
    for i, linha in enumerate(linhas):
        if i in bloqueadas or _PADRAO_LINHA_DIGITAVEL.search(linha):
            continue
        for m in _PADRAO_CNPJ.findall(linha):
            c = _so_digitos(m)
            if _cnpj_valido(c) and _raiz_cnpj(c) not in _RAIZES_PAGADOR:
                achados.add(c)
    return sorted(achados)


# Alguns boletos (confirmado ao vivo: layout Bradesco da duplicata 1554727) não têm nenhuma palavra
# "beneficiário"/"cedente" no texto extraído - o nome da financeira aparece solto perto do
# cabeçalho, sem rótulo. Fundos de recebíveis/factoring seguem convenções de nome características
# (FIDC, "Fundo de Investimento em Direitos Creditórios", "Fomento Mercantil", etc.) - procurar
# esses termos é um sinal independente e mais confiável que a busca por rótulo nesses casos. Foi
# esse padrão que revelou a duplicata 1554727 como factoring de verdade (GFM FUNDO DE INV. EM
# DIREITOS CREDITORIOS MULTICREDITO), que a busca por rótulo sozinha não pegava. "fundo de
# investimento"/"fundo de inv." entraram depois de um caso real perdido ("CASE FUNDO DE
# INVESTIMENTO", sem "direitos creditórios" no nome).
_PALAVRAS_CHAVE_FACTORING = (
    "fundo de investimento",
    "fundo de inv.",
    "direitos creditorios",
    "fomento mercantil",
    "factoring",
    "securitizadora",
    "securitizacao",
    "fidc",
    "cessao de credito",
    "credito cedido",
)


def detectar_indicio_factoring(texto: str) -> tuple[list[str], str]:
    """Procura por termos característicos de fundos de recebíveis/factoring no texto do boleto,
    independente de haver ou não um rótulo "beneficiário"/"cedente". Retorna os CNPJs achados perto
    do termo (se houver) e a linha onde o termo apareceu, para conferência humana."""
    linhas = texto.splitlines()
    linhas_norm = [_normalizar(l) for l in linhas]
    for i, linha_norm in enumerate(linhas_norm):
        if any(palavra in linha_norm for palavra in _PALAVRAS_CHAVE_FACTORING):
            janela = " ".join(linhas[max(0, i - 1) : i + 2])
            cnpjs_na_janela = sorted(
                {c for c in (_so_digitos(m) for m in _PADRAO_CNPJ.findall(janela)) if _cnpj_valido(c)}
            )
            return cnpjs_na_janela, linhas[i].strip()
    return [], ""


# Palavras genéricas de razão social - não identificam a empresa (mesma ideia do casamento por nome
# do projeto "Lançamento de notas").
_PALAVRAS_GENERICAS_NOME = {
    "ltda", "eireli", "me", "epp", "sa", "s", "a", "comercio", "comercial", "industria", "industrial",
    "ind", "com", "de", "da", "do", "das", "dos", "e", "imp", "exp", "importacao", "exportacao",
    "produtos", "prod", "alimenticios", "alimentos", "distribuidora", "servicos", "brasil", "the",
    "grupo", "cia", "companhia", "empresa", "embalagens", "fabrica",
}


def _palavras_do_nome(nome: str) -> set[str]:
    palavras = re.findall(r"[a-z0-9]+", _normalizar(nome))
    return {p for p in palavras if len(p) >= 3 and p not in _PALAVRAS_GENERICAS_NOME}


def _nome_bate(texto: str, nome_favorecido: str) -> bool:
    """O texto cita o fornecedor pelo nome? Exige 2 palavras significativas do nome (ou a única,
    se o nome só tiver uma e ela for longa o bastante pra não casar por acaso)."""
    palavras_nome = _palavras_do_nome(nome_favorecido)
    if not palavras_nome:
        return False
    palavras_texto = set(re.findall(r"[a-z0-9]+", _normalizar(texto)))
    em_comum = palavras_nome & palavras_texto
    if len(palavras_nome) == 1:
        return bool(em_comum) and len(next(iter(palavras_nome))) >= 6
    return len(em_comum) >= 2


def nome_recebedor_pix(texto: str) -> str | None:
    """Nome do recebedor no "PIX copia e cola" impresso no boleto (padrão EMV do Banco Central:
    pares ID(2) + tamanho(2) + valor; o campo 59 é o nome do recebedor). ~1/4 dos boletos que ficavam
    sem resposta tinham esse código - é um sinal independente do rótulo "Beneficiário"."""
    # Só junta as quebras de linha (o PDF quebra o código longo em várias linhas) - espaços de
    # verdade fazem parte dos valores (o nome tem espaço) e contam no tamanho de cada campo.
    compacto = re.sub(r"[ \t]*\r?\n[ \t]*", "", texto)
    inicio = compacto.find("000201")
    if inicio < 0 or "br.gov.bcb.pix" not in compacto.lower():
        return None
    i = inicio
    while i + 4 <= len(compacto):
        campo, tamanho = compacto[i : i + 2], compacto[i + 2 : i + 4]
        if not (campo.isdigit() and tamanho.isdigit()):
            return None
        valor = compacto[i + 4 : i + 4 + int(tamanho)]
        if campo == "59":
            return valor or None
        if campo == "63":
            return None
        i += 4 + int(tamanho)
    return None


def _bloco_do_beneficiario(texto: str) -> str:
    """Texto do rótulo "Beneficiário/Cedente" e das 2 linhas seguintes (onde fica o nome), parando
    no pagador/sacador - pra conferir o NOME quando o CNPJ saiu ilegível."""
    linhas = texto.splitlines()
    linhas_norm = [_normalizar(l) for l in linhas]
    blocos = []
    for i, linha_norm in enumerate(linhas_norm):
        if _ROTULO_BENEFICIARIO_FINAL.search(linha_norm) or not _ROTULO_BENEFICIARIO.search(linha_norm):
            continue
        for j in range(i, min(i + 3, len(linhas))):
            if j > i and _ROTULO_PAGADOR.search(linhas_norm[j]):
                break
            blocos.append(linhas[j])
    return "\n".join(blocos)


def _difere_em_um_digito(a: str, b: str) -> bool:
    return len(a) == len(b) and sum(x != y for x, y in zip(a, b)) == 1


def classificar_texto(
    texto: str,
    cnpj_cadastrado: str,
    raizes_autorizadas: set[str] | frozenset[str] = frozenset(),
    nome_favorecido: str = "",
) -> tuple[str | None, list[str], str]:
    """Decide, só pelo texto de um anexo, se o boleto foi emitido pelo fornecedor cadastrado
    ("normal"), por outra empresa/fundo ("factoring") - ou None quando não dá pra decidir com
    segurança. Retorna (status, CNPJs que embasaram a decisão, trecho/explicação).

    Ordem dos sinais, do mais forte pro mais fraco:
    1. CNPJ válido no campo Beneficiário/Cedente: raiz igual à cadastrada = normal, diferente =
       factoring (mesmo que o texto cite "factoring" em algum aviso, o beneficiário manda).
    2. Palavra-chave de fundo/factoring no texto = factoring.
    3. CNPJ do beneficiário lido por OCR com dígito verificador inválido, mas a 1 dígito da raiz
       cadastrada = normal (erro de OCR típico, ex.: 53.031.664 lido no lugar de 53.031.654).
    4. Sem rótulo de beneficiário: se o ÚNICO CNPJ do documento (fora do bloco do pagador) é do
       fornecedor cadastrado = normal.
    5. Nome do recebedor no PIX copia-e-cola bate com o fornecedor = normal.
    6. Nome do fornecedor escrito no campo Beneficiário (CNPJ ilegível) = normal.
    Os sinais 5 e 6 só confirmam "normal" - nome diferente não basta pra afirmar factoring
    (o PIX às vezes vem em nome do banco/intermediador de pagamento).

    `raizes_autorizadas`: outras empresas (raiz do CNPJ) que o financeiro confirmou poderem emitir
    boleto por esse fornecedor sem ser factoring (mesmo dono/grupo) - contam como o próprio
    fornecedor (ver `storage.beneficiarios_autorizados`)."""
    texto = _limpar_texto(texto)
    raiz_cadastrada = _raiz_cnpj(cnpj_cadastrado)
    raizes_aceitas = {raiz_cadastrada} | set(raizes_autorizadas)

    validos, invalidos, trecho = extrair_cnpjs_beneficiario(texto)
    cnpjs_kw, trecho_kw = detectar_indicio_factoring(texto)

    if validos:
        raizes_validas = {_raiz_cnpj(c) for c in validos}
        if raiz_cadastrada in raizes_validas:
            return "normal", validos, trecho
        if raizes_validas & raizes_aceitas:
            return "normal", validos, f"{trecho} (beneficiário autorizado para este fornecedor)"
        return "factoring", validos, " | ".join(dict.fromkeys(t for t in (trecho, trecho_kw) if t))

    if trecho_kw:
        return "factoring", cnpjs_kw, trecho_kw

    if any(_difere_em_um_digito(_raiz_cnpj(c), raiz_cadastrada) for c in invalidos):
        return "normal", invalidos, f"{trecho} (lido por OCR - 1 dígito divergente do cadastro)"

    outros = _cnpjs_fora_do_bloco_pagador(texto)
    if outros and {_raiz_cnpj(c) for c in outros} <= raizes_aceitas:
        return "normal", outros, "CNPJ do fornecedor encontrado no boleto (sem rótulo de beneficiário legível)"

    if nome_favorecido:
        recebedor_pix = nome_recebedor_pix(texto)
        if recebedor_pix and _nome_bate(recebedor_pix, nome_favorecido):
            return "normal", [], f"Recebedor do PIX do boleto: {recebedor_pix}"
        bloco = _bloco_do_beneficiario(texto)
        if bloco and _nome_bate(bloco, nome_favorecido):
            trecho_nome = " | ".join(l.strip() for l in bloco.splitlines() if l.strip())
            return "normal", [], f"{trecho_nome} (nome do fornecedor no campo beneficiário)"

    return None, [], ""


# --- Download do anexo de boleto (portado de bordero_edi/anexos.py, adaptado para Page) ---

_CANDIDATOS_ICONE_VISUALIZAR = [
    '[title="Preview"]',
    ".btn-success",
    ".fa-search",
    ".fa-eye",
]


def _localizar_linhas_candidatas(page: Page) -> list[Locator]:
    """Lista as linhas de anexo candidatas a ser o boleto, na grade de Anexos (ui-grid), com as que
    têm "boleto" no nome primeiro (caso comum, mais rápido de acertar de cara) seguidas de todas as
    demais. Nem todo anexo de boleto tem literalmente a palavra "boleto" no nome do arquivo
    (confirmado ao vivo) - por isso, em vez de desistir quando nenhum nome bate, a chamadora
    (`verificar_duplicata`) tenta as demais também, uma a uma, até achar uma que se pareça com um
    boleto de verdade (via `classificar_anexo`)."""
    linhas = page.locator(".ui-grid-row")
    for _ in range(10):
        if linhas.count() > 0:
            break
        page.wait_for_timeout(500)
    total = linhas.count()
    if total == 0:
        raise RuntimeError("Nenhum anexo listado nesta duplicata.")
    if total == 1:
        return [linhas.first]

    com_boleto_no_nome: list[Locator] = []
    demais: list[Locator] = []
    for i in range(total):
        linha = linhas.nth(i)
        if linha.locator("[title*='boleto' i]").count() > 0:
            com_boleto_no_nome.append(linha)
        else:
            demais.append(linha)
    return com_boleto_no_nome + demais


def _extensao_da_url(url: str, padrao: str = ".jpg") -> str:
    caminho = urlparse(url).path
    sufixo = Path(caminho).suffix
    return sufixo if sufixo else padrao


def visualizar_e_capturar_anexo(page: Page, duplicata_key: str, linha_boleto: Locator, indice: int = 0) -> Path:
    """Clica no ícone de "visualizar" (Preview) da linha de anexo indicada e baixa o arquivo, seja
    PDF ou imagem. Pra PDF, o clique dispara uma API que responde com uma URL assinada da S3 (JSON
    `{"url": "..."}`); pra imagem, abre um modal `<img src="...">` já com URL assinada própria.
    `indice` diferencia o nome do arquivo salvo quando mais de um anexo é tentado pra mesma
    duplicata (ver `_localizar_linhas_candidatas`) - evita um sobrescrever o outro no disco."""
    contexto = page.context
    urls_pdf: list[str] = []

    def _capturar_resposta(response) -> None:
        try:
            content_type = response.headers.get("content-type", "")
        except Exception:
            content_type = ""
        if "json" not in content_type.lower():
            return
        try:
            dados = response.json()
        except Exception:
            return
        url = dados.get("url") if isinstance(dados, dict) else None
        if url and ".pdf" in url.lower():
            urls_pdf.append(url)

    contexto.on("response", _capturar_resposta)
    erros: list[str] = []
    url_imagem: str | None = None
    try:
        for seletor in _CANDIDATOS_ICONE_VISUALIZAR:
            candidato = linha_boleto.locator(seletor).first
            if candidato.count() == 0:
                erros.append(f"{seletor!r}: nenhum elemento encontrado na linha do boleto")
                continue
            candidato.click()
            page.wait_for_timeout(3000)
            if urls_pdf:
                break
            modal_img = page.locator(".modal.in img").first
            if modal_img.count() > 0:
                url_imagem = modal_img.get_attribute("src")
                if url_imagem:
                    break
            erros.append(f"{seletor!r}: clicou mas não capturei nem PDF nem imagem")
            navigation.fechar_modais_pendentes(page)
    finally:
        contexto.remove_listener("response", _capturar_resposta)

    ANEXOS_DIR.mkdir(exist_ok=True)
    sufixo_nome = f"_{indice}" if indice else ""

    if urls_pdf:
        resposta = page.request.get(urls_pdf[-1])
        if not resposta.ok:
            raise RuntimeError(f"Falha ao buscar o PDF na URL assinada (status {resposta.status}).")
        destino = ANEXOS_DIR / f"duplicata_{duplicata_key}{sufixo_nome}.pdf"
        destino.write_bytes(resposta.body())
        return destino

    if url_imagem:
        resposta = page.request.get(url_imagem)
        if not resposta.ok:
            raise RuntimeError(f"Falha ao buscar a imagem do anexo (status {resposta.status}).")
        destino = ANEXOS_DIR / f"duplicata_{duplicata_key}{sufixo_nome}{_extensao_da_url(url_imagem)}"
        destino.write_bytes(resposta.body())
        return destino

    raise RuntimeError(
        "Não consegui capturar nem PDF nem imagem do anexo com nenhum dos seletores tentados:\n"
        + "\n".join(erros)
    )


# --- Orquestração ---


def classificar_anexo(
    caminho: Path,
    cnpj_cadastrado: str,
    raizes_autorizadas: set[str] | frozenset[str] = frozenset(),
    nome_favorecido: str = "",
) -> tuple[str, str | None, list[str], str]:
    """Extrai o texto de um anexo e classifica (ver `classificar_texto`), tentando leituras cada vez
    mais caras até alguma decidir: texto nativo do PDF -> OCR em escala de cinza com `--psm 6`
    (bloco único - leu 15 de 17 boletos ilegíveis no projeto "Lançamento de notas") -> OCR com
    layout automático (`--psm 3`) em cinza -> e por fim colorido (a leitura original).

    O OCR roda mesmo quando o texto nativo não está vazio, se ele não decidiu nada - confirmado ao
    vivo (duplicata 1269853, boleto SICOOB) que a fonte embutida de alguns boletos corrompe a
    extração nativa (texto cheio de caracteres, mas "Beneficiário" ilegível).

    Retorna (último texto legível, status ou None, CNPJs, trecho)."""
    eh_pdf = caminho.suffix.lower() == ".pdf"

    def _ocr(psm: int, cinza: bool = True) -> str:
        if eh_pdf:
            return ocr.extrair_texto_via_ocr_pdf(caminho, psm=psm, cinza=cinza)
        return ocr.extrair_texto_via_ocr_imagem(caminho, psm=psm, cinza=cinza)

    # Cada leitura acerta boletos diferentes - a colorida com psm 3 (a original) continua como
    # última tentativa porque leu certo alguns que a cinza não leu (ex.: duplicata 1361654).
    extratores = [lambda: _ocr(6), lambda: _ocr(3), lambda: _ocr(3, cinza=False)]
    if eh_pdf:
        extratores.insert(0, lambda: extrair_texto_pdf(caminho))

    ultimo_texto = ""
    for extrator in extratores:
        texto = extrator()
        if not texto.strip():
            continue
        ultimo_texto = texto
        status, cnpjs, trecho = classificar_texto(texto, cnpj_cadastrado, raizes_autorizadas, nome_favorecido)
        if status:
            return texto, status, cnpjs, trecho
    return ultimo_texto, None, [], ""


def anexos_ja_baixados(duplicata_key: str) -> list[Path]:
    """Arquivos de anexo que uma verificação anterior já salvou em `anexos_baixados/`
    (`duplicata_<chave>.<ext>`, `duplicata_<chave>_2.<ext>`...)."""
    if not ANEXOS_DIR.exists():
        return []
    return sorted(
        p for p in ANEXOS_DIR.glob(f"duplicata_{duplicata_key}*")
        if p.stem == f"duplicata_{duplicata_key}" or p.stem.startswith(f"duplicata_{duplicata_key}_")
    )


def reclassificar_com_anexos_baixados(
    boleto: BoletoFactoring, raizes_autorizadas: set[str] | frozenset[str] = frozenset()
) -> BoletoFactoring | None:
    """Reavalia um boleto "não verificado" usando só os anexos já baixados (sem abrir o Bluesoft).
    Retorna o boleto atualizado, ou None se não há anexo salvo pra essa duplicata."""
    arquivos = anexos_ja_baixados(boleto.duplicata_key)
    if not arquivos:
        return None
    texto_legivel = False
    for arquivo in arquivos:
        try:
            texto, status, cnpjs, trecho = classificar_anexo(
                arquivo, boleto.favorecido_cnpj, raizes_autorizadas, boleto.favorecido
            )
        except Exception as erro:
            logger.warning("Falha ao reler anexo %s: %r", arquivo.name, erro)
            continue
        texto_legivel = texto_legivel or bool(texto.strip())
        if status:
            boleto.status, boleto.cnpjs_encontrados, boleto.trecho_beneficiario = status, cnpjs, trecho
            boleto.verificado_em = datetime.now()
            return boleto
    boleto.trecho_beneficiario = (
        f"{len(arquivos)} anexo(s) lido(s) (nativo e OCR) mas nenhum CNPJ de beneficiário identificado."
        if texto_legivel
        else f"{len(arquivos)} anexo(s) tentado(s), nenhum com texto legível (nem nativo, nem OCR)."
    )
    boleto.verificado_em = datetime.now()
    return boleto


def verificar_duplicata(
    page: Page, conta: ContaPagar, raizes_autorizadas: set[str] | frozenset[str] = frozenset()
) -> BoletoFactoring:
    """Abre a duplicata, baixa o boleto anexado, extrai o texto (nativo, com fallback OCR) e
    compara o CNPJ do beneficiário achado no boleto com o favorecido cadastrado no Bluesoft. Nunca
    levanta exceção - falhas viram status "nao_verificado", pra não travar o processamento em lote."""
    agora = datetime.now()
    base = dict(
        duplicata_key=conta.duplicata_key,
        favorecido=conta.favorecido,
        favorecido_cnpj=conta.favorecido_cnpj,
        loja=conta.loja,
        loja_pagadora=conta.loja_pagadora,
        valor=conta.valor,
        data_vencimento=conta.data_vencimento,
        data_quitacao=None,
        nota_fiscal=conta.nota_fiscal,
        verificado_em=agora,
        pago_via_edi=False,
    )
    try:
        url = (
            f"{BLUESOFT_BASE_URL}/erp-app/areas/financeiro/duplicata/index.action"
            f"#/financeiro/duplicata/{conta.duplicata_key}"
        )
        page.goto(url)
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1500)
        navigation.fechar_modais_pendentes(page)

        # Lê a data de quitação de graça, do texto da própria aba "Geral" (carregada por padrão) -
        # a data de vencimento sozinha confunde numa busca que filtra por quitação (uma duplicata
        # vencida em maio pode só ter sido paga, com atraso, em julho).
        base["data_quitacao"] = _extrair_data_quitacao(page.evaluate("() => document.body.innerText"))

        base["pago_via_edi"] = verificar_pagamento_via_edi(page, conta.duplicata_key)
        # verificar_pagamento_via_edi navega pra uma tela totalmente diferente (Ocorrências, outro
        # entrypoint Angular) - volta pra tela de detalhe da duplicata antes de seguir pro Anexos.
        page.goto(url)
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1500)
        navigation.fechar_modais_pendentes(page)

        page.get_by_text("Anexos", exact=True).first.click()
        page.wait_for_timeout(1500)

        linhas_candidatas = _localizar_linhas_candidatas(page)

        texto = ""
        status: str | None = None
        cnpjs_encontrados: list[str] = []
        trecho = ""
        anexos_tentados = 0
        algum_texto_legivel = False

        for indice, linha in enumerate(linhas_candidatas):
            try:
                caminho_anexo = visualizar_e_capturar_anexo(page, conta.duplicata_key, linha, indice)
            except Exception:
                # Esse candidato específico falhou (ícone não respondeu, download deu erro etc.) -
                # tenta o próximo em vez de desistir da duplicata inteira (nem todo anexo de boleto
                # tem "boleto" no nome, então pode haver vários candidatos a testar).
                continue
            anexos_tentados += 1
            texto, status, cnpjs_encontrados, trecho = classificar_anexo(
                caminho_anexo, conta.favorecido_cnpj, raizes_autorizadas, conta.favorecido
            )
            algum_texto_legivel = algum_texto_legivel or bool(texto.strip())
            if status:
                break  # decidiu (normal ou factoring) - não precisa tentar os outros anexos

        if anexos_tentados == 0:
            return BoletoFactoring(
                **base,
                status="nao_verificado",
                cnpjs_encontrados=[],
                trecho_beneficiario="Nenhum dos anexos desta duplicata pôde ser baixado.",
            )

        if status:
            return BoletoFactoring(
                **base, status=status, cnpjs_encontrados=cnpjs_encontrados, trecho_beneficiario=trecho
            )

        return BoletoFactoring(
            **base,
            status="nao_verificado",
            cnpjs_encontrados=[],
            trecho_beneficiario=(
                f"{anexos_tentados} anexo(s) lido(s) (nativo e OCR) mas nenhum CNPJ de beneficiário identificado."
                if algum_texto_legivel
                else f"{anexos_tentados} anexo(s) tentado(s), nenhum com texto legível (nem nativo, nem OCR)."
            ),
        )
    except Exception as erro:
        logger.exception("Falha ao verificar duplicata %s.", conta.duplicata_key)
        # Só a primeira linha - `visualizar_e_capturar_anexo` pode levantar uma mensagem de várias
        # linhas (uma por seletor tentado), longa demais pra caber numa célula de tabela.
        motivo = str(erro).splitlines()[0] if str(erro) else type(erro).__name__
        return BoletoFactoring(
            **base, status="nao_verificado", cnpjs_encontrados=[], trecho_beneficiario=motivo
        )

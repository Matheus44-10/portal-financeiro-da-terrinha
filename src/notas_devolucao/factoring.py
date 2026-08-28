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

# Separadores como `\D` (qualquer caractere não-dígito), não os literais "." "/" "-": confirmado
# ao vivo (duplicata 1269853, boleto SICOOB) que a extração de texto de alguns PDFs corrompe
# caracteres especiais por causa da fonte embutida do banco emissor - a barra "/" do CNPJ vira
# letras soltas tipo "i" ou "t" (ex.: "79.125.936i0001-07" em vez de "79.125.936/0001-07"). Exigir
# a pontuação exata fazia o CNPJ nem ser reconhecido, mesmo com o rótulo "Beneficiário" do lado.
_PADRAO_CNPJ = re.compile(r"\d{2}\D\d{3}\D\d{3}\D\d{4}\D\d{2}")
_ROTULOS_BENEFICIARIO = ("beneficiario", "cedente", "favorecido")
_ROTULOS_PAGADOR = ("pagador", "sacado")
_ROTULOS_EXCLUIR = ("beneficiario final",)
_JANELA_LINHAS = 6


def _normalizar(texto: str) -> str:
    sem_acento = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii")
    return sem_acento.lower()


def _so_digitos(texto: str) -> str:
    return re.sub(r"\D", "", texto)


def _raiz_cnpj(cnpj: str) -> str:
    """Os 8 primeiros dígitos do CNPJ identificam a empresa (a "raiz") - os 4 seguintes são só o
    número da filial/matriz. Comparar o CNPJ completo (14 dígitos) é um falso positivo real e
    confirmado: a mesma empresa pode emitir o boleto por uma filial diferente da que está
    cadastrada na duplicata do Bluesoft (ex.: Elastobor cadastrada como .../0002-10, boleto emitido
    por .../0001-39 - mesma empresa, raiz 53.840.542 idêntica, filial diferente)."""
    digitos = _so_digitos(cnpj)
    return digitos[:8]


def extrair_cnpjs_beneficiario(texto_boleto: str) -> tuple[list[str], str]:
    """Acha o(s) CNPJ(s) do BENEFICIÁRIO/CEDENTE (quem recebe o pagamento) no boleto -
    especificamente, não qualquer CNPJ impresso em qualquer lugar do documento (um CNPJ de
    referência/descrição pode aparecer no texto sem ser o beneficiário de verdade - mesmo cuidado
    já validado no projeto irmão "Criação de borderô EDI titulos", caso Dell/Supplier Flexpay).

    Acha linhas com um rótulo de beneficiário ("beneficiário", "cedente", "favorecido") e procura
    um CNPJ nela mesma ou nas linhas seguintes (janela de `_JANELA_LINHAS`) - mas para de procurar
    se topar com um rótulo de pagador/sacado antes, pra não vazar pro CNPJ da seção errada.

    Retorna os CNPJs achados (só dígitos) e o trecho de texto onde foram encontrados, para
    conferência humana."""
    linhas = texto_boleto.splitlines()
    linhas_norm = [_normalizar(l) for l in linhas]
    cnpjs_achados: set[str] = set()
    trechos: list[str] = []

    for i, linha_norm in enumerate(linhas_norm):
        if any(rotulo in linha_norm for rotulo in _ROTULOS_EXCLUIR):
            continue
        if not any(rotulo in linha_norm for rotulo in _ROTULOS_BENEFICIARIO):
            continue
        for j in range(i, min(i + _JANELA_LINHAS, len(linhas))):
            if j > i and any(rotulo in linhas_norm[j] for rotulo in _ROTULOS_PAGADOR):
                break
            encontrados = _PADRAO_CNPJ.findall(linhas[j])
            if encontrados:
                cnpjs_achados.update(_so_digitos(m) for m in encontrados)
                trechos.append(linhas[j].strip())

    return sorted(cnpjs_achados), " | ".join(dict.fromkeys(trechos))


# Alguns boletos (confirmado ao vivo: layout Bradesco da duplicata 1554727) não têm nenhuma palavra
# "beneficiário"/"cedente" no texto extraído - o nome da financeira aparece solto perto do
# cabeçalho, sem rótulo. Fundos de recebíveis/factoring seguem convenções de nome características
# (FIDC, "Fundo de Investimento em Direitos Creditórios", "Fomento Mercantil", etc.) - procurar
# esses termos é um sinal independente e mais confiável que a busca por rótulo nesses casos. Foi
# esse padrão que revelou a duplicata 1554727 como factoring de verdade (GFM FUNDO DE INV. EM
# DIREITOS CREDITORIOS MULTICREDITO), que a busca por rótulo sozinha não pegava.
_PALAVRAS_CHAVE_FACTORING = (
    "fundo de investimento em direitos creditorios",
    "direitos creditorios",
    "fomento mercantil",
    "factoring",
    "securitizadora",
    "fidc",
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
            cnpjs_na_janela = sorted({_so_digitos(m) for m in _PADRAO_CNPJ.findall(janela)})
            return cnpjs_na_janela, linhas[i].strip()
    return [], ""


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
    boleto de verdade (via `_texto_e_classificacao_do_anexo`)."""
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


def _texto_e_classificacao_do_anexo(caminho: Path) -> tuple[str, list[str], str, list[str], str]:
    """Extrai o texto de um anexo (nativo, com fallback OCR se vazio) e já roda os dois sinais de
    classificação (indício de factoring por palavra-chave, CNPJ de beneficiário por rótulo).
    Retorna (texto, cnpjs_palavra_chave, trecho_palavra_chave, cnpjs_beneficiario, trecho_beneficiario).

    Tenta OCR uma segunda vez mesmo quando o texto nativo não estava vazio, caso os dois sinais
    deem vazio nele - confirmado ao vivo (duplicata 1269853, boleto SICOOB) que a fonte embutida de
    alguns boletos corrompe a extração nativa de forma que o texto vem cheio de caracteres, mas a
    própria palavra "Beneficiário" sai ilegível (virou algo como "Bene?ici??io"), fazendo o CNPJ
    nunca ser achado porque o rótulo que ancora a busca não bate."""
    eh_pdf = caminho.suffix.lower() == ".pdf"

    def _extrair_ocr() -> str:
        return ocr.extrair_texto_via_ocr_pdf(caminho) if eh_pdf else ocr.extrair_texto_via_ocr_imagem(caminho)

    texto = extrair_texto_pdf(caminho) if eh_pdf else ""
    if not texto.strip():
        texto = _extrair_ocr()

    if not texto.strip():
        return texto, [], "", [], ""

    cnpjs_kw, trecho_kw = detectar_indicio_factoring(texto)
    cnpjs, trecho = ([], "") if trecho_kw else extrair_cnpjs_beneficiario(texto)

    if not trecho_kw and not cnpjs:
        texto_ocr = _extrair_ocr()
        if texto_ocr.strip() and texto_ocr != texto:
            texto = texto_ocr
            cnpjs_kw, trecho_kw = detectar_indicio_factoring(texto)
            if not trecho_kw:
                cnpjs, trecho = extrair_cnpjs_beneficiario(texto)

    return texto, cnpjs_kw, trecho_kw, cnpjs, trecho


def verificar_duplicata(page: Page, conta: ContaPagar) -> BoletoFactoring:
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
        cnpjs_palavra_chave: list[str] = []
        trecho_palavra_chave = ""
        cnpjs_encontrados: list[str] = []
        trecho = ""
        anexos_tentados = 0

        for indice, linha in enumerate(linhas_candidatas):
            try:
                caminho_anexo = visualizar_e_capturar_anexo(page, conta.duplicata_key, linha, indice)
            except Exception:
                # Esse candidato específico falhou (ícone não respondeu, download deu erro etc.) -
                # tenta o próximo em vez de desistir da duplicata inteira (nem todo anexo de boleto
                # tem "boleto" no nome, então pode haver vários candidatos a testar).
                continue
            anexos_tentados += 1
            texto, cnpjs_palavra_chave, trecho_palavra_chave, cnpjs_encontrados, trecho = (
                _texto_e_classificacao_do_anexo(caminho_anexo)
            )
            if trecho_palavra_chave or cnpjs_encontrados:
                break  # achou indício de factoring ou CNPJ de beneficiário - não precisa tentar os outros

        if anexos_tentados == 0:
            return BoletoFactoring(
                **base,
                status="nao_verificado",
                cnpjs_encontrados=[],
                trecho_beneficiario="Nenhum dos anexos desta duplicata pôde ser baixado.",
            )

        if not texto.strip():
            return BoletoFactoring(
                **base,
                status="nao_verificado",
                cnpjs_encontrados=[],
                trecho_beneficiario=(
                    f"{anexos_tentados} anexo(s) tentado(s), nenhum com texto legível (nem nativo, nem OCR)."
                ),
            )

        if trecho_palavra_chave:
            return BoletoFactoring(
                **base,
                status="factoring",
                cnpjs_encontrados=cnpjs_palavra_chave,
                trecho_beneficiario=trecho_palavra_chave,
            )

        if not cnpjs_encontrados:
            return BoletoFactoring(
                **base,
                status="nao_verificado",
                cnpjs_encontrados=[],
                trecho_beneficiario=(
                    f"{anexos_tentados} anexo(s) lido(s) (nativo e OCR) mas nenhum CNPJ de "
                    "beneficiário identificado."
                ),
            )

        raiz_cadastrada = _raiz_cnpj(conta.favorecido_cnpj)
        raizes_encontradas = {_raiz_cnpj(c) for c in cnpjs_encontrados}
        status = "normal" if raiz_cadastrada in raizes_encontradas else "factoring"
        return BoletoFactoring(
            **base, status=status, cnpjs_encontrados=cnpjs_encontrados, trecho_beneficiario=trecho
        )
    except Exception as erro:
        logger.exception("Falha ao verificar duplicata %s.", conta.duplicata_key)
        # Só a primeira linha - `visualizar_e_capturar_anexo` pode levantar uma mensagem de várias
        # linhas (uma por seletor tentado), longa demais pra caber numa célula de tabela.
        motivo = str(erro).splitlines()[0] if str(erro) else type(erro).__name__
        return BoletoFactoring(
            **base, status="nao_verificado", cnpjs_encontrados=[], trecho_beneficiario=motivo
        )

from __future__ import annotations

import shutil
from pathlib import Path

import pdfplumber
import pytesseract
from PIL import Image

_CAMINHOS_TESSERACT_PADRAO = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    str(Path.home() / "AppData" / "Local" / "Programs" / "Tesseract-OCR" / "tesseract.exe"),
]

_configurado = False


def _configurar_tesseract() -> None:
    """Aponta o `pytesseract` pro executável do Tesseract, se ele não estiver no PATH. Levanta um
    erro com instrução clara de instalação caso não encontre de jeito nenhum - o Tesseract é um
    programa do sistema (não é instalável via pip), então isso não é feito automaticamente."""
    global _configurado
    if _configurado:
        return
    if shutil.which("tesseract"):
        _configurado = True
        return
    for caminho in _CAMINHOS_TESSERACT_PADRAO:
        if Path(caminho).exists():
            pytesseract.pytesseract.tesseract_cmd = caminho
            _configurado = True
            return
    raise RuntimeError(
        "Tesseract OCR não está instalado (ou não foi encontrado). Baixe o instalador em "
        "https://github.com/UB-Mannheim/tesseract/wiki, rode e marque o pacote de idioma "
        "'Portuguese' durante a instalação. Depois rode de novo."
    )


def _ocr_imagem(imagem: Image.Image, psm: int = 6, cinza: bool = True) -> str:
    """Escala de cinza + `--psm 6` (bloco único de texto) - mesma receita do projeto "Lançamento de
    notas". Numa amostra de 10 boletos em imagem que ficavam "não confirmados", a leitura colorida
    com o layout automático (psm 3) não achou CNPJ em nenhum; cinza + psm 6 achou em 4."""
    _configurar_tesseract()
    try:
        return pytesseract.image_to_string(imagem.convert("L") if cinza else imagem, lang="por", config=f"--psm {psm}")
    except pytesseract.TesseractError as e:
        if "por.traineddata" in str(e) or "Failed loading language" in str(e):
            raise RuntimeError(
                "Tesseract está instalado mas sem o pacote de idioma Português (por.traineddata). "
                "Rode o instalador de novo e marque 'Portuguese' na lista de idiomas."
            ) from e
        raise


def extrair_texto_via_ocr_pdf(caminho: Path, resolucao: int = 300, psm: int = 6, cinza: bool = True) -> str:
    """OCR de um PDF escaneado (sem camada de texto) - renderiza cada página como imagem (usando
    o próprio `pdfplumber`, que já tem Pillow/pypdfium2 como dependência - não precisa de
    Poppler/pdf2image à parte) e roda OCR em cada uma."""
    textos = []
    with pdfplumber.open(caminho) as pdf:
        for pagina in pdf.pages:
            imagem = pagina.to_image(resolution=resolucao).original
            textos.append(_ocr_imagem(imagem, psm=psm, cinza=cinza))
    return "\n".join(textos)


def extrair_texto_via_ocr_imagem(caminho: Path, psm: int = 6, cinza: bool = True) -> str:
    """OCR de um anexo que é diretamente uma imagem (ex.: boleto em foto/print .jpg/.jpeg)."""
    with Image.open(caminho) as imagem:
        return _ocr_imagem(imagem, psm=psm, cinza=cinza)

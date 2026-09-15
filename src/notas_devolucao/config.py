from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]

# "Modo nuvem" - ligado só na instância hospedada no Streamlit Community Cloud (variável de
# ambiente configurada lá nas "Secrets" do app, nunca localmente). Nessa instância não há Bluesoft,
# Outlook nem login manual assistido disponíveis - o app inteiro roda travado em modo leitura, lendo
# um retrato dos dados publicado via `notas_devolucao.publicacao.publicar_dados` (git push, que
# dispara redeploy automático), em vez do banco local de verdade.
MODO_NUVEM = os.environ.get("PORTAL_MODO_NUVEM") == "1"

CONFIG_PATH = BASE_DIR / "config" / ("settings.cloud.toml" if MODO_NUVEM else "settings.toml")
STORAGE_STATE_PATH = BASE_DIR / "storage_state.json"
DADOS_DIR = BASE_DIR / "dados"
DB_PATH = (BASE_DIR / "dados_publicados" / "notas_devolucao.sqlite") if MODO_NUVEM else (DADOS_DIR / "notas_devolucao.db")
ANEXOS_DIR = BASE_DIR / "anexos_baixados"

BLUESOFT_BASE_URL = "https://erp.bluesoft.com.br/daterrinha"


@dataclass
class Configuracao:
    data_inicial: date
    data_final: date
    intercompany_nomes: list[str]
    auth_usuario: str
    auth_senha_hash: str
    antecipacao_planilha_path: Path | None = None
    visualizacao_token: str | None = None


def carregar_configuracao(caminho: Path = CONFIG_PATH) -> Configuracao:
    if not caminho.exists():
        raise FileNotFoundError(
            f"{caminho} não existe. Copie config/settings.example.toml para "
            f"config/settings.toml e ajuste os valores."
        )
    with open(caminho, "rb") as f:
        dados = tomllib.load(f)

    caminho_planilha_antecipacao = dados.get("antecipacao", {}).get("caminho_planilha")
    # Caminho relativo é resolvido a partir da raiz do projeto, não do diretório de trabalho: o
    # settings.toml local aponta pra planilha absoluta do OneDrive, mas o settings.cloud.toml
    # aponta pro retrato versionado em `dados_publicados/` (caminho relativo), e não dá pra contar
    # com qual diretório o Streamlit Community Cloud usa como CWD.
    if caminho_planilha_antecipacao:
        caminho_planilha_antecipacao = Path(caminho_planilha_antecipacao)
        if not caminho_planilha_antecipacao.is_absolute():
            caminho_planilha_antecipacao = BASE_DIR / caminho_planilha_antecipacao

    return Configuracao(
        data_inicial=dados["busca"]["data_inicial"],
        data_final=dados["busca"]["data_final"],
        intercompany_nomes=dados.get("intercompany", {}).get("nomes", []),
        auth_usuario=dados["auth"]["usuario"],
        auth_senha_hash=dados["auth"]["senha_hash"],
        antecipacao_planilha_path=caminho_planilha_antecipacao,
        visualizacao_token=dados.get("visualizacao", {}).get("token") or None,
    )

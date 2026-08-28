from __future__ import annotations

import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]
CONFIG_PATH = BASE_DIR / "config" / "settings.toml"
STORAGE_STATE_PATH = BASE_DIR / "storage_state.json"
DADOS_DIR = BASE_DIR / "dados"
DB_PATH = DADOS_DIR / "notas_devolucao.db"
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


def carregar_configuracao(caminho: Path = CONFIG_PATH) -> Configuracao:
    if not caminho.exists():
        raise FileNotFoundError(
            f"{caminho} não existe. Copie config/settings.example.toml para "
            f"config/settings.toml e ajuste os valores."
        )
    with open(caminho, "rb") as f:
        dados = tomllib.load(f)

    caminho_planilha_antecipacao = dados.get("antecipacao", {}).get("caminho_planilha")

    return Configuracao(
        data_inicial=dados["busca"]["data_inicial"],
        data_final=dados["busca"]["data_final"],
        intercompany_nomes=dados.get("intercompany", {}).get("nomes", []),
        auth_usuario=dados["auth"]["usuario"],
        auth_senha_hash=dados["auth"]["senha_hash"],
        antecipacao_planilha_path=Path(caminho_planilha_antecipacao) if caminho_planilha_antecipacao else None,
    )

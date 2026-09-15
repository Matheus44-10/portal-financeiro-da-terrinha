from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from .config import BASE_DIR, DADOS_DIR

DB_PUBLICADO_PATH = BASE_DIR / "dados_publicados" / "notas_devolucao.sqlite"
PLANILHA_ANTECIPACAO_PUBLICADA_PATH = BASE_DIR / "dados_publicados" / "antecipacao.xlsx"
META_ANTECIPACAO_PATH = BASE_DIR / "dados_publicados" / "antecipacao_meta.json"


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=BASE_DIR, capture_output=True, text=True, timeout=30
    )


def publicar_dados(planilha_antecipacao: Path | None = None) -> str:
    """Copia o banco local pra uma pasta versionada e envia pro GitHub - equivalente ao botão
    "Publicar" do Power BI Desktop. O push dispara redeploy automático do Streamlit Community
    Cloud, que lê esse retrato dos dados (ver config.MODO_NUVEM) em vez do banco local de verdade.
    Nunca chamado no modo nuvem (lá não tem repositório git nem sentido de "publicar" de novo).

    `planilha_antecipacao` é a planilha externa de antecipação de fornecedores (fora do banco, e
    fora do repositório - mora no OneDrive do financeiro). Ela também vira um retrato versionado
    aqui, senão a página "Antecipação de Fornecedores" fica vazia na nuvem: o caminho do OneDrive
    configurado no settings.toml local não existe na máquina do Streamlit Community Cloud."""
    banco_local = DADOS_DIR / "notas_devolucao.db"
    if not banco_local.exists():
        raise RuntimeError(
            "Nenhum dado local encontrado ainda - clique em 'Atualizar dados do Bluesoft' antes de publicar."
        )

    DB_PUBLICADO_PATH.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(banco_local, DB_PUBLICADO_PATH)

    caminhos_relativos = [str(DB_PUBLICADO_PATH.relative_to(BASE_DIR))]

    if planilha_antecipacao and planilha_antecipacao.exists():
        shutil.copy2(planilha_antecipacao, PLANILHA_ANTECIPACAO_PUBLICADA_PATH)
        # A data de modificação real da planilha vai num arquivo ao lado porque o git não guarda
        # mtime: no servidor da nuvem o mtime do arquivo é a hora do deploy, o que faria o portal
        # sempre garantir que a planilha é recente e nunca disparar o aviso de "pode estar
        # desatualizada" - justamente o aviso que existe pra pegar planilha esquecida.
        META_ANTECIPACAO_PATH.write_text(
            json.dumps(
                {"modificada_em": datetime.fromtimestamp(planilha_antecipacao.stat().st_mtime).isoformat()}
            ),
            encoding="utf-8",
        )
        caminhos_relativos.append(str(PLANILHA_ANTECIPACAO_PUBLICADA_PATH.relative_to(BASE_DIR)))
        caminhos_relativos.append(str(META_ANTECIPACAO_PATH.relative_to(BASE_DIR)))

    resultado_add = _git("add", *caminhos_relativos)
    if resultado_add.returncode != 0:
        raise RuntimeError(f"Falha ao preparar a publicação (git add): {resultado_add.stderr.strip()}")

    resultado_commit = _git("commit", "-m", "Publica dados para o portal de visualizacao")
    if resultado_commit.returncode != 0:
        if "nothing to commit" in resultado_commit.stdout.lower():
            return "Os dados já estavam publicados - nenhuma mudança desde a última vez."
        raise RuntimeError(f"Falha ao publicar (git commit): {resultado_commit.stderr.strip()}")

    resultado_push = _git("push")
    if resultado_push.returncode != 0:
        raise RuntimeError(f"Falha ao enviar para o GitHub (git push): {resultado_push.stderr.strip()}")

    return "Dados publicados! O portal na nuvem deve atualizar em alguns instantes."

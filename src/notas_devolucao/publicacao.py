from __future__ import annotations

import shutil
import subprocess

from .config import BASE_DIR, DADOS_DIR

DB_PUBLICADO_PATH = BASE_DIR / "dados_publicados" / "notas_devolucao.sqlite"


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=BASE_DIR, capture_output=True, text=True, timeout=30
    )


def publicar_dados() -> str:
    """Copia o banco local pra uma pasta versionada e envia pro GitHub - equivalente ao botão
    "Publicar" do Power BI Desktop. O push dispara redeploy automático do Streamlit Community
    Cloud, que lê esse retrato dos dados (ver config.MODO_NUVEM) em vez do banco local de verdade.
    Nunca chamado no modo nuvem (lá não tem repositório git nem sentido de "publicar" de novo)."""
    banco_local = DADOS_DIR / "notas_devolucao.db"
    if not banco_local.exists():
        raise RuntimeError(
            "Nenhum dado local encontrado ainda - clique em 'Atualizar dados do Bluesoft' antes de publicar."
        )

    DB_PUBLICADO_PATH.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(banco_local, DB_PUBLICADO_PATH)

    caminho_relativo = str(DB_PUBLICADO_PATH.relative_to(BASE_DIR))
    resultado_add = _git("add", caminho_relativo)
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

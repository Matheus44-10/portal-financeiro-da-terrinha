from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

from .config import DADOS_DIR, DB_PATH
from .models import BoletoFactoring, ContaPagar, NotaDevolucao, Vinculo
from .relatorio_abatimento import NotaQuitada

_SCHEMA = """
CREATE TABLE IF NOT EXISTS notas_devolucao (
    duplicata_key TEXT PRIMARY KEY,
    fornecedor TEXT NOT NULL,
    fornecedor_cnpj TEXT NOT NULL,
    loja INTEGER NOT NULL,
    loja_nome TEXT NOT NULL,
    valor_nominal REAL NOT NULL,
    valor_liquido REAL NOT NULL,
    data_emissao TEXT NOT NULL,
    data_vencimento TEXT NOT NULL,
    descritivo TEXT NOT NULL,
    numero_documento TEXT NOT NULL,
    tipo TEXT NOT NULL,
    dias_de_atraso INTEGER NOT NULL,
    liquidada INTEGER NOT NULL,
    duplicata_pagamento_vinculada TEXT,
    produtos TEXT
);

CREATE TABLE IF NOT EXISTS contas_pagar (
    duplicata_key TEXT PRIMARY KEY,
    favorecido TEXT NOT NULL,
    favorecido_cnpj TEXT NOT NULL,
    loja INTEGER NOT NULL,
    loja_pagadora INTEGER NOT NULL,
    valor REAL NOT NULL,
    data_emissao TEXT NOT NULL,
    data_vencimento TEXT NOT NULL,
    descritivo TEXT NOT NULL,
    nota_fiscal TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notas_devolucao_quitadas (
    duplicata_key TEXT PRIMARY KEY,
    fornecedor TEXT NOT NULL,
    fornecedor_cnpj TEXT NOT NULL,
    loja INTEGER NOT NULL,
    loja_nome TEXT NOT NULL,
    loja_recebedora INTEGER NOT NULL,
    valor_liquido REAL NOT NULL,
    data_emissao TEXT NOT NULL,
    data_criacao TEXT NOT NULL,
    data_quitacao TEXT NOT NULL,
    descritivo TEXT NOT NULL,
    numero_documento TEXT NOT NULL,
    tipo TEXT NOT NULL,
    conta_contabil_dlp TEXT NOT NULL,
    conta_contabil_destino TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vinculos (
    nota_devolucao_key TEXT NOT NULL,
    conta_pagar_key TEXT NOT NULL,
    status TEXT NOT NULL,
    observacao TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (nota_devolucao_key, conta_pagar_key)
);

CREATE TABLE IF NOT EXISTS sincronizacao_info (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    ultima_atualizacao TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contatos_fornecedor (
    fornecedor_cnpj TEXT PRIMARY KEY,
    email TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS boletos_factoring (
    duplicata_key TEXT PRIMARY KEY,
    favorecido TEXT NOT NULL,
    favorecido_cnpj TEXT NOT NULL,
    loja INTEGER NOT NULL,
    loja_pagadora INTEGER NOT NULL,
    valor REAL NOT NULL,
    data_vencimento TEXT NOT NULL,
    data_quitacao TEXT,
    nota_fiscal TEXT NOT NULL,
    status TEXT NOT NULL,
    cnpjs_encontrados TEXT NOT NULL,
    trecho_beneficiario TEXT NOT NULL,
    verificado_em TEXT NOT NULL,
    pago_via_edi INTEGER NOT NULL DEFAULT 0
);
"""


@contextmanager
def _conexao():
    DADOS_DIR.mkdir(parents=True, exist_ok=True)
    conexao = sqlite3.connect(DB_PATH)
    try:
        conexao.execute("PRAGMA foreign_keys = ON")
        yield conexao
        conexao.commit()
    finally:
        conexao.close()


def inicializar(caminho: Path = DB_PATH) -> None:
    with _conexao() as conexao:
        conexao.executescript(_SCHEMA)
        # Migração: bancos criados antes do campo data_quitacao existir não ganham a coluna nova
        # automaticamente só com CREATE TABLE IF NOT EXISTS - adiciona se ainda não existir.
        try:
            conexao.execute("ALTER TABLE boletos_factoring ADD COLUMN data_quitacao TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conexao.execute(
                "ALTER TABLE boletos_factoring ADD COLUMN pago_via_edi INTEGER NOT NULL DEFAULT 0"
            )
        except sqlite3.OperationalError:
            pass
        try:
            conexao.execute("ALTER TABLE notas_devolucao ADD COLUMN produtos TEXT")
        except sqlite3.OperationalError:
            pass


def salvar_cache(
    notas: list[NotaDevolucao], contas: list[ContaPagar], quitadas: list[NotaQuitada] | None = None
) -> None:
    """Sobrescreve o cache de dados extraídos do Bluesoft. Os vínculos manuais (tabela `vinculos`)
    não são tocados - eles referenciam duplicatas pela chave natural, não por rowid, então
    sobrevivem a um refresh do cache mesmo que a duplicata correspondente não volte na extração."""
    with _conexao() as conexao:
        conexao.executescript(_SCHEMA)
        conexao.execute("DELETE FROM notas_devolucao")
        conexao.execute("DELETE FROM contas_pagar")
        conexao.execute("DELETE FROM notas_devolucao_quitadas")
        conexao.executemany(
            """
            INSERT INTO notas_devolucao VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    n.duplicata_key,
                    n.fornecedor,
                    n.fornecedor_cnpj,
                    n.loja,
                    n.loja_nome,
                    n.valor_nominal,
                    n.valor_liquido,
                    n.data_emissao.isoformat(),
                    n.data_vencimento.isoformat(),
                    n.descritivo,
                    n.numero_documento,
                    n.tipo,
                    n.dias_de_atraso,
                    int(n.liquidada),
                    n.duplicata_pagamento_vinculada,
                    n.produtos,
                )
                for n in notas
            ],
        )
        conexao.executemany(
            """
            INSERT INTO contas_pagar VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    c.duplicata_key,
                    c.favorecido,
                    c.favorecido_cnpj,
                    c.loja,
                    c.loja_pagadora,
                    c.valor,
                    c.data_emissao.isoformat(),
                    c.data_vencimento.isoformat(),
                    c.descritivo,
                    c.nota_fiscal,
                )
                for c in contas
            ],
        )
        conexao.executemany(
            """
            INSERT INTO notas_devolucao_quitadas VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    n.duplicata_key,
                    n.fornecedor,
                    n.fornecedor_cnpj,
                    n.loja,
                    n.loja_nome,
                    n.loja_recebedora,
                    n.valor_liquido,
                    n.data_emissao.isoformat(),
                    n.data_criacao.isoformat(),
                    n.data_quitacao.isoformat(),
                    n.descritivo,
                    n.numero_documento,
                    n.tipo,
                    n.conta_contabil_dlp,
                    n.conta_contabil_destino,
                )
                for n in (quitadas or [])
            ],
        )


def carregar_notas_devolucao() -> list[NotaDevolucao]:
    with _conexao() as conexao:
        linhas = conexao.execute("SELECT * FROM notas_devolucao").fetchall()
    return [
        NotaDevolucao(
            duplicata_key=l[0],
            fornecedor=l[1],
            fornecedor_cnpj=l[2],
            loja=l[3],
            loja_nome=l[4],
            valor_nominal=l[5],
            valor_liquido=l[6],
            data_emissao=date.fromisoformat(l[7]),
            data_vencimento=date.fromisoformat(l[8]),
            descritivo=l[9],
            numero_documento=l[10],
            tipo=l[11],
            dias_de_atraso=l[12],
            liquidada=bool(l[13]),
            duplicata_pagamento_vinculada=l[14],
            produtos=l[15],
        )
        for l in linhas
    ]


def carregar_contas_pagar() -> list[ContaPagar]:
    with _conexao() as conexao:
        linhas = conexao.execute("SELECT * FROM contas_pagar").fetchall()
    return [
        ContaPagar(
            duplicata_key=l[0],
            favorecido=l[1],
            favorecido_cnpj=l[2],
            loja=l[3],
            loja_pagadora=l[4],
            valor=l[5],
            data_emissao=date.fromisoformat(l[6]),
            data_vencimento=date.fromisoformat(l[7]),
            descritivo=l[8],
            nota_fiscal=l[9],
        )
        for l in linhas
    ]


def carregar_notas_devolucao_quitadas() -> list[NotaQuitada]:
    with _conexao() as conexao:
        linhas = conexao.execute("SELECT * FROM notas_devolucao_quitadas").fetchall()
    return [
        NotaQuitada(
            duplicata_key=l[0],
            fornecedor=l[1],
            fornecedor_cnpj=l[2],
            loja=l[3],
            loja_nome=l[4],
            loja_recebedora=l[5],
            valor_liquido=l[6],
            data_emissao=date.fromisoformat(l[7]),
            data_criacao=date.fromisoformat(l[8]),
            data_quitacao=date.fromisoformat(l[9]),
            descritivo=l[10],
            numero_documento=l[11],
            tipo=l[12],
            conta_contabil_dlp=l[13],
            conta_contabil_destino=l[14],
        )
        for l in linhas
    ]


def salvar_vinculo(vinculo: Vinculo) -> None:
    with _conexao() as conexao:
        conexao.execute(
            """
            INSERT INTO vinculos (nota_devolucao_key, conta_pagar_key, status, observacao)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(nota_devolucao_key, conta_pagar_key)
            DO UPDATE SET status = excluded.status, observacao = excluded.observacao
            """,
            (vinculo.nota_devolucao_key, vinculo.conta_pagar_key, vinculo.status, vinculo.observacao),
        )


def remover_vinculo(nota_devolucao_key: str, conta_pagar_key: str) -> None:
    with _conexao() as conexao:
        conexao.execute(
            "DELETE FROM vinculos WHERE nota_devolucao_key = ? AND conta_pagar_key = ?",
            (nota_devolucao_key, conta_pagar_key),
        )


def carregar_vinculos() -> list[Vinculo]:
    with _conexao() as conexao:
        linhas = conexao.execute("SELECT nota_devolucao_key, conta_pagar_key, status, observacao FROM vinculos").fetchall()
    return [Vinculo(*l) for l in linhas]


def salvar_email_fornecedor(cnpj: str, email: str) -> None:
    """Grava o e-mail do responsável pelo fornecedor. Uma string vazia remove o registro."""
    email = email.strip()
    with _conexao() as conexao:
        if email:
            conexao.execute(
                """
                INSERT INTO contatos_fornecedor (fornecedor_cnpj, email) VALUES (?, ?)
                ON CONFLICT(fornecedor_cnpj) DO UPDATE SET email = excluded.email
                """,
                (cnpj, email),
            )
        else:
            conexao.execute("DELETE FROM contatos_fornecedor WHERE fornecedor_cnpj = ?", (cnpj,))


def carregar_emails_fornecedor() -> dict[str, str]:
    with _conexao() as conexao:
        linhas = conexao.execute("SELECT fornecedor_cnpj, email FROM contatos_fornecedor").fetchall()
    return {l[0]: l[1] for l in linhas}


def salvar_resultado_factoring(boleto: BoletoFactoring) -> None:
    with _conexao() as conexao:
        conexao.execute(
            """
            INSERT INTO boletos_factoring
            (duplicata_key, favorecido, favorecido_cnpj, loja, loja_pagadora, valor,
             data_vencimento, data_quitacao, nota_fiscal, status, cnpjs_encontrados,
             trecho_beneficiario, verificado_em, pago_via_edi)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(duplicata_key) DO UPDATE SET
                status = excluded.status,
                cnpjs_encontrados = excluded.cnpjs_encontrados,
                trecho_beneficiario = excluded.trecho_beneficiario,
                data_quitacao = excluded.data_quitacao,
                verificado_em = excluded.verificado_em,
                pago_via_edi = excluded.pago_via_edi
            """,
            (
                boleto.duplicata_key,
                boleto.favorecido,
                boleto.favorecido_cnpj,
                boleto.loja,
                boleto.loja_pagadora,
                boleto.valor,
                boleto.data_vencimento.isoformat(),
                boleto.data_quitacao.isoformat() if boleto.data_quitacao else None,
                boleto.nota_fiscal,
                boleto.status,
                ",".join(boleto.cnpjs_encontrados),
                boleto.trecho_beneficiario,
                boleto.verificado_em.isoformat(),
                int(boleto.pago_via_edi),
            ),
        )


def carregar_boletos_factoring() -> list[BoletoFactoring]:
    with _conexao() as conexao:
        linhas = conexao.execute(
            """
            SELECT duplicata_key, favorecido, favorecido_cnpj, loja, loja_pagadora, valor,
                   data_vencimento, data_quitacao, nota_fiscal, status, cnpjs_encontrados,
                   trecho_beneficiario, verificado_em, pago_via_edi
            FROM boletos_factoring
            """
        ).fetchall()
    return [
        BoletoFactoring(
            duplicata_key=l[0],
            favorecido=l[1],
            favorecido_cnpj=l[2],
            loja=l[3],
            loja_pagadora=l[4],
            valor=l[5],
            data_vencimento=date.fromisoformat(l[6]),
            data_quitacao=date.fromisoformat(l[7]) if l[7] else None,
            nota_fiscal=l[8],
            status=l[9],
            cnpjs_encontrados=[c for c in l[10].split(",") if c],
            trecho_beneficiario=l[11],
            verificado_em=datetime.fromisoformat(l[12]),
            pago_via_edi=bool(l[13]),
        )
        for l in linhas
    ]


def chaves_ja_verificadas_factoring() -> set[str]:
    with _conexao() as conexao:
        linhas = conexao.execute("SELECT duplicata_key FROM boletos_factoring").fetchall()
    return {l[0] for l in linhas}


def obter_ultima_verificacao_factoring() -> datetime | None:
    with _conexao() as conexao:
        linha = conexao.execute("SELECT MAX(verificado_em) FROM boletos_factoring").fetchone()
    return datetime.fromisoformat(linha[0]) if linha and linha[0] else None


def remover_verificacao_factoring(duplicata_key: str) -> None:
    """Remove o registro pra essa duplicata ser tratada como "nova" e reprocessada de verdade na
    próxima rodada de `verificar_boletos_factoring` (que hoje pula quem já está na tabela)."""
    with _conexao() as conexao:
        conexao.execute("DELETE FROM boletos_factoring WHERE duplicata_key = ?", (duplicata_key,))


def marcar_status_manual_factoring(duplicata_key: str, status: str, observacao: str = "") -> None:
    """Corrige manualmente o status de uma duplicata que ficou "não verificada" (ex.: depois de
    conferir o boleto na mão no Bluesoft) - sem precisar reabrir o navegador."""
    with _conexao() as conexao:
        conexao.execute(
            """
            UPDATE boletos_factoring
            SET status = ?, trecho_beneficiario = ?, verificado_em = ?
            WHERE duplicata_key = ?
            """,
            (status, observacao, datetime.now().isoformat(), duplicata_key),
        )


def salvar_ultima_atualizacao(momento: datetime) -> None:
    with _conexao() as conexao:
        conexao.execute(
            """
            INSERT INTO sincronizacao_info (id, ultima_atualizacao) VALUES (1, ?)
            ON CONFLICT(id) DO UPDATE SET ultima_atualizacao = excluded.ultima_atualizacao
            """,
            (momento.isoformat(),),
        )


def carregar_ultima_atualizacao() -> datetime | None:
    with _conexao() as conexao:
        linha = conexao.execute("SELECT ultima_atualizacao FROM sincronizacao_info WHERE id = 1").fetchone()
    return datetime.fromisoformat(linha[0]) if linha else None

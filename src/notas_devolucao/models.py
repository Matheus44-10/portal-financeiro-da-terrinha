from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


@dataclass
class NotaDevolucao:
    duplicata_key: str
    fornecedor: str
    fornecedor_cnpj: str
    loja: int
    loja_nome: str
    valor_nominal: float
    valor_liquido: float
    data_emissao: date
    data_vencimento: date
    descritivo: str
    numero_documento: str
    tipo: str  # "Devolução de Compra" ou "Devolução de Trocas"
    dias_de_atraso: int
    liquidada: bool
    # Nº da duplicata de pagamento à qual o Bluesoft já vinculou esta nota como abatimento (visto na
    # tela de detalhe da duplicata de cobrança) - None quando ainda não há vínculo nenhum.
    duplicata_pagamento_vinculada: str | None = None
    # Descrição do(s) item(s) da nota fiscal vinculada, lida direto do Bluesoft (ver
    # navigation.obter_produtos_da_fatura) - None quando ainda não foi possível determinar.
    produtos: str | None = None


@dataclass
class ContaPagar:
    duplicata_key: str
    favorecido: str
    favorecido_cnpj: str
    loja: int
    loja_pagadora: int
    valor: float
    data_emissao: date
    data_vencimento: date
    descritivo: str
    nota_fiscal: str


@dataclass
class BoletoFactoring:
    """Resultado da checagem de uma conta a pagar já quitada, pra saber se o boleto foi pago de
    fato ao fornecedor cadastrado ou a uma factoring/financeira (ver `factoring.py`)."""

    duplicata_key: str
    favorecido: str
    favorecido_cnpj: str
    loja: int
    loja_pagadora: int
    valor: float
    data_vencimento: date
    nota_fiscal: str
    # Data em que a duplicata foi de fato paga (vem do texto da tela de detalhe, não da listagem -
    # a data de vencimento sozinha confunde, já que a busca filtra por quitação: uma duplicata
    # vencida em maio pode ter sido paga (com atraso) só em julho). None se não conseguiu ler.
    data_quitacao: date | None
    status: str  # "factoring", "normal" ou "nao_verificado"
    cnpjs_encontrados: list[str]
    trecho_beneficiario: str
    verificado_em: datetime
    # Lido da tela de "Ocorrências" da duplicata - confirmado ao vivo que pagamentos processados via
    # EDI bancário costumam ter o beneficiário batendo com o fornecedor cadastrado (o próprio banco
    # valida os dados cadastrais antes de processar), diferente de borderô comum - sinal extra pra
    # priorizar revisão manual, não usado pra classificar automaticamente sozinho.
    pago_via_edi: bool = False


@dataclass
class Vinculo:
    nota_devolucao_key: str
    conta_pagar_key: str
    status: str  # "pendente" ou "abatido"
    observacao: str = ""

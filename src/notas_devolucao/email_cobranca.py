from __future__ import annotations

from pathlib import Path

from .models import NotaDevolucao

_LARANJA = "#EE6931"

_MENSAGEM_PEDIDO = (
    "Poderiam, por gentileza, informar se os respectivos valores poderão ser abatidos em "
    "alguma fatura em aberto ou se o valor será estornado?"
)


def _valor_br(valor: float) -> str:
    """Formato brasileiro (vírgula decimal, ponto de milhar) - ex: 1.234,56."""
    return f"{valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _tabela_html(notas: list[NotaDevolucao]) -> str:
    linhas_html = "".join(
        f'<tr style="background-color:{"#FFFFFF" if i % 2 == 0 else "#F7F7F7"};">'
        f'<td style="padding:8px 12px; border-bottom:1px solid #eee;">{n.numero_documento}</td>'
        f'<td style="padding:8px 12px; border-bottom:1px solid #eee; text-align:right;">'
        f"R$ {_valor_br(n.valor_liquido)}</td></tr>"
        for i, n in enumerate(notas)
    )
    total = sum(n.valor_liquido for n in notas)
    return f"""
    <table style="width:100%; border-collapse:collapse; font-size:14px;">
        <tr style="background-color:{_LARANJA}; color:#FFFFFF;">
            <th style="padding:10px 12px; text-align:left;">Nº NFD (Devolução)</th>
            <th style="padding:10px 12px; text-align:right;">Valor Total (R$)</th>
        </tr>
        {linhas_html}
        <tr style="background-color:{_LARANJA}; color:#FFFFFF; font-weight:bold;">
            <td style="padding:10px 12px;">TOTAL GERAL A CREDITAR</td>
            <td style="padding:10px 12px; text-align:right;">R$ {_valor_br(total)}</td>
        </tr>
    </table>
    """


def _corpo_html(fornecedor: str, notas: list[NotaDevolucao]) -> str:
    paragrafo_pedido = _MENSAGEM_PEDIDO.replace("\n", " ")
    return f"""
    <div style="font-family: 'Segoe UI', Arial, sans-serif; max-width:600px; color:#000000;">
        <div style="background-color:{_LARANJA}; padding:16px 24px; border-radius:10px 10px 0 0;
                    display:flex; align-items:center; gap:14px;">
            <img src="cid:logo_terrinha" style="height:48px; display:block;" alt="Da Terrinha Alimentos">
            <span style="color:#FFFFFF; font-size:17px; font-weight:bold;">Da Terrinha Alimentos</span>
        </div>
        <div style="padding:24px; border:1px solid #F0D9CE; border-top:none; border-radius:0 0 10px 10px;">
            <p>Prezados,</p>
            <p>Identificamos algumas notas de devolução em aberto em nosso sistema referentes a
               <strong>{fornecedor.strip()}</strong>:</p>
            {_tabela_html(notas)}
            <p style="margin-top:20px;">{paragrafo_pedido}</p>
            <p>Fico no aguardo de um retorno.</p>
            <p>Obrigado.</p>
            <p>Atenciosamente,<br><strong>Da Terrinha Alimentos</strong></p>
        </div>
    </div>
    """


_PR_ATTACH_CONTENT_ID = "http://schemas.microsoft.com/mapi/proptag/0x3712001E"


def abrir_no_outlook(
    fornecedor: str, notas: list[NotaDevolucao], logo_path: Path, destinatario: str | None = None
) -> None:
    """Abre um rascunho já no Outlook desktop (via COM), com o visual da marca (logo, cor laranja,
    tabela formatada) - o usuário confere e envia manualmente. Se `destinatario` for informado, o
    campo Para já vem preenchido; senão fica em branco para o usuário preencher. Não baixa nenhum
    arquivo: a janela do Outlook aparece direto na tela."""
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    try:
        outlook = win32com.client.Dispatch("Outlook.Application")
        mail = outlook.CreateItem(0)  # olMailItem
        if destinatario:
            mail.To = destinatario
        mail.Subject = f"Devolução pendente de abatimento - {fornecedor.strip()}"

        anexo = mail.Attachments.Add(str(logo_path))
        anexo.PropertyAccessor.SetProperty(_PR_ATTACH_CONTENT_ID, "logo_terrinha")

        mail.HTMLBody = _corpo_html(fornecedor, notas)
        mail.Display(False)
    finally:
        pythoncom.CoUninitialize()

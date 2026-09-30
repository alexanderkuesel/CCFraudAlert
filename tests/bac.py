"""BAC Credomatic (Costa Rica) alert emails, modelled on a real one with personal data replaced."""

from datetime import datetime

from .conftest import make_eml

SENDER = "Notificación BAC <NotificacionBAC@baccredomatic.cr>"


def bac_html(merchant="GLOBAL-E", place=", Reino Unido", date="Sep 29, 2026, 17:01", card=("AMEX", "***********4321"),
             kind="COMPRA", amount="USD 154.64") -> str:
    rows = [("Comercio:", merchant), ("Ciudad y país:", place), ("Fecha:", date), (f"{card[0]}:", card[1]),
            ("Autorización:", "657401"), ("Referencia:", ""), ("Tipo de Transacción:", kind), ("Monto:", amount)]
    return (
        "<html><body><p>Header BAC</p><p>Hola NAME</p>"
        "<p>A continuación le detallamos la transacción realizada:</p><table>"
        + "".join(f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in rows)
        + "</table><p>BANNER PROMOCIONAL</p><p>¿Tiene dudas sobre esta transacción?</p>"
        "<p>Puede contactarnos vía WhatsApp presionando aquí. O bien escríbanos al 8742-9595 agregando la palabra "
        '"alerta".</p><p>Comunicado válido únicamente para Costa Rica.</p>'
        "<p>TODOS LOS DERECHOS RESERVADOS. 2026 © BAC INTERNATIONAL BANK</p></body></html>"
    )


def bac_eml(sent_utc: datetime, merchant="GLOBAL-E", **kw) -> bytes:
    subject = f"Notificación de transacción {merchant} {sent_utc:%d-%m-%Y - %H:%M}"
    return make_eml(subject, bac_html(merchant=merchant, **kw), sent_utc, sender=SENDER, html=True)

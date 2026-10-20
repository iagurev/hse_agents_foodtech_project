import ssl

import truststore


def system_tls_context() -> ssl.SSLContext:
    """Создаёт TLS-контекст с системными доверенными CA, сохраняя проверку сертификатов.

    :return: Контекст для HTTP-клиентов приложения
    """
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

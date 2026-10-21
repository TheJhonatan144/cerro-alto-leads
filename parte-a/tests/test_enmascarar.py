"""Bloque 4: enmascarado antes de enviar texto al LLM."""
from app.enmascarar import contenido_para_llm, enmascarar


def test_correo_telefono_documento_enlace_y_nombre():
    texto = ("Hola, soy Carla Mendoza (carla.mendoza@example.com), mi número es +593 99 123 4567 "
             "o 099-123-4567, cédula 1712345678, pasaporte AB123456. Vi www.promo.example/x y "
             "https://bit.ly/abc. Mi hija CARLA también.")
    t = enmascarar(texto, nombre="Carla Mendoza")
    for dato in ("carla", "Carla", "CARLA", "Mendoza", "example.com", "593", "123 4567", "099-123",
                 "1712345678", "AB123456", "promo.example", "bit.ly"):
        assert dato not in t, dato
    # Bloque 4.1: teléfonos y cédulas son [NUMERO]; los alfanuméricos, [DOCUMENTO].
    for marca in ("[CORREO]", "[NUMERO]", "[DOCUMENTO]", "[ENLACE]", "[NOMBRE]"):
        assert marca in t, marca
    assert "Hola" in t and "hija" in t


def test_telefono_sin_separadores_y_tildes_del_nombre():
    t = enmascarar("Llamen al 0991234567, pregunten por José", nombre="Jose Pérez")
    assert "0991234567" not in t and "José" not in t
    assert "[NOMBRE]" in t


def test_palabras_cortas_del_nombre_no_se_enmascaran():
    t = enmascarar("Ana va a ir con su mamá", nombre="Ana de la Cruz")
    assert "[NOMBRE] va a ir" in t  # "Ana" sí (3 letras); "de", "la" no tocan el texto


def test_truncado_y_caracteres_de_control():
    t = enmascarar("a\x00b\x1bc" + "x" * 5000)
    assert "\x00" not in t and "\x1b" not in t
    assert len(t) <= 1000 + len(" […] ")  # Bloque 4.1: inicio + marca de corte + final


def test_numeros_cortos_se_conservan():
    t = enmascarar("Cuesta $500 y la cita es a las 17:00 del 2026-11-03")
    assert "$500" in t and "17:00" in t


def test_contenido_para_llm_solo_tres_campos():
    c = contenido_para_llm("Soy Ana, 0991234567", servicio="Limpieza", campana="Sonrisa Blanca", nombre="Ana Pérez")
    assert set(c) == {"mensaje", "servicio", "campana"}
    assert "0991234567" not in c["mensaje"] and "Ana" not in c["mensaje"]

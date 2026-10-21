"""Listas de palabras de las reglas de clasificación. Solo datos: revisar y ampliar aquí.

Cada entrada es una expresión regular sobre el texto normalizado (minúsculas, sin tildes,
espacios colapsados). Se buscan como palabras completas: no hace falta poner \\b.
Las palabras clave "meta" que alguien podría escribir para manipular la clasificación
("urgente", "urgencia alta", "prioridad", "caliente") NO están en las listas a propósito.
"""

# Palabras que niegan una señal si aparecen en las 3 palabras anteriores, en la misma frase.
NEGADORES = frozenset({"no", "sin", "ni", "nunca", "nada", "ningun", "ninguna", "ninguno", "tampoco"})

# Dolor que no es dental: se elimina de la frase antes de buscar urgencias.
DOLOR_NO_DENTAL = [
    r"dolor(?:es)?(?: \w+){0,2} de (?:cabeza|espalda|estomago|barriga|panza|garganta|oidos?|pecho|rodillas?|cuello|piernas?)",
    r"(?:me )?duelen? (?:la|el|las|los|mi|mis) (?:cabeza|espalda|estomago|barriga|panza|garganta|oidos?|pecho|rodillas?|cuello|piernas?)",
]

URGENCIA = [
    r"dolor(?:es)? (?:muy )?(?:intensos?|fuertes?|insoportables?|terribles?|horribles?)",
    r"(?:mucho|muchisimo|tremendo|gran) dolor",
    r"(?:me )?duelen? (?:mucho|muchisimo|demasiado|horrible)",
    r"dolor(?:es)? (?:de|en) (?:la |el |una |un |mis? )?(?:muelas?|dientes?|encias?|mandibula|boca)",
    r"(?:me )?duelen? (?:la|el|una|un|las|los|mi|mis) (?:muelas?|dientes?|encias?|mandibula)",
    r"hinch(?:ad[oa]s?|azon|o|aron)", r"inflam(?:ad[oa]s?|acion|o|aron)",
    r"golpe(?:e|o|s)?", r"golpead[oa]s?",
    r"(?:se me )?(?:rompio|partio|quebro|fracturo) (?:un|el|la|una|mi) (?:dientes?|muelas?|corona|incisivo)",
    r"(?:dientes?|muelas?) (?:rot[oa]s?|partid[oa]s?|quebrad[oa]s?|fracturad[oa]s?)",
    r"fractura(?:do|da)?",
    r"sangr(?:a|ado|ando|e) (?:mucho|abundante|demasiado|sin parar)",
    r"sangrado abundante", r"no (?:para|deja) de sangrar", r"sangrado que no (?:para|cede)",
    r"absceso", r"flemon", r"pus",
    r"emergencia",
]
# La fiebre solo cuenta como urgencia dental si el mensaje habla de dolor o de la boca.
FIEBRE = r"fiebre"
CONTEXTO_DENTAL = r"(?:dolor|duele|muelas?|dientes?|encias?|mandibula|boca)"

AGENDAR = [
    r"agend(?:ar|o|a|amos|en|e|arme)", r"citas?", r"turnos?",
    r"reserv(?:ar|o|a)", r"separar (?:una? )?(?:hora|cita|turno)",
    r"(?:me )?(?:pueden|podrian|puede|podria|podrias) (?:llamar|contactar|escribir)",
    r"llam(?:enme|ame|arme)", r"me llamen", r"contact(?:enme|arme|ame)",
    r"cuando (?:puedo|podria|podemos) (?:ir|pasar|acercarme)",
    r"(?:quiero|quisiera) (?:ir|pasar|acercarme)",
    r"tienen (?:disponibilidad|espacio|horario libre)",
]

INTENCION_ALTO_VALOR = [
    r"(?:quiero|quisiera|me gustaria|deseo) (?:empezar|comenzar|iniciar|hacerme|ponerme|arreglarme)",
    r"decidid[oa]s?", r"list[oa]s? para empezar",
    r"me interesa (?:empezar|comenzar|hacerme|ponerme)",
    r"cuando (?:puedo|podemos|podria) (?:empezar|comenzar|iniciar)",
    r"empezar (?:este|esta|el) (?:mes|semana|lunes|ano)",
]
SERVICIOS_ALTO_VALOR = ("ortodoncia", "alineadores")

# Interés real en la clínica (no hace caliente, pero impide declarar basura).
INTERES = [
    r"precios?", r"cuanto (?:cuesta|vale|sale|cobran|seria)", r"costos?", r"valor", r"tarifas?",
    r"informacion", r"info", r"cotizacion", r"presupuesto", r"horarios?", r"atienden",
    r"promocion(?:es)?", r"aceptan tarjeta", r"formas? de pago", r"averiguando", r"pregunt(?:o|ar)",
    r"interesad[oa]s?", r"me interesa", r"(?:quiero|quisiera) saber", r"consultas?", r"valoracion",
    r"limpieza", r"blanqueamiento", r"ortodoncia", r"brackets", r"alineadores", r"implantes?",
    r"carillas?", r"endodoncia", r"extraccion", r"dientes?", r"muelas?", r"encias?", r"dental",
    r"dentista", r"odontolog\w*",
    r"quanto custa", r"aparelho", r"dentes?",  # portugués
]

# Frases fuertes de spam: cada una suma 2 puntos; un enlace suma 1; spam con 2 o más.
# "publicidad" NO está: "vi su publicidad en Instagram" es un lead normal.
SPAM_FUERTE = [
    r"gana(?:r)? (?:dinero|plata)", r"gana \$?\d+", r"dinero (?:facil|rapido)", r"desde casa",
    r"invierte", r"invertir", r"inversion(?:es)?", r"multiplica(?:r)? tu dinero",
    r"casino", r"apuestas?", r"cripto\w*", r"bitcoin", r"forex", r"trading",
    r"seguidores", r"likes", r"seo", r"posicionamiento (?:web|en google)", r"marketing digital",
    r"link (?:en|de) (?:la|nuestra|mi|su) bio", r"cupos limitados",
    r"oferta (?:exclusiva|limitada)", r"prestamos?", r"(?:haz|has|da) (?:clic|click)",
    r"(?:clic|click) aqui",
]
PUNTOS_SPAM_FUERTE = 2
PUNTOS_ENLACE = 1
UMBRAL_SPAM = 2

# Frases dirigidas al sistema (no palabras sueltas: "la radiografía revela una lesión" es normal).
INYECCION = [
    r"ignora\w*(?: (?:todas?|todo))?(?: (?:tus|las|mis|sus|estas))? (?:instrucciones|indicaciones|reglas|ordenes)",
    r"ignora\w* (?:todo )?lo (?:anterior|previo|de arriba)",
    r"(?:olvida|omite|descarta)\w*(?: (?:todas?|todo))? (?:tus|las) (?:instrucciones|indicaciones|reglas)",
    r"instrucciones (?:anteriores|previas|del sistema)",
    r"revela\w* (?:tu|el|tus|las|los|su|sus) (?:prompt|instrucciones|sistema|reglas|configuracion)",
    r"(?:muestra|dime|repite|imprime)\w* (?:tu|el|tus|las) (?:prompt|instrucciones)",
    r"system prompt", r"tu prompt", r"prompt del sistema",
    r"(?:importante|nota|mensaje|aviso) para el (?:sistema|modelo|asistente|clasificador|bot)",
    r"clasifica\w*(?: \w+){0,3} (?:como )?(?:caliente|tibio|basura)",
    r"responde (?:solo |solamente |unicamente )?(?:con )?(?:basura|caliente|tibio)",
    r"actua como (?:un|una|el|la|si)", r"eres (?:un|una) (?:asistente|modelo|sistema|clasificador|ia)",
    r"modo (?:desarrollador|developer)",
]

DOMINIOS_DESECHABLES = frozenset({
    "mailinator.com", "guerrillamail.com", "guerrillamail.net", "sharklasers.com",
    "10minutemail.com", "tempmail.com", "temp-mail.org", "yopmail.com", "trashmail.com",
    "getnada.com", "dispostable.com", "maildrop.cc", "throwawaymail.com", "fakeinbox.com",
    "mailnesia.com", "mintemail.com",
})

# Etiquetas legibles para los motivos.
ETIQUETAS = {
    "urgencia_clinica": "síntomas de urgencia clínica",
    "pide_agendar_o_llamada": "pide agendar o que lo llamen",
    "alto_valor_con_intencion": "intención clara en un tratamiento de alto valor",
    "paciente_con_pendiente": "ya es paciente con tratamiento pendiente",
    "spam": "publicidad o spam",
    "contacto_falso": "datos de contacto claramente falsos",
}

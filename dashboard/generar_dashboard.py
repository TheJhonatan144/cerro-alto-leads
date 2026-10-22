"""Genera dashboard/index.html a partir de leads_90_dias.csv.

Uso (desde la carpeta dashboard/):
    pip install pandas
    python generar_dashboard.py [ruta/al/leads_90_dias.csv]

Lee el CSV, calcula todas las cifras y las inserta en plantilla.html junto con Chart.js
(vendor/chart.umd.js), y escribe index.html: un solo archivo que se abre sin servidor y sin
conexión. Todo número que aparece en el texto sale del CSV; nada está escrito a mano.
"""
import json
import sys
from math import sqrt
from pathlib import Path

import pandas as pd

AQUI = Path(__file__).resolve().parent
CSV = Path(sys.argv[1]) if len(sys.argv) > 1 else AQUI / "leads_90_dias.csv"

MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]
DIAS_SEMANA = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
PLAZO_MIN = 5  # el aviso a recepción debe salir en menos de 5 minutos


def fecha_corta(ts):
    return f"{ts.day} {MESES[ts.month - 1]}"


def pct(x, dec=1):
    v = round(float(x) * 100, dec)
    return int(v) if dec == 0 else v


def wilson(k, n, z=1.96):
    p = k / n
    den = 1 + z * z / n
    centro = p + z * z / (2 * n)
    mar = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return round((centro - mar) / den * 100, 1), round((centro + mar) / den * 100, 1)


d = pd.read_csv(CSV)
d["t"] = pd.to_datetime(d["recibido_en"].str.slice(0, 19))  # hora de Quito (-05:00) tal cual viene
d["dia"] = d["t"].dt.normalize()
d["dow"] = d["t"].dt.dayofweek
d["hora"] = d["t"].dt.hour

todos_los_dias = pd.date_range(d["dia"].min(), d["dia"].max())
n_dias = len(todos_los_dias)
n = len(d)

# ---------- Q1: leads por día, plataforma y campaña ----------
por_dia_plat = d.groupby(["dia", "plataforma"]).size().unstack(fill_value=0).reindex(todos_los_dias, fill_value=0)
fb = por_dia_plat["facebook"].astype(int).tolist()
ig = por_dia_plat["instagram"].astype(int).tolist()
total_dia = [a + b for a, b in zip(fb, ig)]
por_dow = (d.groupby(["dia"]).size().reindex(todos_los_dias, fill_value=0)
           .groupby(todos_los_dias.dayofweek).mean().round(1).tolist())
campanas = ["Sonrisa Blanca", "Ortodoncia sin Excusas", "Chequeo Anual"]
camp_plat = {p: [int(((d.campana == c) & (d.plataforma == p)).sum()) for c in campanas] for p in ("facebook", "instagram")}
camp_total = [int((d.campana == c).sum()) for c in campanas]

# ---------- Q2: ruta por campaña (solo leads procesados) ----------
proc = d[d.estado == "procesado"]
ruta_camp = {r: [int(((proc.campana == c) & (proc.ruta == r)).sum()) for c in campanas] for r in ("caliente", "tibio", "basura")}
proc_camp = [int((proc.campana == c).sum()) for c in campanas]

# ---------- Q3: calientes que terminan en cita ----------
cal = d[d.ruta == "caliente"].assign(cita=lambda x: x.cita_agendada == "si")
tib = d[d.ruta == "tibio"].assign(cita=lambda x: x.cita_agendada == "si")
conv_cal = []
for c in campanas:
    g = cal[cal.campana == c]
    lo, hi = wilson(int(g.cita.sum()), len(g))
    conv_cal.append({"campana": c, "citas": int(g.cita.sum()), "n": len(g), "pct": pct(g.cita.mean()), "lo": lo, "hi": hi})
conv_tib = [{"campana": c, "citas": int(tib[tib.campana == c].cita.sum()), "n": int((tib.campana == c).sum()),
             "pct": pct(tib[tib.campana == c].cita.mean())} for c in campanas]


def z_prop(a, b):
    ga, gb = cal[cal.campana == a].cita, cal[cal.campana == b].cita
    p = (ga.sum() + gb.sum()) / (len(ga) + len(gb))
    return (ga.mean() - gb.mean()) / sqrt(p * (1 - p) * (1 / len(ga) + 1 / len(gb)))


z_sb_or = round(z_prop("Sonrisa Blanca", "Ortodoncia sin Excusas"), 1)
z_sb_ch = round(z_prop("Sonrisa Blanca", "Chequeo Anual"), 1)
cita_cal_total = int(cal.cita.sum())
cita_tib_total = int(tib.cita.sum())

# ---------- Q4: aviso a recepción en menos de 5 minutos ----------
m = cal["minutos_hasta_aviso"]
dentro = int((m < PLAZO_MIN).sum())
fuera = int((m >= PLAZO_MIN).sum())
bins = list(range(0, 10)) + [10]
hist = [int(((m >= a) & (m < a + 1)).sum()) for a in bins[:-1]] + [int((m >= 10).sum())]
hist_labels = [f"{a}–{a + 1}" for a in bins[:-1]] + ["10+"]
semanas = cal.groupby(pd.Grouper(key="t", freq="W-MON", label="left", closed="left"))["minutos_hasta_aviso"]
sem_pct = semanas.apply(lambda s: (s < PLAZO_MIN).mean() * 100 if len(s) else None)
sem_n = semanas.count()
sem_df = pd.DataFrame({"pct": sem_pct, "n": sem_n}).dropna()
sem_df = sem_df[sem_df.n >= 20]  # semanas parciales (menos de 20 calientes) se omiten
sem_labels = [fecha_corta(x) for x in sem_df.index]

# ---------- Q5: salud del sistema ----------
estados = d.estado.value_counts().to_dict()
fall = d[d.estado == "fallido"]
motivos = ["timeout API pacientes", "JSON inválido del LLM"]
fall_dia = {mot: fall[fall.motivo_fallo == mot].groupby("dia").size().reindex(todos_los_dias, fill_value=0).astype(int).tolist()
            for mot in motivos}
fall_total = {mot: int((fall.motivo_fallo == mot).sum()) for mot in motivos}
rech_total = int((d.estado == "rechazado").sum())
dup_total = int((d.estado == "duplicado").sum())

# ---------- Anomalía 1: Instagram en cero ----------
ig_all = d[d.plataforma == "instagram"].sort_values("t")
hueco = ig_all.t.diff().dt.total_seconds().div(3600)
i_max = hueco.idxmax()
ig_hueco_h = round(float(hueco.max()), 1)
ig_fin = ig_all.loc[i_max, "t"]
ig_ini = ig_all[ig_all.t < ig_fin].t.max()
otros_huecos = hueco.drop(i_max).nlargest(1).iloc[0]
dias_cero = [x for x in todos_los_dias if por_dia_plat.loc[x, "instagram"] == 0]
cero_ini, cero_fin = dias_cero[0], dias_cero[-1]
base_ig = por_dia_plat["instagram"].drop(dias_cero)
dow_ig = base_ig.groupby(base_ig.index.dayofweek).mean()
esp_ig = round(float(sum(dow_ig[x.dayofweek] for x in dias_cero)), 0)
ig_p = d[d.plataforma == "instagram"]
sh_cal = float((ig_p.ruta == "caliente").mean())
sh_tib = float((ig_p.ruta == "tibio").mean())
conv_c = float(cal.cita.mean())
conv_t = float(tib.cita.mean())
esp_cal_ig = round(esp_ig * sh_cal)
esp_cita_ig = round(esp_ig * (sh_cal * conv_c + sh_tib * conv_t))
fb_ventana = int(por_dia_plat.loc[dias_cero, "facebook"].sum())
fb_esp = float(sum(por_dia_plat["facebook"].drop(dias_cero).groupby(por_dia_plat.drop(dias_cero).index.dayofweek).mean()[x.dayofweek] for x in dias_cero))
rechazados_ventana = int(((d.estado.isin(["rechazado", "fallido"])) & d.dia.isin(dias_cero)).sum())

# regla de alerta: 0 leads de una plataforma durante 5 horas seguidas, 8:00 a 20:00, lunes a viernes
H = 5


def ventanas_en_cero(plat):
    x = d[d.plataforma == plat]
    res = []
    for dia in todos_los_dias:
        if dia.dayofweek >= 5:
            continue
        for s in range(8, 20 - H + 1):
            a = dia + pd.Timedelta(hours=s)
            b = a + pd.Timedelta(hours=H)
            if ((x.t >= a) & (x.t < b)).sum() == 0:
                res.append((dia, s + H))  # (día, hora en que se cumplen las H horas)
    return res


v_ig = ventanas_en_cero("instagram")
v_fb = ventanas_en_cero("facebook")
en_corte = [v for v in v_ig if v[0] in dias_cero]
fuera_corte = [v for v in v_ig if v[0] not in dias_cero] + v_fb
primera_alerta = min(en_corte, key=lambda v: (v[0], v[1])) if en_corte else None
dias_habiles = sum(1 for x in todos_los_dias if x.dayofweek < 5 and x not in dias_cero)

# ---------- Anomalía 2: racha de timeouts de la API de pacientes ----------
to = fall[fall.motivo_fallo == "timeout API pacientes"]
to_dia = to.groupby("dia").size().reindex(todos_los_dias, fill_value=0)
umbral = to_dia.sort_values(ascending=False)
racha_dias = [x for x in todos_los_dias if to_dia[x] >= 4]
racha_ini, racha_fin = racha_dias[0], racha_dias[-1]
racha_rango = pd.date_range(racha_ini, racha_fin)
racha_n = int(to_dia[racha_rango].sum())
base_to = float(to_dia.drop(racha_rango).mean())
max_fuera = int(to_dia.drop(racha_rango).max())
t_ini = to[to.dia.isin(racha_rango)].t.min()
t_fin = to[to.dia.isin(racha_rango)].t.max()
leads_racha = d[d.dia.isin(racha_rango)]
falla_racha = pct((leads_racha.estado == "fallido").mean(), 1)
falla_resto = pct((d[~d.dia.isin(racha_rango)].estado == "fallido").mean(), 1)
# regla "3 timeouts en W horas": se prueba con ventanas cada vez más largas hasta la primera que
# no se dispare en falso fuera de la racha (una alerta que suena sin razón se acaba ignorando)
to_ord = to.sort_values("t").reset_index(drop=True)
en_racha = lambda ts: racha_ini <= ts.normalize() <= racha_fin
alerta_to, alerta_to_w, reglas_to_falsas = None, None, 0
for W in (3, 6, 12, 24):
    disp = [to_ord.t[i] for i in range(2, len(to_ord)) if (to_ord.t[i] - to_ord.t[i - 2]).total_seconds() <= W * 3600]
    falsos = sorted({x.normalize() for x in disp if not en_racha(x)})
    if not falsos:
        alerta_to, alerta_to_w, reglas_to_falsas = min(x for x in disp if en_racha(x)), W, 0
        break
w24_falsas = len({x.normalize() for x in [to_ord.t[i] for i in range(2, len(to_ord))
                  if (to_ord.t[i] - to_ord.t[i - 2]).total_seconds() <= 24 * 3600] if not en_racha(x)})

# ---------- Observación: lunes 14 de septiembre ----------
lunes = d[d.dow == 0].groupby("dia").size()
pico_dia = lunes.idxmax()
pico_n = int(lunes.max())
lunes_prom = round(float(lunes.drop(pico_dia).mean()), 0)
pico = d[d.dia == pico_dia]
pico_cal = pico[pico.ruta == "caliente"]
pico_fuera = pct((pico_cal.minutos_hasta_aviso >= PLAZO_MIN).mean(), 0)

# ---------- KPIs ----------
n_proc = int((d.estado == "procesado").sum())
K = {
    "total": n, "n_dias": n_dias, "prom_dia": round(n / n_dias),
    "desde": f"{todos_los_dias[0].day} de {['enero','febrero','marzo','abril','mayo','junio','julio','agosto','septiembre','octubre','noviembre','diciembre'][todos_los_dias[0].month-1]}",
    "hasta": f"{todos_los_dias[-1].day} de {['enero','febrero','marzo','abril','mayo','junio','julio','agosto','septiembre','octubre','noviembre','diciembre'][todos_los_dias[-1].month-1]} de {todos_los_dias[-1].year}",
    "calientes": len(cal), "pct_calientes": pct(len(cal) / n_proc),
    "citas_cal": cita_cal_total, "pct_cita_cal": pct(cita_cal_total / len(cal)),
    "citas_tib": cita_tib_total, "pct_cita_tib": pct(cita_tib_total / len(tib)),
    "pct_plazo": pct(dentro / len(cal)), "dentro": dentro, "fuera": fuera,
    "p50": round(float(m.median()), 1), "p90": round(float(m.quantile(0.9)), 1), "max_min": round(float(m.max()), 1),
    "fallidos": estados.get("fallido", 0), "pct_fallidos": pct(estados.get("fallido", 0) / n, 1),
    "rechazados": rech_total, "duplicados": dup_total, "procesados": n_proc,
    "pct_proc": pct(n_proc / n, 1), "pct_dup": pct(dup_total / n, 1), "pct_rech": pct(rech_total / n, 1),
    "fall_timeout": fall_total[motivos[0]], "fall_json": fall_total[motivos[1]],
    "ig_hueco_h": ig_hueco_h, "ig_otro_hueco_h": round(float(otros_huecos), 1),
    "ig_ultimo": ig_ini.strftime("%d/%m a las %H:%M"), "ig_vuelve": ig_fin.strftime("%d/%m a las %H:%M"),
    "cero_ini": f"{DIAS_SEMANA[cero_ini.dayofweek]} {cero_ini.day} de agosto",
    "cero_fin": f"{DIAS_SEMANA[cero_fin.dayofweek]} {cero_fin.day} de agosto",
    "n_dias_cero": len(dias_cero), "esp_ig": int(esp_ig), "esp_cal_ig": int(esp_cal_ig), "esp_cita_ig": int(esp_cita_ig),
    "fb_ventana": fb_ventana, "fb_esp": round(fb_esp), "rech_ventana": rechazados_ventana,
    "alerta_ig_h": H, "alerta_ig_dia": f"{DIAS_SEMANA[primera_alerta[0].dayofweek]} {primera_alerta[0].day} de agosto" if primera_alerta else "",
    "alerta_ig_hora": f"{primera_alerta[1]}:00" if primera_alerta else "", "alerta_falsas": len(fuera_corte), "dias_habiles": dias_habiles,
    "racha_ini": f"{DIAS_SEMANA[racha_ini.dayofweek]} {racha_ini.day} de septiembre",
    "racha_fin": f"{DIAS_SEMANA[racha_fin.dayofweek]} {racha_fin.day} de septiembre",
    "racha_n": racha_n, "base_to": round(base_to, 1).__str__().replace(".", ","), "max_to_fuera": max_fuera,
    "racha_rate": falla_racha, "resto_rate": falla_resto,
    "alerta_to_cuando": f"{alerta_to.day} de septiembre a las {alerta_to.strftime('%H:%M')}",
    "alerta_to_falsas": reglas_to_falsas, "alerta_to_w": alerta_to_w, "w24_falsas": w24_falsas,
    "alerta_to_dia": f"{DIAS_SEMANA[alerta_to.dayofweek]} {alerta_to.day} de septiembre", "alerta_to_hora": alerta_to.strftime("%H:%M"),
    "pico_dia": f"{DIAS_SEMANA[pico_dia.dayofweek]} {pico_dia.day} de septiembre", "pico_n": pico_n, "lunes_prom": int(lunes_prom),
    "pico_fuera": pico_fuera,
    "z_sb_or": z_sb_or, "z_sb_ch": z_sb_ch,
    "pct_ig": pct(ig_p.shape[0] / n, 0), "pct_fb": pct((d.plataforma == "facebook").sum() / n, 0),
    "pico_x": round(pico_n / lunes_prom, 1), "ciudad_vacia": pct(d.ciudad.isna().mean(), 1),
    "paciente_vacio": int(d.ya_es_paciente.isna().sum()),
    "pct_c0": pct(camp_total[0] / n, 0), "pct_c1": pct(camp_total[1] / n, 0), "pct_c2": pct(camp_total[2] / n, 0),
    **{f"cal{i}": pct(ruta_camp["caliente"][i] / proc_camp[i], 1) for i in range(3)},
    **{f"tib{i}": pct(ruta_camp["tibio"][i] / proc_camp[i], 1) for i in range(3)},
    **{f"bas{i}": pct(ruta_camp["basura"][i] / proc_camp[i], 1) for i in range(3)},
    **{f"conv{i}": conv_cal[i]["pct"] for i in range(3)},
    "dif_sb_or": round(conv_cal[0]["pct"] - conv_cal[1]["pct"], 1),
    "dow_lun": round(por_dow[0]), "dow_entre": round(sum(por_dow[1:5]) / 4), "dow_fin": round(sum(por_dow[5:]) / 2),
    "tib_min": min(round(ruta_camp["tibio"][i] / proc_camp[i] * 100) for i in range(3)),
    "tib_max": max(round(ruta_camp["tibio"][i] / proc_camp[i] * 100) for i in range(3)),
    "bas_min": min(round(ruta_camp["basura"][i] / proc_camp[i] * 100) for i in range(3)),
    "bas_max": max(round(ruta_camp["basura"][i] / proc_camp[i] * 100) for i in range(3)),
    "sem_min": round(min(sem_df.pct)), "sem_max": round(max(sem_df.pct)),
    "to_despues": int(((to.t > alerta_to) & to.dia.isin(racha_rango)).sum()),
    "json_sem_min": int(fall[fall.motivo_fallo == motivos[1]].groupby(fall.dia.dt.to_period("W")).size().min()),
    "json_sem_max": int(fall[fall.motivo_fallo == motivos[1]].groupby(fall.dia.dt.to_period("W")).size().max()),
    "fallidos_racha": int(((d.estado == "fallido") & d.dia.isin(racha_rango)).sum()),
    "leads_racha": int(len(leads_racha)),
    "pico_cal": int(len(pico_cal)),
}

DATOS = {
    "dias": [x.strftime("%Y-%m-%d") for x in todos_los_dias],
    "fb": fb, "ig": ig, "total_dia": total_dia, "por_dow": por_dow,
    "campanas": campanas, "camp_plat": camp_plat, "camp_total": camp_total,
    "ruta_camp": ruta_camp, "proc_camp": proc_camp,
    "conv_cal": conv_cal, "conv_tib": conv_tib,
    "hist": hist, "hist_labels": hist_labels, "plazo": PLAZO_MIN,
    "sem_labels": sem_labels, "sem_pct": [round(x, 1) for x in sem_df.pct], "sem_n": [int(x) for x in sem_df.n],
    "fall_dia": fall_dia, "motivos": motivos,
    "estados": {k: int(v) for k, v in estados.items()},
    "bandas": {
        "ig_cero": [dias_cero[0].strftime("%Y-%m-%d"), dias_cero[-1].strftime("%Y-%m-%d")],
        "timeouts": [racha_ini.strftime("%Y-%m-%d"), racha_fin.strftime("%Y-%m-%d")],
        "pico": pico_dia.strftime("%Y-%m-%d"),
    },
    "K": K,
}

plantilla = (AQUI / "plantilla.html").read_text(encoding="utf-8")
chart = (AQUI / "vendor" / "chart.umd.js").read_text(encoding="utf-8")
html = plantilla.replace("/*CHARTJS*/", chart.replace("</script", "<\\/script"))
html = html.replace("/*DATOS*/null", json.dumps(DATOS, ensure_ascii=False))


def fmt(v):
    if isinstance(v, float):
        return str(v).replace(".", ",")
    if isinstance(v, int) and abs(v) >= 1000:
        return f"{v:,}".replace(",", ".")
    return str(v)


for clave, valor in K.items():
    html = html.replace("{{" + clave + "}}", fmt(valor))
faltan = sorted(set(__import__("re").findall(r"\{\{(\w+)\}\}", html)))
if faltan:
    sys.exit(f"Faltan valores para: {faltan}")
(AQUI / "index.html").write_text(html, encoding="utf-8")
print(f"index.html escrito ({len(html) // 1024} KB). Leads: {n}, días: {n_dias}.")
print(json.dumps({k: K[k] for k in ("alerta_ig_dia", "alerta_ig_hora", "alerta_falsas", "alerta_to_cuando", "alerta_to_falsas",
                                      "esp_ig", "esp_cal_ig", "esp_cita_ig", "fb_ventana", "fb_esp", "rech_ventana")}, ensure_ascii=False))

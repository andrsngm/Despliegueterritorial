import os
import re
import pandas as pd
from flask import Flask, render_template, request, jsonify
from sqlalchemy import create_engine, text

app = Flask(__name__)
UPLOAD_FOLDER = 'uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Motor de base de datos consolidado
engine = create_engine('sqlite:///semilleros_cientificos.db')

def normalizar_area(nombre_pestaña):
    """Normaliza el nombre de la pestaña eliminando números romanos, dígitos y plurales."""
    if not nombre_pestaña: return "SIN AREA"
    area = nombre_pestaña.upper().strip()
    replacements = (("Á", "A"), ("É", "E"), ("Í", "I"), ("Ó", "O"), ("Ú", "U"))
    for a, b in replacements: area = area.replace(a, b)
    area = re.sub(r'\b[IVXLCDM]+\b', '', area)
    area = re.sub(r'\d+', '', area)
    area = re.sub(r'\bY\b', ' ', area)
    palabras = area.split()
    palabras_singular = [p[:-1] if p.endswith('S') and len(p) > 3 else p for p in palabras]
    return re.sub(r'\s+', ' ', " ".join(palabras_singular)).strip()

def detectar_grado(texto):
    """
    Detecta el número de grado (1-6) basándose en números, palabras clave 
    o variaciones comunes de tipeo.
    """
    t = str(texto).upper().strip()
    # Mapeo de palabras clave a números (Regex para mayor flexibilidad)
    mapeo = {
        '1': [r'1', r'PRIMER', r'1ERO', r'1RO'],
        '2': [r'2', r'SEGUNDO', r'2DO'],
        '3': [r'3', r'TERCER', r'3ERO', r'3RO'],
        '4': [r'4', r'CUARTO', r'4TO'],
        '5': [r'5', r'QUINTO', r'5TO'],
        '6': [r'6', r'SEXTO', r'6TO']
    }
    
    for grado, patrones in mapeo.items():
        for patron in patrones:
            if re.search(rf'\b{patron}\b', t) or (patron.isdigit() and patron in t):
                return f"g{grado}"
    return None

# --- RUTAS DE NAVEGACIÓN ---

@app.route('/')
def index():
    return render_template('html.html')

@app.route('/visor')
def visor():
    return render_template('ver_datos.html')

@app.route('/gestor_archivos')
def gestor_archivos():
    return render_template('gestor_archivos.html')

# --- RUTAS DE PROCESAMIENTO ---

@app.route('/procesar', methods=['POST'])
def procesar():
    estado = request.form.get('estado')
    archivos = request.files.getlist('archivos')
    
    if not estado or not archivos:
        return jsonify({"error": "Faltan datos"}), 400

    datos_acumulados = []

    for file in archivos:
        if file.filename == '': continue
        ruta_temporal = os.path.join(UPLOAD_FOLDER, file.filename)
        file.save(ruta_temporal)
        
        try:
            with pd.ExcelFile(ruta_temporal) as excel:
                for pestaña in excel.sheet_names:
                    if "INSTRUCCIONES" in pestaña.upper(): continue
                    
                    df = pd.read_excel(excel, sheet_name=pestaña, skiprows=6)
                    df.columns = [" ".join(str(c).replace('\n', ' ').split()) for c in df.columns]
                    
                    if 'Nombres del estudiante' in df.columns:
                        df = df.dropna(subset=['Nombres del estudiante'], how='all')
                    
                    if not df.empty:
                        area_base = normalizar_area(pestaña)
                        
                        # Conteo de Sexo
                        fem, masc = 0, 0
                        col_sexo = 'Sexo (Masculino o Femenino)'
                        if col_sexo in df.columns:
                            serie_sexo = df[col_sexo].astype(str).str.strip().str.upper()
                            fem = serie_sexo.str.startswith('F', na=False).sum()
                            masc = serie_sexo.str.startswith('M', na=False).sum()
                        
                        # Conteo de Grados Inteligente
                        grados_conteo = {f"g{i}": 0 for i in range(1, 7)}
                        col_grado = 'Grado que cursa'
                        
                        if col_grado in df.columns:
                            grados_detectados = df[col_grado].apply(detectar_grado)
                            conteo_real = grados_detectados.value_counts().to_dict()
                            for g_key in grados_conteo.keys():
                                grados_conteo[g_key] = int(conteo_real.get(g_key, 0))

                        total_jovenes = len(df)
                        tiene_error = (int(fem) + int(masc)) != total_jovenes

                        item = {
                            "estado": estado, 
                            "area": area_base, 
                            "grupos": 1,
                            "femeninas": int(fem), 
                            "masculinos": int(masc),
                            "jovenes": int(total_jovenes), 
                            "error": tiene_error
                        }
                        item.update(grados_conteo)
                        datos_acumulados.append(item)
        finally:
            if os.path.exists(ruta_temporal): os.remove(ruta_temporal)

    if datos_acumulados:
        df_final = pd.DataFrame(datos_acumulados)
        agg_rules = {
            'grupos': 'sum', 'femeninas': 'sum', 'masculinos': 'sum',
            'jovenes': 'sum', 'error': 'max',
            'g1': 'sum', 'g2': 'sum', 'g3': 'sum', 'g4': 'sum', 'g5': 'sum', 'g6': 'sum'
        }
        reporte = df_final.groupby(['estado', 'area']).agg(agg_rules).reset_index()
        
        reporte['area'] = reporte.apply(
            lambda x: f"{x['area']} <span style='font-size: 1.6em; vertical-align: middle;'>⚠️</span>" if x['error'] else x['area'], axis=1
        )
        return jsonify(reporte.drop(columns=['error']).to_dict(orient='records'))
    
    return jsonify([])

# --- RUTAS DE BASE DE DATOS ---

@app.route('/guardar_definitivo', methods=['POST'])
def guardar_definitivo():
    """Guarda confirmando y añadiendo columnas faltantes si la BD es antigua."""
    try:
        data = request.json
        if not data: return jsonify({"status": "error", "message": "No hay datos"}), 400
        
        df = pd.DataFrame(data)
        df['area'] = df['area'].str.split('<').str[0].str.strip()
        
        # --- MIGRACIÓN AUTOMÁTICA DE COLUMNAS ---
        with engine.connect() as conn:
            for i in range(1, 7):
                col_name = f"g{i}"
                try:
                    conn.execute(text(f"SELECT {col_name} FROM conformacion_grupos_final LIMIT 1"))
                except Exception:
                    conn.execute(text(f"ALTER TABLE conformacion_grupos_final ADD COLUMN {col_name} INTEGER DEFAULT 0"))
                    conn.commit()
        # ----------------------------------------
        
        df.to_sql('conformacion_grupos_final', con=engine, if_exists='append', index=False)
        return jsonify({"status": "success", "message": "¡Datos y grados guardados exitosamente!"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/obtener_datos_bd', methods=['GET'])
def obtener_datos_bd():
    try:
        df = pd.read_sql('SELECT * FROM conformacion_grupos_final', con=engine)
        return jsonify(df.to_dict(orient='records'))
    except Exception:
        return jsonify([])

@app.route('/eliminar_registro', methods=['POST'])
def eliminar_registro():
    """Elimina un área específica de un estado."""
    try:
        data = request.json
        estado = data.get('estado')
        area = data.get('area')
        
        if not estado or not area:
            return jsonify({"status": "error", "message": "Faltan datos para eliminar"}), 400

        with engine.connect() as conn:
            query = text("DELETE FROM conformacion_grupos_final WHERE estado = :estado AND area = :area")
            conn.execute(query, {"estado": estado, "area": area})
            conn.commit()
            
        return jsonify({"status": "success", "message": f"Registros de {area} en {estado} eliminados."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)

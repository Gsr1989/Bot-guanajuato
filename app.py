from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from supabase import create_client, Client
import fitz
import os
from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.filters import Command
from aiogram.types import FSInputFile, ContentType
from contextlib import asynccontextmanager, suppress
import asyncio
import qrcode
from io import BytesIO
import random
from PIL import Image
import json

# ------------ CONFIG ------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "https://xsagwqepoljfsogusubw.supabase.co")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InhzYWd3cWVwb2xqZnNvZ3VzdWJ3Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NDM5NjM3NTUsImV4cCI6MjA1OTUzOTc1NX0.NUixULn0m2o49At8j6X58UqbXre2O2_JStqzls_8Gws")
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
OUTPUT_DIR = "documentos"
PLANTILLA_GUANAJUATO_PRIMERA = "guanajuato_imagen_fullhd.pdf"
PLANTILLA_GUANAJUATO_SEGUNDA = "guanajuato.pdf"
ADMIN_PASSWORD = "sero"

PRECIO_PERMISO = 150

# URL de verificación para QR dinámico
URL_VERIFICACION_BASE = "https://direcciongeneraltransporteguanajuato-gob.onrender.com"

os.makedirs(OUTPUT_DIR, exist_ok=True)

# ------------ SUPABASE ------------
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ------------ BOT ------------
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ------------ SISTEMA DE FOLIOS 192X (ATÓMICO Y ÚNICO) ------------
_folio_lock = asyncio.Lock()
_ultimo_consecutivo = None

def _leer_ultimo_consecutivo_local():
    """Lee el último consecutivo desde archivo local"""
    try:
        with open("folio_192_cursor.json") as f:
            data = json.load(f)
            return int(data.get("ultimo", 0))
    except Exception:
        return 0

def _guardar_consecutivo_local(consecutivo: int):
    """Guarda el consecutivo actual en archivo local"""
    try:
        with open("folio_192_cursor.json", "w") as f:
            json.dump({"ultimo": consecutivo}, f)
    except Exception as e:
        print(f"[WARN] No se pudo guardar consecutivo local: {e}")

def _leer_ultimo_consecutivo_db():
    """Lee el último consecutivo desde Supabase (folios que empiezan con 192)"""
    try:
        # Buscar todos los folios que empiezan con "192"
        resp = (
            supabase.table("folios_registrados")
            .select("folio")
            .like("folio", "192%")
            .order("folio", desc=True)
            .limit(1)
            .execute()
        )
        
        if resp.data and len(resp.data) > 0:
            ultimo_folio = str(resp.data[0]["folio"])
            # Extraer el consecutivo (todo después de "192")
            if ultimo_folio.startswith("192"):
                consecutivo = int(ultimo_folio[3:])  # Quitar "192" y convertir a int
                print(f"[FOLIO 192] Último consecutivo en DB: {consecutivo} (folio completo: {ultimo_folio})")
                return consecutivo
        
        print(f"[FOLIO 192] No hay folios 192X en DB, empezando desde 1")
        return 0
        
    except Exception as e:
        print(f"[ERROR] Consultando último folio 192X: {e}")
        return 0

async def inicializar_sistema_folios_192():
    """Inicializa el sistema de folios 192X al arrancar el bot"""
    global _ultimo_consecutivo
    
    consecutivo_local = _leer_ultimo_consecutivo_local()
    consecutivo_db = _leer_ultimo_consecutivo_db()
    
    # Usar el mayor entre local y DB
    _ultimo_consecutivo = max(consecutivo_local, consecutivo_db)
    
    print(f"[FOLIO 192] Sistema inicializado. Último consecutivo: {_ultimo_consecutivo}")
    print(f"[FOLIO 192] Próximo folio será: 192{_ultimo_consecutivo + 1}")
    
    _guardar_consecutivo_local(_ultimo_consecutivo)

async def generar_folio_192():
    """
    Genera el siguiente folio con prefijo 192
    Formato: 1921, 1922, 1923, 1924... hasta el infinito
    El prefijo 192 NUNCA cambia, solo incrementa el consecutivo
    """
    global _ultimo_consecutivo
    
    max_intentos = 50
    
    async with _folio_lock:
        for intento in range(max_intentos):
            _ultimo_consecutivo += 1
            folio = f"192{_ultimo_consecutivo}"
            
            # Verificar que no existe en la BD
            try:
                verificacion = supabase.table("folios_registrados") \
                    .select("folio") \
                    .eq("folio", folio) \
                    .execute()
                
                if not verificacion.data:
                    # Folio disponible
                    _guardar_consecutivo_local(_ultimo_consecutivo)
                    print(f"[FOLIO 192] Generado: {folio} (consecutivo: {_ultimo_consecutivo})")
                    return folio
                else:
                    print(f"[FOLIO 192] {folio} duplicado en DB, intentando siguiente...")
                    continue
                    
            except Exception as e:
                print(f"[ERROR] Verificando folio {folio}: {e}")
                continue
        
        # Si llegamos aquí, hubo un problema grave
        print(f"[ERROR CRÍTICO] No se pudo generar folio después de {max_intentos} intentos")
        # Fallback: usar timestamp
        import time
        folio_fallback = f"192{int(time.time()) % 1000000}"
        return folio_fallback

async def guardar_folio_con_reintento(datos, user_id, username):
    """Inserta el folio en DB con reintentos ante colisión"""
    max_intentos = 20
    
    for intento in range(max_intentos):
        try:
            folio = await generar_folio_192()
            datos["folio"] = folio
            
            supabase.table("folios_registrados").insert({
                "folio": folio,
                "marca": datos["marca"],
                "linea": datos["linea"],
                "anio": datos["anio"],
                "numero_serie": datos["serie"],
                "numero_motor": datos["motor"],
                "color": datos["color"],
                "nombre": datos["nombre"],
                "fecha_expedicion": datos["fecha_exp"].date().isoformat(),
                "fecha_vencimiento": datos["fecha_ven"].date().isoformat(),
                "entidad": "Guanajuato",
                "estado": "PENDIENTE",
                "user_id": user_id,
                "username": username or "Sin username"
            }).execute()
            
            print(f"[ÉXITO] Folio {folio} guardado (intento {intento + 1})")
            return True, folio
            
        except Exception as e:
            em = str(e).lower()
            if "duplicate" in em or "unique constraint" in em or "23505" in em:
                print(f"[DUPLICADO] Folio existe, generando siguiente (intento {intento + 1}/{max_intentos})")
                await asyncio.sleep(0.1)
                continue
            
            print(f"[ERROR BD] {e}")
            return False, None
    
    print(f"[ERROR FATAL] No se pudo guardar tras {max_intentos} intentos")
    return False, None

# ------------ TIMER MANAGEMENT (24 HORAS) ------------
timers_activos = {}

async def eliminar_folio_automatico(user_id: int, folio: str):
    """Elimina folio automáticamente después de 24 horas"""
    try:
        supabase.table("folios_registrados").delete().eq("folio", folio).execute()
        supabase.table("borradores_registros").delete().eq("folio", folio).execute()
        
        await bot.send_message(
            user_id,
            f"⏰ TIEMPO AGOTADO - GUANAJUATO\n\n"
            f"El folio {folio} ha sido eliminado por no completar el pago en 24 horas.\n\n"
            f"Para tramitar un nuevo permiso use /permiso"
        )
        
        if folio in timers_activos:
            del timers_activos[folio]
            
    except Exception as e:
        print(f"[ERROR] Eliminando folio {folio}: {e}")

async def enviar_recordatorio(user_id: int, folio: str, horas_restantes: int):
    """Envía recordatorios de pago"""
    try:
        await bot.send_message(
            user_id,
            f"⚡ RECORDATORIO DE PAGO - GUANAJUATO\n\n"
            f"Folio: {folio}\n"
            f"⏱️ Te quedan {horas_restantes} horas para pagar\n"
            f"💰 Precio: ${PRECIO_PERMISO}\n\n"
            f"📸 Envía tu comprobante de pago para validar\n"
            f"🔐 O usa el comando: {ADMIN_PASSWORD}{folio}"
        )
    except Exception as e:
        print(f"[ERROR] Enviando recordatorio a {user_id}: {e}")

async def iniciar_timer_pago(user_id: int, folio: str):
    """Timer de 24 horas con avisos 12h/4h/1h antes del vencimiento"""
    async def timer_task():
        print(f"[TIMER] Iniciado para folio {folio}, usuario {user_id}")
        
        # Dormir 12 horas
        await asyncio.sleep(12 * 3600)
        if folio not in timers_activos:
            return
        await enviar_recordatorio(user_id, folio, 12)
        
        # Dormir 8 horas más (total 20h, faltan 4)
        await asyncio.sleep(8 * 3600)
        if folio not in timers_activos:
            return
        await enviar_recordatorio(user_id, folio, 4)
        
        # Dormir 3 horas más (total 23h, falta 1)
        await asyncio.sleep(3 * 3600)
        if folio not in timers_activos:
            return
        await enviar_recordatorio(user_id, folio, 1)
        
        # Dormir la última hora
        await asyncio.sleep(1 * 3600)
        
        # Eliminar si sigue activo
        if folio in timers_activos:
            print(f"[TIMER] Expirado para folio {folio} - eliminando")
            await eliminar_folio_automatico(user_id, folio)
    
    task = asyncio.create_task(timer_task())
    timers_activos[folio] = {
        "task": task,
        "user_id": user_id,
        "start_time": datetime.now()
    }
    
    print(f"[TIMER] Iniciado 24h para folio {folio}. Total activos: {len(timers_activos)}")

def cancelar_timer(folio: str):
    """Cancela el timer cuando se recibe el pago o comando admin"""
    if folio in timers_activos:
        timers_activos[folio]["task"].cancel()
        del timers_activos[folio]
        print(f"[TIMER] Cancelado folio {folio}. Restantes: {len(timers_activos)}")

# ------------ FSM STATES ------------
class PermisoForm(StatesGroup):
    marca = State()
    linea = State()
    anio = State()
    serie = State()
    motor = State()
    color = State()
    nombre = State()

# ------------ COORDENADAS GUANAJUATO ------------
coords_gto_primera = {
    "folio": (1800, 455, 60, (1, 0, 0)),
    "fecha": (2200, 580, 35, (0, 0, 0)),
    "marca": (385, 715, 35, (0, 0, 0)),
    "serie": (350, 800, 35, (0, 0, 0)),
    "linea": (800, 715, 35, (0, 0, 0)),
    "motor": (1290, 800, 35, (0, 0, 0)),
    "anio": (1500, 715, 35, (0, 0, 0)),
    "color": (1960, 715, 35, (0, 0, 0)),
    "nombre": (950, 1100, 50, (0, 0, 0)),
    "vigencia": (2200, 645, 35, (0, 0, 0)),
}

coords_gto_segunda = {
    "numero_serie": (255.0, 180.0, 10, (0, 0, 0)),
    "fecha": (255.0, 396.0, 10, (0, 0, 0)),
}

# Coordenadas para QR dinámico (ajustar según tu PDF)
coords_qr_dinamico = {
    "x": 135,
    "y": 320,
    "ancho": 270,
    "alto": 270
}

# ------------ GENERACIÓN DE QRs ------------
def generar_qr_dinamico(folio):
    """Genera QR dinámico que apunta DIRECTAMENTE al resultado"""
    try:
        # Ahora usa /consulta/{folio} en lugar de /consulta_folio?folio=
        url_verificacion = f"{URL_VERIFICACION_BASE}/consulta/{folio}"
        
        qr = qrcode.QRCode(
            version=2,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=4,
            border=1
        )
        qr.add_data(url_verificacion)
        qr.make(fit=True)
        
        img_qr = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        print(f"[QR DINÁMICO] Generado para folio {folio} -> {url_verificacion}")
        return img_qr, url_verificacion
        
    except Exception as e:
        print(f"[ERROR QR DINÁMICO] {e}")
        return None, None
        
def generar_qr_texto(datos, folio):
    """Genera QR con texto de los datos del vehículo (el que ya tenías)"""
    try:
        texto_qr = f"""FOLIO: {folio}
NOMBRE: {datos.get('nombre', '')}
MARCA: {datos.get('marca', '')}
LINEA: {datos.get('linea', '')}
AÑO: {datos.get('anio', '')}
SERIE: {datos.get('serie', '')}
MOTOR: {datos.get('motor', '')}
COLOR: {datos.get('color', '')}
GUANAJUATO PERMISOS DIGITALES"""
        
        qr = qrcode.QRCode(
            version=2,
            error_correction=qrcode.constants.ERROR_CORRECT_H,
            box_size=10,
            border=2
        )
        qr.add_data(texto_qr.upper())
        qr.make(fit=True)
        
        img_qr = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        print(f"[QR TEXTO] Generado para folio {folio}")
        return img_qr
        
    except Exception as e:
        print(f"[ERROR QR TEXTO] {e}")
        return None

# ------------ GENERACIÓN PDF GUANAJUATO UNIFICADO ------------
def generar_pdf_guanajuato_unificado(folio, datos, fecha_exp, fecha_ven):
    """Genera UN SOLO PDF con ambas plantillas + QR dinámico + QR de texto"""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    
    # Crear documento final vacío
    doc_final = fitz.open()
    
    # === PRIMERA PLANTILLA ===
    doc_primera = fitz.open(PLANTILLA_GUANAJUATO_PRIMERA)
    pg1 = doc_primera[0]
    
    # Insertar datos
    pg1.insert_text(coords_gto_primera["folio"][:2], folio, 
                    fontsize=coords_gto_primera["folio"][2], 
                    color=coords_gto_primera["folio"][3])
    
    f_exp = fecha_exp.strftime("%d/%m/%Y")
    f_ven = fecha_ven.strftime("%d/%m/%Y")
    
    pg1.insert_text(coords_gto_primera["fecha"][:2], f_exp, 
                    fontsize=coords_gto_primera["fecha"][2], 
                    color=coords_gto_primera["fecha"][3])
    pg1.insert_text(coords_gto_primera["vigencia"][:2], f_ven, 
                    fontsize=coords_gto_primera["vigencia"][2], 
                    color=coords_gto_primera["vigencia"][3])

    for key in ["marca", "serie", "linea", "motor", "anio", "color"]:
        if key in datos:
            x, y, s, col = coords_gto_primera[key]
            pg1.insert_text((x, y), datos[key], fontsize=s, color=col)

    pg1.insert_text(coords_gto_primera["nombre"][:2], datos.get("nombre", ""), 
                    fontsize=coords_gto_primera["nombre"][2], 
                    color=coords_gto_primera["nombre"][3])

    # === INSERTAR QR DE TEXTO (el original) ===
    img_qr_texto = generar_qr_texto(datos, folio)
    if img_qr_texto:
        buf_texto = BytesIO()
        img_qr_texto.save(buf_texto, format="PNG")
        buf_texto.seek(0)
        qr_texto_pix = fitz.Pixmap(buf_texto.read())

        cm = 85.05
        ancho_qr = alto_qr = cm * 3.0
        page_width = pg1.rect.width
        x_qr = page_width - (2.5 * cm) - ancho_qr
        y_qr = 20.5 * cm

        pg1.insert_image(
            fitz.Rect(x_qr, y_qr, x_qr + ancho_qr, y_qr + alto_qr),
            pixmap=qr_texto_pix,
            overlay=True
        )
        print(f"[QR TEXTO] Insertado en PDF")

    # === INSERTAR QR DINÁMICO (nuevo) ===
    img_qr_dinamico, url_verificacion = generar_qr_dinamico(folio)
    if img_qr_dinamico:
        buf_dinamico = BytesIO()
        img_qr_dinamico.save(buf_dinamico, format="PNG")
        buf_dinamico.seek(0)
        qr_dinamico_pix = fitz.Pixmap(buf_dinamico.read())

        x_qr_din = coords_qr_dinamico["x"]
        y_qr_din = coords_qr_dinamico["y"]
        ancho_qr_din = coords_qr_dinamico["ancho"]
        alto_qr_din = coords_qr_dinamico["alto"]

        pg1.insert_image(
            fitz.Rect(x_qr_din, y_qr_din, x_qr_din + ancho_qr_din, y_qr_din + alto_qr_din),
            pixmap=qr_dinamico_pix,
            overlay=True
        )
        print(f"[QR DINÁMICO] Insertado en PDF -> {url_verificacion}")
    
    # Agregar primera página
    doc_final.insert_pdf(doc_primera)
    doc_primera.close()
    
    # === SEGUNDA PLANTILLA ===
    doc_segunda = fitz.open(PLANTILLA_GUANAJUATO_SEGUNDA)
    pg2 = doc_segunda[0]
    
    pg2.insert_text(coords_gto_segunda["numero_serie"][:2], 
                    datos.get("serie", ""), 
                    fontsize=coords_gto_segunda["numero_serie"][2], 
                    color=coords_gto_segunda["numero_serie"][3])
    
    pg2.insert_text(coords_gto_segunda["fecha"][:2], 
                    f_exp, 
                    fontsize=coords_gto_segunda["fecha"][2], 
                    color=coords_gto_segunda["fecha"][3])
    
    # Agregar segunda página
    doc_final.insert_pdf(doc_segunda)
    doc_segunda.close()
    
    # Guardar
    salida_unificada = os.path.join(OUTPUT_DIR, f"{folio}_guanajuato_completo.pdf")
    doc_final.save(salida_unificada)
    doc_final.close()
    
    print(f"[PDF] Generado: {salida_unificada}")
    return salida_unificada

# ------------ HANDLERS GUANAJUATO ------------
@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "🏛️ SISTEMA DIGITAL DE PERMISOS - GUANAJUATO\n\n"
        f"🚗 Permiso de circulación: ${PRECIO_PERMISO}\n"
        "⏰ Tiempo límite de pago: 24 horas\n"
        "💳 Métodos: Transferencia y OXXO\n\n"
        "Para iniciar use /permiso\n\n"
        f"🔐 Admin: {ADMIN_PASSWORD}[folio] para detener timer"
    )

@dp.message(Command("permiso"))
async def permiso_cmd(message: types.Message, state: FSMContext):
    await message.answer(
        f"🚗 NUEVO PERMISO DE GUANAJUATO (${PRECIO_PERMISO})\n\n"
        "Primer dato: MARCA del vehículo"
    )
    await state.set_state(PermisoForm.marca)

@dp.message(PermisoForm.marca)
async def get_marca(message: types.Message, state: FSMContext):
    marca = message.text.strip().upper()
    await state.update_data(marca=marca)
    await message.answer(f"✅ MARCA: {marca}\n\nAhora la LÍNEA:")
    await state.set_state(PermisoForm.linea)

@dp.message(PermisoForm.linea)
async def get_linea(message: types.Message, state: FSMContext):
    linea = message.text.strip().upper()
    await state.update_data(linea=linea)
    await message.answer(f"✅ LÍNEA: {linea}\n\nEl AÑO (4 dígitos):")
    await state.set_state(PermisoForm.anio)

@dp.message(PermisoForm.anio)
async def get_anio(message: types.Message, state: FSMContext):
    anio = message.text.strip()
    if not anio.isdigit() or len(anio) != 4:
        await message.answer("⚠️ Año inválido. Use 4 dígitos (ej: 2020):")
        return
    
    await state.update_data(anio=anio)
    await message.answer(f"✅ AÑO: {anio}\n\nNÚMERO DE SERIE:")
    await state.set_state(PermisoForm.serie)

@dp.message(PermisoForm.serie)
async def get_serie(message: types.Message, state: FSMContext):
    serie = message.text.strip().upper()
    if len(serie) < 5:
        await message.answer("⚠️ Serie muy corta. Verifique:")
        return
        
    await state.update_data(serie=serie)
    await message.answer(f"✅ SERIE: {serie}\n\nNÚMERO DE MOTOR:")
    await state.set_state(PermisoForm.motor)

@dp.message(PermisoForm.motor)
async def get_motor(message: types.Message, state: FSMContext):
    motor = message.text.strip().upper()
    await state.update_data(motor=motor)
    await message.answer(f"✅ MOTOR: {motor}\n\nCOLOR del vehículo:")
    await state.set_state(PermisoForm.color)

@dp.message(PermisoForm.color)
async def get_color(message: types.Message, state: FSMContext):
    color = message.text.strip().upper()
    await state.update_data(color=color)
    await message.answer(f"✅ COLOR: {color}\n\nNOMBRE COMPLETO del propietario:")
    await state.set_state(PermisoForm.nombre)

@dp.message(PermisoForm.nombre)
async def get_nombre(message: types.Message, state: FSMContext):
    datos = await state.get_data()
    nombre = message.text.strip().upper()
    datos["nombre"] = nombre

    hoy = datetime.now()
    fecha_ven = hoy + timedelta(days=30)
    datos["fecha_exp"] = hoy
    datos["fecha_ven"] = fecha_ven

    await message.answer("🔄 Generando folio único 192X...")

    try:
        # Guardar con reintento
        ok, folio = await guardar_folio_con_reintento(datos, message.from_user.id, message.from_user.username)
        
        if not ok:
            await message.answer(
                "❌ Error generando folio. Intente nuevamente con /permiso"
            )
            await state.clear()
            return

        await message.answer(
            f"📋 PROCESANDO PERMISO DE GUANAJUATO\n\n"
            f"Folio: {folio}\n"
            f"Titular: {nombre}\n"
            f"Vigencia: 30 días\n\n"
            "Generando documentación..."
        )

        # Generar PDF unificado con ambos QRs
        pdf_completo = generar_pdf_guanajuato_unificado(folio, datos, hoy, fecha_ven)

        # Enviar documento
        await message.answer_document(
            FSInputFile(pdf_completo),
            caption=f"📋 PERMISO COMPLETO GUANAJUATO\n"
                   f"Folio: {folio}\n"
                   f"Vigencia: {fecha_ven.strftime('%d/%m/%Y')}\n"
                   f"📄 2 páginas + QR dinámico de verificación\n"
                   f"🏛️ Sistema oficial Guanajuato"
        )

        # Guardar borrador
        try:
            supabase.table("borradores_registros").insert({
                "folio": folio,
                "entidad": "Guanajuato",
                "numero_serie": datos["serie"],
                "marca": datos["marca"],
                "linea": datos["linea"],
                "numero_motor": datos["motor"],
                "anio": datos["anio"],
                "color": datos["color"],
                "fecha_expedicion": hoy.isoformat(),
                "fecha_vencimiento": fecha_ven.isoformat(),
                "contribuyente": datos["nombre"],
                "estado": "PENDIENTE",
                "user_id": message.from_user.id
            }).execute()
        except Exception as e:
            print(f"[WARN] Error guardando borrador: {e}")

        # Iniciar timer de 24 horas
        await iniciar_timer_pago(message.from_user.id, folio)

        # Instrucciones de pago
        await message.answer(
            f"💰 INSTRUCCIONES DE PAGO\n\n"
            f"📄 Folio: {folio}\n"
            f"💵 Cantidad: ${PRECIO_PERMISO}\n"
            f"⏰ Tiempo límite: 24 horas\n\n"
            
            "🏦 TRANSFERENCIA:\n"
            "• Banco: [TU BANCO]\n"
            "• Cuenta: [TU CUENTA]\n"
            "• CLABE: [TU CLABE]\n"
            f"• Concepto: Permiso {folio}\n\n"
            
            "🏪 OXXO:\n"
            "• Referencia: [TU REFERENCIA]\n"
            f"• Cantidad: ${PRECIO_PERMISO}\n\n"
            
            f"📸 Envía foto del comprobante para validar\n"
            f"⚠️ Si no pagas en 24h, el folio {folio} será eliminado\n\n"
            f"🔐 ADMIN: {ADMIN_PASSWORD}{folio} para detener timer"
        )
        
    except Exception as e:
        await message.answer(f"❌ ERROR: {str(e)}\n\nIntente con /permiso")
        print(f"[ERROR] get_nombre: {e}")
    finally:
        await state.clear()

# Handler para comprobantes (imágenes)
@dp.message(lambda message: message.content_type == ContentType.PHOTO)
async def recibir_comprobante(message: types.Message):
    user_id = message.from_user.id
    
    # Buscar folios del usuario
    folios_usuario = [folio for folio, data in timers_activos.items() if data["user_id"] == user_id]
    
    if not folios_usuario:
        await message.answer(
            "ℹ️ No tienes permisos pendientes de pago.\n"
            "Usa /permiso para generar uno nuevo."
        )
        return
    
    # Tomar el más reciente
    folio = folios_usuario[-1]
    
    # Cancelar timer
    cancelar_timer(folio)
    
    # Actualizar estado
    try:
        supabase.table("folios_registrados").update({
            "estado": "COMPROBANTE_ENVIADO",
            "fecha_comprobante": datetime.now().isoformat()
        }).eq("folio", folio).execute()
        
        supabase.table("borradores_registros").update({
            "estado": "COMPROBANTE_ENVIADO",
            "fecha_comprobante": datetime.now().isoformat()
        }).eq("folio", folio).execute()
    except Exception as e:
        print(f"[ERROR] Actualizando estado: {e}")
    
    await message.answer(
        f"✅ COMPROBANTE RECIBIDO\n\n"
        f"📄 Folio: {folio}\n"
        f"⏱️ Timer detenido\n"
        f"🔍 Verificando pago...\n\n"
        f"Su comprobante está siendo validado.\n"
        f"Gracias por usar el sistema de Guanajuato."
    )

# Handler para comando admin (sero + folio completo)
@dp.message(lambda message: message.text and message.text.startswith(ADMIN_PASSWORD))
async def admin_detener_timer(message: types.Message):
    texto = message.text.strip()
    
    # Extraer folio después de "sero"
    if len(texto) > len(ADMIN_PASSWORD):
        folio = texto[len(ADMIN_PASSWORD):]
        
        if folio in timers_activos:
            cancelar_timer(folio)
            
            # Actualizar a VALIDADO_ADMIN
            try:
                supabase.table("folios_registrados").update({
                    "estado": "VALIDADO_ADMIN",
                    "fecha_admin_stop": datetime.now().isoformat()
                }).eq("folio", folio).execute()
                
                supabase.table("borradores_registros").update({
                    "estado": "VALIDADO_ADMIN",
                    "fecha_admin_stop": datetime.now().isoformat()
                }).eq("folio", folio).execute()
            except Exception as e:
                print(f"[ERROR] Actualizando estado admin: {e}")
            
            await message.answer(
                f"🔐 ADMIN: Timer detenido\n\n"
                f"Folio: {folio}\n"
                f"⏱️ Timer cancelado\n"
                f"✅ Estado: VALIDADO_ADMIN"
            )
            
            # Notificar al usuario
            try:
                user_id = timers_activos[folio]["user_id"]
                await bot.send_message(
                    user_id,
                    f"✅ PAGO VALIDADO - GUANAJUATO\n\n"
                    f"Folio: {folio}\n"
                    f"Tu permiso está activo para circular."
                )
            except Exception as e:
                print(f"[ERROR] Notificando usuario: {e}")
        else:
            await message.answer(
                f"⚠️ El folio {folio} no tiene timer activo.\n"
                f"Timers activos: {len(timers_activos)}"
            )
    else:
        await message.answer(
            f"📋 TIMERS ACTIVOS: {len(timers_activos)}\n\n"
            f"Para detener:\n"
            f"{ADMIN_PASSWORD}[FOLIO_COMPLETO]\n\n"
            f"Ejemplo: {ADMIN_PASSWORD}1921"
        )

@dp.message()
async def fallback(message: types.Message):
    respuestas = [
        "🏛️ Sistema Guanajuato. Use /permiso para tramitar",
        "🚗 Para permisos use: /permiso",
        "📋 Comando: /permiso",
    ]
    await message.answer(random.choice(respuestas))

# ------------ FASTAPI ------------
_keep_task = None

async def keep_alive():
    while True:
        await asyncio.sleep(600)
        print("[HEARTBEAT] Bot Guanajuato activo")

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _keep_task
    
    # Inicializar sistema de folios 192X
    await inicializar_sistema_folios_192()
    
    await bot.delete_webhook(drop_pending_updates=True)
    if BASE_URL:
        await bot.set_webhook(f"{BASE_URL}/webhook", allowed_updates=["message"])
        _keep_task = asyncio.create_task(keep_alive())
    
    print("[SISTEMA] Bot Guanajuato iniciado correctamente")
    print(f"[FOLIO 192] Sistema de folios 192X operativo")
    
    yield
    
    if _keep_task:
        _keep_task.cancel()
        with suppress(asyncio.CancelledError):
            await _keep_task
    await bot.session.close()

app = FastAPI(lifespan=lifespan)

@app.post("/webhook")
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
        update = types.Update(**data)
        await dp.feed_webhook_update(bot, update)
        return {"ok": True}
    except Exception as e:
        print(f"[ERROR] webhook: {e}")
        return {"ok": False, "error": str(e)}

@app.get("/")
async def root():
    return {
        "bot": "Guanajuato Permisos Sistema",
        "status": "running",
        "version": "2.0",
        "sistema_folios": "192X (prefijo fijo 192)",
        "ultimo_consecutivo": _ultimo_consecutivo,
        "proximo_folio": f"192{_ultimo_consecutivo + 1}",
        "timers_activos": len(timers_activos),
        "caracteristicas": [
            "Folios únicos 192X (1921, 1922, 1923...)",
            "QR dinámico de verificación",
            "QR de texto con datos",
            "Timer 24h con avisos",
            "Comando admin: sero[folio]"
        ]
    }

@app.get("/status")
async def status():
    return {
        "sistema": "Guanajuato v2.0",
        "folios_generados": _ultimo_consecutivo,
        "proximo": f"192{_ultimo_consecutivo + 1}",
        "timers": len(timers_activos),
        "url_verificacion": URL_VERIFICACION_BASE
    }

if __name__ == '__main__':
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    print(f"[ARRANQUE] Puerto {port}")
    uvicorn.run(app, host="0.0.0.0", port=port) 

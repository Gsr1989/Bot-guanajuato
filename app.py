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
from aiogram.types import FSInputFile, ContentType, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
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

PRECIO_PERMISO = 150

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
            if ultimo_folio.startswith("192"):
                consecutivo = int(ultimo_folio[3:])
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
    
    _ultimo_consecutivo = max(consecutivo_local, consecutivo_db)
    
    print(f"[FOLIO 192] Sistema inicializado. Último consecutivo: {_ultimo_consecutivo}")
    print(f"[FOLIO 192] Próximo folio será: 192{_ultimo_consecutivo + 1}")
    
    _guardar_consecutivo_local(_ultimo_consecutivo)

async def generar_folio_192():
    """Genera el siguiente folio con prefijo 192"""
    global _ultimo_consecutivo
    
    max_intentos = 100000
    
    async with _folio_lock:
        for intento in range(max_intentos):
            _ultimo_consecutivo += 1
            folio = f"192{_ultimo_consecutivo}"
            
            try:
                verificacion = supabase.table("folios_registrados") \
                    .select("folio") \
                    .eq("folio", folio) \
                    .execute()
                
                if not verificacion.data:
                    _guardar_consecutivo_local(_ultimo_consecutivo)
                    print(f"[FOLIO 192] Generado: {folio} (consecutivo: {_ultimo_consecutivo})")
                    return folio
                else:
                    print(f"[FOLIO 192] {folio} duplicado en DB, intentando siguiente...")
                    continue
                    
            except Exception as e:
                print(f"[ERROR] Verificando folio {folio}: {e}")
                continue
        
        print(f"[ERROR CRÍTICO] No se pudo generar folio después de {max_intentos} intentos")
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

# ------------ TIMER MANAGEMENT - 36 HORAS ------------
timers_activos = {}
user_folios = {}

async def eliminar_folio_automatico(folio: str):
    """Elimina folio automáticamente después de 36 horas"""
    try:
        user_id = None
        if folio in timers_activos:
            user_id = timers_activos[folio]["user_id"]
        
        supabase.table("folios_registrados").delete().eq("folio", folio).execute()
        supabase.table("borradores_registros").delete().eq("folio", folio).execute()
        
        if user_id:
            await bot.send_message(
                user_id,
                f"⏰ TIEMPO AGOTADO - GUANAJUATO\n\n"
                f"El folio {folio} ha sido eliminado del sistema por no completar el pago en 36 horas.\n\n"
                f"📋 Para generar otro permiso use /chuleta"
            )
        
        limpiar_timer_folio(folio)
    except Exception as e:
        print(f"Error eliminando folio {folio}: {e}")

async def enviar_recordatorio(folio: str, minutos_restantes: int):
    """Envía recordatorios de pago"""
    try:
        if folio not in timers_activos:
            return
            
        user_id = timers_activos[folio]["user_id"]
        
        await bot.send_message(
            user_id,
            f"⚡ RECORDATORIO DE PAGO - GUANAJUATO\n\n"
            f"Folio: {folio}\n"
            f"Tiempo restante: {minutos_restantes} minutos\n"
            f"Monto: ${PRECIO_PERMISO}\n\n"
            f"📸 Envíe su comprobante de pago (imagen) para validar el trámite.\n\n"
            f"📋 Para generar otro permiso use /chuleta"
        )
    except Exception as e:
        print(f"Error enviando recordatorio para folio {folio}: {e}")

async def iniciar_timer_pago(user_id: int, folio: str):
    """Inicia el timer de 36 horas con recordatorios progresivos"""
    async def timer_task():
        start_time = datetime.now()
        print(f"[TIMER] Iniciado para folio {folio}, usuario {user_id} (36 horas)")
        
        await asyncio.sleep(34.5 * 3600)

        if folio not in timers_activos:
            return
        await enviar_recordatorio(folio, 90)
        await asyncio.sleep(30 * 60)

        if folio not in timers_activos:
            return
        await enviar_recordatorio(folio, 60)
        await asyncio.sleep(30 * 60)

        if folio not in timers_activos:
            return
        await enviar_recordatorio(folio, 30)
        await asyncio.sleep(20 * 60)

        if folio not in timers_activos:
            return
        await enviar_recordatorio(folio, 10)
        await asyncio.sleep(10 * 60)

        if folio in timers_activos:
            print(f"[TIMER] Expirado para folio {folio} - eliminando")
            await eliminar_folio_automatico(folio)
    
    task = asyncio.create_task(timer_task())
    timers_activos[folio] = {
        "task": task,
        "user_id": user_id,
        "start_time": datetime.now()
    }
    
    if user_id not in user_folios:
        user_folios[user_id] = []
    user_folios[user_id].append(folio)
    
    print(f"[SISTEMA] Timer 36h iniciado para folio {folio}, total timers: {len(timers_activos)}")

def cancelar_timer_folio(folio: str):
    """Cancela el timer de un folio específico cuando el usuario paga"""
    if folio in timers_activos:
        timers_activos[folio]["task"].cancel()
        user_id = timers_activos[folio]["user_id"]
        del timers_activos[folio]
        
        if user_id in user_folios and folio in user_folios[user_id]:
            user_folios[user_id].remove(folio)
            if not user_folios[user_id]:
                del user_folios[user_id]
        
        print(f"[SISTEMA] Timer cancelado para folio {folio}")

def limpiar_timer_folio(folio: str):
    """Limpia todas las referencias de un folio tras expirar"""
    if folio in timers_activos:
        user_id = timers_activos[folio]["user_id"]
        del timers_activos[folio]
        
        if user_id in user_folios and folio in user_folios[user_id]:
            user_folios[user_id].remove(folio)
            if not user_folios[user_id]:
                del user_folios[user_id]

def obtener_folios_usuario(user_id: int) -> list:
    """Obtiene todos los folios activos de un usuario"""
    return user_folios.get(user_id, [])

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

coords_qr_dinamico = {
    "x": 205,
    "y": 328,
    "ancho": 290,
    "alto": 290
}

# ------------ GENERACIÓN DE QRs ------------
def generar_qr_dinamico(folio):
    """Genera QR dinámico que apunta DIRECTAMENTE al resultado"""
    try:
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
    """Genera QR con texto de los datos del vehículo"""
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
    
    doc_final = fitz.open()
    
    # === PRIMERA PLANTILLA ===
    doc_primera = fitz.open(PLANTILLA_GUANAJUATO_PRIMERA)
    pg1 = doc_primera[0]
    
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

    # === INSERTAR QR DE TEXTO ===
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

    # === INSERTAR QR DINÁMICO ===
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
    
    doc_final.insert_pdf(doc_segunda)
    doc_segunda.close()
    
    salida_unificada = os.path.join(OUTPUT_DIR, f"{folio}_guanajuato_completo.pdf")
    doc_final.save(salida_unificada)
    doc_final.close()
    
    print(f"[PDF] Generado: {salida_unificada}")
    return salida_unificada

# ------------ HANDLERS ------------
@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "🏛️ SISTEMA DIGITAL DE PERMISOS - GUANAJUATO\n\n"
        f"🚗 Permiso de circulación: ${PRECIO_PERMISO}\n"
        "⏰ Tiempo límite de pago: 36 horas\n"
        "💳 Métodos: Transferencia y OXXO\n\n"
        "⚠️ IMPORTANTE: Su folio será eliminado automáticamente si no realiza el pago dentro del tiempo límite"
    )

@dp.message(Command("chuleta"))
async def chuleta_cmd(message: types.Message, state: FSMContext):
    folios_activos = obtener_folios_usuario(message.from_user.id)
    
    mensaje_folios = ""
    if folios_activos:
        mensaje_folios = f"\n\n📋 FOLIOS ACTIVOS: {', '.join(folios_activos)}\n(Cada folio tiene su propio timer de 36 horas)"
    
    await message.answer(
        f"🚗 NUEVO PERMISO DE GUANAJUATO\n\n"
        f"📋 Costo: ${PRECIO_PERMISO}\n"
        f"⏰ Tiempo para pagar: 36 horas\n"
        f"📱 Concepto de pago: Su folio asignado\n"
        + mensaje_folios + "\n\n"
        "Primer dato: MARCA del vehículo"
    )
    await state.set_state(PermisoForm.marca)

@dp.message(PermisoForm.marca)
async def get_marca(message: types.Message, state: FSMContext):
    marca = message.text.strip().upper()
    await state.update_data(marca=marca)
    await message.answer("LÍNEA/MODELO del vehículo:")
    await state.set_state(PermisoForm.linea)

@dp.message(PermisoForm.linea)
async def get_linea(message: types.Message, state: FSMContext):
    linea = message.text.strip().upper()
    await state.update_data(linea=linea)
    await message.answer("AÑO del vehículo (4 dígitos):")
    await state.set_state(PermisoForm.anio)

@dp.message(PermisoForm.anio)
async def get_anio(message: types.Message, state: FSMContext):
    anio = message.text.strip()
    if not anio.isdigit() or len(anio) != 4:
        await message.answer("⚠️ Año inválido. Use 4 dígitos (ej: 2020):")
        return
    
    await state.update_data(anio=anio)
    await message.answer("NÚMERO DE SERIE:")
    await state.set_state(PermisoForm.serie)

@dp.message(PermisoForm.serie)
async def get_serie(message: types.Message, state: FSMContext):
    serie = message.text.strip().upper()
    await state.update_data(serie=serie)
    await message.answer("NÚMERO DE MOTOR:")
    await state.set_state(PermisoForm.motor)

@dp.message(PermisoForm.motor)
async def get_motor(message: types.Message, state: FSMContext):
    motor = message.text.strip().upper()
    await state.update_data(motor=motor)
    await message.answer("COLOR del vehículo:")
    await state.set_state(PermisoForm.color)

@dp.message(PermisoForm.color)
async def get_color(message: types.Message, state: FSMContext):
    color = message.text.strip().upper()
    await state.update_data(color=color)
    await message.answer("NOMBRE COMPLETO del propietario:")
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
        ok, folio = await guardar_folio_con_reintento(datos, message.from_user.id, message.from_user.username)
        
        if not ok:
            await message.answer(
                "❌ Error generando folio. Intente nuevamente con /chuleta"
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

        pdf_completo = generar_pdf_guanajuato_unificado(folio, datos, hoy, fecha_ven)

        # BOTONES INLINE
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🔑 Validar Admin", callback_data=f"validar_{folio}"),
                InlineKeyboardButton(text="⏹️ Detener Timer", callback_data=f"detener_{folio}")
            ]
        ])

        await message.answer_document(
            FSInputFile(pdf_completo),
            caption=f"📋 PERMISO COMPLETO GUANAJUATO\n"
                   f"Folio: {folio}\n"
                   f"Vigencia: {fecha_ven.strftime('%d/%m/%Y')}\n"
                   f"📄 2 páginas + QR dinámico de verificación\n\n"
                   f"⏰ TIMER ACTIVO (36 horas)",
            reply_markup=keyboard
        )

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

        await iniciar_timer_pago(message.from_user.id, folio)

        await message.answer(
            f"💰 INSTRUCCIONES DE PAGO\n\n"
            f"📄 Folio: {folio}\n"
            f"💵 Cantidad: ${PRECIO_PERMISO}\n"
            f"⏰ Tiempo límite: 36 horas\n\n"
            
            "🏦 TRANSFERENCIA:\n"
            "• Banco: [TU BANCO]\n"
            "• Cuenta: [TU CUENTA]\n"
            "• CLABE: [TU CLABE]\n"
            f"• Concepto: Permiso {folio}\n\n"
            
            "🏪 OXXO:\n"
            "• Referencia: [TU REFERENCIA]\n"
            f"• Cantidad: ${PRECIO_PERMISO}\n\n"
            
            f"📸 Envía foto del comprobante para validar\n"
            f"⚠️ Si no pagas en 36 horas, el folio {folio} será eliminado\n\n"
            f"📋 Para generar otro permiso use /chuleta"
        )
        
    except Exception as e:
        await message.answer(f"❌ ERROR: {str(e)}\n\n📋 Para generar otro permiso use /chuleta")
        print(f"[ERROR] get_nombre: {e}")
    finally:
        await state.clear()

# ------------ CALLBACK HANDLERS (BOTONES) ------------
@dp.callback_query(lambda c: c.data and c.data.startswith("validar_"))
async def callback_validar_admin(callback: CallbackQuery):
    folio = callback.data.replace("validar_", "")
    
    if not folio.startswith("192"):
        await callback.answer("❌ Folio inválido", show_alert=True)
        return
    
    if folio in timers_activos:
        user_con_folio = timers_activos[folio]["user_id"]
        cancelar_timer_folio(folio)
        
        try:
            supabase.table("folios_registrados").update({
                "estado": "VALIDADO_ADMIN",
                "fecha_comprobante": datetime.now().isoformat()
            }).eq("folio", folio).execute()
            supabase.table("borradores_registrados").update({
                "estado": "VALIDADO_ADMIN",
                "fecha_comprobante": datetime.now().isoformat()
            }).eq("folio", folio).execute()
        except Exception as e:
            print(f"Error actualizando BD para folio {folio}: {e}")
        
        await callback.answer("✅ Folio validado por administración", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=None)
        
        try:
            await bot.send_message(
                user_con_folio,
                f"✅ PAGO VALIDADO POR ADMINISTRACIÓN - GUANAJUATO\n"
                f"Folio: {folio}\n"
                f"Tu permiso está activo para circular.\n\n"
                f"📋 Para generar otro permiso use /chuleta"
            )
        except Exception as e:
            print(f"Error notificando al usuario {user_con_folio}: {e}")
    else:
        await callback.answer("❌ Folio no encontrado en timers activos", show_alert=True)

@dp.callback_query(lambda c: c.data and c.data.startswith("detener_"))
async def callback_detener_timer(callback: CallbackQuery):
    folio = callback.data.replace("detener_", "")
    
    if folio in timers_activos:
        cancelar_timer_folio(folio)
        
        try:
            supabase.table("folios_registrados").update({
                "estado": "TIMER_DETENIDO",
                "fecha_detencion": datetime.now().isoformat()
            }).eq("folio", folio).execute()
        except Exception as e:
            print(f"Error actualizando BD para folio {folio}: {e}")
        
        await callback.answer("⏹️ Timer detenido exitosamente", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(
            f"⏹️ TIMER DETENIDO\n\n"
            f"Folio: {folio}\n"
            f"El timer de eliminación automática ha sido detenido.\n\n"
            f"📋 Para generar otro permiso use /chuleta"
        )
    else:
        await callback.answer("❌ Timer ya no está activo", show_alert=True)

# ------------ CÓDIGO ADMIN SERO ------------
@dp.message(lambda message: message.text and message.text.strip().upper().startswith("SERO"))
async def admin_detener_timer(message: types.Message):
    texto = message.text.strip().upper()
    
    if len(texto) > 4:
        folio = texto[4:]
        
        if not folio.startswith("192"):
            await message.answer(
                f"⚠️ FOLIO INVÁLIDO\n\n"
                f"El folio {folio} no es un folio GUANAJUATO válido.\n"
                f"Los folios de GUANAJUATO deben comenzar con 192.\n\n"
                f"Ejemplo correcto: SERO1921\n\n"
                f"📋 Para generar otro permiso use /chuleta"
            )
            return
        
        if folio in timers_activos:
            user_id = timers_activos[folio]["user_id"]
            cancelar_timer_folio(folio)
            
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
                f"✅ VALIDACIÓN ADMINISTRATIVA OK\n"
                f"Folio: {folio}\n"
                f"Timer cancelado y estado actualizado.\n"
                f"Usuario ID: {user_id}\n"
                f"Timers restantes: {len(timers_activos)}\n\n"
                f"📋 Para generar otro permiso use /chuleta"
            )
            
            try:
                await bot.send_message(
                    user_id,
                    f"✅ PAGO VALIDADO POR ADMINISTRACIÓN - GUANAJUATO\n\n"
                    f"Folio: {folio}\n"
                    f"Tu permiso está activo para circular.\n\n"
                    f"📋 Para generar otro permiso use /chuleta"
                )
            except Exception as e:
                print(f"[ERROR] Notificando usuario: {e}")
        else:
            await message.answer(
                f"❌ FOLIO NO LOCALIZADO EN TIMERS ACTIVOS\n"
                f"Folio consultado: {folio}\n"
                f"Timers activos: {len(timers_activos)}\n\n"
                f"📋 Para generar otro permiso use /chuleta"
            )
    else:
        await message.answer(
            f"📋 TIMERS ACTIVOS: {len(timers_activos)}\n\n"
            f"Para detener:\n"
            f"SERO[FOLIO_COMPLETO]\n\n"
            f"Ejemplo: SERO1921\n\n"
            f"📋 Para generar otro permiso use /chuleta"
        )

@dp.message(lambda message: message.content_type == ContentType.PHOTO)
async def recibir_comprobante(message: types.Message):
    user_id = message.from_user.id
    folios_usuario = obtener_folios_usuario(user_id)
    
    if not folios_usuario:
        await message.answer(
            "ℹ️ No tienes permisos pendientes de pago.\n\n"
            "📋 Para generar otro permiso use /chuleta"
        )
        return
    
    if len(folios_usuario) > 1:
        lista_folios = '\n'.join([f"• {folio}" for folio in folios_usuario])
        await message.answer(
            f"📄 MÚLTIPLES FOLIOS ACTIVOS\n\n"
            f"Tienes {len(folios_usuario)} folios pendientes:\n{lista_folios}\n\n"
            f"Responde con el NÚMERO DE FOLIO para este comprobante.\n\n"
            f"📋 Para generar otro permiso use /chuleta"
        )
        return
    
    folio = folios_usuario[0]
    
    cancelar_timer_folio(folio)
    
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
        f"✅ COMPROBANTE RECIBIDO CORRECTAMENTE\n\n"
        f"📄 Folio: {folio}\n"
        f"⏱️ Timer detenido\n"
        f"🔍 Verificando pago...\n\n"
        f"Su comprobante está siendo validado.\n"
        f"Gracias por usar el sistema de Guanajuato.\n\n"
        f"📋 Para generar otro permiso use /chuleta"
    )

@dp.message(Command("folios"))
async def ver_folios_activos(message: types.Message):
    user_id = message.from_user.id
    folios_usuario = obtener_folios_usuario(user_id)
    
    if not folios_usuario:
        await message.answer(
            "ℹ️ NO HAY FOLIOS ACTIVOS\n\n"
            "No tienes folios pendientes de pago.\n\n"
            "📋 Para generar otro permiso use /chuleta"
        )
        return
    
    lista_folios = []
    for folio in folios_usuario:
        if folio in timers_activos:
            tiempo_restante = 2160 - int((datetime.now() - timers_activos[folio]["start_time"]).total_seconds() / 60)
            tiempo_restante = max(0, tiempo_restante)
            horas = tiempo_restante // 60
            minutos = tiempo_restante % 60
            lista_folios.append(f"• {folio} ({horas}h {minutos}min restantes)")
        else:
            lista_folios.append(f"• {folio} (sin timer)")
    
    await message.answer(
        f"📋 FOLIOS GUANAJUATO ACTIVOS ({len(folios_usuario)})\n\n"
        + '\n'.join(lista_folios) +
        f"\n\n⏰ Cada folio tiene timer de 36 horas.\n"
        f"📸 Para enviar comprobante, use imagen.\n\n"
        f"📋 Para generar otro permiso use /chuleta"
    )

@dp.message(lambda message: message.text and any(palabra in message.text.lower() for palabra in [
    'costo', 'precio', 'cuanto', 'cuánto', 'deposito', 'depósito', 'pago', 'valor', 'monto'
]))
async def responder_costo(message: types.Message):
    await message.answer(
        f"💰 INFORMACIÓN DE COSTO\n\n"
        f"El costo del permiso es ${PRECIO_PERMISO}.\n\n"
        "📋 Para generar otro permiso use /chuleta"
    )

@dp.message()
async def fallback(message: types.Message):
    await message.answer("🏛️ Sistema Guanajuato.")

# ------------ FASTAPI ------------
_keep_task = None

async def keep_alive():
    while True:
        await asyncio.sleep(600)
        print("[HEARTBEAT] Bot Guanajuato activo")

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _keep_task
    
    await inicializar_sistema_folios_192()
    
    await bot.delete_webhook(drop_pending_updates=True)
    if BASE_URL:
        await bot.set_webhook(f"{BASE_URL}/webhook", allowed_updates=["message", "callback_query"])
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
        "version": "5.0 - Botones Inline + /chuleta selectivo",
        "sistema_folios": "192X (prefijo fijo 192)",
        "ultimo_consecutivo": _ultimo_consecutivo,
        "proximo_folio": f"192{_ultimo_consecutivo + 1}",
        "timers_activos": len(timers_activos),
        "comando_secreto": "/chuleta (selectivo)",
        "caracteristicas": [
            "Botones inline para validar/detener",
            "Sin restricciones en campos (solo año 4 dígitos)",
            "/chuleta SOLO al final y en respuestas específicas",
            "Formulario limpio sin /chuleta",
            "Folios únicos 192X (1921, 1922, 1923...)",
            "QR dinámico de verificación",
            "QR de texto con datos",
            "Timer 36h con avisos 90/60/30/10",
            "Comando admin: SERO[folio]"
        ]
    }

@app.get("/status")
async def status():
    return {
        "sistema": "Guanajuato v5.0 - /chuleta selectivo",
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

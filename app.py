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
from aiogram.types import FSInputFile
from contextlib import asynccontextmanager, suppress
import asyncio
import qrcode
from io import BytesIO
import random

# ------------ CONFIG ------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "https://xsagwqepoljfsogusubw.supabase.co")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InhzYWd3cWVwb2xqZnNvZ3VzdWJ3Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NDM5NjM3NTUsImV4cCI6MjA1OTUzOTc1NX0.NUixULn0m2o49At8j6X58UqbXre2O2_JStqzls_8Gws")
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
OUTPUT_DIR = "documentos"
PLANTILLA_GUANAJUATO_PRIMERA = "guanajuato_imagen_fullhd.pdf"
PLANTILLA_GUANAJUATO_SEGUNDA = "guanajuato.pdf"

os.makedirs(OUTPUT_DIR, exist_ok=True)

# ------------ SUPABASE ------------
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ------------ BOT ------------
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ------------ FOLIO GUANAJUATO ------------
folio_counter = {"count": 659}
def nuevo_folio() -> str:
    folio = f"{folio_counter['count']}"
    folio_counter["count"] += 1
    return folio

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
# Coordenadas para la PRIMERA plantilla (permiso guanajuato.pdf)
coords_gto_primera = {
    "folio": (1800,455,60,(1,0,0)),
    "fecha": (2200,580,35,(0,0,0)),
    "marca": (385,715,35,(0,0,0)),
    "serie": (350,800,35,(0,0,0)),
    "linea": (800,715,35,(0,0,0)),
    "motor": (1290,800,35,(0,0,0)),
    "anio": (1500,715,35,(0,0,0)),
    "color": (1960,715,35,(0,0,0)),
    "nombre": (950,1100,50,(0,0,0)),
    "vigencia": (2200,645,35,(0,0,0)),
}

# Coordenadas para la SEGUNDA plantilla (guanajuato.pdf)
coords_gto_segunda = {
    "numero_serie": (255.0, 180.0, 10, (0,0,0)),
    "fecha": (255.0, 396.0, 10, (0,0,0)),
}

# ------------ GENERACIÓN PDF GUANAJUATO ------------
def generar_pdf_guanajuato_completo(folio, datos, fecha_exp, fecha_ven):
    """
    Genera AMBAS plantillas de Guanajuato en un solo PDF multi-página
    """
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    
    # === PRIMERA PLANTILLA (permiso guanajuato.pdf) ===
    doc_primera = fitz.open(PLANTILLA_GUANAJUATO_PRIMERA)
    pg1 = doc_primera[0]
    
    # Insertar datos en primera plantilla
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

    # --- Generar QR para primera plantilla ---
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
    buf = BytesIO()
    img_qr.save(buf, format="PNG")
    buf.seek(0)
    qr_pix = fitz.Pixmap(buf.read())

    # Insertar QR en primera plantilla
    cm = 85.05
    ancho_qr = alto_qr = cm * 3.0
    page_width = pg1.rect.width
    x_qr = page_width - (2.5 * cm) - ancho_qr
    y_qr = 20.5 * cm

    pg1.insert_image(
        fitz.Rect(x_qr, y_qr, x_qr + ancho_qr, y_qr + alto_qr),
        pixmap=qr_pix,
        overlay=True
    )
    
    # === SEGUNDA PLANTILLA (guanajuato.pdf) ===
    doc_segunda = fitz.open(PLANTILLA_GUANAJUATO_SEGUNDA)
    pg2 = doc_segunda[0]
    
    # Insertar datos en segunda plantilla
    pg2.insert_text(coords_gto_segunda["numero_serie"][:2], 
                    datos.get("serie", ""), 
                    fontsize=coords_gto_segunda["numero_serie"][2], 
                    color=coords_gto_segunda["numero_serie"][3])
    
    pg2.insert_text(coords_gto_segunda["fecha"][:2], 
                    f_exp, 
                    fontsize=coords_gto_segunda["fecha"][2], 
                    color=coords_gto_segunda["fecha"][3])
    
    # === COMBINAR AMBAS PLANTILLAS EN UN SOLO PDF ===
    # Crear documento final
    doc_final = fitz.open()
    
    # Insertar primera página (plantilla principal)
    doc_final.insert_pdf(doc_primera)
    
    # Insertar segunda página (plantilla secundaria)
    doc_final.insert_pdf(doc_segunda)
    
    # Guardar el PDF combinado
    salida = os.path.join(OUTPUT_DIR, f"{folio}_guanajuato_completo.pdf")
    doc_final.save(salida)
    
    # Cerrar todos los documentos
    doc_primera.close()
    doc_segunda.close()
    doc_final.close()
    
    return salida

# ------------ HANDLERS GUANAJUATO ------------
@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "🏛️ ¡Órale! Sistema Digital de Permisos GUANAJUATO.\n"
        "El estado más chingón para tramitar tus permisos, compadre.\n\n"
        "🚗 Usa /permiso para tramitar tu documento oficial de Guanajuato."
    )

@dp.message(Command("permiso"))
async def permiso_cmd(message: types.Message, state: FSMContext):
    await message.answer(
        "🚗 Vamos a generar tu permiso de GUANAJUATO.\n"
        "Primero escribe la MARCA del vehículo:"
    )
    await state.set_state(PermisoForm.marca)

@dp.message(PermisoForm.marca)
async def get_marca(message: types.Message, state: FSMContext):
    marca = message.text.strip().upper()
    await state.update_data(marca=marca)
    await message.answer(
        f"✅ MARCA: {marca} - Registrado.\n\n"
        "Ahora la LÍNEA del vehículo:"
    )
    await state.set_state(PermisoForm.linea)

@dp.message(PermisoForm.linea)
async def get_linea(message: types.Message, state: FSMContext):
    linea = message.text.strip().upper()
    await state.update_data(linea=linea)
    await message.answer(
        f"✅ LÍNEA: {linea} - Anotado.\n\n"
        "El AÑO del vehículo (4 dígitos):"
    )
    await state.set_state(PermisoForm.anio)

@dp.message(PermisoForm.anio)
async def get_anio(message: types.Message, state: FSMContext):
    anio = message.text.strip()
    if not anio.isdigit() or len(anio) != 4:
        await message.answer(
            "⚠️ El año debe ser de 4 dígitos (ej: 2020).\n"
            "Inténtelo de nuevo:"
        )
        return
    
    await state.update_data(anio=anio)
    await message.answer(
        f"✅ AÑO: {anio} - Confirmado.\n\n"
        "NÚMERO DE SERIE del vehículo:"
    )
    await state.set_state(PermisoForm.serie)

@dp.message(PermisoForm.serie)
async def get_serie(message: types.Message, state: FSMContext):
    serie = message.text.strip().upper()
    if len(serie) < 5:
        await message.answer(
            "⚠️ El número de serie parece muy corto.\n"
            "Revise bien y escriba el número completo:"
        )
        return
        
    await state.update_data(serie=serie)
    await message.answer(
        f"✅ SERIE: {serie} - En el sistema.\n\n"
        "NÚMERO DE MOTOR:"
    )
    await state.set_state(PermisoForm.motor)

@dp.message(PermisoForm.motor)
async def get_motor(message: types.Message, state: FSMContext):
    motor = message.text.strip().upper()
    await state.update_data(motor=motor)
    await message.answer(
        f"✅ MOTOR: {motor} - Capturado.\n\n"
        "COLOR del vehículo:"
    )
    await state.set_state(PermisoForm.color)

@dp.message(PermisoForm.color)
async def get_color(message: types.Message, state: FSMContext):
    color = message.text.strip().upper()
    await state.update_data(color=color)
    await message.answer(
        f"✅ COLOR: {color} - Registrado.\n\n"
        "Por último, el NOMBRE COMPLETO del solicitante:"
    )
    await state.set_state(PermisoForm.nombre)

@dp.message(PermisoForm.nombre)
async def get_nombre(message: types.Message, state: FSMContext):
    datos = await state.get_data()
    nombre = message.text.strip().upper()
    datos["nombre"] = nombre
    datos["folio"] = nuevo_folio()

    # -------- FECHAS --------
    hoy = datetime.now()
    fecha_ven = hoy + timedelta(days=30)
    # -------------------------

    await message.answer(
        f"🔄 PROCESANDO PERMISO DE GUANAJUATO...\n"
        f"Folio: {datos['folio']}\n"
        f"Titular: {nombre}\n\n"
        "Generando ambas plantillas oficiales..."
    )

    try:
        # Generar PDF con ambas plantillas
        pdf_path = generar_pdf_guanajuato_completo(datos['folio'], datos, hoy, fecha_ven)

        await message.answer_document(
            FSInputFile(pdf_path),
            caption=f"📋 PERMISO OFICIAL GUANAJUATO\n"
                   f"Folio: {datos['folio']}\n"
                   f"Vigencia: 30 días\n"
                   f"🏛️ Documento con ambas plantillas incluidas"
        )

        # Guardar en base de datos
        supabase.table("folios_registrados").insert({
            "folio": datos["folio"],
            "marca": datos["marca"],
            "linea": datos["linea"],
            "anio": datos["anio"],
            "numero_serie": datos["serie"],
            "numero_motor": datos["motor"],
            "nombre": datos["nombre"],
            "color": datos["color"],
            "fecha_expedicion": hoy.date().isoformat(),
            "fecha_vencimiento": fecha_ven.date().isoformat(),
            "entidad": "guanajuato",
        }).execute()

        # También en la tabla borradores (compatibilidad)
        supabase.table("borradores_registros").insert({
            "folio": datos["folio"],
            "entidad": "Guanajuato",
            "numero_serie": datos["serie"],
            "marca": datos["marca"],
            "linea": datos["linea"],
            "numero_motor": datos["motor"],
            "anio": datos["anio"],
            "color": datos["color"],
            "fecha_expedicion": hoy.isoformat(),
            "fecha_vencimiento": fecha_ven.isoformat(),
            "contribuyente": datos["nombre"]
        }).execute()

        await message.answer(
            f"🎯 PERMISO DE GUANAJUATO GENERADO EXITOSAMENTE\n\n"
            f"📄 Folio: {datos['folio']}\n"
            f"🚗 Vehículo: {datos['marca']} {datos['linea']} {datos['anio']}\n"
            f"📅 Vigencia: 30 días\n"
            f"✅ Estado: ACTIVO\n\n"
            "Su documento incluye:\n"
            "• Página 1: Permiso principal con QR\n"
            "• Página 2: Documento de verificación\n\n"
            "Para otro trámite, use /permiso nuevamente."
        )
        
    except Exception as e:
        await message.answer(
            f"💥 ERROR EN EL SISTEMA DE GUANAJUATO\n\n"
            f"Fallo: {str(e)}\n\n"
            "Intente nuevamente con /permiso\n"
            "Si persiste, contacte al administrador."
        )
    finally:
        await state.clear()

@dp.message()
async def fallback(message: types.Message):
    respuestas_random = [
        "🏛️ No entiendo, compadre. Use /permiso para tramitar en Guanajuato.",
        "🚗 Para permisos de Guanajuato use: /permiso",
        "🎯 Directo al grano: /permiso para iniciar su trámite guanajuatense.",
        "🔥 Sistema de Guanajuato: /permiso es lo que necesita.",
    ]
    await message.answer(random.choice(respuestas_random))

# ------------ FASTAPI + LIFESPAN ------------
_keep_task = None

async def keep_alive():
    while True:
        await asyncio.sleep(600)

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _keep_task
    await bot.delete_webhook(drop_pending_updates=True)
    if BASE_URL:
        await bot.set_webhook(f"{BASE_URL}/webhook", allowed_updates=["message"])
        _keep_task = asyncio.create_task(keep_alive())
    yield
    if _keep_task:
        _keep_task.cancel()
        with suppress(asyncio.CancelledError):
            await _keep_task
    await bot.session.close()

app = FastAPI(lifespan=lifespan)

@app.post("/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = types.Update(**data)
    await dp.feed_webhook_update(bot, update)
    return {"ok": True}

@app.get("/")
async def root():
    return {"message": "Bot Guanajuato funcionando correctamente"}

if __name__ == '__main__':
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)

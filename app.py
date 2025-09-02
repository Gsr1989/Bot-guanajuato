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

# ------------ CONFIG ------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "https://xsagwqepoljfsogusubw.supabase.co")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InhzYWd3cWVwb2xqZnNvZ3VzdWJ3Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NDM5NjM3NTUsImV4cCI6MjA1OTUzOTc1NX0.NUixULn0m2o49At8j6X58UqbXre2O2_JStqzls_8Gws")
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
OUTPUT_DIR = "documentos"
PLANTILLA_GUANAJUATO_PRIMERA = "guanajuato_imagen_fullhd.pdf"
PLANTILLA_GUANAJUATO_SEGUNDA = "guanajuato.pdf"

# Precio del permiso
PRECIO_PERMISO = 150  # Cambia por tu precio

os.makedirs(OUTPUT_DIR, exist_ok=True)

# ------------ SUPABASE ------------
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ------------ BOT ------------
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ------------ TIMER MANAGEMENT ------------
timers_activos = {}  # {user_id: {"task": task, "folio": folio, "start_time": datetime}}

async def eliminar_folio_automatico(user_id: int, folio: str):
    """Elimina folio automáticamente después del tiempo límite"""
    try:
        # Eliminar de base de datos
        supabase.table("folios_registrados").delete().eq("folio", folio).execute()
        supabase.table("borradores_registros").delete().eq("folio", folio).execute()
        
        # Notificar al usuario
        await bot.send_message(
            user_id,
            f"❌ TIEMPO AGOTADO\n\n"
            f"El folio {folio} ha sido eliminado por falta de pago.\n"
            f"Para tramitar un nuevo permiso use /permiso"
        )
        
        # Limpiar timer
        if user_id in timers_activos:
            del timers_activos[user_id]
            
    except Exception as e:
        print(f"Error eliminando folio {folio}: {e}")

async def enviar_recordatorio(user_id: int, folio: str, minutos_restantes: int):
    """Envía recordatorios de pago"""
    try:
        await bot.send_message(
            user_id,
            f"⏰ RECORDATORIO DE PAGO\n\n"
            f"Folio: {folio}\n"
            f"⏱️ Te quedan {minutos_restantes} minutos para pagar\n"
            f"💰 Precio: ${PRECIO_PERMISO}\n\n"
            f"Envía tu comprobante de pago (imagen) para validar."
        )
    except Exception as e:
        print(f"Error enviando recordatorio a {user_id}: {e}")

async def iniciar_timer_pago(user_id: int, folio: str):
    """Inicia el timer de 2 horas con recordatorios"""
    async def timer_task():
        start_time = datetime.now()
        
        # Recordatorios cada 30 minutos
        for minutos in [30, 60, 90]:
            await asyncio.sleep(30 * 60)  # 30 minutos
            
            # Verificar si el timer sigue activo
            if user_id not in timers_activos:
                return  # Timer cancelado (usuario pagó)
                
            minutos_restantes = 120 - minutos
            await enviar_recordatorio(user_id, folio, minutos_restantes)
        
        # Último recordatorio a los 110 minutos (faltan 10)
        await asyncio.sleep(20 * 60)  # 20 minutos más
        if user_id in timers_activos:
            await enviar_recordatorio(user_id, folio, 10)
        
        # Esperar 10 minutos finales
        await asyncio.sleep(10 * 60)
        
        # Si llegamos aquí, se acabó el tiempo
        if user_id in timers_activos:
            await eliminar_folio_automatico(user_id, folio)
    
    # Crear y guardar el task
    task = asyncio.create_task(timer_task())
    timers_activos[user_id] = {
        "task": task,
        "folio": folio,
        "start_time": datetime.now()
    }

def cancelar_timer(user_id: int):
    """Cancela el timer cuando el usuario paga"""
    if user_id in timers_activos:
        timers_activos[user_id]["task"].cancel()
        del timers_activos[user_id]

# ------------ FOLIO GUANAJUATO CON PREFIJO 323 PROGRESIVO ------------
FOLIO_PREFIJO = "323"
folio_counter = {"siguiente": 2}  # Empezar en 323+2 = 3232

def nuevo_folio():
    """
    Genera nuevo folio con prefijo 323 progresivo.
    Busca el último en Supabase para evitar duplicados.
    Ej: 3232, 3234, 32349987, 32399999999999999999999
    """
    max_intentos = 50
    
    for _ in range(max_intentos):
        try:
            # Buscar el último folio con prefijo 323
            response = supabase.table("folios_registrados") \
                .select("folio") \
                .like("folio", "323%") \
                .order("id", desc=True) \
                .limit(1) \
                .execute()

            if response.data:
                ultimo_folio = response.data[0]["folio"]
                if ultimo_folio.startswith("323"):
                    # Extraer número después de 323
                    numero_parte = ultimo_folio[3:]  # Todo después de "323"
                    try:
                        ultimo_numero = int(numero_parte)
                        nuevo_numero = ultimo_numero + 1
                    except:
                        # Si no se puede convertir, usar contador interno
                        nuevo_numero = folio_counter["siguiente"]
                else:
                    nuevo_numero = folio_counter["siguiente"]
            else:
                # No hay folios, empezar con 2
                nuevo_numero = 2

            # Generar nuevo folio
            nuevo_folio_str = f"323{nuevo_numero}"
            
            # Verificar que no existe (por si las moscas)
            verificacion = supabase.table("folios_registrados") \
                .select("folio") \
                .eq("folio", nuevo_folio_str) \
                .execute()
                
            if not verificacion.data:  # No existe, perfecto
                folio_counter["siguiente"] = nuevo_numero + 1
                return nuevo_folio_str
            else:
                # Si ya existe, incrementar y seguir intentando
                folio_counter["siguiente"] = nuevo_numero + 1
                continue
                
        except Exception as e:
            print(f"[ERROR] Generando folio 323: {e}")
            # Fallback: usar timestamp
            import time
            timestamp = int(time.time())
            return f"323{timestamp}"
    
    # Si llegamos aquí, usar timestamp como último recurso
    import time
    timestamp = int(time.time())
    return f"323{timestamp}"

def inicializar_folio_desde_supabase():
    """
    Inicializa el contador basado en el último folio 323 en Supabase.
    """
    try:
        response = supabase.table("folios_registrados") \
            .select("folio") \
            .like("folio", "323%") \
            .order("id", desc=True) \
            .limit(1) \
            .execute()

        if response.data:
            ultimo_folio = response.data[0]["folio"]
            if ultimo_folio.startswith("323"):
                numero_parte = ultimo_folio[3:]
                try:
                    ultimo_numero = int(numero_parte)
                    folio_counter["siguiente"] = ultimo_numero + 1
                except:
                    folio_counter["siguiente"] = 2
            else:
                folio_counter["siguiente"] = 2
        else:
            folio_counter["siguiente"] = 2
    except Exception as e:
        print(f"[ERROR] Inicializando contador 323: {e}")
        folio_counter["siguiente"] = 2

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

# ------------ GENERACIÓN PDF GUANAJUATO SEPARADOS ------------
def generar_pdfs_guanajuato_separados(folio, datos, fecha_exp, fecha_ven):
    """
    Genera DOS archivos PDF separados para las plantillas de Guanajuato
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
    
    # Guardar PRIMERA plantilla
    salida_primera = os.path.join(OUTPUT_DIR, f"{folio}_guanajuato_principal.pdf")
    doc_primera.save(salida_primera)
    doc_primera.close()
    
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
    
    # Guardar SEGUNDA plantilla
    salida_segunda = os.path.join(OUTPUT_DIR, f"{folio}_guanajuato_secundario.pdf")
    doc_segunda.save(salida_segunda)
    doc_segunda.close()
    
    return salida_primera, salida_segunda

# ------------ HANDLERS GUANAJUATO ------------
@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "🏛️ ¡Órale! Sistema Digital de Permisos GUANAJUATO.\n"
        "El estado más chingón para tramitar tus permisos, compadre.\n\n"
        f"🚗 Usa /permiso para tramitar tu documento oficial (${PRECIO_PERMISO})\n"
        "💳 Métodos de pago: Transferencia bancaria y OXXO"
    )

@dp.message(Command("permiso"))
async def permiso_cmd(message: types.Message, state: FSMContext):
    # Cancelar timer anterior si existe
    cancelar_timer(message.from_user.id)
    
    await message.answer(
        f"🚗 Vamos a generar tu permiso de GUANAJUATO (${PRECIO_PERMISO})\n"
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
        "Generando ambas plantillas por separado..."
    )

    try:
        # Generar AMBOS PDFs por separado
        pdf_principal, pdf_secundario = generar_pdfs_guanajuato_separados(datos['folio'], datos, hoy, fecha_ven)

        # Enviar el primer archivo
        await message.answer_document(
            FSInputFile(pdf_principal),
            caption=f"📋 PERMISO PRINCIPAL GUANAJUATO\n"
                   f"Folio: {datos['folio']}\n"
                   f"Vigencia: 30 días\n"
                   f"🏛️ Documento principal con QR"
        )

        # Enviar el segundo archivo
        await message.answer_document(
            FSInputFile(pdf_secundario),
            caption=f"📋 PERMISO SECUNDARIO GUANAJUATO\n"
                   f"Folio: {datos['folio']}\n"
                   f"Vigencia: 30 días\n"
                   f"🏛️ Documento de verificación"
        )

        # Guardar en base de datos con estado PENDIENTE
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
            "estado": "PENDIENTE",  # NUEVO: Estado de pago
            "user_id": message.from_user.id,  # NUEVO: ID del usuario
            "username": message.from_user.username or "Sin username"  # NUEVO
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
            "contribuyente": datos["nombre"],
            "estado": "PENDIENTE",
            "user_id": message.from_user.id
        }).execute()

        # INICIAR TIMER DE PAGO
        await iniciar_timer_pago(message.from_user.id, datos['folio'])

        # Mensaje de instrucciones de pago
        await message.answer(
            f"💰 INSTRUCCIONES DE PAGO\n\n"
            f"📄 Folio: {datos['folio']}\n"
            f"💵 Cantidad: ${PRECIO_PERMISO}\n"
            f"⏰ Tiempo límite: 2 horas\n\n"
            
            "🏦 TRANSFERENCIA BANCARIA:\n"
            "• Banco: [TU BANCO]\n"
            "• Cuenta: [TU CUENTA]\n"
            "• CLABE: [TU CLABE]\n"
            "• Concepto: Permiso " + datos['folio'] + "\n\n"
            
            "🏪 PAGO EN OXXO:\n"
            "• Referencia: [TU REFERENCIA]\n"
            "• Cantidad exacta: $" + str(PRECIO_PERMISO) + "\n\n"
            
            f"📸 Una vez que pagues, envía la foto de tu comprobante.\n"
            f"⚠️ Si no pagas en 2 horas, tu folio {datos['folio']} será eliminado."
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

# Handler para recibir comprobantes de pago (imágenes)
@dp.message(lambda message: message.content_type == ContentType.PHOTO)
async def recibir_comprobante(message: types.Message):
    user_id = message.from_user.id
    
    # Verificar si tiene timer activo
    if user_id not in timers_activos:
        await message.answer(
            "🤔 No tienes ningún permiso pendiente de pago.\n"
            "Usa /permiso para generar uno nuevo."
        )
        return
    
    folio = timers_activos[user_id]["folio"]
    
    # Cancelar timer
    cancelar_timer(user_id)
    
    # Actualizar estado en base de datos
    supabase.table("folios_registrados").update({
        "estado": "COMPROBANTE_ENVIADO",
        "fecha_comprobante": datetime.now().isoformat()
    }).eq("folio", folio).execute()
    
    supabase.table("borradores_registros").update({
        "estado": "COMPROBANTE_ENVIADO",
        "fecha_comprobante": datetime.now().isoformat()
    }).eq("folio", folio).execute()
    
    await message.answer(
        f"✅ COMPROBANTE RECIBIDO\n\n"
        f"📄 Folio: {folio}\n"
        f"📸 Imagen guardada correctamente\n"
        f"⏱️ Timer de pago detenido\n\n"
        f"Su comprobante está siendo verificado.\n"
        f"Una vez validado el pago, su permiso quedará activo para circular.\n\n"
        f"Gracias por usar nuestro sistema de Guanajuato."
    )

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
    # Inicializar contador de folios 323 al arrancar
    inicializar_folio_desde_supabase()
    
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
    # Inicializar contador de folios 323 al arrancar
    inicializar_folio_desde_supabase()
    
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)

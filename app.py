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
ADMIN_PASSWORD = "sero"  # Palabra clave para detener timers

# Precio del permiso
PRECIO_PERMISO = 150

os.makedirs(OUTPUT_DIR, exist_ok=True)

# ------------ SUPABASE ------------
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ------------ BOT ------------
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ------------ TIMER MANAGEMENT (INDEPENDIENTES) ------------
timers_activos = {}  # {folio: {"task": task, "user_id": user_id, "start_time": datetime}}

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
        if folio in timers_activos:
            del timers_activos[folio]
            
    except Exception as e:
        print(f"Error eliminando folio {folio}: {e}")

async def enviar_recordatorio(user_id: int, folio: str, horas_restantes: int):
    """Envía recordatorios de pago"""
    try:
        await bot.send_message(
            user_id,
            f"⏰ RECORDATORIO DE PAGO\n\n"
            f"Folio: {folio}\n"
            f"⏱️ Te quedan {horas_restantes} horas para pagar\n"
            f"💰 Precio: ${PRECIO_PERMISO}\n\n"
            f"Envía tu comprobante de pago (imagen) para validar.\n"
            f"O escribe '{ADMIN_PASSWORD}{folio}' para detener el timer (solo admin)."
        )
    except Exception as e:
        print(f"Error enviando recordatorio a {user_id}: {e}")

async def iniciar_timer_pago(user_id: int, folio: str):
    """Inicia el timer de 24 horas con recordatorios - INDEPENDIENTE por folio"""
    async def timer_task():
        start_time = datetime.now()
        
        # Recordatorio a las 12 horas
        await asyncio.sleep(12 * 60 * 60)  # 12 horas
        
        # Verificar si el timer sigue activo
        if folio not in timers_activos:
            return  # Timer cancelado (usuario pagó o admin lo detuvo)
            
        await enviar_recordatorio(user_id, folio, 12)
        
        # Recordatorio a las 20 horas (faltan 4)
        await asyncio.sleep(8 * 60 * 60)  # 8 horas más
        if folio not in timers_activos:
            return
            
        await enviar_recordatorio(user_id, folio, 4)
        
        # Recordatorio a las 23 horas (falta 1)
        await asyncio.sleep(3 * 60 * 60)  # 3 horas más
        if folio not in timers_activos:
            return
            
        await enviar_recordatorio(user_id, folio, 1)
        
        # Esperar la última hora
        await asyncio.sleep(1 * 60 * 60)  # 1 hora final
        
        # Si llegamos aquí, se acabó el tiempo
        if folio in timers_activos:
            await eliminar_folio_automatico(user_id, folio)
    
    # Crear y guardar el task (INDEXADO POR FOLIO, NO POR USER_ID)
    task = asyncio.create_task(timer_task())
    timers_activos[folio] = {
        "task": task,
        "user_id": user_id,
        "start_time": datetime.now()
    }
    print(f"[TIMER] Iniciado para folio {folio} (user {user_id}). Total activos: {len(timers_activos)}")

def cancelar_timer(folio: str):
    """Cancela el timer cuando el usuario paga o admin lo detiene"""
    if folio in timers_activos:
        timers_activos[folio]["task"].cancel()
        del timers_activos[folio]
        print(f"[TIMER] Cancelado folio {folio}. Restantes activos: {len(timers_activos)}")

# ------------ FOLIO GUANAJUATO CON PREFIJO 9978 PROGRESIVO ------------
def nuevo_folio():
    """
    Genera nuevo folio empezando desde 9978 y creciendo.
    Si encuentra duplicado, intenta el siguiente hasta encontrar uno libre.
    """
    max_intentos = 1000  # Aumentado para más seguridad
    
    for intento in range(max_intentos):
        try:
            # Buscar el folio más alto que empiece con 9978
            response = supabase.table("folios_registrados") \
                .select("folio") \
                .like("folio", "9978%") \
                .order("folio", desc=True) \
                .limit(1) \
                .execute()

            if response.data:
                ultimo_folio = response.data[0]["folio"]
                try:
                    # Convertir todo el folio a número y sumar
                    ultimo_numero = int(ultimo_folio)
                    nuevo_numero = ultimo_numero + 1 + intento  # Incrementar según intentos
                except:
                    # Si falla, usar timestamp
                    import time
                    nuevo_numero = int(f"9978{int(time.time())}")
            else:
                # No hay folios, empezar con 9978
                nuevo_numero = 9978 + intento

            # El folio ES el número completo
            nuevo_folio_str = str(nuevo_numero)
            
            # Verificar que no existe
            verificacion = supabase.table("folios_registrados") \
                .select("folio") \
                .eq("folio", nuevo_folio_str) \
                .execute()
                
            if not verificacion.data:  # No existe, perfecto
                print(f"[FOLIO] Generado: {nuevo_folio_str} (intento {intento + 1})")
                return nuevo_folio_str
            else:
                # Ya existe, siguiente iteración intentará +1
                print(f"[FOLIO] {nuevo_folio_str} duplicado, intentando siguiente...")
                continue
                
        except Exception as e:
            print(f"[ERROR] Generando folio: {e}")
            # Fallback: usar timestamp con prefijo
            import time
            return f"9978{int(time.time())}"
    
    # Si llegamos aquí después de 1000 intentos, usar timestamp
    import time
    folio_fallback = f"9978{int(time.time())}"
    print(f"[FOLIO] FALLBACK después de {max_intentos} intentos: {folio_fallback}")
    return folio_fallback

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

coords_gto_segunda = {
    "numero_serie": (255.0, 180.0, 10, (0,0,0)),
    "fecha": (255.0, 396.0, 10, (0,0,0)),
}

# ------------ GENERACIÓN PDF GUANAJUATO UNIFICADO ------------
def generar_pdf_guanajuato_unificado(folio, datos, fecha_exp, fecha_ven):
    """
    Genera UN SOLO PDF con ambas plantillas unidas
    """
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    
    # Crear nuevo documento PDF vacío
    doc_final = fitz.open()
    
    # === PRIMERA PLANTILLA ===
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

    # Generar QR
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

    # Insertar QR
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
    
    # Agregar primera página al documento final
    doc_final.insert_pdf(doc_primera)
    doc_primera.close()
    
    # === SEGUNDA PLANTILLA ===
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
    
    # Agregar segunda página al documento final
    doc_final.insert_pdf(doc_segunda)
    doc_segunda.close()
    
    # Guardar documento unificado
    salida_unificada = os.path.join(OUTPUT_DIR, f"{folio}_guanajuato_completo.pdf")
    doc_final.save(salida_unificada)
    doc_final.close()
    
    return salida_unificada

# ------------ HANDLERS GUANAJUATO ------------
@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "🏛️ ¡Órale! Sistema Digital de Permisos GUANAJUATO.\n"
        "El estado más chingón para tramitar tus permisos, compadre.\n\n"
        f"🚗 Usa /permiso para tramitar tu documento oficial (${PRECIO_PERMISO})\n"
        "💳 Métodos de pago: Transferencia bancaria y OXXO\n"
        f"🔐 Admin: escribe '{ADMIN_PASSWORD}' + folio para detener timer"
    )

@dp.message(Command("permiso"))
async def permiso_cmd(message: types.Message, state: FSMContext):
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

    hoy = datetime.now()
    fecha_ven = hoy + timedelta(days=30)

    await message.answer(
        f"🔄 PROCESANDO PERMISO DE GUANAJUATO...\n"
        f"Folio: {datos['folio']}\n"
        f"Titular: {nombre}\n\n"
        "Generando documento unificado (2 páginas en 1 PDF)..."
    )

    try:
        # Generar PDF UNIFICADO
        pdf_completo = generar_pdf_guanajuato_unificado(datos['folio'], datos, hoy, fecha_ven)

        # Enviar el archivo unificado
        await message.answer_document(
            FSInputFile(pdf_completo),
            caption=f"📋 PERMISO COMPLETO GUANAJUATO\n"
                   f"Folio: {datos['folio']}\n"
                   f"Vigencia: 30 días (hasta {fecha_ven.strftime('%d/%m/%Y')})\n"
                   f"📄 Documento con 2 páginas:\n"
                   f"   • Página 1: Permiso principal con QR\n"
                   f"   • Página 2: Permiso de verificación\n"
                   f"🏛️ Sistema oficial de Guanajuato"
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
            "estado": "PENDIENTE",
            "user_id": message.from_user.id,
            "username": message.from_user.username or "Sin username"
        }).execute()

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

        # INICIAR TIMER INDEPENDIENTE (24 horas)
        await iniciar_timer_pago(message.from_user.id, datos['folio'])

        # Mensaje de instrucciones
        await message.answer(
            f"💰 INSTRUCCIONES DE PAGO\n\n"
            f"📄 Folio: {datos['folio']}\n"
            f"💵 Cantidad: ${PRECIO_PERMISO}\n"
            f"⏰ Tiempo límite: 24 horas\n\n"
            
            "🏦 TRANSFERENCIA BANCARIA:\n"
            "• Banco: [TU BANCO]\n"
            "• Cuenta: [TU CUENTA]\n"
            "• CLABE: [TU CLABE]\n"
            "• Concepto: Permiso " + datos['folio'] + "\n\n"
            
            "🏪 PAGO EN OXXO:\n"
            "• Referencia: [TU REFERENCIA]\n"
            "• Cantidad exacta: $" + str(PRECIO_PERMISO) + "\n\n"
            
            f"📸 Una vez que pagues, envía la foto de tu comprobante.\n"
            f"⚠️ Si no pagas en 24 horas, tu folio {datos['folio']} será eliminado.\n\n"
            f"🔐 ADMIN: Para detener el timer escribe: {ADMIN_PASSWORD}{datos['folio']}"
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

# Handler para comprobantes de pago (imágenes)
@dp.message(lambda message: message.content_type == ContentType.PHOTO)
async def recibir_comprobante(message: types.Message):
    user_id = message.from_user.id
    
    # Buscar si el usuario tiene algún folio pendiente
    folios_usuario = [folio for folio, data in timers_activos.items() if data["user_id"] == user_id]
    
    if not folios_usuario:
        await message.answer(
            "🤔 No tienes ningún permiso pendiente de pago.\n"
            "Usa /permiso para generar uno nuevo."
        )
        return
    
    # Si tiene varios, tomar el más reciente
    folio = folios_usuario[-1]
    
    # Cancelar timer
    cancelar_timer(folio)
    
    # Actualizar estado
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

# Handler para comando de admin (detener timer)
@dp.message(lambda message: message.text and message.text.startswith(ADMIN_PASSWORD))
async def admin_detener_timer(message: types.Message):
    texto = message.text.strip()
    
    # Extraer folio después de la palabra admin
    if len(texto) > len(ADMIN_PASSWORD):
        folio = texto[len(ADMIN_PASSWORD):]
        
        if folio in timers_activos:
            cancelar_timer(folio)
            
            # Actualizar estado a ADMIN_DETENIDO
            supabase.table("folios_registrados").update({
                "estado": "ADMIN_DETENIDO",
                "fecha_admin_stop": datetime.now().isoformat()
            }).eq("folio", folio).execute()
            
            await message.answer(
                f"🔐 ADMIN: Timer detenido\n\n"
                f"Folio: {folio}\n"
                f"⏱️ Timer cancelado por administrador\n"
                f"📊 Estado actualizado en base de datos"
            )
        else:
            await message.answer(
                f"⚠️ El folio {folio} no tiene timer activo.\n"
                f"Timers activos: {len(timers_activos)}"
            )
    else:
        await message.answer(
            f"📋 TIMERS ACTIVOS: {len(timers_activos)}\n\n"
            f"Para detener un timer específico:\n"
            f"{ADMIN_PASSWORD}[FOLIO]\n\n"
            f"Ejemplo: {ADMIN_PASSWORD}9978001"
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

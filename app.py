from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from supabase import create_client, Client
import fitz
import os
from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.filters import Command
from aiogram.types import FSInputFile, ContentType, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from contextlib import asynccontextmanager, suppress
import asyncio
import aiohttp
import qrcode
from io import BytesIO
import random
from PIL import Image

# ------------ CONFIG ------------
BOT_TOKEN    = os.getenv("BOT_TOKEN", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "https://xsagwqepoljfsogusubw.supabase.co")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InhzYWd3cWVwb2xqZnNvZ3VzdWJ3Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NDM5NjM3NTUsImV4cCI6MjA1OTUzOTc1NX0.NUixULn0m2o49At8j6X58UqbXre2O2_JStqzls_8Gws")
BASE_URL     = os.getenv("BASE_URL", "").rstrip("/")
OUTPUT_DIR   = "documentos"
PLANTILLA_GUANAJUATO_PRIMERA = "guanajuato_imagen_fullhd.pdf"
PLANTILLA_GUANAJUATO_SEGUNDA = "guanajuato.pdf"

PRECIO_PERMISO = 150
URL_VERIFICACION_BASE = "https://direcciongeneraltransporteguanajuato-gob.onrender.com"

os.makedirs(OUTPUT_DIR, exist_ok=True)

# ------------ SUPABASE ------------
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ------------ BOT con timeout 300s — evita HTTP timeout error ------------
_bot_session = AiohttpSession(timeout=aiohttp.ClientTimeout(total=300))
bot     = Bot(token=BOT_TOKEN, session=_bot_session)
storage = MemoryStorage()
dp      = Dispatcher(storage=storage)

# ------------ TIMERS ------------
timers_activos = {}
user_folios    = {}

# ============ FOLIOS 192 — WATERMARK ==========================================
# Prefijo "192" + número consecutivo.
# Watermark en tabla folio_watermark (prefijo = "GTO") — nunca retrocede.

FOLIO_NUM_PREFIJO  = "192"
FOLIO_PREFIJO_WM   = "GTO"
_folio_counter     = {"siguiente": 1}
_folio_lock        = asyncio.Lock()

def _sb_leer_watermark_gto() -> int | None:
    try:
        r = supabase.table("folio_watermark") \
            .select("ultimo_asignado").eq("prefijo", FOLIO_PREFIJO_WM).execute()
        if r.data:
            return r.data[0]["ultimo_asignado"]
        return None
    except Exception as e:
        print(f"[ERROR] leer_watermark GTO: {e}")
        return None

def _sb_guardar_watermark_gto(numero: int):
    try:
        supabase.table("folio_watermark").upsert({
            "prefijo":         FOLIO_PREFIJO_WM,
            "ultimo_asignado": numero
        }).execute()
        print(f"[WATERMARK GTO] Guardado: {FOLIO_NUM_PREFIJO}{numero}")
    except Exception as e:
        print(f"[ERROR] guardar_watermark GTO: {e}")

def inicializar_folio_desde_supabase():
    """
    Al arrancar:
    1) Lee watermark (máximo histórico real).
    2) Si no existe, busca el máximo en DB activa y crea el watermark.
    3) El contador NUNCA baja aunque se borren folios expirados.
    """
    watermark = _sb_leer_watermark_gto()
    if watermark is not None:
        _folio_counter["siguiente"] = watermark + 1
        print(f"[GTO] Desde watermark: {FOLIO_NUM_PREFIJO}{watermark} "
              f"-> siguiente: {_folio_counter['siguiente']}")
        return

    try:
        resp = supabase.table("folios_registrados") \
            .select("folio").eq("entidad", "Guanajuato") \
            .like("folio", f"{FOLIO_NUM_PREFIJO}%").execute()
        numeros = []
        for row in resp.data or []:
            f = row.get("folio", "")
            if isinstance(f, str) and f.startswith(FOLIO_NUM_PREFIJO):
                sufijo = f[len(FOLIO_NUM_PREFIJO):]
                if sufijo.isdigit():
                    numeros.append(int(sufijo))
        if numeros:
            maximo = max(numeros)
            _folio_counter["siguiente"] = maximo + 1
            _sb_guardar_watermark_gto(maximo)
            print(f"[GTO] Desde DB (primera vez): {FOLIO_NUM_PREFIJO}{maximo} "
                  f"-> siguiente: {_folio_counter['siguiente']}")
        else:
            _folio_counter["siguiente"] = 1
            print(f"[GTO] Sin folios previos, empezando desde {FOLIO_NUM_PREFIJO}1")
    except Exception as e:
        print(f"[ERROR] inicializar_folio GTO: {e}")
        _folio_counter["siguiente"] = 1

def _sb_folio_existe_gto(folio: str) -> bool:
    try:
        r = supabase.table("folios_registrados").select("folio").eq("folio", folio).execute()
        return len(r.data) > 0
    except Exception as e:
        print(f"[ERROR] verificar folio {folio}: {e}")
        return False

def _generar_folio_gto_sync() -> str:
    """Busca SIEMPRE hacia arriba. Nunca retrocede."""
    candidato = _folio_counter["siguiente"]
    for _ in range(100_000):
        folio = f"{FOLIO_NUM_PREFIJO}{candidato}"
        if not _sb_folio_existe_gto(folio):
            _folio_counter["siguiente"] = candidato + 1
            _sb_guardar_watermark_gto(candidato)
            print(f"[FOLIO GTO] Asignado: {folio} (siguiente: {_folio_counter['siguiente']})")
            return folio
        print(f"[FOLIO GTO] {folio} ocupado -> probando siguiente")
        candidato += 1
    import time
    fb = f"{FOLIO_NUM_PREFIJO}{int(time.time()) % 1_000_000}"
    print(f"[FOLIO GTO] Fallback: {fb}")
    return fb

async def generar_folio_192() -> str:
    async with _folio_lock:
        return await asyncio.to_thread(_generar_folio_gto_sync)

# ------------ TIMER HELPERS ---------------------------------------------------

async def eliminar_folio_automatico(folio: str):
    try:
        user_id = timers_activos.get(folio, {}).get("user_id")
        await asyncio.to_thread(lambda: (
            supabase.table("folios_registrados").delete().eq("folio", folio).execute(),
            supabase.table("borradores_registros").delete().eq("folio", folio).execute(),
        ))
        if user_id:
            await bot.send_message(user_id,
                f"⏰ TIEMPO AGOTADO - GUANAJUATO\n\n"
                f"El folio {folio} ha sido eliminado por no completar el pago en 36 horas.\n\n"
                f"📋 Para generar otro permiso use /banamex")
        limpiar_timer_folio(folio)
    except Exception as e:
        print(f"Error eliminando folio {folio}: {e}")

async def enviar_recordatorio(folio: str, minutos_restantes: int):
    try:
        if folio not in timers_activos: return
        user_id = timers_activos[folio]["user_id"]
        await bot.send_message(user_id,
            f"⚡ RECORDATORIO DE PAGO - GUANAJUATO\n\n"
            f"Folio: {folio}\n"
            f"Tiempo restante: {minutos_restantes} minutos\n"
            f"Monto: ${PRECIO_PERMISO}\n\n"
            f"📸 Envíe su comprobante de pago (imagen).\n\n"
            f"📋 Para generar otro permiso use /banamex")
    except Exception as e:
        print(f"Error enviando recordatorio para folio {folio}: {e}")

async def iniciar_timer_pago(user_id: int, folio: str):
    async def timer_task():
        print(f"[TIMER] Iniciado folio {folio}, usuario {user_id} (36h)")
        await asyncio.sleep(34.5 * 3600)
        if folio not in timers_activos: return
        await enviar_recordatorio(folio, 90)
        await asyncio.sleep(30 * 60)
        if folio not in timers_activos: return
        await enviar_recordatorio(folio, 60)
        await asyncio.sleep(30 * 60)
        if folio not in timers_activos: return
        await enviar_recordatorio(folio, 30)
        await asyncio.sleep(20 * 60)
        if folio not in timers_activos: return
        await enviar_recordatorio(folio, 10)
        await asyncio.sleep(10 * 60)
        if folio in timers_activos:
            print(f"[TIMER] Expirado folio {folio} - eliminando")
            await eliminar_folio_automatico(folio)

    task = asyncio.create_task(timer_task())
    timers_activos[folio] = {"task": task, "user_id": user_id, "start_time": datetime.now()}
    user_folios.setdefault(user_id, []).append(folio)
    print(f"[SISTEMA] Timer 36h iniciado folio {folio}, total: {len(timers_activos)}")

def cancelar_timer_folio(folio: str):
    if folio in timers_activos:
        timers_activos[folio]["task"].cancel()
        uid = timers_activos[folio]["user_id"]
        del timers_activos[folio]
        if uid in user_folios and folio in user_folios[uid]:
            user_folios[uid].remove(folio)
            if not user_folios[uid]: del user_folios[uid]
        print(f"[SISTEMA] Timer cancelado folio {folio}")

def limpiar_timer_folio(folio: str):
    if folio in timers_activos:
        uid = timers_activos[folio]["user_id"]
        del timers_activos[folio]
        if uid in user_folios and folio in user_folios[uid]:
            user_folios[uid].remove(folio)
            if not user_folios[uid]: del user_folios[uid]

def obtener_folios_usuario(user_id: int) -> list:
    return user_folios.get(user_id, [])

# ------------ FSM ------------
class PermisoForm(StatesGroup):
    marca  = State()
    linea  = State()
    anio   = State()
    serie  = State()
    motor  = State()
    color  = State()
    nombre = State()

# ------------ COORDENADAS ------------
coords_gto_primera = {
    "folio":   (1800, 455, 60, (1, 0, 0)),
    "fecha":   (2200, 580, 35, (0, 0, 0)),
    "marca":   ( 385, 715, 35, (0, 0, 0)),
    "serie":   ( 350, 800, 35, (0, 0, 0)),
    "linea":   ( 800, 715, 35, (0, 0, 0)),
    "motor":   (1290, 800, 35, (0, 0, 0)),
    "anio":    (1500, 715, 35, (0, 0, 0)),
    "color":   (1960, 715, 35, (0, 0, 0)),
    "nombre":  ( 950,1100, 50, (0, 0, 0)),
    "vigencia":(2200, 645, 35, (0, 0, 0)),
}

coords_gto_segunda = {
    "numero_serie": (255.0, 180.0, 10, (0, 0, 0)),
    "fecha":        (255.0, 396.0, 10, (0, 0, 0)),
}

coords_qr_dinamico = {"x": 205, "y": 328, "ancho": 290, "alto": 290}

# ------------ QR ------------
def generar_qr_dinamico(folio):
    try:
        url = f"{URL_VERIFICACION_BASE}/consulta/{folio}"
        qr  = qrcode.QRCode(version=2, error_correction=qrcode.constants.ERROR_CORRECT_M,
                             box_size=4, border=1)
        qr.add_data(url); qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        print(f"[QR DINÁMICO] {folio} -> {url}")
        return img, url
    except Exception as e:
        print(f"[ERROR QR DINÁMICO] {e}")
        return None, None

def generar_qr_texto(datos, folio):
    try:
        texto = (f"FOLIO: {folio}\nNOMBRE: {datos.get('nombre','')}\n"
                 f"MARCA: {datos.get('marca','')}\nLINEA: {datos.get('linea','')}\n"
                 f"AÑO: {datos.get('anio','')}\nSERIE: {datos.get('serie','')}\n"
                 f"MOTOR: {datos.get('motor','')}\nCOLOR: {datos.get('color','')}\n"
                 f"GUANAJUATO PERMISOS DIGITALES")
        qr = qrcode.QRCode(version=2, error_correction=qrcode.constants.ERROR_CORRECT_H,
                            box_size=10, border=2)
        qr.add_data(texto.upper()); qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        print(f"[QR TEXTO] Generado folio {folio}")
        return img
    except Exception as e:
        print(f"[ERROR QR TEXTO] {e}")
        return None

# ------------ PDF (síncrono) --------------------------------------------------
def generar_pdf_guanajuato_unificado(folio, datos, fecha_exp, fecha_ven):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    doc_final   = fitz.open()
    doc_primera = fitz.open(PLANTILLA_GUANAJUATO_PRIMERA)
    pg1         = doc_primera[0]

    f_exp = fecha_exp.strftime("%d/%m/%Y")
    f_ven = fecha_ven.strftime("%d/%m/%Y")

    pg1.insert_text(coords_gto_primera["folio"][:2],   folio, fontsize=coords_gto_primera["folio"][2],   color=coords_gto_primera["folio"][3])
    pg1.insert_text(coords_gto_primera["fecha"][:2],   f_exp, fontsize=coords_gto_primera["fecha"][2],   color=coords_gto_primera["fecha"][3])
    pg1.insert_text(coords_gto_primera["vigencia"][:2],f_ven, fontsize=coords_gto_primera["vigencia"][2],color=coords_gto_primera["vigencia"][3])

    for key in ["marca","serie","linea","motor","anio","color"]:
        if key in datos:
            x, y, s, col = coords_gto_primera[key]
            pg1.insert_text((x, y), datos[key], fontsize=s, color=col)

    pg1.insert_text(coords_gto_primera["nombre"][:2], datos.get("nombre",""),
                    fontsize=coords_gto_primera["nombre"][2], color=coords_gto_primera["nombre"][3])

    img_qr_texto = generar_qr_texto(datos, folio)
    if img_qr_texto:
        buf = BytesIO(); img_qr_texto.save(buf, format="PNG"); buf.seek(0)
        cm = 85.05; ancho_qr = alto_qr = cm * 3.0
        page_width = pg1.rect.width
        x_qr = page_width - (2.5 * cm) - ancho_qr; y_qr = 20.5 * cm
        pg1.insert_image(fitz.Rect(x_qr, y_qr, x_qr+ancho_qr, y_qr+alto_qr),
                         pixmap=fitz.Pixmap(buf.read()), overlay=True)

    img_qr_din, url_v = generar_qr_dinamico(folio)
    if img_qr_din:
        buf2 = BytesIO(); img_qr_din.save(buf2, format="PNG"); buf2.seek(0)
        x, y = coords_qr_dinamico["x"], coords_qr_dinamico["y"]
        w, h = coords_qr_dinamico["ancho"], coords_qr_dinamico["alto"]
        pg1.insert_image(fitz.Rect(x, y, x+w, y+h),
                         pixmap=fitz.Pixmap(buf2.read()), overlay=True)
        print(f"[QR DINÁMICO] Insertado -> {url_v}")

    doc_final.insert_pdf(doc_primera); doc_primera.close()

    doc_segunda = fitz.open(PLANTILLA_GUANAJUATO_SEGUNDA)
    pg2         = doc_segunda[0]
    pg2.insert_text(coords_gto_segunda["numero_serie"][:2], datos.get("serie",""),
                    fontsize=coords_gto_segunda["numero_serie"][2],
                    color=coords_gto_segunda["numero_serie"][3])
    pg2.insert_text(coords_gto_segunda["fecha"][:2], f_exp,
                    fontsize=coords_gto_segunda["fecha"][2],
                    color=coords_gto_segunda["fecha"][3])
    doc_final.insert_pdf(doc_segunda); doc_segunda.close()

    salida = os.path.join(OUTPUT_DIR, f"{folio}_guanajuato_completo.pdf")
    doc_final.save(salida); doc_final.close()
    print(f"[PDF] Generado: {salida}")
    return salida

# ============ BACKGROUND TASK =================================================
async def _generar_y_enviar_background(chat_id: int, datos: dict,
                                        user_id: int, username: str,
                                        folio: str, hoy: datetime, fecha_ven: datetime):
    """
    PDF, insert y envío en background.
    El webhook ya respondió — Telegram no manda duplicados.
    """
    folio_final = folio
    try:
        pdf_path = await asyncio.to_thread(
            generar_pdf_guanajuato_unificado, folio_final, datos, hoy, fecha_ven
        )

        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔑 Validar Admin", callback_data=f"validar_{folio_final}"),
            InlineKeyboardButton(text="⏹️ Detener Timer", callback_data=f"detener_{folio_final}")
        ]])

        await bot.send_document(
            chat_id,
            FSInputFile(pdf_path),
            caption=(
                f"📋 PERMISO COMPLETO GUANAJUATO\n"
                f"Folio: {folio_final}\n"
                f"Vigencia: {fecha_ven.strftime('%d/%m/%Y')}\n"
                f"📄 2 páginas + QR dinámico de verificación\n\n"
                f"⏰ TIMER ACTIVO (36 horas)"
            ),
            reply_markup=keyboard
        )

        def _insert_folios(folio_usar: str):
            supabase.table("folios_registrados").insert({
                "folio":             folio_usar,
                "marca":             datos["marca"],
                "linea":             datos["linea"],
                "anio":              datos["anio"],
                "numero_serie":      datos["serie"],
                "numero_motor":      datos["motor"],
                "color":             datos["color"],
                "nombre":            datos["nombre"],
                "fecha_expedicion":  hoy.date().isoformat(),
                "fecha_vencimiento": fecha_ven.date().isoformat(),
                "entidad":           "Guanajuato",
                "estado":            "PENDIENTE",
                "user_id":           user_id,
                "username":          username or "Sin username"
            }).execute()

        for _ in range(20):
            try:
                await asyncio.to_thread(_insert_folios, folio_final)
                print(f"[DB] Insertado folio {folio_final}")
                break
            except Exception as e:
                em = str(e).lower()
                if any(k in em for k in ("duplicate","unique","23505")):
                    print(f"[DB] Folio {folio_final} duplicado — obteniendo nuevo...")
                    folio_final = await generar_folio_192()
                else:
                    print(f"[DB ERROR] {e}"); break

        try:
            await asyncio.to_thread(lambda: supabase.table("borradores_registros").insert({
                "folio":             folio_final,
                "entidad":           "Guanajuato",
                "numero_serie":      datos["serie"],
                "marca":             datos["marca"],
                "linea":             datos["linea"],
                "numero_motor":      datos["motor"],
                "anio":              datos["anio"],
                "color":             datos["color"],
                "fecha_expedicion":  hoy.isoformat(),
                "fecha_vencimiento": fecha_ven.isoformat(),
                "contribuyente":     datos["nombre"],
                "estado":            "PENDIENTE",
                "user_id":           user_id
            }).execute())
        except Exception as e:
            print(f"[WARN] Error guardando borrador: {e}")

        await iniciar_timer_pago(user_id, folio_final)

        await bot.send_message(user_id,
            f"💰 INSTRUCCIONES DE PAGO\n\n"
            f"📄 Folio: {folio_final}\n"
            f"💵 Cantidad: ${PRECIO_PERMISO}\n"
            f"⏰ Tiempo límite: 36 horas\n\n"
            "🏦 TRANSFERENCIA:\n"
            "• Banco: [TU BANCO]\n"
            "• Cuenta: [TU CUENTA]\n"
            "• CLABE: [TU CLABE]\n"
            f"• Concepto: Permiso {folio_final}\n\n"
            "🏪 OXXO:\n"
            "• Referencia: [TU REFERENCIA]\n"
            f"• Cantidad: ${PRECIO_PERMISO}\n\n"
            f"📸 Envía foto del comprobante para validar.\n"
            f"⚠️ Sin pago en 36h el folio se elimina.\n\n"
            f"📋 Para generar otro permiso use /banamex")

    except Exception as e:
        print(f"[ERROR] background folio {folio_final}: {e}")
        try:
            await bot.send_message(user_id,
                f"❌ Error al generar el documento: {e}\n\nUse /banamex para reintentar.")
        except Exception:
            pass

# ------------ HANDLERS --------------------------------------------------------

@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "🏛️ SISTEMA DIGITAL DE PERMISOS - GUANAJUATO\n\n"
        f"🚗 Permiso de circulación: ${PRECIO_PERMISO}\n"
        "⏰ Tiempo límite de pago: 36 horas\n"
        "💳 Métodos: Transferencia y OXXO\n\n"
        "⚠️ Su folio será eliminado automáticamente si no realiza el pago dentro del tiempo límite"
    )

@dp.message(Command("banamex"))
async def banamex_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    folios_activos = obtener_folios_usuario(message.from_user.id)

    if folios_activos:
        texto   = "📋 FOLIOS ACTIVOS CON TIMER\n" + "─" * 28 + "\n\n"
        botones = []
        for f in folios_activos:
            if f in timers_activos:
                seg  = max(0, int(36*3600 -
                    (datetime.now()-timers_activos[f]["start_time"]).total_seconds()))
                h, m = divmod(seg // 60, 60)
                texto += f"Folio: {f}\n{h}h {m}min restantes\n\n"
            else:
                texto += f"Folio: {f}\n(sin timer)\n\n"
            botones.append([InlineKeyboardButton(
                text=f"⏹️ Detener timer {f}", callback_data=f"detener_{f}")])
        await message.answer(texto.strip(),
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=botones))
        await message.answer(
            f"Para NUEVO permiso escribe la MARCA del vehículo:\n\nCosto: ${PRECIO_PERMISO} | Plazo: 36h")
    else:
        await message.answer(
            f"🚗 NUEVO PERMISO DE GUANAJUATO\n\n"
            f"📋 Costo: ${PRECIO_PERMISO}\n"
            f"⏰ Tiempo para pagar: 36 horas\n\n"
            "Primer dato: MARCA del vehículo")
    await state.set_state(PermisoForm.marca)

@dp.message(PermisoForm.marca)
async def get_marca(message: types.Message, state: FSMContext):
    await state.update_data(marca=message.text.strip().upper())
    await message.answer("LÍNEA/MODELO del vehículo:")
    await state.set_state(PermisoForm.linea)

@dp.message(PermisoForm.linea)
async def get_linea(message: types.Message, state: FSMContext):
    await state.update_data(linea=message.text.strip().upper())
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
    await state.update_data(serie=message.text.strip().upper())
    await message.answer("NÚMERO DE MOTOR:")
    await state.set_state(PermisoForm.motor)

@dp.message(PermisoForm.motor)
async def get_motor(message: types.Message, state: FSMContext):
    await state.update_data(motor=message.text.strip().upper())
    await message.answer("COLOR del vehículo:")
    await state.set_state(PermisoForm.color)

@dp.message(PermisoForm.color)
async def get_color(message: types.Message, state: FSMContext):
    await state.update_data(color=message.text.strip().upper())
    await message.answer("NOMBRE COMPLETO del propietario:")
    await state.set_state(PermisoForm.nombre)

@dp.message(PermisoForm.nombre)
async def get_nombre(message: types.Message, state: FSMContext):
    datos           = await state.get_data()
    datos["nombre"] = message.text.strip().upper()
    datos["username"] = message.from_user.username or "Sin username"

    folio     = await generar_folio_192()
    hoy       = datetime.now()
    fecha_ven = hoy + timedelta(days=30)

    # state.clear() ANTES del create_task — evita re-triggers
    await state.clear()

    await message.answer(
        f"📋 PROCESANDO PERMISO DE GUANAJUATO\n\n"
        f"Folio: {folio}\n"
        f"Titular: {datos['nombre']}\n"
        f"Vigencia: 30 días\n\n"
        "Generando documentación...")

    # Webhook regresa inmediatamente — PDF en background
    asyncio.create_task(
        _generar_y_enviar_background(
            message.chat.id, datos, message.from_user.id,
            datos["username"], folio, hoy, fecha_ven
        )
    )

# ------------ CALLBACKS -------------------------------------------------------

@dp.callback_query(lambda c: c.data and c.data.startswith("validar_"))
async def callback_validar_admin(callback: CallbackQuery):
    folio = callback.data.replace("validar_", "")
    if not folio.startswith("192"):
        await callback.answer("❌ Folio inválido", show_alert=True); return
    if folio in timers_activos:
        uid = timers_activos[folio]["user_id"]
        cancelar_timer_folio(folio)
        try:
            now = datetime.now().isoformat()
            await asyncio.to_thread(lambda: (
                supabase.table("folios_registrados").update(
                    {"estado": "VALIDADO_ADMIN", "fecha_comprobante": now}
                ).eq("folio", folio).execute(),
                supabase.table("borradores_registros").update(
                    {"estado": "VALIDADO_ADMIN", "fecha_comprobante": now}
                ).eq("folio", folio).execute()
            ))
        except Exception as e:
            print(f"Error BD validar {folio}: {e}")
        await callback.answer("✅ Folio validado por administración", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=None)
        try:
            await bot.send_message(uid,
                f"✅ PAGO VALIDADO POR ADMINISTRACIÓN - GUANAJUATO\n"
                f"Folio: {folio}\nTu permiso está activo.\n\n"
                f"📋 Para generar otro permiso use /banamex")
        except Exception as e:
            print(f"Error notificando usuario {uid}: {e}")
    else:
        await callback.answer("❌ Folio no encontrado en timers activos", show_alert=True)

@dp.callback_query(lambda c: c.data and c.data.startswith("detener_"))
async def callback_detener_timer(callback: CallbackQuery):
    folio = callback.data.replace("detener_", "")
    if folio in timers_activos:
        cancelar_timer_folio(folio)
        try:
            await asyncio.to_thread(lambda: supabase.table("folios_registrados").update(
                {"estado": "TIMER_DETENIDO", "fecha_detencion": datetime.now().isoformat()}
            ).eq("folio", folio).execute())
        except Exception as e:
            print(f"Error BD detener {folio}: {e}")
        await callback.answer("⏹️ Timer detenido exitosamente", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(
            f"⏹️ TIMER DETENIDO\n\nFolio: {folio}\n"
            f"El timer de eliminación automática ha sido detenido.\n\n"
            f"📋 Para generar otro permiso use /banamex")
    else:
        await callback.answer("❌ Timer ya no está activo", show_alert=True)

@dp.message(lambda m: m.text and m.text.strip().upper().startswith("SERO"))
async def admin_detener_timer(message: types.Message):
    texto = message.text.strip().upper()
    if len(texto) <= 4:
        await message.answer(
            f"📋 TIMERS ACTIVOS: {len(timers_activos)}\n\n"
            f"Para detener: SERO[FOLIO]\nEjemplo: SERO1921\n\n"
            f"📋 Para generar otro permiso use /banamex"); return
    folio = texto[4:]
    if not folio.startswith("192"):
        await message.answer(
            f"⚠️ Folio {folio} no es GUANAJUATO (debe iniciar con 192)\n\n"
            f"📋 Para generar otro permiso use /banamex"); return
    if folio in timers_activos:
        uid = timers_activos[folio]["user_id"]
        cancelar_timer_folio(folio)
        try:
            now = datetime.now().isoformat()
            await asyncio.to_thread(lambda: (
                supabase.table("folios_registrados").update(
                    {"estado": "VALIDADO_ADMIN", "fecha_admin_stop": now}
                ).eq("folio", folio).execute(),
                supabase.table("borradores_registros").update(
                    {"estado": "VALIDADO_ADMIN", "fecha_admin_stop": now}
                ).eq("folio", folio).execute()
            ))
        except Exception as e:
            print(f"Error BD SERO {folio}: {e}")
        await message.answer(
            f"✅ VALIDACIÓN ADMINISTRATIVA OK\nFolio: {folio}\nTimer cancelado.\n\n"
            f"📋 Para generar otro permiso use /banamex")
        try:
            await bot.send_message(uid,
                f"✅ PAGO VALIDADO POR ADMINISTRACIÓN - GUANAJUATO\n\n"
                f"Folio: {folio}\nTu permiso está activo.\n\n"
                f"📋 Para generar otro permiso use /banamex")
        except Exception as e:
            print(f"Error notificando usuario {uid}: {e}")
    else:
        await message.answer(
            f"❌ Folio {folio} no encontrado en timers activos.\n"
            f"Timers activos: {len(timers_activos)}\n\n"
            f"📋 Para generar otro permiso use /banamex")

@dp.message(lambda m: m.content_type == ContentType.PHOTO)
async def recibir_comprobante(message: types.Message):
    uid    = message.from_user.id
    folios = obtener_folios_usuario(uid)
    if not folios:
        await message.answer(
            "ℹ️ No tienes permisos pendientes de pago.\n\n"
            "📋 Para generar otro permiso use /banamex"); return
    if len(folios) > 1:
        lista = '\n'.join([f"• {f}" for f in folios])
        await message.answer(
            f"📄 MÚLTIPLES FOLIOS ACTIVOS\n\n{lista}\n\n"
            f"Responde con el NÚMERO DE FOLIO para este comprobante.\n\n"
            f"📋 Para generar otro permiso use /banamex"); return
    folio = folios[0]; cancelar_timer_folio(folio)
    try:
        now = datetime.now().isoformat()
        await asyncio.to_thread(lambda: (
            supabase.table("folios_registrados").update(
                {"estado": "COMPROBANTE_ENVIADO", "fecha_comprobante": now}
            ).eq("folio", folio).execute(),
            supabase.table("borradores_registros").update(
                {"estado": "COMPROBANTE_ENVIADO", "fecha_comprobante": now}
            ).eq("folio", folio).execute()
        ))
    except Exception as e:
        print(f"Error actualizando estado: {e}")
    await message.answer(
        f"✅ COMPROBANTE RECIBIDO\n\n📄 Folio: {folio}\n⏱️ Timer detenido.\n\n"
        f"📋 Para generar otro permiso use /banamex")

@dp.message(Command("folios"))
async def ver_folios_activos(message: types.Message):
    uid    = message.from_user.id
    folios = obtener_folios_usuario(uid)
    if not folios:
        await message.answer(
            "ℹ️ NO HAY FOLIOS ACTIVOS\n\n"
            "📋 Para generar otro permiso use /banamex"); return
    lista   = []
    botones = []
    for folio in folios:
        if folio in timers_activos:
            seg  = max(0, int(36*3600 -
                (datetime.now()-timers_activos[folio]["start_time"]).total_seconds()))
            h, m = divmod(seg // 60, 60)
            lista.append(f"• {folio} ({h}h {m}min)")
        else:
            lista.append(f"• {folio} (sin timer)")
        botones.append([InlineKeyboardButton(
            text=f"⏹️ Detener {folio}", callback_data=f"detener_{folio}")])
    await message.answer(
        f"📋 FOLIOS GUANAJUATO ACTIVOS ({len(folios)})\n\n" + '\n'.join(lista) +
        f"\n\n⏰ Timer 36h por folio.\n📸 Envía imagen para comprobante.\n\n"
        "📋 Para generar otro permiso use /banamex",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=botones))

@dp.message(lambda m: m.text and any(p in m.text.lower() for p in
    ['costo','precio','cuanto','cuánto','deposito','depósito','pago','valor','monto']))
async def responder_costo(message: types.Message):
    await message.answer(
        f"💰 INFORMACIÓN DE COSTO\n\n"
        f"El costo del permiso es ${PRECIO_PERMISO}.\n\n"
        "📋 Para generar otro permiso use /banamex")

@dp.message()
async def fallback(message: types.Message):
    await message.answer("🏛️ Sistema Guanajuato.")

# ------------ FASTAPI ---------------------------------------------------------
_keep_task = None

async def keep_alive():
    while True:
        await asyncio.sleep(600)
        print("[HEARTBEAT] Bot Guanajuato activo")

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _keep_task
    inicializar_folio_desde_supabase()
    await bot.delete_webhook(drop_pending_updates=True)
    if BASE_URL:
        await bot.set_webhook(f"{BASE_URL}/webhook", allowed_updates=["message","callback_query"])
        print(f"[WEBHOOK] {BASE_URL}/webhook")
        _keep_task = asyncio.create_task(keep_alive())
    else:
        print("[POLLING] Sin webhook")
    print(f"[SISTEMA] Guanajuato v6.0 listo — "
          f"siguiente folio: {FOLIO_NUM_PREFIJO}{_folio_counter['siguiente']}")
    yield
    if _keep_task:
        _keep_task.cancel()
        with suppress(asyncio.CancelledError): await _keep_task
    await bot.session.close()

app = FastAPI(lifespan=lifespan)

@app.post("/webhook")
async def telegram_webhook(request: Request):
    try:
        data   = await request.json()
        update = types.Update(**data)
        await dp.feed_webhook_update(bot, update)
        return {"ok": True}
    except Exception as e:
        print(f"[ERROR] webhook: {e}")
        return {"ok": False, "error": str(e)}

@app.get("/")
async def root():
    return {
        "ok":              True,
        "version":         "6.0",
        "entidad":         "Guanajuato",
        "siguiente_folio": f"{FOLIO_NUM_PREFIJO}{_folio_counter['siguiente']}",
        "timers_activos":  len(timers_activos),
        "cambios_v6.0": [
            "AiohttpSession timeout=300s — elimina HTTP timeout error",
            "PDF en background task — webhook responde inmediatamente",
            "Watermark Supabase clave GTO — contador nunca retrocede",
            "/banamex en lugar de /chuleta",
            "state.clear() antes del create_task — sin duplicados",
        ]
    }

@app.get("/status")
async def status():
    return {
        "sistema":         "Guanajuato v6.0",
        "siguiente_folio": f"{FOLIO_NUM_PREFIJO}{_folio_counter['siguiente']}",
        "timers":          len(timers_activos),
        "url_verificacion":URL_VERIFICACION_BASE
    }

if __name__ == '__main__':
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    print(f"[ARRANQUE] Puerto {port}")
    uvicorn.run(app, host="0.0.0.0", port=port)

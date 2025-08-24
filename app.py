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
import zipfile
from pathlib import Path
import tempfile
from PIL import Image
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Table, Paragraph, Image as RLImage, PageBreak, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib import colors

# ------------ CONFIG ------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "https://xsagwqepoljfsogusubw.supabase.co")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InhzYWd3cWVwb2xqZnNvZ3VzdWJ3Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NDM5NjM3NTUsImV4cCI6MjA1OTUzOTc1NX0.NUixULn0m2o49At8j6X58UqbXre2O2_JStqzls_8Gws")
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
OUTPUT_DIR = "documentos"
IMAGES_DIR = "imagenes_pago"
PLANTILLA_GUANAJUATO_PRIMERA = "guanajuato_imagen_fullhd.pdf"
PLANTILLA_GUANAJUATO_SEGUNDA = "guanajuato.pdf"

# Admin User ID
ADMIN_USER_ID = 8478687124

# Precio del permiso
PRECIO_PERMISO = 150

os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(IMAGES_DIR, exist_ok=True)

# ------------ SUPABASE ------------
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ------------ BOT ------------
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ------------ TIMER MANAGEMENT ------------
timers_activos = {}

async def eliminar_folio_automatico(user_id: int, folio: str):
    """Elimina folio automáticamente después del tiempo límite"""
    try:
        # Buscar datos del folio para obtener serie
        response = supabase.table("folios_registrados").select("numero_serie").eq("folio", folio).execute()
        numero_serie = ""
        if response.data:
            numero_serie = response.data[0].get("numero_serie", "")
        
        # Eliminar de base de datos
        supabase.table("folios_registrados").delete().eq("folio", folio).execute()
        supabase.table("borradores_registros").delete().eq("folio", folio).execute()
        
        # Agregar serie a lista de bloqueados
        if numero_serie:
            try:
                supabase.table("series_bloqueadas").insert({
                    "numero_serie": numero_serie,
                    "fecha_bloqueo": datetime.now().isoformat(),
                    "motivo": "TIEMPO_AGOTADO_PAGO",
                    "folio_original": folio
                }).execute()
            except:
                pass
        
        # Notificar al usuario
        await bot.send_message(
            user_id,
            f"🚫 **TIEMPO AGOTADO - FOLIO ELIMINADO**\n\n"
            f"📄 Folio: {folio}\n"
            f"⏰ Se acabaron las 2 horas para pagar\n\n"
            f"🔒 **SERIE/NIV BLOQUEADA:**\n"
            f"• {numero_serie}\n"
            f"• Este vehículo NO podrá tramitar permisos futuros\n"
            f"• El bloqueo es PERMANENTE\n\n"
            f"⚠️ Para tramitar un nuevo permiso necesitará otro vehículo.\n"
            f"Use /start para consultar términos y condiciones.",
            parse_mode="Markdown"
        )
        
        if user_id in timers_activos:
            del timers_activos[user_id]
            
    except Exception as e:
        print(f"Error eliminando folio {folio}: {e}")

async def enviar_recordatorio(user_id: int, folio: str, minutos_restantes: int):
    """Envía recordatorios de pago"""
    try:
        await bot.send_message(
            user_id,
            f"⏰ **RECORDATORIO URGENTE DE PAGO**\n\n"
            f"📄 Folio: {folio}\n"
            f"⏱️ Te quedan **{minutos_restantes} minutos** para pagar\n"
            f"💰 Cantidad: ${PRECIO_PERMISO}\n\n"
            f"📸 **ENVÍA TU COMPROBANTE AHORA**\n"
            f"🚫 Si no pagas, tu serie/NIV será **BLOQUEADA PERMANENTEMENTE**",
            parse_mode="Markdown"
        )
    except Exception as e:
        print(f"Error enviando recordatorio a {user_id}: {e}")

async def iniciar_timer_pago(user_id: int, folio: str):
    """Inicia el timer de 2 horas con recordatorios"""
    async def timer_task():
        for minutos in [30, 60, 90]:
            await asyncio.sleep(30 * 60)
            if user_id not in timers_activos:
                return
            minutos_restantes = 120 - minutos
            await enviar_recordatorio(user_id, folio, minutos_restantes)
        
        await asyncio.sleep(20 * 60)
        if user_id in timers_activos:
            await enviar_recordatorio(user_id, folio, 10)
        
        await asyncio.sleep(10 * 60)
        
        if user_id in timers_activos:
            await eliminar_folio_automatico(user_id, folio)
    
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

# ------------ SISTEMA DE REPORTES PDF ------------
async def generar_reporte_diario():
    """Genera reporte PDF diario con folios e imágenes de comprobantes"""
    try:
        cdmx_tz = ZoneInfo("America/Mexico_City")
        hoy_cdmx = datetime.now(cdmx_tz).date()
        
        inicio_dia = datetime.combine(hoy_cdmx, datetime.min.time())
        fin_dia = inicio_dia + timedelta(days=1)
        
        response = supabase.table("folios_registrados").select("*").gte(
            "fecha_expedicion", inicio_dia.isoformat()
        ).lt("fecha_expedicion", fin_dia.isoformat()).execute()
        
        folios_data = response.data
        
        if not folios_data:
            await bot.send_message(
                ADMIN_USER_ID,
                f"📊 REPORTE DIARIO - {hoy_cdmx.strftime('%d/%m/%Y')}\n\n"
                f"❌ No se generaron permisos hoy.\n"
                f"💤 Día tranquilo, jefe."
            )
            return
        
        # Estadísticas
        total_folios = len(folios_data)
        con_comprobante = len([f for f in folios_data if f.get('estado') == 'COMPROBANTE_ENVIADO'])
        pendientes = len([f for f in folios_data if f.get('estado') == 'PENDIENTE'])
        activados = len([f for f in folios_data if f.get('estado') == 'ACTIVO'])
        
        ingresos_potenciales = total_folios * PRECIO_PERMISO
        ingresos_con_comprobante = con_comprobante * PRECIO_PERMISO
        ingresos_confirmados = activados * PRECIO_PERMISO
        
        # Crear PDF con ReportLab
        pdf_filename = f"reporte_guanajuato_{hoy_cdmx.strftime('%Y%m%d')}.pdf"
        pdf_path = os.path.join(OUTPUT_DIR, pdf_filename)
        
        doc = SimpleDocTemplate(pdf_path, pagesize=A4)
        elementos = []
        styles = getSampleStyleSheet()
        
        # Título
        titulo = Paragraph(
            f"<b>REPORTE DIARIO GUANAJUATO - {hoy_cdmx.strftime('%d/%m/%Y')}</b>", 
            styles['Title']
        )
        elementos.append(titulo)
        elementos.append(Spacer(1, 20))
        
        # Estadísticas
        stats_text = f"""
        <b>ESTADÍSTICAS DEL DÍA:</b><br/>
        • Total permisos generados: {total_folios}<br/>
        • Pendientes de pago: {pendientes}<br/>
        • Con comprobante enviado: {con_comprobante}<br/>
        • Permisos activados: {activados}<br/><br/>
        
        <b>ANÁLISIS FINANCIERO:</b><br/>
        • Ingresos potenciales: ${ingresos_potenciales:,}<br/>
        • Por validar: ${ingresos_con_comprobante:,}<br/>
        • Confirmados: ${ingresos_confirmados:,}<br/>
        • Tasa de conversión: {(con_comprobante/total_folios)*100:.1f}%
        """
        
        elementos.append(Paragraph(stats_text, styles['Normal']))
        elementos.append(PageBreak())
        
        # Por cada folio con comprobante
        folios_con_comprobante = [f for f in folios_data if f.get('estado') == 'COMPROBANTE_ENVIADO']
        
        for folio_data in folios_con_comprobante:
            folio = folio_data['folio']
            
            # Datos del folio
            datos_texto = f"""
            <b>FOLIO:</b> {folio}<br/>
            <b>NOMBRE:</b> {folio_data['nombre']}<br/>
            <b>VEHÍCULO:</b> {folio_data['marca']} {folio_data['linea']} {folio_data['anio']}<br/>
            <b>SERIE:</b> {folio_data['numero_serie']}<br/>
            <b>MOTOR:</b> {folio_data['numero_motor']}<br/>
            <b>COLOR:</b> {folio_data['color']}<br/>
            <b>ESTADO:</b> {folio_data['estado']}<br/>
            <b>USUARIO:</b> @{folio_data.get('username', 'sin_username')}<br/>
            <b>FECHA:</b> {folio_data['fecha_expedicion']}
            """
            
            # Buscar imagen del comprobante
            imagen_path = None
            if os.path.exists(IMAGES_DIR):
                for archivo in os.listdir(IMAGES_DIR):
                    if archivo.startswith(f"{folio}_"):
                        imagen_path = os.path.join(IMAGES_DIR, archivo)
                        break
            
            # Crear tabla con datos e imagen lado a lado
            if imagen_path and os.path.exists(imagen_path):
                try:
                    # Redimensionar imagen para el PDF
                    img = RLImage(imagen_path, width=2.5*inch, height=3*inch)
                    
                    # Tabla con datos e imagen
                    tabla_data = [
                        [Paragraph(datos_texto, styles['Normal']), img]
                    ]
                    
                    tabla = Table(tabla_data, colWidths=[4*inch, 3*inch])
                    tabla.setStyle([
                        ('VALIGN', (0,0), (-1,-1), 'TOP'),
                        ('GRID', (0,0), (-1,-1), 1, colors.black),
                        ('BACKGROUND', (0,0), (0,0), colors.lightgrey)
                    ])
                    
                except Exception as e:
                    # Si falla la imagen, solo texto
                    tabla_data = [[Paragraph(datos_texto + f"<br/><b>IMAGEN:</b> Error cargando - {str(e)}", styles['Normal'])]]
                    tabla = Table(tabla_data, colWidths=[7*inch])
                    tabla.setStyle([
                        ('GRID', (0,0), (-1,-1), 1, colors.black),
                        ('BACKGROUND', (0,0), (-1,-1), colors.lightgrey)
                    ])
            else:
                # Sin imagen
                tabla_data = [[Paragraph(datos_texto + f"<br/><b>COMPROBANTE:</b> No enviado", styles['Normal'])]]
                tabla = Table(tabla_data, colWidths=[7*inch])
                tabla.setStyle([
                    ('GRID', (0,0), (-1,-1), 1, colors.black),
                    ('BACKGROUND', (0,0), (-1,-1), colors.lightyellow)
                ])
            
            elementos.append(tabla)
            elementos.append(Spacer(1, 20))
        
        # Generar PDF
        doc.build(elementos)
        
        # Crear ZIP con imágenes
        zip_path = None
        imagenes_encontradas = []
        folios_hoy = [str(f['folio']) for f in folios_data]
        
        if os.path.exists(IMAGES_DIR) and os.listdir(IMAGES_DIR):
            zip_filename = f"comprobantes_guanajuato_{hoy_cdmx.strftime('%Y%m%d')}.zip"
            zip_path = os.path.join(OUTPUT_DIR, zip_filename)
                
            with zipfile.ZipFile(zip_path, 'w') as zipf:
                for archivo in os.listdir(IMAGES_DIR):
                    if archivo.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')):
                        folio_imagen = archivo.split('_')[0]
                        if folio_imagen in folios_hoy:
                            archivo_path = os.path.join(IMAGES_DIR, archivo)
                            zipf.write(archivo_path, archivo)
                            imagenes_encontradas.append(archivo)
        
        # Mensaje de estadísticas
        mensaje_reporte = (
            f"📊 REPORTE DIARIO GUANAJUATO - {hoy_cdmx.strftime('%d/%m/%Y')}\n"
            f"🕐 Generado a las {datetime.now(cdmx_tz).strftime('%H:%M:%S')} hrs\n\n"
            
            f"📈 ESTADÍSTICAS DEL DÍA:\n"
            f"• 📄 Total permisos generados: {total_folios}\n"
            f"• ⏳ Pendientes de pago: {pendientes}\n"
            f"• 📸 Con comprobante enviado: {con_comprobante}\n"
            f"• ✅ Permisos activados: {activados}\n"
            f"• 🖼️ Imágenes guardadas: {len(imagenes_encontradas)}\n\n"
            
            f"💰 ANÁLISIS FINANCIERO:\n"
            f"• 💵 Ingresos potenciales: ${ingresos_potenciales:,}\n"
            f"• 📄 Por validar: ${ingresos_con_comprobante:,}\n"
            f"• ✅ Confirmados: ${ingresos_confirmados:,}\n"
            f"• 📊 Tasa de conversión: {(con_comprobante/total_folios)*100:.1f}%\n\n"
            
            f"🛠️ Usa los comandos admin para gestionar pagos."
        )
        
        await bot.send_message(ADMIN_USER_ID, mensaje_reporte)
        
        # Enviar PDF con folios e imágenes
        await bot.send_document(
            ADMIN_USER_ID,
            FSInputFile(pdf_path, filename=pdf_filename),
            caption=f"📊 Reporte PDF - {total_folios} folios con imágenes de comprobantes"
        )
        
        # Enviar ZIP con imágenes separadas
        if zip_path and imagenes_encontradas:
            await bot.send_document(
                ADMIN_USER_ID,
                FSInputFile(zip_path, filename=os.path.basename(zip_path)),
                caption=f"📸 {len(imagenes_encontradas)} comprobantes de pago separados"
            )
        
        print(f"✅ Reporte PDF generado para {hoy_cdmx}")
            
    except Exception as e:
        error_msg = f"❌ ERROR generando reporte diario:\n{str(e)}"
        print(error_msg)
        try:
            await bot.send_message(ADMIN_USER_ID, error_msg)
        except:
            print("Error enviando mensaje de error al admin")

# ------------ SCHEDULER PARA REPORTE DIARIO ------------
async def scheduler_reporte_diario():
    """Scheduler que ejecuta el reporte a las 8 PM CDMX"""
    print("🕐 Scheduler iniciado - reportes a las 20:00 hrs CDMX")
    
    while True:
        try:
            cdmx_tz = ZoneInfo("America/Mexico_City")
            now = datetime.now(cdmx_tz)
            target_time = now.replace(hour=20, minute=0, second=0, microsecond=0)
            
            if now >= target_time:
                target_time += timedelta(days=1)
            
            wait_seconds = (target_time - now).total_seconds()
            
            print(f"⏰ Próximo reporte: {target_time.strftime('%d/%m/%Y %H:%M:%S')} CDMX")
            print(f"⌛ Esperando {wait_seconds/3600:.1f} horas...")
            
            await asyncio.sleep(wait_seconds)
            
            print(f"🚀 Ejecutando reporte diario automático...")
            await generar_reporte_diario()
            
        except Exception as e:
            print(f"❌ Error en scheduler: {e}")
            await asyncio.sleep(3600)

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

# ------------ VALIDACIÓN DE SERIES BLOQUEADAS ------------
async def verificar_serie_bloqueada(numero_serie: str) -> bool:
    """Verifica si una serie está bloqueada"""
    try:
        response = supabase.table("series_bloqueadas").select("*").eq("numero_serie", numero_serie).execute()
        return len(response.data) > 0
    except:
        return False

# ------------ GENERACIÓN PDF GUANAJUATO ------------
def generar_pdfs_guanajuato_separados(folio, datos, fecha_exp, fecha_ven):
    """Genera DOS archivos PDF separados para las plantillas de Guanajuato"""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    
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

    # QR Code
    texto_qr = f"""FOLIO: {folio}
NOMBRE: {datos.get('nombre', '')}
MARCA: {datos.get('marca', '')}
LINEA: {datos.get('linea', '')}
AÑO: {datos.get('anio', '')}
SERIE: {datos.get('serie', '')}
MOTOR: {datos.get('motor', '')}
COLOR: {datos.get('color', '')}
GUANAJUATO PERMISOS DIGITALES"""

    qr = qrcode.QRCode(version=2, error_correction=qrcode.constants.ERROR_CORRECT_H, box_size=10, border=2)
    qr.add_data(texto_qr.upper())
    qr.make(fit=True)

    img_qr = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    buf = BytesIO()
    img_qr.save(buf, format="PNG")
    buf.seek(0)
    qr_pix = fitz.Pixmap(buf.read())

    cm = 85.05
    ancho_qr = alto_qr = cm * 3.0
    page_width = pg1.rect.width
    x_qr = page_width - (2.5 * cm) - ancho_qr
    y_qr = 20.5 * cm

    pg1.insert_image(fitz.Rect(x_qr, y_qr, x_qr + ancho_qr, y_qr + alto_qr), pixmap=qr_pix, overlay=True)
    
    salida_primera = os.path.join(OUTPUT_DIR, f"{folio}_guanajuato_principal.pdf")
    doc_primera.save(salida_primera)
    doc_primera.close()
    
    # === SEGUNDA PLANTILLA ===
    doc_segunda = fitz.open(PLANTILLA_GUANAJUATO_SEGUNDA)
    pg2 = doc_segunda[0]
    
    pg2.insert_text(coords_gto_segunda["numero_serie"][:2], datos.get("serie", ""), 
                    fontsize=coords_gto_segunda["numero_serie"][2], 
                    color=coords_gto_segunda["numero_serie"][3])
    pg2.insert_text(coords_gto_segunda["fecha"][:2], f_exp, 
                    fontsize=coords_gto_segunda["fecha"][2], 
                    color=coords_gto_segunda["fecha"][3])
    
    salida_segunda = os.path.join(OUTPUT_DIR, f"{folio}_guanajuato_secundario.pdf")
    doc_segunda.save(salida_segunda)
    doc_segunda.close()
    
    return salida_primera, salida_segunda

# ------------ HANDLERS PRINCIPALES ------------
@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [
            types.InlineKeyboardButton(text="✅ ESTOY DE ACUERDO", callback_data="acepto_terminos"),
            types.InlineKeyboardButton(text="❌ NO ESTOY DE ACUERDO", callback_data="rechazo_terminos")
        ]
    ])
    
    await message.answer(
        "🏛️ **GOBIERNO DEL ESTADO DE GUANAJUATO**\n"
        "📋 Sistema Oficial de Permisos Vehiculares\n\n"
        
        "⚠️ **TÉRMINOS Y CONDICIONES IMPORTANTES:**\n\n"
        
        f"💰 **COSTO DEL TRÁMITE:** ${PRECIO_PERMISO} pesos\n\n"
        
        "⏰ **TIEMPO LÍMITE DE PAGO:** 2 horas exactas\n"
        "• Una vez generado tu folio tienes MÁXIMO 2 horas para pagar\n"
        "• Debes enviar el comprobante de pago a este chat\n\n"
        
        "🚫 **ADVERTENCIA DE BLOQUEO:**\n"
        "• Si NO pagas en las 2 horas, tu folio será ELIMINADO\n"
        "• El número de serie/NIV quedará BLOQUEADO permanentemente\n"
        "• NO podrás tramitar futuros permisos con ese vehículo\n\n"
        
        "📋 **DOCUMENTOS REQUERIDOS:**\n"
        "• Comprobante de pago (transferencia o OXXO)\n"
        "• Todos los datos del vehículo correctos\n\n"
        
        "**¿ACEPTAS estos términos y condiciones para continuar?**",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )

# ------------ CALLBACK HANDLERS ------------
@dp.callback_query(lambda c: c.data == "acepto_terminos")
async def acepto_terminos(callback: types.CallbackQuery):
    await callback.answer()
    await callback.message.edit_text(
        "✅ **TÉRMINOS ACEPTADOS**\n\n"
        "🏛️ Bienvenido al Sistema Oficial de Permisos de Guanajuato\n\n"
        
        f"🚗 **INICIAR TRÁMITE:** /permiso\n"
        f"💰 **Costo:** ${PRECIO_PERMISO} pesos\n\n"
        
        "📋 **MÉTODOS DE PAGO DISPONIBLES:**\n"
        "• 🏦 Transferencia bancaria AZTECA\n"
        "• 🏪 Depósito en OXXO\n\n"
        
        "⚡ **¿Listo para tramitar tu permiso?**\n"
        "Escribe /permiso para comenzar.",
        parse_mode="Markdown"
    )

@dp.callback_query(lambda c: c.data == "acepto_terminos")
async def acepto_terminos(callback: types.CallbackQuery):
    await callback.answer()
    await callback.message.edit_text(
        "✅ **TÉRMINOS ACEPTADOS**\n\n"
        "🏛️ Bienvenido al Sistema Oficial de Permisos de Guanajuato\n\n"
        
        f"🚗 **INICIAR TRÁMITE:** /permiso\n"
        f"💰 **Costo:** ${PRECIO_PERMISO} pesos\n\n"
        
        "📋 **MÉTODOS DE PAGO DISPONIBLES:**\n"
        "• 🏦 Transferencia bancaria AZTECA\n"
        "• 🏪 Depósito en OXXO\n\n"
        
        "⚡ **¿Listo para tramitar tu permiso?**\n"
        "Escribe /permiso para comenzar.",
        parse_mode="Markdown"
    )
    
@dp.callback_query(lambda c: c.data == "rechazo_terminos")
async def rechazo_terminos(callback: types.CallbackQuery):
    await callback.answer()
    await callback.message.edit_text(
        "❌ **TÉRMINOS RECHAZADOS**\n\n"
        "🏛️ No puedes usar el sistema sin aceptar los términos.\n\n"
        "📋 Si cambias de opinión, usa /start para volver a leer los términos.\n\n"
        "📞 Para dudas, contacta a las oficinas gubernamentales correspondientes."
    )

@dp.message(Command("permiso"))
async def permiso_cmd(message: types.Message, state: FSMContext):
    cancelar_timer(message.from_user.id)
    await message.answer(
        "🏛️ **TRÁMITE OFICIAL INICIADO**\n"
        "📋 Sistema de Permisos del Gobierno de Guanajuato\n\n"
        
        "📝 **INSTRUCCIONES CLARAS:**\n"
        "• Proporciona EXACTAMENTE la información solicitada\n"
        "• Verifica que todos los datos sean CORRECTOS\n"
        "• NO uses abreviaciones\n\n"
        
        "🚗 **PASO 1 de 7**\n"
        "Escribe la **MARCA** del vehículo:\n"
        "(Ejemplo: NISSAN, TOYOTA, VOLKSWAGEN)",
        parse_mode="Markdown"
    )
    await state.set_state(PermisoForm.marca)

@dp.message(PermisoForm.marca)
async def get_marca(message: types.Message, state: FSMContext):
    marca = message.text.strip().upper()
    await state.update_data(marca=marca)
    await message.answer(
        f"✅ **MARCA REGISTRADA:** {marca}\n\n"
        "🚗 **PASO 2 de 7**\n"
        "Escribe la **LÍNEA/MODELO** del vehículo:\n"
        "(Ejemplo: SENTRA, COROLLA, JETTA)",
        parse_mode="Markdown"
    )
    await state.set_state(PermisoForm.linea)

@dp.message(PermisoForm.linea)
async def get_linea(message: types.Message, state: FSMContext):
    linea = message.text.strip().upper()
    await state.update_data(linea=linea)
    await message.answer(
        f"✅ **LÍNEA REGISTRADA:** {linea}\n\n"
        "📅 **PASO 3 de 7**\n"
        "Escribe el **AÑO** del vehículo:\n"
        "(Debe ser de 4 dígitos - Ejemplo: 2020)",
        parse_mode="Markdown"
    )
    await state.set_state(PermisoForm.anio)

@dp.message(PermisoForm.anio)
async def get_anio(message: types.Message, state: FSMContext):
    anio = message.text.strip()
    if not anio.isdigit() or len(anio) != 4:
        await message.answer(
            "⚠️ **ERROR EN EL AÑO**\n\n"
            "El año debe ser de **4 dígitos exactos**\n"
            "Ejemplo correcto: 2020\n"
            "Ejemplo incorrecto: 20\n\n"
            "Escribe el año nuevamente:",
            parse_mode="Markdown"
        )
        return
    
    await state.update_data(anio=anio)
    await message.answer(
        f"✅ **AÑO REGISTRADO:** {anio}\n\n"
        "🔢 **PASO 4 de 7**\n"
        "Escribe el **NÚMERO DE SERIE (NIV)** del vehículo:\n"
        "• Mínimo 10 caracteres\n"
        "• Verifica que esté correcto\n"
        "• Este número se usará para identificar tu vehículo",
        parse_mode="Markdown"
    )
    await state.set_state(PermisoForm.serie)

@dp.message(PermisoForm.serie)
async def get_serie(message: types.Message, state: FSMContext):
    serie = message.text.strip().upper()
    
    if len(serie) < 10:
        await message.answer(
            "⚠️ **NÚMERO DE SERIE INCORRECTO**\n\n"
            "El número de serie debe tener **mínimo 10 caracteres**\n"
            "Revisa tu tarjeta de circulación y escribe el número completo:",
            parse_mode="Markdown"
        )
        return
    
    # Verificar si la serie está bloqueada
    if await verificar_serie_bloqueada(serie):
        await message.answer(
            "🚫 **SERIE/NIV BLOQUEADA**\n\n"
            f"El número de serie {serie} está **BLOQUEADO** en el sistema.\n\n"
            "**MOTIVOS POSIBLES:**\n"
            "• No se completó un pago anterior\n"
            "• Incumplimiento de términos y condiciones\n"
            "• Decisión administrativa\n\n"
            "❌ **NO puedes tramitar permisos con este vehículo**\n"
            "📞 Para más información contacta a las oficinas gubernamentales.",
            parse_mode="Markdown"
        )
        await state.clear()
        return
        
    await state.update_data(serie=serie)
    await message.answer(
        f"✅ **SERIE/NIV REGISTRADO:** {serie}\n\n"
        "⚙️ **PASO 5 de 7**\n"
        "Escribe el **NÚMERO DE MOTOR** del vehículo:",
        parse_mode="Markdown"
    )
    await state.set_state(PermisoForm.motor)

@dp.message(PermisoForm.motor)
async def get_motor(message: types.Message, state: FSMContext):
    motor = message.text.strip().upper()
    await state.update_data(motor=motor)
    await message.answer(
        f"✅ **MOTOR REGISTRADO:** {motor}\n\n"
        "🎨 **PASO 6 de 7**\n"
        "Escribe el **COLOR** del vehículo:\n"
        "(Ejemplo: BLANCO, NEGRO, ROJO, AZUL)",
        parse_mode="Markdown"
    )
    await state.set_state(PermisoForm.color)

@dp.message(PermisoForm.color)
async def get_color(message: types.Message, state: FSMContext):
    color = message.text.strip().upper()
    await state.update_data(color=color)
    await message.answer(
        f"✅ **COLOR REGISTRADO:** {color}\n\n"
        "👤 **PASO 7 de 7 - FINAL**\n"
        "Escribe tu **NOMBRE COMPLETO** tal como aparece en tu identificación:\n"
        "• Sin abreviaciones\n"
        "• Nombre y apellidos completos",
        parse_mode="Markdown"
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
        f"🔄 **PROCESANDO PERMISO DE GUANAJUATO...**\n"
        f"Folio: {datos['folio']}\n"
        f"Titular: {nombre}\n\n"
        "Generando documentos oficiales...",
        parse_mode="Markdown"
    )

    try:
        pdf_principal, pdf_secundario = generar_pdfs_guanajuato_separados(datos['folio'], datos, hoy, fecha_ven)

        await message.answer_document(
            FSInputFile(pdf_principal),
            caption=f"📋 **PERMISO PRINCIPAL GUANAJUATO**\nFolio: {datos['folio']}\nVigencia: 30 días\n🏛️ Documento oficial con código QR"
        )

        await message.answer_document(
            FSInputFile(pdf_secundario),
            caption=f"📋 **PERMISO SECUNDARIO GUANAJUATO**\nFolio: {datos['folio']}\nVigencia: 30 días\n🏛️ Documento de respaldo"
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

        # INICIAR TIMER
        await iniciar_timer_pago(message.from_user.id, datos['folio'])

        await message.answer(
            f"🎉 **PERMISO GENERADO EXITOSAMENTE**\n\n"
            f"📄 **Folio:** {datos['folio']}\n"
            f"👤 **Titular:** {nombre}\n"
            f"🚗 **Vehículo:** {datos['marca']} {datos['linea']} {datos['anio']}\n\n"
            
            "✅ **PERMISO YA EN SISTEMA**\n"
            "📄 Listo para imprimir y colocar en lugar visible\n\n"
            
            "⚠️ **RECORDATORIO IMPORTANTE:**\n"
            f"⏰ Tienes **2 HORAS** para completar el pago\n"
            f"🚫 Si no pagas, el folio se dará de baja\n"
            f"🔒 La serie/NIV {datos['serie']} quedará **BLOQUEADA** para futuros trámites\n\n"
            
            "💰 **PROCEDE AL PAGO INMEDIATAMENTE:**",
            parse_mode="Markdown"
        )

        await message.answer(
            f"💳 **DATOS PARA PAGO - FOLIO {datos['folio']}**\n\n"
            
            "🏦 **TRANSFERENCIA BANCARIA:**\n"
            f"• **Banco:** AZTECA\n"
            f"• **Titular:** LIZABETH LAURENT MOSQUEDA\n"
            f"• **Número de cuenta:** 12718001303757954\n"
            f"• **Concepto:** Permiso {datos['folio']}\n"
            f"• **Cantidad exacta:** ${PRECIO_PERMISO}\n\n"
            
            "🏪 **DEPÓSITO EN OXXO:**\n"
            f"• **Referencia:** 2242 1701 8038 5581\n"
            f"• **Titular:** LIZABETH LAURENT MOSQUEDA\n"
            f"• **Cantidad exacta:** ${PRECIO_PERMISO}\n\n"
            
            f"📸 **DESPUÉS DE PAGAR:**\n"
            f"• Envía la **FOTO DEL COMPROBANTE** a este chat\n"
            f"• El sistema validará tu pago automáticamente\n\n"
            
            f"⚠️ **ADVERTENCIA FINAL:**\n"
            f"🕐 Timer iniciado: **2 horas exactas**\n"
            f"🚫 Sin pago = Serie/NIV **BLOQUEADA PERMANENTEMENTE**",
            parse_mode="Markdown"
        )
        
    except Exception as e:
        await message.answer(f"💥 **ERROR EN EL SISTEMA**\n\nFallo: {str(e)}\n\nIntente nuevamente con /permiso")
    finally:
        await state.clear()

# ------------ HANDLER DE COMPROBANTES ------------
@dp.message(lambda message: message.content_type == ContentType.PHOTO)
async def recibir_comprobante(message: types.Message):
    user_id = message.from_user.id
    
    if user_id not in timers_activos:
        await message.answer(
            "🤔 **NO TIENES PERMISOS PENDIENTES**\n\n"
            "No hay ningún permiso esperando pago.\n"
            "Usa /start para generar uno nuevo.",
            parse_mode="Markdown"
        )
        return
    
    folio = timers_activos[user_id]["folio"]
    
    try:
        file_info = await bot.get_file(message.photo[-1].file_id)
        file_extension = file_info.file_path.split('.')[-1] if '.' in file_info.file_path else 'jpg'
        
        image_filename = f"{folio}_comprobante.{file_extension}"
        image_path = os.path.join(IMAGES_DIR, image_filename)
        
        await bot.download_file(file_info.file_path, image_path)
        
        cancelar_timer(user_id)
        
        supabase.table("folios_registrados").update({
            "estado": "COMPROBANTE_ENVIADO",
            "fecha_comprobante": datetime.now().isoformat()
        }).eq("folio", folio).execute()
        
        supabase.table("borradores_registros").update({
            "estado": "COMPROBANTE_ENVIADO",
            "fecha_comprobante": datetime.now().isoformat()
        }).eq("folio", folio).execute()
        
        await message.answer(
            f"✅ **COMPROBANTE RECIBIDO Y PROCESADO**\n\n"
            f"📄 **Folio:** {folio}\n"
            f"📸 **Estado:** Imagen guardada correctamente\n"
            f"⏱️ **Timer:** Detenido automáticamente\n\n"
            
            f"🔍 **PROCESO DE VALIDACIÓN:**\n"
            f"• Su comprobante está siendo verificado\n"
            f"• Recibirá notificación una vez validado el pago\n"
            f"• Su permiso quedará activo para circular\n\n"
            
            f"🏛️ **Gracias por usar el Sistema Oficial de Guanajuato**",
            parse_mode="Markdown"
        )
        
    except Exception as e:
        await message.answer(f"❌ **ERROR GUARDANDO COMPROBANTE:** {str(e)}")

# ------------ COMANDOS ADMIN ------------
def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_USER_ID

@dp.message(Command("admin"))
async def admin_menu(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    
    await message.answer(
        "🛠️ **PANEL DE ADMINISTRACIÓN GUANAJUATO**\n\n"
        "📊 /listar - Ver folios pendientes\n"
        "✅ /activar [folio] - Activar permiso\n"
        "❌ /eliminar [folio] - Eliminar folio\n"
        "📈 /activar_lote [folio1,folio2] - Activar varios\n"
        "🗑️ /eliminar_lote [folio1,folio2] - Eliminar varios\n"
        "📊 /reporte_hoy - Generar reporte manual\n"
        "📋 /estadisticas - Ver estadísticas generales\n"
        "⏰ /timers - Ver timers activos\n"
        "🎯 /buscar [folio] - Buscar folio específico\n"
        "🚫 /bloqueadas - Ver series bloqueadas",
        parse_mode="Markdown"
    )

@dp.message(Command("listar"))
async def listar_pendientes(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    
    try:
        response = supabase.table("folios_registrados").select("*").eq("estado", "COMPROBANTE_ENVIADO").execute()
        folios = response.data
        
        if not folios:
            await message.answer("📋 No hay folios pendientes de activación.")
            return
        
        mensaje = "📋 **FOLIOS PENDIENTES:**\n\n"
        
        for folio in folios:
            tiempo_espera = ""
            if folio.get('fecha_comprobante'):
                fecha_comp = datetime.fromisoformat(folio['fecha_comprobante'])
                horas_esperando = (datetime.now() - fecha_comp).total_seconds() / 3600
                tiempo_espera = f" ({horas_esperando:.1f}h)"
            
            mensaje += f"• **{folio['folio']}** - {folio['nombre'][:25]}\n"
            mensaje += f"  📱 @{folio.get('username', 'sin_username')}{tiempo_espera}\n\n"
        
        mensaje += f"💡 **Total:** {len(folios)} folios\n✅ Usa /activar [folio]"
        
        await message.answer(mensaje, parse_mode="Markdown")
        
    except Exception as e:
        await message.answer(f"❌ Error: {str(e)}")

@dp.message(Command("activar"))
async def activar_folio(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    
    try:
        texto = message.text.strip()
        if len(texto.split()) < 2:
            await message.answer("❌ **Uso:** /activar [folio]\n**Ejemplo:** /activar 659", parse_mode="Markdown")
            return
        
        folio = texto.split()[1].strip()
        response = supabase.table("folios_registrados").select("*").eq("folio", folio).execute()
        
        if not response.data:
            await message.answer(f"❌ Folio {folio} no encontrado.")
            return
        
        folio_data = response.data[0]
        
        if folio_data['estado'] == 'ACTIVO':
            await message.answer(f"⚠️ El folio {folio} ya está **ACTIVO**.", parse_mode="Markdown")
            return
        
        # Activar folio
        supabase.table("folios_registrados").update({
            "estado": "ACTIVO",
            "fecha_activacion": datetime.now().isoformat()
        }).eq("folio", folio).execute()
        
        supabase.table("borradores_registros").update({
            "estado": "ACTIVO",
            "fecha_activacion": datetime.now().isoformat()
        }).eq("folio", folio).execute()
        
        # Notificar al usuario
        try:
            await bot.send_message(
                folio_data['user_id'],
                f"🎉 **¡PERMISO OFICIALMENTE ACTIVADO!**\n\n"
                f"📄 **Folio:** {folio}\n"
                f"✅ **Estado:** ACTIVO\n"
                f"🚗 **Ya puedes circular** con tu permiso de Guanajuato\n"
                f"📅 **Vigente hasta:** {datetime.fromisoformat(folio_data['fecha_vencimiento']).strftime('%d/%m/%Y')}\n\n"
                f"🏛️ **Gobierno del Estado de Guanajuato**\n"
                f"Gracias por usar nuestros servicios oficiales.",
                parse_mode="Markdown"
            )
        except:
            pass
        
        await message.answer(
            f"✅ **FOLIO ACTIVADO**\n\n"
            f"📄 **Folio:** {folio}\n"
            f"👤 **Usuario:** {folio_data['nombre']}\n"
            f"🚗 **Vehículo:** {folio_data['marca']} {folio_data['linea']}\n"
            f"📅 Usuario notificado",
            parse_mode="Markdown"
        )
        
    except Exception as e:
        await message.answer(f"❌ Error activando: {str(e)}")

@dp.message(Command("reporte_hoy"))
async def reporte_manual(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    
    await message.answer("🔄 **Generando reporte PDF...**", parse_mode="Markdown")
    await generar_reporte_diario()

@dp.message(Command("estadisticas"))
async def estadisticas_generales(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    
    try:
        response = supabase.table("folios_registrados").select("*").execute()
        todos_folios = response.data
        
        total = len(todos_folios)
        pendientes = len([f for f in todos_folios if f.get('estado') == 'PENDIENTE'])
        con_comprobante = len([f for f in todos_folios if f.get('estado') == 'COMPROBANTE_ENVIADO'])
        activos = len([f for f in todos_folios if f.get('estado') == 'ACTIVO'])
        
        hoy = datetime.now().date()
        folios_hoy = [f for f in todos_folios if f.get('fecha_expedicion') and 
                     datetime.fromisoformat(f['fecha_expedicion']).date() == hoy]
        
        timers_count = len(timers_activos)
        
        try:
            response_bloqueadas = supabase.table("series_bloqueadas").select("*").execute()
            series_bloqueadas = len(response_bloqueadas.data)
        except:
            series_bloqueadas = 0
        
        mensaje = (
            f"📊 **ESTADÍSTICAS GUANAJUATO**\n\n"
            f"🔢 **TOTALES:**\n"
            f"• Total permisos: {total}\n"
            f"• Pendientes: {pendientes}\n"
            f"• Con comprobante: {con_comprobante}\n"
            f"• Activos: {activos}\n"
            f"• Series bloqueadas: {series_bloqueadas}\n\n"
            
            f"📅 **HOY:** {len(folios_hoy)} permisos\n"
            f"⏰ **Timers activos:** {timers_count}\n"
            f"🔢 **Próximo folio:** {folio_counter['count']}\n\n"
            
            f"💰 **INGRESOS:**\n"
            f"• Potenciales: ${total * PRECIO_PERMISO:,}\n"
            f"• Por validar: ${con_comprobante * PRECIO_PERMISO:,}\n"
            f"• Confirmados: ${activos * PRECIO_PERMISO:,}"
        )
        
        await message.answer(mensaje, parse_mode="Markdown")
        
    except Exception as e:
        await message.answer(f"❌ Error: {str(e)}")

# ------------ FASTAPI SETUP ------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    await bot.delete_webhook(drop_pending_updates=True)
    if BASE_URL:
        await bot.set_webhook(f"{BASE_URL}/webhook", allowed_updates=["message"])
    
    # Iniciar scheduler de reportes
    asyncio.create_task(scheduler_reporte_diario())
    print("✅ Bot iniciado y scheduler activado para las 20:00 CDMX")
    
    yield
    
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
    return {"message": "Bot Guanajuato funcionando - reportes PDF a las 20:00 CDMX"}

if __name__ == '__main__':
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)

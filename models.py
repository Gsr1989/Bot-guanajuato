from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()

class Vehiculo(db.Model):
    __tablename__ = 'vehiculos'  # Nombre opcional de la tabla
    id = db.Column(db.Integer, primary_key=True)
    marca = db.Column(db.String(100), nullable=False)
    linea = db.Column(db.String(100), nullable=False)
    anio = db.Column(db.Integer, nullable=False)
    numero_serie = db.Column(db.String(100), unique=True, nullable=False)
    numero_motor = db.Column(db.String(100), nullable=False)
    fecha = db.Column(db.DateTime, default=datetime.utcnow)
